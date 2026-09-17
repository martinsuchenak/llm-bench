# AGENTS.md

Guidance for AI agents working in this repository. User-facing docs live in
[README.md](README.md). Read both before changing `runner.py`.

## What this is

`llm-bench` compares **LLM output quality** (not speed) across providers. It
collects raw, comparable outputs as JSON (`outputs/*.json`); grading is a
separate downstream concern (LLM-as-judge + human review). The whole tool is
written in **Scriptling** — Python-like but NOT Python. Verify every change
by actually running it (see Verification); do not trust Python instincts.

## Files

| Path | Role |
|---|---|
| `runner.py` | The tool. Scriptling script; the only code that matters at runtime. |
| `config.example.json` | Template: providers (`type`/`base_url`/`api_key_env`), `default_targets`, `max_parallel`, `output_dir`. Committed. |
| `config.json` | Live config, copied from the example by the user. Gitignored — never commit it or mention real endpoints in it. |
| `.env` / `.env.example` | API keys. The scriptling CLI auto-loads `.env` from the CWD. Never commit `.env`; never write keys into outputs. |
| `tasks/*.json` | Your real bench tasks — **gitignored** (public repo, private prompts). `prompt` required; optional `system`, `max_tokens`, `temperature`, `timeout`, `targets` (overrides `default_targets`). |
| `tasks/examples/*.json` | The five shipped scenario examples (coding, code-review, test-running, feature-design, summarization). Directory scans are NOT recursive: `runner.py tasks` never picks these up; run them explicitly via `tasks/examples`. Test-only tasks (mock-provider `smoke.json`, `samples.json`) live in `tests/` for the same reason. |
| `judge/prompt.md` | LLM-as-judge prompt: blind comparison rubric + strict-JSON verdict. Placeholders `{{TASK}}`/`{{CANDIDATES}}`/`{{NOTES}}`; candidates must be anonymized (provider/model stripped, random labels) before judging. |
| `judge/run.py` | Judge automation: task/run file → latest run → anonymized+shuffled candidates → judge call via `scriptling.ai` → verdict + de-anonymized merge into `judge/out/` (gitignored). Also accepts a pasted judge verdict as input (raw JSON or a CLI reply with prose/fences) and merges it with the sibling `mapping.json` — merge mode for CLI-tool judges (claude/gemini/kiro). Same fatal-die-in-main-frame rule as runner.py; fatal validation lives only in `main()`. |
| `agent-runner.py` | The A/B harness: measures whether code-intelligence (skopos MCP) makes AI coding agents faster/cheaper/better. Modes `doctor`/`validate`/`run` — note the required `--` separator (scriptling's CLI eats unknown flags). Zero agent tokens unless `run` is invoked. |
| `agents.example.json` | Harness config template: `repos` (path + pinned ref), `agent` (command template with `{instruction_file}`/`{model}`), `arms`, `default_arms`, `phantom_bin`, `output_dir`. Committed. |
| `agents.json` | Live harness config, copied from the example. **Gitignored.** Skopos arm needs `SKOPOS_MCP_URL`/`SKOPOS_API_KEY` in `.env`. |
| `tasks/agents/examples/` | Generic corpus TEMPLATES (committed) — anatomy docs + lookup/feature/bugfix examples referencing a fictional repo. NOT runnable as-is. |
| `.local/corpus/*.json` | The real bench corpus (gitignored — private ground truth). Same anatomy: `type`/`repo`/`instruction`/`setup`/`checker`/`oracle`/`timeout_min`; support files in `.local/corpus/files/`, referenced via `{files_dir}`. Located via `corpus_dirs` in `agents.json` (default `[.local/corpus]`). |
| `arms/*.sh` | One prep script per arm; runs inside the overlay mount before the agent. baseline = plain repo; skopos = project-scope `skopos install`. An arm IS its prep script + overlay contents. |
| `outputs/` | Generated results (gitignored). Agent runs land in `outputs/agents-<ts>/` (results.json, summary.json, logs/). |
| `tests/mock_server.py` | Python stdlib mock of all three provider dialects, with failure- and judge-mode directives. Test infrastructure only. |

## Architecture (deliberate decisions — don't undo without cause)

- **Transport is `scriptling.ai` (`ai.Client`), NOT `requests`/`requests.parallel`.**
  Chosen because the stock `requests` library hard-caps every request at 30s
  (`pool/pool.go` DefaultConfig; measured on 0.22.0, still true on 0.25.1) —
  unusable for real LLM
  generations — while the `scriptling.ai` client honors per-call `timeout`
  with no cap (own transport, 10-minute default). Trade-off accepted by the
  owner: responses are the client's **normalized** completion dicts rather
  than provider-native bodies, and 429/5xx auto-retry is ON by default
  (retried results carry a `retry` key so this stays visible).
- **Concurrency**: one `runtime.background` isolated worker per target, in
  waves of `max_parallel`, results collected via `Promise.get()`. Workers
  must never raise: they catch everything and return a result record so one
  bad target can't kill the batch.
- **Statuses**: `ok` / `error` / `config_error`. Failure *details* live in
  `error` text (throttle vs. decode vs. timeout); don't invent finer status
  enums without a reason.
- **Multi-sampling**: a task's `samples` N expands every target into N jobs
  (target-major; `sample` field on each result; provider caps pace the
  sampling automatically). The judge groups results by provider/model into
  one candidate with a `samples` list — it ranks models, not samples.
- **The output JSON schema is a contract** for the grading stage. Add fields;
  don't rename or remove existing ones.
- **Fatal vs. per-result errors**: fatal errors (bad usage, unreadable
  config/task files) must `die()` from `main()`'s own frame — see the exit
  code quirk below. Per-target problems are always result records.

### agent-runner harness (A/B code-index effectiveness)

Design source of truth: `.local/index-effectiveness-harness.md` (gitignored).
Deviations from it, all deliberate:

- The harness runs the agent itself as a subprocess with cwd = overlay mount
  (NOT `phantom run`): task setup and arm prep must execute inside the
  overlay between creation and agent start, and phantom run couples the two.
  phantom stays the overlay/diff/cleanup layer (`start`/`diff`/`stop`).
- Checkers run in the harness, not via phantom hooks: direct result
  retrieval, and hooks.yaml is global (~/.phantom) which risks cross-arm
  contamination.
- `validate` proves MORE than the doc requires: checker must FAIL on a fresh
  overlay (red) before the oracle makes it pass (green) — an oracle passing
  an already-green test proves nothing and hides difficulty calibration.
- Repos are pinned by ref via a **pristine detached git worktree** under
  `.local/repos/<name>-<ref12>/` (phantom overlays the WORKING TREE, so the
  user's real checkout and its WIP must never be the base). Doctor enforces
  HEAD == ref and a clean tree.
- Bugfix tasks seed their defect: `setup` applies a seed patch (generated
  with git in a throwaway overlay — never dirty the base repo), the oracle
  patch reverses it; both live in the corpus `files/` dir next to the task.
- Ground truth for exact-answer tasks must be UNCONTESTED between grep and
  the index (e.g. "callers of ids.New" is disqualified: the index's
  name-based edges conflate uuid.New/ids.New, grep finds 5 files, index ~15).

## Scriptling rules that bite (verified on 0.22.0; full suite re-run green on 0.25.1)

1. **No pathlib for path strings.** `str(Path)` and `print(Path)` yield
   `<Path object at 0x…>`. Use plain strings with `os.listdir`,
   `os.path.join`; derive task ids via `split("/")[-1].removesuffix(".json")`.
2. **Exit codes vs. frame depth.** An uncaught exception (including
   `sys.exit`) raised across 3+ call frames prints its message but the
   process exits **0**. This is why all fatal validation (`die()`) happens
   directly in `main()`, tasks are pre-loaded before running, and `run_task`
   has no fatal paths. Keep it that way. Both scripts also wrap `main()` in
   `try/except` at the `__main__` block: exceptions whose message starts
   with `"fatal: "` (i.e. came from `die()`, which already printed its
   message) exit 1 silently; anything unexpected re-raises so the
   interpreter's diagnostics still show. Usage errors print a usage block
   and `sys.exit(1)` inline in `main()` (SystemExit is uncatchable and
   propagates cleanly at that depth).
3. **Isolated background handlers** run in a fresh environment: re-import
   libraries inside them (`import scriptling.ai as ai`, `import time`),
   pass only transferable args (strings/numbers/dicts — no instances or
   functions), and return results instead of raising.
4. **`ai.Client` URL shapes**: the CLAUDE provider appends `/messages`
   (base must end in `/v1`), GEMINI appends `/models/<model>:generateContent`
   (base must end in `/v1beta`), OpenAI-compatible appends
   `/chat/completions` (base includes `/v1` or the vendor equivalent).
   Provider type mapping: `anthropic → ai.CLAUDE`, `google → ai.GEMINI`,
   `openai → ai.OPENAI`.
5. **Thinking models** (GLM, qwen) consume `max_tokens` on hidden reasoning
   and return empty `text` with `finish_reason: "length"` when it's too
   small — use `max_tokens ≥ 2048` for open-ended tasks, and don't "fix"
   empty-but-ok results; check `finish_reason` in `response`.
6. **`requests.parallel` (if ever reintroduced)** returns a full-length list,
   but transport-failed positions raise a catchable exception **on index
   access** — access `results[i]` inside try/except, never iterate. Malformed
   specs surface as `status_code == 0` with the message in `body`.
7. General language gaps: no type annotations, no `yield`, no walrus, no
   `open()` (use `os.read_file`/`os.write_file`), `import os.path`
   separately, dict iteration via `.items()`. Lint with
   `scriptling --lint runner.py` — editor Python LSP errors on
   `os.read_file`/`time.now`/`scriptling.*` are false positives.
8. **No backslash line-continuations** (parser error: ILLEGAL). Continue
   long expressions only inside parentheses (call args, parens); no `try/finally`
   either — restructure so cleanup runs after the except block. No
   `os.popen`, no `os.getpid`, `time.now()` returns a STRING (no arithmetic).
9. **subprocess.run works but has sharp edges** (all verified on 0.25.1):
   `cwd=`/exit-code passthrough fine; `capture_output` captures stdout only
   (stderr is force-dropped — the command itself must `2>&1`); the `timeout`
   kwarg is SILENTLY IGNORED (wrap commands in a shell watchdog:
   exec + sleep/kill, exit 124); and if ANY backgrounded process inherits
   the stdout pipe (your watchdog, or a daemon the command spawns), the call
   DEADLOCKS forever in pthread_cond_wait — route output through a temp
   file and `cat` it from the main shell (see `run_cmd` in agent-runner.py).
10. **The scriptling CLI parses unknown `--flags` itself** and rejects them
    before the script runs ("unknown flag: --reps", exit 0 thanks to quirk
    #2). Scripts that take flags need the literal `--` separator:
    `scriptling agent-runner.py -- run --reps 5`.
11. `__file__` is RELATIVE when invoked as `scriptling foo.py` from the
    script's directory, so `os.path.dirname(__file__)` can be `""` —
    absolutize SCRIPT_DIR against `os.getcwd()` before deriving paths that
    subprocesses (with different cwd) must resolve.
12. JSON null defeats `.get(key, default)`: when the key EXISTS with value
    null, the default is NOT used, and iterating the result raises
    "expected iterable, got NULL" (phantom diff emits `"files": null` for an
    unchanged overlay). Guard with `or []` / isinstance.

## Verification

Never ship a change to `runner.py` without running it:

```bash
scriptling --lint runner.py                      # syntax check
python3 tests/mock_server.py 8899 &              # start mock (once)

# In a scratch COPY of the project (never point the real config at the mock):
#   providers: anthropic → http://127.0.0.1:8899/v1
#              google    → http://127.0.0.1:8899/v1beta
#              openai-type → http://127.0.0.1:8899/v1 (+ extra provider
#              "dead" → http://127.0.0.1:9/v1 for conn-refused)
#   then: cp tests/smoke.json <scratch>/tasks/
#         cp tests/samples.json <scratch>/
scriptling runner.py tasks/smoke.json            # expect ok=5 failed=5 across
                                                 # ok/error/config_error paths
scriptling runner.py samples.json                # multi-sampling: expect
                                                 # ok=6 failed=3, results with
                                                 # sample 1..3 per target
                                                 # (target-major order)
scriptling runner.py tasks                       # runs the copied smoke.json;
                                                 # subdirs (examples/) are
                                                 # skipped by design - green
scriptling runner.py tasks/missing.json; echo $? # expect exit 1
scriptling runner.py;                  echo $?   # expect exit 1 (usage)

# Judge path (same mock; judgejson returns a complete verdict for A..I,
# matching smoke.json's 9 non-config_error candidates):
scriptling judge/run.py tasks/smoke.json zai/smoke-judgejson
                                                 # expect a de-anonymized ranking,
                                                 # ZERO consistency warnings, and a
                                                 # judge block (provider/model/
                                                 # params/judged_at) in judge/out/
                                                 # smoke-…/verdict-merged.json
scriptling judge/run.py <that judge/out/smoke-…/verdict.json>
                                                 # merge mode: same ranking, judge
                                                 # source "pasted verdict"; a PARTIAL
                                                 # pasted verdict (some labels/pairs
                                                 # missing) must print consistency
                                                 # warnings and record them in
                                                 # verdict-merged.json's validation

# agent-runner (needs the local env: phantom on PATH, agents.json pointing at
# a pristine pinned worktree in .local/repos/, .env with SKOPOS_* vars):
scriptling agent-runner.py;                 echo $?   # expect exit 1 (usage)
scriptling agent-runner.py -- doctor                 # expect all checks [ok ]
scriptling agent-runner.py -- validate               # expect Nx red -> oracle ->
                                                     # green (N = corpus size),
                                                     # exit 0, ZERO agent tokens
scriptling agent-runner.py -- run --reps 1 --task <corpus-task-id> \
  --arm baseline                                     # REAL agent spend; smoke
                                                     # cheaply by temporarily
                                                     # setting agents.json
                                                     # agent.command to "cat {instruction_file}"
                                                     # (tier1 FAIL + ok status =
                                                     # machinery works)
```

`tests/smoke.json` covers the full matrix: normal targets (all three provider
types), a model containing `http500`, `badjson`, `slow` (with a short
`timeout`), `empty`, a `dead` provider (connection refused), and an unknown
provider name (`config_error`). Every result must land in the output JSON
with the batch intact.

Live check (any real OpenAI-compatible endpoint you can reach — add a
temporary provider entry for it in your local `config.json` first):

```bash
scriptling runner.py <task file targeting that provider,
                       max_tokens 2048, timeout 240>
```

## Conventions

- Scriptling 4-space indent, double quotes, explicit `str()` concatenation
  (no implicit coercion); comments only where a rule above isn't obvious.
- Keep stdout output stable-ish: task header, `- target` lines, a
  `[wave n/m] running:` line per wave, `·` live completion lines printed by
  the background workers themselves (they land out of order — that's the
  point), ordered `[status]` per-result lines, `wrote …` summary. Humans and
  scripts skim it.
- `output_dir` and `config.json` resolve relative to the script's own
  directory (`SCRIPT_DIR`), so the tool works from any CWD.
- Docs: user-facing changes update README.md; agent-facing traps and
  procedures update this file.
