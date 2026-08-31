"""llm-bench runner: send one prompt to multiple LLM providers/models
concurrently and collect comparable outputs as JSON.

Usage:
    scriptling runner.py <task-file-or-directory> [more tasks...]

Reads config.json (next to this script). API keys come from the environment
(put them in .env; the scriptling CLI auto-loads it from the working
directory). For each task file, writes outputs/<task_id>-<timestamp>.json
containing the prompt, per-target status/text/response, and timing.

Transport is the scriptling.ai client (provider-native Claude/Gemini plus
OpenAI-compatible), run concurrently via runtime.background in waves of
max_parallel. Per-call timeout is passed straight through to the client, so
slow generations (>30s) work on stock Scriptling builds. Responses are the
client's normalized completion dicts (id/choices/usage); ai.text() strips
thinking blocks for the comparable "text" field. The client may retry
rate-limit/server errors (its default) - a "retry" key appears on results
that were retried, so retries stay visible.

Per-result statuses:
    ok            completion returned; text extracted
    error         request failed (HTTP, timeout, decode, ...) - see error
    config_error  unknown provider or bad config for this target
"""

import json
import os
import os.path
import sys
import time
import scriptling.runtime as runtime

SCRIPT_DIR = os.path.dirname(__file__)

DEFAULT_MAX_TOKENS = 1024
DEFAULT_TEMPERATURE = 0.7
DEFAULT_TIMEOUT = 120


def die(message):
    # Fatal errors must be raised from main()'s frame: in Scriptling 0.22.0 an
    # uncaught exception crossing 3+ call frames still prints but exits 0.
    print("error: " + message)
    raise Exception("fatal: " + message)


def load_json_in_main(path, what):
    """Read+parse JSON, dying in the CALLER's frame (use only from main())."""
    try:
        return json.loads(os.read_file(path))
    except Exception as e:
        die("cannot read " + what + " '" + path + "': " + str(e))


def run_target(idx, provider, provider_type, base_url, api_key, model,
               prompt, system, max_tokens, temperature, timeout,
               max_retries, retry_backoff):
    """Background worker: one completion call. Runs in an ISOLATED environment
    (re-imports its libraries); always returns a result record instead of
    raising, so one bad target cannot kill the batch. Prints a live progress
    line the moment it finishes, so long waves show out-of-order progress."""
    import time
    import scriptling.ai as ai

    record = {
        "index": idx,
        "provider": provider,
        "provider_type": provider_type,
        "model": model,
        "elapsed_seconds": None,
        "status": "error",
        "text": None,
        "error": None,
        "response": None,
        "usage": None,
    }
    t0 = time.perf_counter()
    try:
        provider_const = ai.OPENAI
        if provider_type == "anthropic":
            provider_const = ai.CLAUDE
        elif provider_type == "google":
            provider_const = ai.GEMINI

        client_kwargs = {}
        if max_retries is not None:
            client_kwargs["max_retries"] = max_retries
        if retry_backoff is not None:
            client_kwargs["retry_backoff"] = retry_backoff
        client = ai.Client(base_url, provider=provider_const, api_key=api_key, **client_kwargs)

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        kwargs = {"max_tokens": max_tokens, "timeout": timeout}
        if temperature is not None:
            kwargs["temperature"] = temperature

        response = client.completion(model, messages, **kwargs)

        record["status"] = "ok"
        record["text"] = ai.text(response)
        record["response"] = response
        record["usage"] = response.get("usage")
        if response.get("retry") is not None:
            record["retry"] = response.get("retry")
    except Exception as e:
        msg = str(e)
        if msg.find("context deadline exceeded") != -1 or msg.find("Client.Timeout") != -1:
            msg = msg + " (hit the " + str(timeout) + "s task timeout - raise 'timeout' in the task file for slow local models, or pre-load the model so loading does not eat the budget)"
        record["error"] = msg[:500]
    record["elapsed_seconds"] = round(time.perf_counter() - t0, 3)
    label = provider + "/" + model
    if record["status"] == "ok":
        print("  · " + label + " finished (" + str(record["elapsed_seconds"]) + "s)")
    else:
        print("  · " + label + " failed (" + str(record["elapsed_seconds"]) + "s): " + str(record["error"])[:80])
    return record


def build_jobs(task_id, task, config):
    """Validate targets against config; returns a list of job dicts."""
    providers = config["providers"]
    jobs = []
    for t in task.get("targets", []):
        provider = t.get("provider", "")
        model = t.get("model", "")
        job = {"provider": provider, "model": model, "index": len(jobs)}
        pcfg = providers.get(provider)
        if pcfg is None:
            job["config_error"] = "unknown provider '" + provider + "' (not in config.json)"
        else:
            ptype = pcfg.get("type", "")
            if ptype not in ["anthropic", "google", "openai"]:
                job["config_error"] = "provider '" + provider + "' has unsupported type '" + str(ptype) + "'"
            elif not pcfg.get("base_url", ""):
                job["config_error"] = "provider '" + provider + "' has no base_url in config.json"
            elif not model:
                job["config_error"] = "target has no model"
            else:
                env_name = pcfg.get("api_key_env", "")
                # A missing key is not fatal: local servers are often keyless,
                # and a real provider error comes back as a normal error result.
                job["api_key"] = (os.getenv(env_name, "") or "") if env_name else ""
                job["provider_type"] = ptype
                job["base_url"] = pcfg.get("base_url", "")
                if pcfg.get("max_retries") is not None:
                    job["max_retries"] = pcfg.get("max_retries")
                if pcfg.get("retry_backoff") is not None:
                    job["retry_backoff"] = pcfg.get("retry_backoff")
        jobs.append(job)
    return jobs


def build_waves(jobs, global_max, provider_caps):
    """Split jobs into waves: <= global_max jobs per wave, and per provider no
    more than the provider's own max_parallel cap (default: the global value).
    This lets a single local backend (LM Studio, Ollama, ...) load one model
    at a time while remote providers keep running wide-open in parallel.
    Later jobs that don't fit are deferred to the next wave, not flushed."""
    if global_max < 1:
        global_max = 1
    waves = []
    remaining = []
    for j in jobs:
        remaining.append(j)
    while len(remaining) > 0:
        wave = []
        counts = {}
        still = []
        for j in remaining:
            p = j["provider"]
            cap = provider_caps.get(p, global_max)
            if cap < 1:
                cap = 1
            c = counts.get(p, 0)
            if len(wave) < global_max and c < cap:
                wave.append(j)
                counts[p] = c + 1
            else:
                still.append(j)
        waves.append(wave)
        remaining = still
    return waves


def run_task(task_path, task, config, out_dir):
    task_id = task_path.split("/")[-1].removesuffix(".json")
    prompt = task.get("prompt", "")

    targets = task.get("targets", [])
    if not targets:
        targets = config["default_targets"]
    if len(targets) == 0:
        print("warning: task '" + task_id + "' has no targets (task has none and config has no default_targets), skipping")
        return

    task["targets"] = targets
    max_parallel = task.get("max_parallel", config.get("max_parallel", 4))
    if max_parallel < 1:
        max_parallel = 1
    system = task.get("system", "")
    max_tokens = task.get("max_tokens", DEFAULT_MAX_TOKENS)
    temperature = task.get("temperature", DEFAULT_TEMPERATURE)
    timeout = task.get("timeout", DEFAULT_TIMEOUT)

    jobs = build_jobs(task_id, task, config)
    live = [j for j in jobs if "config_error" not in j]

    print()
    print("==> " + task_id + ": " + str(len(jobs)) + " target(s), max_parallel=" +
          str(max_parallel) + ", timeout=" + str(timeout) + "s")
    for j in jobs:
        line = "    - " + j["provider"] + "/" + j["model"]
        if "config_error" in j:
            line = line + "  [config error: " + j["config_error"] + "]"
        print(line)

    started_at = time.now()
    t0 = time.perf_counter()

    results = {}
    provider_caps = {}
    for name in config["providers"]:
        cap = config["providers"][name].get("max_parallel")
        if cap is not None:
            provider_caps[name] = cap
    waves = build_waves(live, max_parallel, provider_caps)
    wave_total = len(waves)
    wave_num = 0
    for wave in waves:
        wave_num = wave_num + 1
        inflight = []
        for j in wave:
            inflight.append(j["provider"] + "/" + j["model"])
        print("  [wave " + str(wave_num) + "/" + str(wave_total) + "] running: " + ", ".join(inflight))
        promises = []
        for j in wave:
            name = "bench-" + task_id + "-" + str(j["index"])
            p = runtime.background(
                name, "run_target",
                j["index"], j["provider"], j["provider_type"], j["base_url"],
                j["api_key"], j["model"],
                prompt, system, max_tokens, temperature, timeout,
                j.get("max_retries"), j.get("retry_backoff"),
            )
            promises.append((j["index"], p))
        for idx, p in promises:
            try:
                results[idx] = p.get()
            except Exception as e:
                results[idx] = {
                    "index": idx, "provider_type": None, "model": None,
                    "elapsed_seconds": None, "status": "error", "text": None,
                    "error": ("background task failed: " + str(e))[:500],
                    "response": None, "usage": None,
                }

    elapsed = time.perf_counter() - t0

    ordered = []
    ok_count = 0
    for j in jobs:
        if "config_error" in j:
            record = {
                "index": j["index"], "provider": j["provider"],
                "provider_type": None, "model": j["model"],
                "elapsed_seconds": None, "status": "config_error", "text": None,
                "error": j["config_error"], "response": None, "usage": None,
            }
        else:
            record = results.get(j["index"]) or {
                "index": j["index"], "provider_type": None, "model": j["model"],
                "elapsed_seconds": None, "status": "error", "text": None,
                "error": "no result returned", "response": None, "usage": None,
            }
            record["provider"] = j["provider"]
        ordered.append(record)
        if record["status"] == "ok":
            ok_count += 1
            print("    [ok]    " + record["provider"] + "/" + str(record["model"]) +
                  "  (" + str(len(record["text"] or "")) + " chars, " +
                  str(record["elapsed_seconds"]) + "s)")
        else:
            print("    [" + record["status"] + "] " + record["provider"] + "/" +
                  str(record["model"]) + "  " + str(record["error"])[:120])

    payload = {
        "task_id": task_id,
        "task_file": task_path,
        "prompt": task.get("prompt"),
        "system": system,
        "params": {
            "max_tokens": max_tokens,
            "temperature": temperature,
            "timeout": timeout,
        },
        "targets": [{"provider": t.get("provider"), "model": t.get("model")} for t in targets],
        "started_at": started_at,
        "total_elapsed_seconds": round(elapsed, 3),
        "summary": {"ok": ok_count, "failed": len(ordered) - ok_count},
        "results": ordered,
    }

    timestamp = time.strftime("%Y%m%d-%H%M%S")
    out_path = os.path.join(out_dir, task_id + "-" + timestamp + ".json")
    os.makedirs(out_dir, exist_ok=True)
    os.write_file(out_path, json.dumps(payload, indent="  "))

    print("    wrote " + out_path + "  (ok=" + str(ok_count) +
          " failed=" + str(len(ordered) - ok_count) + ", " + f"{elapsed:.2f}" + "s)")


def collect_task_files(args):
    """Expand args (file or directory) into task file paths. Non-directories
    are passed through; main() fails loudly if they cannot be read."""
    task_files = []
    for arg in args:
        names = None
        try:
            names = os.listdir(arg)
        except Exception:
            names = None
        if names is not None:
            json_names = sorted([n for n in names if n.endswith(".json")])
            if len(json_names) == 0:
                print("warning: no *.json task files in '" + arg + "'")
            for n in json_names:
                task_files.append(os.path.join(arg, n))
        else:
            task_files.append(arg)
    return task_files


def main():
    argv = sys.argv
    if len(argv) < 2:
        print("usage: scriptling runner.py <task-file-or-directory> [more...]")
        print()
        print("examples:")
        print("  scriptling runner.py tasks/examples         # the shipped example tasks")
        print("  scriptling runner.py tasks                   # every task in a directory (non-recursive)")
        print("  scriptling runner.py tasks/coding.json       # a single task")
        sys.exit(1)

    config = load_json_in_main(os.path.join(SCRIPT_DIR, "config.json"), "config")
    if not isinstance(config.get("providers"), dict):
        die("config.json has no 'providers' object")
    defaults = config.get("default_targets")
    if not isinstance(defaults, list) or len(defaults) == 0:
        die("config.json has no 'default_targets' list")

    out_dir_value = config.get("output_dir", "outputs")
    if out_dir_value.startswith("/"):
        out_dir = out_dir_value
    else:
        out_dir = os.path.join(SCRIPT_DIR, out_dir_value)

    task_files = collect_task_files(argv[1:])

    # Pre-load and validate every task file up front (in this frame) so a
    # missing or malformed task fails fast with a proper exit code, before
    # any requests are made.
    tasks = []
    for tf in task_files:
        parsed = load_json_in_main(tf, "task file")
        if not isinstance(parsed, dict):
            die("task file '" + tf + "' does not contain a JSON object")
        if not isinstance(parsed.get("prompt"), str) or parsed.get("prompt") == "":
            die("task '" + tf + "' has no 'prompt'")
        tasks.append(parsed)

    if len(tasks) == 0:
        die("no task files to run - point at a task file or a directory containing *.json files (the shipped examples live in tasks/examples/)")

    os.makedirs(out_dir, exist_ok=True)

    print("llm-bench: " + str(len(tasks)) + " task(s), output dir: " + out_dir)

    for i in range(len(tasks)):
        run_task(task_files[i], tasks[i], config, out_dir)

    print()
    print("done")


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
