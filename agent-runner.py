"""llm-bench agent-runner: A/B harness measuring whether code-intelligence
(skopos MCP + steering) makes AI coding agents faster, cheaper, or better.

Usage (the `--` is required: the scriptling CLI parses unknown --flags itself
and rejects them before the script ever sees sys.argv):
    scriptling agent-runner.py -- doctor
    scriptling agent-runner.py -- validate [--task <id-or-path>]...
    scriptling agent-runner.py -- run --reps N [--task ...] [--arm ...]

Design: .local/index-effectiveness-harness.md (builds on phantom overlays:
arm = overlay contents; baseline = plain repo, skopos = project-scope install
in the overlay's upper layer). Reads agents.json (next to this script, copied
from agents.example.json). Task corpus: tasks/agents/*.json, each with its own
checker + oracle - a task only counts as defined once checker AND oracle run
green with no agent involved (that is what `validate` proves, at zero agent
token cost).

Per-run capture: wall time (harness), turns/tokens/cost/tool-calls (parsed
from the agent's own stream-json stdout), tier-1 quality (checker executed in
the overlay), changed files (phantom diff). Stats are reported per task and
per task TYPE - never a single blended average.

Deviation from the design doc (recorded on the skopos blackboard): the
harness runs the agent itself as a subprocess with cwd = overlay mount path,
instead of `phantom run`. Reason: task setup and arm prep must execute inside
the overlay BETWEEN overlay creation and agent start; phantom run couples the
two. phantom stays the overlay/diff/cleanup layer.

Scriptling notes (same traps as runner.py, see AGENTS.md): plain strings not
pathlib, no open(), fatal die() only from main()'s frame, subprocess has no
working timeout so every command is wrapped in a shell watchdog.
"""

import json
import os
import os.path
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(__file__)
# __file__ is relative when invoked as `scriptling agent-runner.py` from the
# script's own directory; subprocesses run with cwd = overlay mount paths, so
# every derived path must be absolute or {files_dir} breaks.
if SCRIPT_DIR == "" or not SCRIPT_DIR.startswith("/"):
    SCRIPT_DIR = os.path.join(os.getcwd(), SCRIPT_DIR)
TASKS_DIR = os.path.join(SCRIPT_DIR, "tasks", "agents")
FILES_DIR = os.path.join(TASKS_DIR, "files")
ARMS_DIR = os.path.join(SCRIPT_DIR, "arms")

DEFAULT_CHECKER_TIMEOUT = 600
SETUP_TIMEOUT = 120
OVERLAY_TIMEOUT = 180
ARM_PREP_TIMEOUT = 180

_run_counter = 0


def die(message):
    # Must be raised from main()'s frame (see AGENTS.md exit-code quirk).
    print("error: " + message)
    raise Exception("fatal: " + message)


def load_json_in_main(path, what):
    """Read+parse JSON, dying in the CALLER's frame (use only from main())."""
    try:
        return json.loads(os.read_file(path))
    except Exception as e:
        die("cannot read " + what + " '" + path + "': " + str(e))


def expand_path(p):
    if p.startswith("~"):
        return os.path.join(os.getenv("HOME", ""), p[1:])
    if p.startswith("/"):
        return p
    return os.path.join(SCRIPT_DIR, p)


def run_cmd(cmd, cwd, timeout_s):
    """Run a shell command with a hard timeout. Scriptling's subprocess has
    no working timeout of its own, so the command is exec'd in a subshell and
    a watchdog kills it after timeout_s (exit code 124).

    Output goes through a temp file, NOT the inherited pipe: a backgrounded
    watchdog (or a daemon spawned by the command, e.g. phantom's FUSE helper)
    inherits the stdout pipe and keeps it open, and Scriptling's subprocess
    then waits for pipe EOF forever (verified hang, main thread parked in
    pthread_cond_wait with zero children). The main shell cats the file right
    before exiting, so EOF arrives cleanly."""
    global _run_counter
    _run_counter = _run_counter + 1
    out_file = "/tmp/agrun-" + time.strftime("%H%M%S") + "-" + str(_run_counter) + ".out"
    if timeout_s is not None and timeout_s > 0:
        inner = "( exec " + cmd + " ) > " + out_file + " 2>&1 & pid=$!; ( sleep " + str(int(timeout_s)) + "; kill -9 $pid 2>/dev/null ) > /dev/null 2>&1 & watcher=$!; wait $pid; rc=$?; kill $watcher 2>/dev/null; wait $watcher 2>/dev/null; cat " + out_file + "; rm -f " + out_file + "; if [ $rc -ge 128 ]; then exit 124; fi; exit $rc"
    else:
        inner = "( " + cmd + " ) > " + out_file + " 2>&1; rc=$?; cat " + out_file + "; rm -f " + out_file + "; exit $rc"
    r = subprocess.run(inner, shell=True, capture_output=True, cwd=cwd, timeout=(timeout_s or 0) + 60)
    return {"rc": r.returncode, "out": str(r.stdout or "")}


def phantom(pb, args, timeout_s):
    return run_cmd(pb + " " + args, None, timeout_s)


def start_overlay(pb, repo_path, name):
    r = phantom(pb, "start " + repo_path + " -n " + name, OVERLAY_TIMEOUT)
    if r["rc"] != 0:
        return None, "phantom start failed: " + r["out"].strip()[-300:]
    mount = ""
    for line in r["out"].split("\n"):
        if line.strip() != "":
            mount = line.strip()
    if mount == "" or not os.path.isdir(mount):
        return None, "phantom start gave no mount path: " + r["out"].strip()[-300:]
    return mount, None


def stop_overlay(pb, name):
    phantom(pb, "stop " + name + " --cleanup", OVERLAY_TIMEOUT)


def subst(s, files_dir):
    d = files_dir
    if d is None or not os.path.isdir(d):
        d = FILES_DIR
    return s.replace("{files_dir}", d)


def run_setup(mount, task):
    for cmd in (task.get("setup") or []):
        r = run_cmd(subst(cmd, task.get("files_dir")), mount, SETUP_TIMEOUT)
        if r["rc"] != 0:
            return "setup command failed ('" + cmd + "'): " + r["out"].strip()[-300:]
    return None


def run_arm_prep(mount, arm_name, arm_cfg, pb):
    prep = arm_cfg.get("prep", "arms/" + arm_name + ".sh")
    if prep == "":
        return None
    script = expand_path(prep)
    if not os.path.exists(script):
        return None
    r = run_cmd("sh '" + script + "'", mount, ARM_PREP_TIMEOUT)
    if r["rc"] != 0:
        return "arm prep failed (" + arm_name + "): " + r["out"].strip()[-300:]
    return None


def normalize_lines(lines):
    out = []
    for l in lines:
        t = l.strip()
        if t != "":
            out.append(t)
    return sorted(set(out))


def run_checker(mount, task, checker_timeout):
    c = task["checker"]
    mode = c.get("mode", "")
    if mode == "exit0":
        r = run_cmd(c["command"], mount, checker_timeout)
        ok = r["rc"] == 0
        detail = "rc=" + str(r["rc"])
        tail = r["out"].strip()
        if tail != "":
            detail = detail + " | " + tail[-400:]
        return ok, detail
    if mode == "exact_set":
        expected = normalize_lines(c.get("answer") or [])
        path = os.path.join(mount, c["file"])
        try:
            raw = os.read_file(path)
        except Exception as e:
            return False, "cannot read " + c["file"] + ": " + str(e)[:120]
        got = normalize_lines(raw.split("\n"))
        if got == expected:
            return True, "exact match (" + str(len(expected)) + " lines)"
        missing = [x for x in expected if x not in got]
        extra = [x for x in got if x not in expected]
        return False, "missing: " + (", ".join(missing) or "-") + " | extra: " + (", ".join(extra) or "-")
    return False, "unknown checker mode '" + str(mode) + "'"


def apply_oracle(mount, task):
    o = task.get("oracle") or {}
    mode = o.get("mode", "")
    if mode == "answer_file":
        c = task["checker"]
        content = "\n".join(normalize_lines(c.get("answer", []))) + "\n"
        try:
            os.write_file(os.path.join(mount, c["file"]), content)
            return None
        except Exception as e:
            return "oracle answer_file write failed: " + str(e)[:200]
    if mode == "patch":
        patch_path = expand_path(subst(o.get("file", ""), task.get("files_dir")))
        r = run_cmd("git apply '" + patch_path + "'", mount, SETUP_TIMEOUT)
        if r["rc"] != 0:
            return "oracle patch failed: " + r["out"].strip()[-300:]
        return None
    if mode == "commands":
        for cmd in (o.get("commands") or []):
            r = run_cmd(subst(cmd, task.get("files_dir")), mount, SETUP_TIMEOUT)
            if r["rc"] != 0:
                return "oracle command failed ('" + cmd + "'): " + r["out"].strip()[-300:]
        return None
    return "unknown oracle mode '" + str(mode) + "'"


def parse_stream(text):
    """Parse an agent's stdout event stream into metrics. Two dialects are
    understood and auto-detected per line (agents may differ per experiment):
      - claude: `-p --output-format stream-json` - assistant/tool_use events,
        final `result` event carries num_turns/usage/total_cost_usd.
      - opencode: `run --format json` - step_start/step_finish events; tokens
        are PER STEP (each step's input re-counts the whole context), so they
        are SUMMED - a different definition than claude's session totals, but
        identical across arms, which is what the A/B needs. Never raises:
    malformed lines are skipped; an unparsable log yields Nones."""
    turns = 0
    usage = None
    cost = None
    result_text = None
    tools = {}
    mcp_servers = []
    oc_in = 0
    oc_out = 0
    oc_cr = 0
    oc_cw = 0
    oc_cost = 0.0
    saw_claude_result = False
    saw_opencode = False
    for line in text.split("\n"):
        s = line.strip()
        if s == "":
            continue
        try:
            obj = json.loads(s)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        t = obj.get("type")
        if t == "assistant":
            msg = obj.get("message")
            if isinstance(msg, dict):
                content = msg.get("content")
                if isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "tool_use":
                            name = str(item.get("name", "?"))
                            tools[name] = tools.get(name, 0) + 1
        elif t == "system":
            ms = obj.get("mcp_servers")
            if isinstance(ms, list):
                mcp_servers = [str(x) for x in ms]
        elif t == "result":
            saw_claude_result = True
            turns = obj.get("num_turns")
            usage = obj.get("usage")
            cost = obj.get("total_cost_usd")
            result_text = obj.get("result")
        elif t == "step_start":
            saw_opencode = True
            turns = turns + 1
        elif t == "tool_use":
            part = obj.get("part")
            if isinstance(part, dict):
                name = str(part.get("tool", "?"))
                tools[name] = tools.get(name, 0) + 1
        elif t == "step_finish":
            saw_opencode = True
            part = obj.get("part")
            if isinstance(part, dict):
                tk = part.get("tokens")
                if isinstance(tk, dict):
                    oc_in = oc_in + int(tk.get("input") or 0)
                    oc_out = oc_out + int(tk.get("output") or 0)
                    cache = tk.get("cache")
                    if isinstance(cache, dict):
                        oc_cr = oc_cr + int(cache.get("read") or 0)
                        oc_cw = oc_cw + int(cache.get("write") or 0)
                c = part.get("cost")
                if c is not None:
                    oc_cost = oc_cost + float(c)
    if saw_claude_result:
        return {"turns": turns, "usage": usage, "cost_usd": cost,
                "result_text": result_text, "tool_calls": tools,
                "mcp_servers": mcp_servers}
    if saw_opencode:
        return {"turns": turns,
                "usage": {"input_tokens": oc_in, "output_tokens": oc_out,
                          "cache_read_input_tokens": oc_cr,
                          "cache_creation_input_tokens": oc_cw},
                "cost_usd": round(oc_cost, 6), "result_text": result_text,
                "tool_calls": tools, "mcp_servers": mcp_servers}
    return {"turns": None, "usage": None, "cost_usd": None,
            "result_text": None, "tool_calls": tools,
            "mcp_servers": mcp_servers}


def overlay_diff_files(pb, name):
    r = phantom(pb, "diff " + name + " --format json", OVERLAY_TIMEOUT)
    if r["rc"] != 0:
        return None
    try:
        d = json.loads(r["out"])
    except Exception:
        return None
    # phantom emits "files": null for an unchanged overlay; .get(key, default)
    # does NOT fall back when the key exists with a null value.
    files_raw = d.get("files")
    if not isinstance(files_raw, list):
        files_raw = []
    files = []
    for f in files_raw:
        if not isinstance(f, dict):
            continue
        p = str(f.get("path", ""))
        if p.startswith(".git/"):
            continue
        files.append(p)
    return sorted(files)


def next_run_name(task_id, arm, rep):
    global _run_counter
    _run_counter = _run_counter + 1
    return "agrun-" + task_id + "-" + arm + "-r" + str(rep) + "-" + str(_run_counter)


# ---------------------------------------------------------------- stats

def median(vals):
    s = sorted(vals)
    n = len(s)
    if n == 0:
        return None
    if n % 2 == 1:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2


def percentile(vals, p):
    s = sorted(vals)
    n = len(s)
    if n == 0:
        return None
    if n == 1:
        return s[0]
    k = (n - 1) * p
    f = int(k)
    c = k - f
    if f + 1 < n:
        return s[f] + (s[f + 1] - s[f]) * c
    return s[f]


def summarize_runs(runs):
    """Median + IQR per metric over the runs of one cell. Pass rate is a
    plain fraction. Never mixes tiers: tier-2 (judged) scoring happens in
    judge/run.py, downstream of this file."""
    def med_iqr(getter):
        vals = []
        for r in runs:
            v = getter(r)
            if v is not None:
                vals.append(v)
        if len(vals) == 0:
            return {"median": None, "q1": None, "q3": None, "n": 0}
        return {"median": round(median(vals), 4),
                "q1": round(percentile(vals, 0.25), 4),
                "q3": round(percentile(vals, 0.75), 4),
                "n": len(vals)}

    def usage_val(r, key):
        u = r.get("usage") or {}
        v = u.get(key)
        if v is None:
            return None
        return float(v)

    passed = 0
    for r in runs:
        if r.get("tier1_pass") == True:
            passed = passed + 1
    return {
        "runs": len(runs),
        "tier1_pass_rate": round(passed / len(runs), 4) if len(runs) else None,
        "wall_seconds": med_iqr(lambda r: r.get("wall_seconds")),
        "turns": med_iqr(lambda r: r.get("turns")),
        "input_tokens": med_iqr(lambda r: usage_val(r, "input_tokens")),
        "output_tokens": med_iqr(lambda r: usage_val(r, "output_tokens")),
        "cache_read_tokens": med_iqr(lambda r: usage_val(r, "cache_read_input_tokens")),
        "cache_write_tokens": med_iqr(lambda r: usage_val(r, "cache_creation_input_tokens")),
        "cost_usd": med_iqr(lambda r: r.get("cost_usd")),
    }


# ---------------------------------------------------------------- modes

def load_corpus(config, args):
    """Resolve --task args (id or path) against the configured corpus dirs
    (agents.json corpus_dirs, default tasks/agents/), or take every task in
    all of them. Support files for a task live in a files/ dir next to it
    ({files_dir} placeholder). Validates task shape; dies in the CALLER's
    frame."""
    dirs = []
    for d in (config.get("corpus_dirs") or [TASKS_DIR]):
        dirs.append(expand_path(str(d)))
    explicit = args
    paths = []
    if len(explicit) == 0:
        for d in dirs:
            try:
                names = os.listdir(d)
            except Exception:
                names = []
            for n in sorted(names):
                if n.endswith(".json"):
                    paths.append(os.path.join(d, n))
    else:
        for a in explicit:
            if a.endswith(".json"):
                p = a if a.startswith("/") else os.path.join(SCRIPT_DIR, a)
                paths.append(p)
                continue
            p = None
            for d in dirs:
                cand = os.path.join(d, a + ".json")
                if os.path.exists(cand):
                    p = cand
                    break
            if p is None:
                die("no task '" + a + ".json' in corpus dirs: " + ", ".join(dirs))
            paths.append(p)
    tasks = []
    for p in paths:
        t = load_json_in_main(p, "agent task file")
        for field in ["type", "repo", "instruction", "checker", "oracle"]:
            if t.get(field) in [None, ""]:
                die("agent task '" + p + "' lacks '" + field + "'")
        tid = p.split("/")[-1].removesuffix(".json")
        t["task_id"] = tid
        t["task_path"] = p
        t["files_dir"] = os.path.join(os.path.dirname(p), "files")
        tasks.append(t)
    if len(tasks) == 0:
        die("no agent tasks found in: " + ", ".join(dirs))
    return tasks


def validate_one(pb, rcfg, t, checker_timeout):
    """One task's red -> oracle -> green proof on a fresh overlay. Returns
    (ok, message). Overlay is always stopped (Scriptling has no finally)."""
    name = "agval-" + t["task_id"]
    mount, err = start_overlay(pb, rcfg["path"], name)
    if mount is None:
        return False, err
    ok = False
    msg = ""
    try:
        err = run_setup(mount, t)
        if err is not None:
            msg = err
        else:
            red, red_detail = run_checker(mount, t, checker_timeout)
            if red:
                msg = "checker already passes before any solution (not red): " + red_detail[:160]
            else:
                oerr = apply_oracle(mount, t)
                if oerr is not None:
                    msg = oerr
                else:
                    green, green_detail = run_checker(mount, t, checker_timeout)
                    if green:
                        ok = True
                        msg = green_detail[:120]
                    else:
                        msg = "checker still fails after oracle: " + green_detail[:160]
    except Exception as e:
        msg = "unexpected: " + str(e)[:200]
    stop_overlay(pb, name)
    return ok, msg


def mode_validate(config, tasks):
    """Prove every task is well-defined WITHOUT any agent: on a fresh overlay
    the checker must FAIL (red), then the oracle must make it PASS (green).
    Zero agent tokens are spent here."""
    pb = config.get("phantom_bin", "phantom")
    repos = config["repos"]
    checker_timeout = config.get("checker_timeout", DEFAULT_CHECKER_TIMEOUT)
    all_ok = True
    for t in tasks:
        rcfg = repos.get(t["repo"])
        if rcfg is None:
            die("task '" + t["task_id"] + "' references unknown repo '" + str(t["repo"]) + "'")
        print("==> " + t["task_id"] + "  [" + str(t.get("type")) + "]")
        ok, msg = validate_one(pb, rcfg, t, checker_timeout)
        if ok:
            print("    [ok] red -> oracle -> green  (" + msg + ")")
        else:
            print("    [FAIL] " + msg)
            all_ok = False
    print()
    if all_ok:
        print("validate: all " + str(len(tasks)) + " task(s) well-defined (red -> oracle -> green)")
        return
    print("validate: FAILURES above - fix tasks before any agent run")
    raise Exception("fatal: corpus validation failed")


def mode_doctor(config, tasks):
    pb = config.get("phantom_bin", "phantom")
    arms = config.get("default_arms", [])
    failures = 0

    def check(ok, label, detail):
        nonlocal failures
        mark = "ok " if ok else "FAIL"
        print("  [" + mark + "] " + label + ("" if detail == "" else " - " + detail))
        if not ok:
            failures = failures + 1

    print("doctor:")
    r = run_cmd(pb + " health", None, 60)
    check(r["rc"] == 0, "phantom (" + pb + ")", r["out"].strip().split("\n")[0][:100])

    agent_cmd = config.get("agent", {}).get("command", "")
    for cli in ["claude", "gemini", "codex", "opencode", "qwen", "kiro", "aider", "copilot"]:
        if cli in agent_cmd:
            r = run_cmd("command -v " + cli, None, 15)
            check(r["rc"] == 0, "agent CLI '" + cli + "' on PATH", "")

    suite_tasks = 0
    for t in tasks:
        if "go test" in str(t.get("checker", {}).get("command", "")):
            suite_tasks = suite_tasks + 1
    if suite_tasks > 0:
        r = run_cmd("command -v go", None, 15)
        check(r["rc"] == 0, "go toolchain", str(suite_tasks) + " suite-graded task(s) need it")

    if "skopos" in arms:
        r = run_cmd("command -v skopos", None, 15)
        check(r["rc"] == 0, "skopos CLI on PATH", "")
        check(os.getenv("SKOPOS_MCP_URL", "") != "", "SKOPOS_MCP_URL set (skopos arm)", "")
        check(os.getenv("SKOPOS_API_KEY", "") != "", "SKOPOS_API_KEY set (skopos arm)", "")

    for name in sorted(config.get("repos", {}).keys()):
        rcfg = config["repos"][name]
        path = expand_path(str(rcfg.get("path", "")))
        r = run_cmd("git -C '" + path + "' rev-parse HEAD", None, 30)
        head = r["out"].strip()
        ref = str(rcfg.get("ref", ""))
        check(r["rc"] == 0 and head != "", "repo '" + name + "' is a git checkout", path)
        if r["rc"] == 0:
            check(head == ref, "repo '" + name + "' at pinned ref", "want " + ref[:12] + ", have " + head[:12])
            s = run_cmd("git -C '" + path + "' status --porcelain", None, 60)
            check(s["rc"] == 0 and s["out"].strip() == "", "repo '" + name + "' clean", s["out"].strip()[:80])

    print()
    if failures > 0:
        print("doctor: " + str(failures) + " problem(s) found")
        raise Exception("fatal: doctor failed")
    print("doctor: all checks passed")


def mode_run(config, tasks, reps, arms_sel):
    pb = config.get("phantom_bin", "phantom")
    repos = config["repos"]
    agent_cmd = config["agent"]["command"]
    model = str(config["agent"].get("model", ""))
    checker_timeout = config.get("checker_timeout", DEFAULT_CHECKER_TIMEOUT)
    arms_cfg = config.get("arms", {})

    for a in arms_sel:
        if a not in arms_cfg:
            die("unknown arm '" + a + "' (agents.json arms: " + ", ".join(sorted(arms_cfg.keys())) + ")")

    total = len(tasks) * len(arms_sel) * reps
    print("agent-runner: " + str(len(tasks)) + " task(s) x " + str(len(arms_sel)) +
          " arm(s) x " + str(reps) + " rep(s) = " + str(total) + " agent run(s)")
    print("arms: " + ", ".join(arms_sel) + "  (alternated within each cell; fresh session per run)")

    timestamp = time.strftime("%Y%m%d-%H%M%S")
    out_root = os.path.join(expand_path(config.get("output_dir", "outputs")),
                            "agents-" + timestamp)
    logs_dir = os.path.join(out_root, "logs")
    os.makedirs(logs_dir, exist_ok=True)

    results = []
    for t in tasks:
        rcfg = repos.get(t["repo"])
        if rcfg is None:
            die("task '" + t["task_id"] + "' references unknown repo '" + str(t["repo"]) + "'")
        print()
        print("==> " + t["task_id"] + "  [" + str(t.get("type")) + "]")
        for rep in range(1, reps + 1):
            # Alternate which arm goes first each rep (pre-registered rule 3:
            # averages out time-of-day / provider-load drift between arms).
            order = list(arms_sel)
            if rep % 2 == 0:
                order.reverse()
            for arm in order:
                rec = run_one(pb, repos, agent_cmd, model, checker_timeout,
                              arms_cfg, t, rcfg, arm, rep, logs_dir)
                results.append(rec)
                mark = "ok "
                if rec["status"] != "ok":
                    mark = rec["status"][:7].ljust(7)
                tier = "tier1 PASS"
                if not rec["tier1_pass"]:
                    tier = "tier1 FAIL"
                print("    [" + mark + "] " + arm + " r" + str(rep) + "  " + tier +
                      "  " + f"{rec['wall_seconds']:.0f}" + "s" +
                      (", " + str(rec["turns"]) + " turns" if rec["turns"] is not None else "") +
                      (", $" + f"{rec['cost_usd']:.4f}" if rec["cost_usd"] is not None else ""))

    # ---- aggregate: per task x arm, then per type (never a blended average)
    summary = {"per_task": {}, "per_type": {}}
    cells = {}
    type_cells = {}
    for r in results:
        key = r["task_id"] + "|" + r["arm"]
        if key not in cells:
            cells[key] = []
        cells[key].append(r)
    for key in sorted(cells.keys()):
        task_id, arm = key.split("|", 1)
        summary["per_task"][key] = summarize_runs(cells[key])
    for r in results:
        tk = str(r["type"])
        if tk not in type_cells:
            type_cells[tk] = {}
        if r["arm"] not in type_cells[tk]:
            type_cells[tk][r["arm"]] = []
        type_cells[tk][r["arm"]].append(r)
    for tk in sorted(type_cells.keys()):
        summary["per_type"][tk] = {}
        for arm in sorted(type_cells[tk].keys()):
            summary["per_type"][tk][arm] = summarize_runs(type_cells[tk][arm])

    payload = {
        "started_at": time.now(),
        "reps": reps,
        "arms": arms_sel,
        "tasks": [{"task_id": t["task_id"], "type": t.get("type"), "repo": t.get("repo")} for t in tasks],
        "summary": summary,
        "results": results,
    }
    os.write_file(os.path.join(out_root, "results.json"), json.dumps(payload, indent="  "))
    os.write_file(os.path.join(out_root, "summary.json"), json.dumps(summary, indent="  "))

    print()
    print("per-task cells:")
    for key in sorted(cells.keys()):
        s = summary["per_task"][key]
        w = s["wall_seconds"]
        print("  " + key.ljust(52) + " pass=" + str(s["tier1_pass_rate"]) +
              "  wall=" + str(w["median"]) + "s [" + str(w["q1"]) + "-" + str(w["q3"]) + "]" +
              "  n=" + str(w["n"]))
    print("per-type roll-up:")
    for tk in sorted(type_cells.keys()):
        for arm in sorted(type_cells[tk].keys()):
            s = summary["per_type"][tk][arm]
            print("  " + (tk + "/" + arm).ljust(30) + " pass=" + str(s["tier1_pass_rate"]) +
                  "  wall=" + str(s["wall_seconds"]["median"]) + "s  n=" + str(s["wall_seconds"]["n"]))
    print()
    print("wrote " + os.path.join(out_root, "results.json"))
    print("wrote " + os.path.join(out_root, "summary.json"))
    print("tier-2 (blind judged) scoring: feed results.json to judge/run.py afterwards")


def run_one(pb, repos, agent_cmd, model, checker_timeout, arms_cfg,
            task, rcfg, arm, rep, logs_dir):
    name = next_run_name(task["task_id"], arm, rep)
    rec = {
        "task_id": task["task_id"], "type": task.get("type"), "repo": task.get("repo"),
        "arm": arm, "rep": rep, "overlay": name,
        "status": "ok", "wall_seconds": None, "turns": None, "usage": None,
        "cost_usd": None, "tool_calls": None, "mcp_servers": None,
        "tier1_pass": False, "checker_detail": None, "changed_files": None,
        "agent_log": None, "error": None,
    }
    mount, err = start_overlay(pb, rcfg["path"], name)
    if mount is None:
        rec["status"] = "overlay_error"
        rec["error"] = err
        return rec
    try:
        err = run_setup(mount, task)
        if err is not None:
            rec["status"] = "setup_error"
            rec["error"] = err
        else:
            err = run_arm_prep(mount, arm, arms_cfg.get(arm, {}), pb)
            if err is not None:
                rec["status"] = "arm_error"
                rec["error"] = err
            else:
                agent_inner(rec, mount, pb, agent_cmd, model, checker_timeout, task, arm, name, logs_dir)
    except Exception as e:
        rec["status"] = "harness_error"
        rec["error"] = str(e)[:400]
    stop_overlay(pb, name)
    return rec


def agent_inner(rec, mount, pb, agent_cmd, model, checker_timeout, task, arm, name, logs_dir):
    """Execute the agent + capture + checker inside a started overlay. Fills
    rec in place; never raises."""
    instr_path = os.path.join(logs_dir, name + ".instruction.txt")
    os.write_file(instr_path, task["instruction"] + "\n")
    cmd = agent_cmd.replace("{instruction_file}", instr_path).replace("{model}", model)
    timeout_s = int(task.get("timeout_min", 15)) * 60
    t0 = time.perf_counter()
    r = run_cmd(cmd, mount, timeout_s)
    rec["wall_seconds"] = round(time.perf_counter() - t0, 1)
    rec["agent_log"] = os.path.join(logs_dir, name + ".stream.jsonl")
    os.write_file(rec["agent_log"], r["out"])
    if r["rc"] == 124:
        rec["status"] = "agent_timeout"
    elif r["rc"] != 0:
        rec["status"] = "agent_error"

    m = parse_stream(r["out"])
    rec["turns"] = m["turns"]
    rec["usage"] = m["usage"]
    rec["cost_usd"] = m["cost_usd"]
    rec["tool_calls"] = m["tool_calls"]
    rec["mcp_servers"] = m["mcp_servers"]

    ok, detail = run_checker(mount, task, checker_timeout)
    rec["tier1_pass"] = ok
    rec["checker_detail"] = detail[:400]
    rec["changed_files"] = overlay_diff_files(pb, name)


# ---------------------------------------------------------------- main

def usage():
    print("usage: scriptling agent-runner.py -- <mode> [options]")
    print()
    print("modes:")
    print("  doctor                    preflight: phantom, agent CLI, repos, env")
    print("  validate [--task t]...    corpus self-test: red -> oracle -> green, no agent")
    print("  run --reps N [--task t] [--arm a]...")
    print("                            the A/B experiment (spends agent tokens)")
    print()
    print("  --task accepts a task id (tasks/agents/<id>.json) or a path.")
    print("  the leading `--` is required: scriptling's own CLI otherwise eats flags.")
    print("config: agents.json next to this script (see agents.example.json).")
    sys.exit(1)


def main():
    argv = sys.argv
    if len(argv) < 2:
        usage()
    mode = argv[1]

    config = load_json_in_main(os.path.join(SCRIPT_DIR, "agents.json"), "agents config")
    if not isinstance(config.get("repos"), dict):
        die("agents.json has no 'repos' object")
    agent_cmd_cfg = config.get("agent", {}).get("command")
    if not isinstance(agent_cmd_cfg, str) or agent_cmd_cfg == "":
        die("agents.json has no agent.command")

    rest = argv[2:]
    tasks_args = []
    arms_sel = None
    reps = None
    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "--task":
            i = i + 1
            if i >= len(rest):
                die("--task needs a value")
            tasks_args.append(rest[i])
        elif a == "--arm":
            i = i + 1
            if i >= len(rest):
                die("--arm needs a value")
            arms_sel = [x.strip() for x in rest[i].split(",") if x.strip() != ""]
        elif a == "--reps":
            i = i + 1
            if i >= len(rest):
                die("--reps needs a value")
            reps = int(rest[i])
        else:
            die("unknown option '" + a + "'")
        i = i + 1

    if arms_sel is None:
        arms_sel = config.get("default_arms", [])
    if len(arms_sel) == 0:
        die("no arms selected (agents.json default_arms is empty and no --arm given)")

    tasks = load_corpus(config, tasks_args)

    if mode == "doctor":
        mode_doctor(config, tasks)
    elif mode == "validate":
        mode_validate(config, tasks)
    elif mode == "run":
        if reps is None:
            die("run mode requires --reps N (>=1; pre-registered minimum is 5 per cell)")
        if reps < 1:
            die("--reps must be >= 1")
        mode_run(config, tasks, reps, arms_sel)
    else:
        print("unknown mode '" + mode + "'")
        usage()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        if str(e).startswith("fatal: "):
            sys.exit(1)
        raise
