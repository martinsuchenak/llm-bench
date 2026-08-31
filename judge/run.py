"""judge/run.py - automate the judging loop for one bench run.

Usage:
    scriptling judge/run.py <task-file-or-run-file> [judge-provider/model] [notes-file]

Arguments:
    task file (tasks/coding.json)   finds the LATEST outputs/<task_id>-*.json run
    run file (outputs/coding-…)     judges exactly that run
    judge target ("provider/model") overrides config.json "judge_target";
                                    pass "none" to force prepare-only mode even
                                    when config.json defines a judge_target
    notes file                      optional ground-truth text for {{NOTES}};
                                    pass "none" for none

With no judge target (arg and config both absent) the script runs in
prepare-only mode: it writes the prompt and mapping files and exits.

Produces judge/out/<task_id>-<timestamp>/ :
    prompt.md            the complete judge prompt (as sent)
    candidates.json      anonymized, shuffled candidates
    mapping.json         label -> provider/model (for the human, NOT the judge)
    notes.md             the notes used, when a notes file was given
    verdict.json         the judge's parsed verdict (still anonymized)
    verdict-merged.json  verdict with labels replaced by provider/model
    judge-response.json  the raw judge completion (audit / debugging)

The judge model should ideally NOT be one of the candidates: models show
measurable self-preference when grading their own output.
"""

import json
import os
import os.path
import sys
import time
import secrets
import scriptling.ai as ai

SCRIPT_DIR = os.path.dirname(__file__)
PROJECT_DIR = "/".join(SCRIPT_DIR.split("/")[:-1])
TEMPLATE_PATH = os.path.join(SCRIPT_DIR, "prompt.md")
OUT_ROOT = os.path.join(SCRIPT_DIR, "out")

LABELS = ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L", "M",
          "N", "O", "P", "Q", "R", "S", "T", "U", "V", "W", "X", "Y", "Z"]

JUDGE_MAX_TOKENS = 4096
JUDGE_TEMPERATURE = 0.1
JUDGE_TIMEOUT = 240


def die(message):
    # Fatal errors must be raised from main()'s frame (Scriptling 0.22.0
    # exit-code quirk - see AGENTS.md).
    print("error: " + message)
    raise Exception("fatal: " + message)


def load_json_in_main(path, what):
    """Read+parse JSON, dying in the CALLER's frame (use only from main())."""
    try:
        return json.loads(os.read_file(path))
    except Exception as e:
        die("cannot read " + what + " '" + path + "': " + str(e))


def shuffled(items):
    out = []
    for x in items:
        out.append(x)
    i = len(out) - 1
    while i > 0:
        j = secrets.randbelow(i + 1)
        tmp = out[i]
        out[i] = out[j]
        out[j] = tmp
        i = i - 1
    return out


def find_run_file(task_path, outputs_dir):
    """Latest outputs/<task_id>-*.json for a task file, or None."""
    task_id = task_path.split("/")[-1].removesuffix(".json")
    names = None
    try:
        names = os.listdir(outputs_dir)
    except Exception:
        names = None
    if names is None:
        return None
    runs = [n for n in names if n.startswith(task_id + "-") and n.endswith(".json")]
    if len(runs) == 0:
        return None
    return os.path.join(outputs_dir, sorted(runs)[-1])


def build_case(run):
    """Returns (candidates, mapping, excluded, error). config_error results
    are bench misconfigurations, not model outputs, and are excluded."""
    included = []
    excluded = 0
    for r in run.get("results", []):
        if r.get("status") == "config_error":
            excluded = excluded + 1
        else:
            included.append(r)
    if len(included) == 0:
        return None, None, excluded, "run has no candidate results (all config_error)"
    if len(included) > len(LABELS):
        return None, None, excluded, ("run has " + str(len(included)) +
                                      " candidates, max supported is " + str(len(LABELS)))

    order = shuffled(included)
    candidates = []
    mapping = {}
    for i in range(len(order)):
        r = order[i]
        label = LABELS[i]
        finish = None
        response = r.get("response")
        if isinstance(response, dict):
            choices = response.get("choices", [])
            if len(choices) > 0:
                finish = choices[0].get("finish_reason")
        candidates.append({
            "label": label,
            "status": r.get("status"),
            "finish_reason": finish,
            "text": r.get("text"),
        })
        mapping[label] = {"provider": r.get("provider"), "model": r.get("model")}
    return candidates, mapping, excluded, None


def fill_template(template, task_block, candidates, notes_text):
    prompt = template.replace("{{TASK}}", json.dumps(task_block, indent="  "))
    prompt = prompt.replace("{{CANDIDATES}}", json.dumps(candidates, indent="  "))
    return prompt.replace("{{NOTES}}", notes_text)


def call_judge(judge_cfg, model, prompt, max_tokens, temperature, timeout):
    """Returns (text, response). Raises on failure."""
    ptype = judge_cfg.get("type", "")
    provider = ai.OPENAI
    if ptype == "anthropic":
        provider = ai.CLAUDE
    elif ptype == "google":
        provider = ai.GEMINI
    env_name = judge_cfg.get("api_key_env", "")
    api_key = (os.getenv(env_name, "") or "") if env_name else ""
    client = ai.Client(judge_cfg.get("base_url", ""), provider=provider, api_key=api_key)
    response = client.completion(model, [{"role": "user", "content": prompt}],
                                 max_tokens=max_tokens,
                                 temperature=temperature,
                                 timeout=timeout)
    return ai.text(response), response


def extract_verdict(text):
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start:end + 1])
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        return None
    return None


def name_of(mapping, label):
    m = mapping.get(label)
    if m is None:
        return label
    return str(m.get("provider")) + "/" + str(m.get("model"))


def is_verdict(d):
    return isinstance(d, dict) and ("ranking" in d or "pairwise" in d)


def merge_verdict(verdict, mapping):
    merged = {
        "scenario": verdict.get("scenario"),
        "confidence": verdict.get("confidence"),
        "notes_for_human": verdict.get("notes_for_human"),
        "candidates": {},
        "pairwise": [],
        "ranking": [],
        "best_overall": None,
    }
    for label, c in verdict.get("candidates", {}).items():
        merged["candidates"][name_of(mapping, label)] = c
    for p in verdict.get("pairwise", []):
        pair = p.get("pair")
        if not isinstance(pair, list) or len(pair) != 2:
            continue
        merged["pairwise"].append({
            "pair": [name_of(mapping, pair[0]), name_of(mapping, pair[1])],
            "winner": name_of(mapping, p.get("winner")),
            "margin": p.get("margin"),
            "reason": p.get("reason"),
        })
    for label in verdict.get("ranking", []):
        merged["ranking"].append(name_of(mapping, label))
    if verdict.get("best_overall") is not None:
        merged["best_overall"] = name_of(mapping, verdict.get("best_overall"))
    return merged


def print_merged(merged):
    print()
    print("ranking (de-anonymized):")
    rank = 1
    for name in merged["ranking"]:
        scores = merged["candidates"].get(name, {}).get("scores", {})
        parts = []
        for k, v in scores.items():
            parts.append(k + "=" + str(v))
        print("  " + str(rank) + ". " + name + "  [" + ", ".join(parts) + "]")
        rank = rank + 1
    if len(merged["pairwise"]) > 0:
        print("pairwise:")
        for p in merged["pairwise"]:
            print("  " + p["pair"][0] + " vs " + p["pair"][1] + " -> " +
                  p["winner"] + " (" + str(p["margin"]) + ") " + str(p["reason"] or "")[:90])
    print("confidence: " + str(merged.get("confidence")))
    notes = merged.get("notes_for_human")
    if notes:
        print("notes for human: " + str(notes)[:300])


def main():
    argv = sys.argv
    if len(argv) < 2 or len(argv) > 4:
        print("usage: scriptling judge/run.py <task-file-or-run-file> [judge-provider/model] [notes-file]")
        print()
        print("  task file      tasks/coding.json                 judge the latest outputs run for the task")
        print("  run file       outputs/coding-2026….json         judge that exact run")
        print("  verdict file   judge/out/…/verdict-pasted.json   merge a pasted (CLI-tool) verdict")
        print('  judge target   provider/model, "none", or omit   none = prepare-only; omit = config judge_target')
        print("  notes file     optional ground-truth text for the judge prompt")
        print()
        print("examples:")
        print("  scriptling judge/run.py tasks/coding.json")
        print("  scriptling judge/run.py tasks/coding.json none")
        print("  scriptling judge/run.py tasks/coding.json zai/glm-4.7 notes.md")
        sys.exit(1)

    input_path = argv[1]
    # "none" is the skip/off sentinel: a bare "-" cannot be used because the
    # scriptling CLI treats dash-prefixed tokens as flags, never as args.
    judge_forced_off = False
    judge_target_arg = None
    if len(argv) > 2:
        if argv[2] == "none":
            judge_forced_off = True
        else:
            judge_target_arg = argv[2]
    notes_path = None
    if len(argv) > 3 and argv[3] != "none":
        notes_path = argv[3]

    notes_text = "(none)"
    if notes_path is not None:
        try:
            notes_text = os.read_file(notes_path)
        except Exception as e:
            die("cannot read notes file '" + notes_path + "': " + str(e))

    config = load_json_in_main(os.path.join(PROJECT_DIR, "config.json"), "config")
    if not isinstance(config.get("providers"), dict):
        die("config.json has no 'providers' object")

    out_dir_value = config.get("output_dir", "outputs")
    outputs_dir = out_dir_value if out_dir_value.startswith("/") else os.path.join(PROJECT_DIR, out_dir_value)

    # Input detection: a judge verdict (merge mode), a run file, or a task
    # file. Verdict files may be raw JSON or CLI-tool replies with prose and
    # markdown fences around the JSON.
    try:
        raw_input = os.read_file(input_path)
    except Exception as e:
        die("cannot read input file '" + input_path + "': " + str(e))

    input_json = None
    try:
        input_json = json.loads(raw_input)
    except Exception:
        input_json = None
    if input_json is None:
        extracted = extract_verdict(raw_input)
        if is_verdict(extracted):
            input_json = extracted

    run = None
    if is_verdict(input_json):
        # Merge mode: de-anonymize a (possibly CLI-produced) verdict using
        # the mapping.json written next to the prompt it answers.
        mapping_path = os.path.join(os.path.dirname(input_path), "mapping.json")
        mapping = load_json_in_main(mapping_path, "mapping file")
        merged = merge_verdict(input_json, mapping)
        target_dir = os.path.dirname(input_path)
        merged_path = os.path.join(target_dir, "verdict-merged.json")
        os.write_file(merged_path, json.dumps(merged, indent="  "))
        print_merged(merged)
        print()
        print("wrote " + merged_path)
        return
    elif isinstance(input_json, dict) and isinstance(input_json.get("results"), list):
        run = input_json
        print("judging run file: " + input_path)
    elif isinstance(input_json, dict) and isinstance(input_json.get("prompt"), str):
        found = find_run_file(input_path, outputs_dir)
        if found is None:
            die("no outputs run found for task '" + input_path + "' in " + outputs_dir + " - run runner.py first")
        run = load_json_in_main(found, "run file")
        print("task file given: using latest run " + found)
    else:
        die("input is not a run file, task file, or judge verdict: " + input_path)

    candidates, mapping, excluded, case_error = build_case(run)
    if case_error is not None:
        die(case_error)

    try:
        template = os.read_file(TEMPLATE_PATH)
    except Exception as e:
        die("cannot read judge template '" + TEMPLATE_PATH + "': " + str(e))

    task_block = {
        "task_id": run.get("task_id"),
        "prompt": run.get("prompt"),
        "system": run.get("system", ""),
        "params": run.get("params", {}),
    }
    prompt = fill_template(template, task_block, candidates, notes_text)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = os.path.join(OUT_ROOT, str(run.get("task_id")) + "-" + stamp)
    os.makedirs(out_dir, exist_ok=True)

    os.write_file(os.path.join(out_dir, "prompt.md"), prompt)
    os.write_file(os.path.join(out_dir, "candidates.json"), json.dumps(candidates, indent="  "))
    os.write_file(os.path.join(out_dir, "mapping.json"), json.dumps(mapping, indent="  "))
    if notes_path is not None:
        os.write_file(os.path.join(out_dir, "notes.md"), notes_text)

    if excluded > 0:
        print("excluded " + str(excluded) + " config_error result(s) (bench misconfiguration, not model output)")
    print("candidates: " + str(len(candidates)) + " (anonymized + shuffled; labels in mapping.json)")

    # Resolve judge target: CLI arg beats config; "-" forces none; none
    # configured = prepare-only mode.
    target = judge_target_arg
    if target is None and not judge_forced_off:
        cfg_target = config.get("judge_target")
        if isinstance(cfg_target, dict) and cfg_target.get("provider"):
            target = str(cfg_target.get("provider")) + "/" + str(cfg_target.get("model", ""))
    if target is None:
        prompt_path = os.path.join(out_dir, "prompt.md")
        print()
        print("prepare-only mode (no judge target). Give the prompt to any capable")
        print("model - an API provider via this script, or a local CLI tool:")
        print()
        print('    claude -p "$(cat ' + prompt_path + ')" > ' + os.path.join(out_dir, "verdict-pasted.json"))
        print('    gemini -p "$(cat ' + prompt_path + ')" > ' + os.path.join(out_dir, "verdict-pasted.json"))
        print()
        print("(or paste the file contents into kiro/any tool and save the reply)")
        print("then de-anonymize the verdict with:")
        print("    scriptling judge/run.py " + os.path.join(out_dir, "verdict-pasted.json"))
        print()
        print("label mapping (keep private until judged): " + os.path.join(out_dir, "mapping.json"))
        return

    slash = target.find("/")
    if slash <= 0 or slash == len(target) - 1:
        die("judge target must look like provider/model, got '" + target + "'")
    judge_provider = target[:slash]
    judge_model = target[slash + 1:]
    judge_cfg = config.get("providers", {}).get(judge_provider)
    if judge_cfg is None:
        die("unknown judge provider '" + judge_provider + "' (not in config.json)")

    # Judge call parameters: the config judge_target object may override the
    # built-in defaults (applies whichever way the target was chosen).
    judge_max_tokens = JUDGE_MAX_TOKENS
    judge_temperature = JUDGE_TEMPERATURE
    judge_timeout = JUDGE_TIMEOUT
    cfg_target = config.get("judge_target")
    if isinstance(cfg_target, dict):
        if cfg_target.get("max_tokens") is not None:
            judge_max_tokens = cfg_target.get("max_tokens")
        if cfg_target.get("temperature") is not None:
            judge_temperature = cfg_target.get("temperature")
        if cfg_target.get("timeout") is not None:
            judge_timeout = cfg_target.get("timeout")

    print("judge: " + judge_provider + "/" + judge_model + "  (temperature " +
          str(judge_temperature) + ", max_tokens " + str(judge_max_tokens) + ")")

    t0 = time.perf_counter()
    try:
        text, response = call_judge(judge_cfg, judge_model, prompt,
                                    judge_max_tokens, judge_temperature, judge_timeout)
    except Exception as e:
        os.write_file(os.path.join(out_dir, "judge-response.json"),
                      json.dumps({"call_failed": str(e)[:500]}))
        die("judge call failed: " + str(e)[:300])
    elapsed = time.perf_counter() - t0

    os.write_file(os.path.join(out_dir, "judge-response.json"), json.dumps(response, indent="  "))
    print("judge replied in " + f"{elapsed:.1f}" + "s (" + str(len(text)) + " chars)")

    if len(text) == 0:
        finish = None
        completion = None
        choices = response.get("choices", [])
        if len(choices) > 0:
            finish = choices[0].get("finish_reason")
        usage = response.get("usage")
        if isinstance(usage, dict):
            completion = usage.get("completion_tokens")
        die("judge returned an empty reply (finish_reason: " + str(finish) +
            ", completion_tokens: " + str(completion) + " of max_tokens " + str(judge_max_tokens) + ")" +
            " - a thinking judge model likely spent its whole budget on hidden reasoning." +
            " Use a judge model that reasons less, raise judge_target max_tokens in config.json," +
            " or judge with a CLI tool via prepare-only mode. Raw reply: " +
            os.path.join(out_dir, "judge-response.json"))

    verdict = extract_verdict(text)
    if verdict is None:
        die("judge did not return parseable JSON - raw reply saved to " +
            os.path.join(out_dir, "judge-response.json"))
    os.write_file(os.path.join(out_dir, "verdict.json"), json.dumps(verdict, indent="  "))

    merged = merge_verdict(verdict, mapping)
    os.write_file(os.path.join(out_dir, "verdict-merged.json"), json.dumps(merged, indent="  "))
    print_merged(merged)
    print()
    print("wrote " + out_dir + "/  (verdict-merged.json is the human-review file)")


if __name__ == "__main__":
    # die() already printed its message; exit cleanly instead of letting the
    # interpreter add an "Uncaught exception" line. SystemExit (usage) is not
    # catchable and propagates on its own; anything unexpected re-raises so
    # the full diagnostics still show.
    try:
        main()
    except Exception as e:
        if str(e).startswith("fatal: "):
            sys.exit(1)
        raise
