# llm-bench

A small CLI tool for comparing **LLM output quality** (not speed) across
providers and models. Define tasks as prompts, run them against multiple
models in parallel, and collect raw, comparable results as JSON for later
scoring — blind pairwise comparison, LLM-as-judge, or human review.

Written in [Scriptling](https://scriptling.dev), a sandboxed Python-like
language. Runs on the stock `scriptling` CLI (0.22.0+); no patched build or
extra runtime required.

Licensed under the [MIT License](LICENSE).

## How it works

```
tasks/*.json ──► runner.py ──► scriptling.ai clients ──► providers
                    │            (Claude / Gemini /
                    │             OpenAI-compatible)
                    ▼
        outputs/<task_id>-<timestamp>.json
```

1. `runner.py` loads `config.json` (providers, defaults) and each task file.
2. Each task resolves its target list (its own `targets`, else
   `default_targets`).
3. Targets run concurrently — isolated `runtime.background` workers in waves
   of `max_parallel`, one `scriptling.ai` client each, with the task's
   timeout passed straight through to the client.
4. Per-target failures are contained: a timeout, HTTP error, or bad response
   becomes an `error` result; the rest of the batch is unaffected.
5. One JSON file per task is written to `outputs/`, and a progress summary
   prints as results land.

## Setup

Requirements: the [`scriptling` CLI](https://scriptling.dev/docs/quick-start/cli/)
(0.22.0 or newer). `python3` is only needed for the optional mock test server.

```bash
cp config.example.json config.json   # then adjust providers/targets
cp .env.example .env                 # then fill in real API keys
scriptling runner.py tasks/examples  # try the shipped example tasks
```

The Scriptling CLI auto-loads `.env` from the working directory. `.env` is
gitignored; `.env.example` documents the variable names referenced by
`config.json`.

## Usage

```bash
scriptling runner.py tasks/                    # every task in a directory
scriptling runner.py tasks/examples            # the shipped examples
scriptling runner.py tasks/coding.json         # a single task
scriptling runner.py tasks/coding.json tasks/summarization.json
```

Progress output:

```
==> coding: 4 target(s), max_parallel=4, timeout=120s
    - anthropic/claude-sonnet-4-5
    - google/gemini-2.5-pro
    - zai/glm-4.7
    - local/qwen3:8b
  [wave 1/1] running: anthropic/claude-sonnet-4-5, google/gemini-2.5-pro, zai/glm-4.7, local/qwen3:8b
  · google/gemini-2.5-pro finished (9.87s)
  · anthropic/claude-sonnet-4-5 finished (11.42s)
  · local/qwen3:8b failed (2.03s): chat completion failed: …
  · zai/glm-4.7 finished (12.01s)
    [ok]    anthropic/claude-sonnet-4-5  (812 chars, 11.42s)
    [ok]    google/gemini-2.5-pro  (764 chars, 9.87s)
    [ok]    zai/glm-4.7  (690 chars, 12.0s)
    [error] local/qwen3:8b  chat completion failed: ... (truncated)
    wrote outputs/coding-20260830-234728.json  (ok=3 failed=1, 12.01s)
```

Each wave prints what's in flight, and every target reports the moment it
finishes (out of order — that's real progress); the ordered `[status]`
summary follows at the end.

Exit codes: `0` success (per-target errors are data, not program failure),
`1` fatal setup problems (bad usage, missing/invalid task or config file).

## Configuration

### `config.json`

Start from `config.example.json` and copy it to `config.json` (gitignored —
your providers and endpoints stay local):

```json
{
  "providers": {
    "anthropic": {
      "type": "anthropic",
      "base_url": "https://api.anthropic.com/v1",
      "api_key_env": "ANTHROPIC_API_KEY"
    },
    "google":    { "type": "google", "base_url": "https://generativelanguage.googleapis.com/v1beta", "api_key_env": "GEMINI_API_KEY" },
    "zai":       { "type": "openai", "base_url": "https://api.z.ai/api/paas/v4", "api_key_env": "ZAI_API_KEY" },
    "local":     { "type": "openai", "base_url": "http://localhost:11434/v1", "api_key_env": "LOCAL_API_KEY" }
  },
  "default_targets": [
    {"provider": "anthropic", "model": "claude-sonnet-4-5"},
    {"provider": "google",    "model": "gemini-2.5-pro"},
    {"provider": "zai",       "model": "glm-4.7"},
    {"provider": "local",     "model": "qwen3:8b"}
  ],
  "max_parallel": 4,
  "output_dir": "outputs"
}
```

| Field | Meaning |
|---|---|
| `providers.<name>.type` | Request/response dialect: `anthropic`, `google`, or `openai` (any OpenAI-compatible endpoint — Z.ai, Ollama, vLLM, llama.cpp, routers). |
| `providers.<name>.base_url` | Full base **including version path**: Anthropic needs `…/v1`, Gemini needs `…/v1beta`; OpenAI-compatible needs `…/v1` (or equivalent). The client appends the endpoint. |
| `providers.<name>.api_key_env` | Env var holding the key. Empty string = send no auth (keyless local/router endpoints). A missing key is not fatal — a real provider answers 401, captured as a normal `error` result. |
| `providers.<name>.max_parallel` | Optional per-provider concurrency cap within a wave (default: the global `max_parallel`). Set `1` on a local backend (LM Studio, Ollama, …) so it only ever JIT-loads one model at a time — concurrent loads make backends evict models mid-request (`{"error":"Model unloaded."}`, 503 "model is loading"), and it's not always the same model that loses. |
| `providers.<name>.max_retries` / `retry_backoff` | Optional, passed to the AI client: auto-retry of 429/5xx (retried results carry a `retry` key). Defaults: 3 retries, 1s base backoff doubling. `-1` disables retries (`0` still means default 3). For local backends that cold-load models (Ollama & co.), raise both — e.g. `5` / `10` — so a 503-while-loading resolves once the model finishes loading. |
| `default_targets` | `{provider, model}` pairs used when a task has no `targets`. One provider may appear with several different models — each pair is one job. |
| `max_parallel` | Concurrent requests per task (default `4`). |
| `output_dir` | Output directory; relative paths resolve next to `runner.py` (default `outputs`). |

Add a provider = add one entry; reference it from `default_targets` or any
task's `targets`.

### Task files (`tasks/*.json`)

Your tasks live directly in `tasks/` (committed, so the bench suite stays
versioned); the five shipped **example** tasks live in `tasks/examples/` —
run them with `scriptling runner.py tasks/examples`, or copy one as a
template for a new task. Directory scans are **not recursive**, so a plain
`scriptling runner.py tasks` runs only your tasks and never the examples.
Test-only tasks (e.g. `tests/smoke.json`, which targets mock-only providers)
stay outside `tasks/` entirely so mock-only targets never mix into real runs.

| Field | Required | Default | Notes |
|---|---|---|---|
| `prompt` | yes | — | The user message. |
| `system` | no | — | System message. |
| `max_tokens` | no | `1024` | Shared by **all** targets in the task; thinking/reasoning models often need `≥ 2048` or they return empty text (`finish_reason: length`) — size for the hungriest model in the task. |
| `temperature` | no | `0.7` | `null` omits it from the request. |
| `timeout` | no | `120` | Per-target seconds, enforced by the AI client (no 30s cap). |
| `max_parallel` | no | config value | Overrides `max_parallel` for this task. Use `1`–`2` for local-only tasks: local backends cold-load models, and simultaneous loads of several models make whichever loses the race fail ("model is loading"/503 — not always the same one). |
| `targets` | no | `default_targets` | `[{provider, model}]` — override for scenarios that only make sense for certain models. |

The task id (used in the output filename) is the filename without `.json`.

The example tasks map one-to-one to the five comparison scenarios: `coding` (precision and
restraint — includes a deliberately reported non-bug), `code-review`
(calibrated skepticism), `test-running` (interpreting failures from
evidence), `feature-design` (clarifying questions and trade-offs; local model
excluded via a `targets` override), and `summarization` (faithfulness and
compression).

## Output format

One file per run per task: `outputs/<task_id>-<timestamp>.json`.

```json
{
  "task_id": "coding",
  "task_file": "tasks/coding.json",
  "prompt": "…",
  "system": "…",
  "params": {"max_tokens": 800, "temperature": 0.2, "timeout": 120},
  "targets": [{"provider": "anthropic", "model": "claude-sonnet-4-5"}, "…"],
  "started_at": "2026-08-30T23:47:28.123456",
  "total_elapsed_seconds": 12.01,
  "summary": {"ok": 3, "failed": 1},
  "results": [
    {
      "index": 0,
      "provider": "anthropic",
      "provider_type": "anthropic",
      "model": "claude-sonnet-4-5",
      "status": "ok",
      "text": "…extracted answer, thinking blocks stripped…",
      "error": null,
      "elapsed_seconds": 11.42,
      "response": {"id": "…", "choices": ["…"], "usage": {"…"}},
      "usage": {"prompt_tokens": 19, "completion_tokens": 7, "total_tokens": 26},
      "retry": {"attempts": 2, "rate_limit_hit": true, "total_backoff": 1.0}
    }
  ]
}
```

- `status`: `ok` (completion returned) · `error` (request failed — HTTP,
  timeout, decode; details in `error`) · `config_error` (unknown provider or
  bad config; never sent).
- `response` is the `scriptling.ai` client's normalized completion dict
  (OpenAI-shaped for every provider, including finish reasons and usage) —
  the ground truth for judging. `text` is `ai.text(response)` with thinking
  blocks stripped, for direct comparison.
- `retry` appears only on results the client retried (rate limits / server
  errors are retried by default), so retries stay visible to graders.
- API keys and request headers are never written to output.

## Judging the outputs

`judge/run.py` automates the full loop for one run: it finds the latest
outputs for a task, anonymizes and shuffles the candidates, builds the
complete judge prompt from `judge/prompt.md`, calls a judge model through
the same provider config, parses its strict-JSON verdict, and merges the
label→model mapping back into a de-anonymized ranking.

```bash
scriptling judge/run.py tasks/coding.json                          # judge from config
scriptling judge/run.py tasks/coding.json anthropic/claude-sonnet-4-5   # judge override
scriptling judge/run.py outputs/coding-20260830-….json zai/glm-4.7     # a specific run
scriptling judge/run.py tasks/coding.json none            # prepare-only, ignore config judge
scriptling judge/run.py tasks/coding.json none notes.md   # prepare-only + ground-truth notes
```

- **Judge target** — optional `provider/model` argument overrides the
  optional `"judge_target"` key in `config.json`. With neither given, the
  config value is used; with neither configured, the script runs in
  **prepare-only mode**. Pass `none` to force prepare-only mode even when a
  judge is configured (e.g. you want to judge with a CLI tool instead).
  To pass a notes file while judging via the config target, name the target
  explicitly. `"judge_target"` may also set judge-call parameters:
  `max_tokens` (default 4096), `temperature` (0.1), `timeout` (240).
  Beware thinking models as judges: they can burn the whole `max_tokens`
  budget on hidden reasoning and return an empty reply — the script detects
  this and tells you; prefer a judge that reasons less or raise the budget.
- **Notes** — the optional third argument is a ground-truth file whose
  content fills the prompt's `{{NOTES}}` section (e.g. for the coding task:
  which reported inputs are actually broken).
- **Artifacts** per invocation, in `judge/out/<task_id>-<timestamp>/`:
  `prompt.md` (the complete prompt as sent), `candidates.json` (anonymized),
  `mapping.json` (label→model — for you, not the judge), `verdict.json`
  (anonymized), `verdict-merged.json` (de-anonymized — the human-review
  file), and `judge-response.json` (the raw judge completion).
- **Bias control** — candidates are randomly relabeled A/B/C… with model
  identities stripped from everything the judge sees; the judge model should
  also ideally not be one of the candidates (self-preference bias).
- **Failures** — `config_error` results are excluded (bench misconfiguration,
  not a model output); failed/truncated generations stay in and the judge is
  instructed to mark them `failed`/`truncated` and rank them last.

The verdict carries per-candidate scores on five dimensions (1–5),
strengths/defects, every pairwise winner with margin, a ranking, confidence,
and `notes_for_human`. Scenario addenda for the five comparison scenarios are
built into `judge/prompt.md`.

### Judging via CLI tools (no API access)

Prepare-only mode plus merge mode make any local CLI tool (Claude Code,
Gemini CLI, Kiro, …) the judge:

```bash
scriptling judge/run.py tasks/coding.json none  # 1. prepare (ignores any configured judge)
claude -p "$(cat judge/out/coding-…/prompt.md)" > judge/out/coding-…/verdict-pasted.json
# or: gemini -p "$(cat …)" … — or paste the prompt into any tool and save the reply
scriptling judge/run.py judge/out/coding-…/verdict-pasted.json   # 2. de-anonymize + summary
```

The saved reply can be raw JSON or a CLI-style answer with prose and markdown
fences — the script extracts the JSON object itself and merges it with the
`mapping.json` written next to the prompt it answers. Keep the mapping
private until the verdict is in, so the judging stays blind.

When reviewing, treat non-`ok` candidates by their error detail (throttle vs.
safety filter vs. timeout) rather than as uniform failures.

## Testing without API keys

`tests/mock_server.py` fakes all three provider dialects and injects
failure modes by model name:

```bash
python3 tests/mock_server.py 8899 &
```

| Model name contains | Behavior |
|---|---|
| `http500` | 500 with a provider-shaped error body |
| `badjson` | 200 with an unparseable body |
| `slow` | sleeps 30s (pair with a short task `timeout`) |
| `sleep35` | responds after 35s (long-timeout checks) |
| `empty` | well-formed response with no text |

Point a provider's `base_url` at `http://127.0.0.1:8899` (`/v1` for
anthropic/openai types, `/v1beta` for google) and run any task against it.

## Limitations / notes

- Auto-retry on 429/5xx is on (client default) — visible via the `retry`
  key. Turn it off in `run_target()` (`max_retries=-1` on `ai.Client`) if
  you want first-shot-only behavior.
- The stock `requests` library (incl. `requests.parallel`) hard-caps at 30s
  per request in Scriptling 0.22.0 — this is why the transport uses
  `scriptling.ai`, which has no such cap. See AGENTS.md before touching the
  transport.
- `runner.py` is Scriptling, not Python: it looks like Python but must obey
  Scriptling's rules. AGENTS.md lists the ones that matter here.
