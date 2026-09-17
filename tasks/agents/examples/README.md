# Agent-harness corpus — examples

Task files here are **generic templates**: they document the anatomy and are
meant to be copied into a real corpus dir and filled in for your repo. They
reference a fictional `widgets` repo and are not runnable as-is.

Real corpora live outside git (default `.local/corpus/`, set via
`corpus_dirs` in `agents.json`). Every task must pass
`scriptling agent-runner.py -- validate` — checker FAILS on a fresh overlay
(red) and the oracle makes it PASS (green) — before any agent run.

## Anatomy

| field | role |
|---|---|
| `type` | reporting bucket: `lookup`, `callgraph`, `feature`, `bugfix`, `navigation`, `anchorless`, ... |
| `repo` | key into `agents.json` `repos` (path + pinned ref) |
| `instruction` | exactly what the agent gets; for answer tasks, pin the ANSWER.txt format |
| `setup` | shell commands run in the overlay before the agent (place test files, apply seed patches); `{files_dir}` = the corpus `files/` dir |
| `checker` | tier-1 machine verification: `exact_set` (file/lines, sorted, normalized) or `exit0` (any command) |
| `oracle` | the known-good solution: `answer_file`, `patch` (git apply), or `commands` — must turn red into green |
| `timeout_min` | per-run wall cap |

## Ground-truth rules (learned the hard way)

- Exact answers must be **uncontested between grep and your index**: if the
  two disagree (e.g. a callers question where the index counts definition/
  type-reference edges), rephrase the question to match one semantics —
  usually "only files containing actual call sites; the defining file does
  not count".
- For suite tasks, `setup` places the failing test/check script and (for
  bugfix tasks) applies a seed patch introducing the defect; the oracle patch
  reverses it. Generate patches in a throwaway overlay, never in your
  checkout.
- For anchorless/semantic questions, phrase by **role, never identifier**,
  grade file sets, and record provenance + a grep-distance audit.

See `AGENTS.md` and `INDEX_EFFECTIVENESS_REPORT.md` at the repo root for the
measurement methodology and what each task shape is good for.
