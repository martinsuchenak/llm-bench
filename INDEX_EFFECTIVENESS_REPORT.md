# Code-Index Effectiveness — Full Experiment Report

**Question:** does giving AI coding agents access to a code index make them *faster, cheaper, or better*?

**Series:** 9 experiment passes · 224 measured agent runs · 4 repositories (256 → 12,633 files) · 2 models · 4 code-intelligence providers · Sept 14–15, 2026
**Harness:** `agent-runner.py` (this repo) over phantom overlays; all runs tier-1 machine-verified; zero Claude/API spend (opencode + GLM-5.3 coding plan + local LM Studio model)
**Design source:** `.local/index-effectiveness-harness.md` (pre-registered rules)

---

## 1. Executive summary

1. **For a strong model (GLM-5.3) doing normally-anchored code work, no index beat plain grep in any configuration tested.** Baseline grep was fastest at every repo size; index arms cost 1.5–2× wall time and up to 2.4× tokens at small scales. Quality was at ceiling for both sides nearly everywhere.
2. **But the index's economics scale with repo size.** Input-token cost crossed parity around ~2,200 files and at 12,633 files the index arm used *fewer* input tokens than grep on read-heavy tasks (up to −37%).
3. **And the effect flips sign with model strength.** The same index that made GLM-5.3 55% *slower* on a feature task made a local 35B model 35% *faster* — with 65% fewer input tokens. A compact index measurably rescues a weak model in a huge repo.
4. **Index answer semantics transfer to agent correctness.** All three index providers answered a "who calls X" question identically *wrong* (per the checker) by including the definition file in caller edges — a compliant agent inherits the index's semantic looseness. Task phrasing must match index semantics or exact-answer grading punishes indexed agents unfairly.
5. **Steering compliance is a solved-but-nontrivial prerequisite.** Project-rule steering (AGENTS.md) was read, quoted, and ignored by strong models. Agent-level instruction overrides produced 100% compliance (GLM) and partial compliance (local 35B: 4/8–8/8 depending on provider). Without compliance, an "index arm" silently measures nothing.
6. **Provider profiles differ sharply:** GraphMind = compact results, token champion (baseline token parity, and the only provider to beat baseline wall time — for the weak model); CodeGraph = strongest treatment wall-time at small/medium scale; skopos = richest tool surface, heaviest context cost.

**One-line conclusion:** *index value = f(model weakness × repo size × task read-heaviness × result compactness × lexical anchoredness)* — deploy indexes for weak/local models on large repos, and for anchorless semantic work at any model strength; for strong models on lexically anchored tasks they are pure overhead.

---

## 2. Hypothesis and pre-registered rules

The design doc fixed the rules before any agent ran:

- **Arms:** same agent, same model, same task; the only independent variable is which code-intelligence the agent can reach. An arm *is* the overlay contents — the treatment overlay contains the provider's install, the baseline overlay contains nothing extra. No HOME-config flipping.
- **Repetitions:** ≥5 per cell pre-registered; exploration passes used 2 per cell (noted as a limitation); single runs treated as noise.
- **Fresh session per run**, arms alternated within each cell to average out time-of-day/provider drift.
- **Fair baseline:** the baseline keeps ordinary grep/read competence. No strawman.
- **A task is only defined when its checker AND oracle run green with no agent** (`validate` mode enforces red → oracle → green).
- **Metrics:** wall time, turns, input/output/cache tokens, cost, tool calls by name (from the agent's own event stream), tier-1 pass (machine-verified), changed files. Medians + IQRs per task and per task type — never a blended average. Tier-2 (blind LLM judging) reserved for open-ended tasks; all tasks in this series were tier-1 checkable.
- **Expectation pre-registered:** the index's value should scale with repo size; a neutral result on small repos is itself a finding.

## 3. Methodology

### 3.1 The harness (`agent-runner.py`, Scriptling)

| Mode | What it does | Agent tokens |
|---|---|---|
| `doctor` | Preflight: phantom, agent CLI, go/php, skopos env, repos pinned+clean | 0 |
| `validate` | Per task, on a fresh overlay: checker must FAIL (red), oracle must make it PASS (green) | 0 |
| `run --reps N` | The experiment; per-run overlay + metrics capture + stats | all |

Per run: `phantom start` (copy-on-write overlay over a pristine worktree) → task setup (place test files / apply seed patch) → arm prep script → agent subprocess (cwd = overlay mount) → checker in the overlay → `phantom diff` (changed files, `.git` filtered) → `phantom stop --cleanup`. Full agent event streams and instructions are archived under `outputs/agents-<ts>/logs/`.

### 3.2 Repos and corpora

| repo | files (indexed) | language | tasks | types |
|---|---|---|---|---|
| Repo A | 256 | Go | 4 | lookup, callgraph, feature, bugfix |
| Repo B | 657 | Go | 4 | lookup, callgraph, feature, bugfix |
| Repo C | 2,246 | PHP | 7 | + 3 navigation chains |
| Repo D | 12,633 | PHP | 4 | lookup, callgraph, feature, bugfix |

Every repo pinned to an exact commit in a **pristine detached git worktree** (overlays see the working tree, so the user's checkout and its WIP must never be the base). Task anatomy: `instruction / setup / checker / oracle / timeout_min`. Checkers are exact-set (ANSWER.txt, sorted line set) or exit-0 commands (`go test`, plain `php` scripts — no framework needed). Bugfix tasks ship a seeded defect patch (applied by setup) plus an oracle patch that reverses it; feature tasks ship a placed test/check script plus an oracle implementation patch.

### 3.3 Arms (equal-strength steering)

| arm | treatment contents |
|---|---|
| `baseline` | plain repo overlay; agent keeps grep/read |
| `skopos` | project-scope skopos install (remote MCP), steering trimmed to code-exploration only (status/blackboard ceremony stripped), agent-override instruction |
| `repoA-full` | as above but with the full stock steering incl. session cadence (preserved for a future 3-arm pass; measured once in pass 2) |
| `codegraph` | pre-built local CodeGraph index (`.codegraph/` rides into overlays), project MCP wiring (`codegraph serve --mcp`), agent-override |
| `graphmind` | GraphMind graph in its global store, registered per repo as `bench-<HEADsha12>` (tools address it by explicit `project` parameter → overlay-safe), project MCP wiring (`graphmind mcp`), agent-override |

The **compliance mechanism** (measured, see §5.1): opencode's built-in agent is overridden via `.opencode/agent/build.md` with an imperative "query the index BEFORE any grep" instruction plus benchmark constraints (no memory/status tools). Arm commands are identical across arms; only the overlay contents differ.

### 3.4 Models

- **GLM-5.3** (strong; zai coding plan via opencode `--format json` stream)
- **qwen3.6-35b-a3b-mtp** (weak/local; LM Studio, 64k context)

Metrics parser understands both opencode's event dialect and claude's stream-json (the harness is agent-CLI agnostic; claude config is documented but untested in this series).

---

## 4. Pass-by-pass results

Wall times are per-cell medians (baseline vs best-index unless noted). Full data: `outputs/agents-*/results.json` + `summary.json`.

### Pass 1 — Repo A, local qwen3.6-35b, 40 runs (steering = stock AGENTS block only)
- 40/40 tier1 PASS both arms (ceiling from the start).
- skopos arm slower everywhere: lookup 43→55s, callgraph 48→57s, bugfix 75→92s, feature 89→105s; input tokens +28–64%.
- **Tool compliance only 4/20** — the treatment often wasn't even engaged.
- **Confound discovered:** opencode ships its own built-in codegraph tool; the "baseline" was not index-free (1–2 incidental uses).

### Pass 2 — Repo A, GLM-5.3, full-ceremony skopos arm, 16 runs
- 16/16 PASS. After the compliance fix (agent override): 8/8 runs used skopos tools.
- skopos arm ~2× slower on every type (lookup 29→58s, callgraph 23→45s, bugfix 52→152s, feature 97→178s); input tokens 1.1–4.3× (feature 8.8k→38k).
- **Compliance ladder established** (§5.1).
- **Ceremony cost isolated:** every treatment run paid for `skopos_workspaces` (8/8), `skopos_context` (7/8), `report_status` (6/8), `blackboard_write` (3/8) — coordination overhead that contributes nothing to solving. → arm redesigned to index-only.

### Pass 3 — Repo B (657 files), GLM-5.3, index-only arm, 16 runs
- 16/16 PASS; compliance 8/8; ceremony zero (verified: 0 context/report/blackboard calls).
- Still ~2× slower: lookup 34→65s, callgraph 22→43s, feature 59→111s, bugfix 51→133s.
- Output tokens doubled-to-tripled (chunky index results re-serialized in reasoning across more turns).
- Remote MCP latency ruled out as the cause (code_search round-trips: 35–55 ms). The cost is extra model round-trips and larger per-step contexts.

### Pass 4 — Repo C (2,246 files), GLM-5.3, index-only, 16 runs
- 14/16 — **first quality differentiation, negative for the index:** callgraph 0/2 in the index arm vs 2/2 baseline, both failures identical — the agent included the definition file (the helper's own definition file) because the index's caller edges are inclusive of definition/type references and the agent (per steering) did not cross-check with grep.
- Token penalty **gone at this scale**: direction mixed per task (feature 19k→16k, bugfix 18k→21.5k) — baseline now pays to read huge legacy files (`project_functions.php` is 8,700+ lines).

### Pass 5 — Repo C navigation chains, GLM-5.3, 12 runs
- Three multi-hop, string-indirection chains (API route → controller → procedural payment function; config component name → `new $c()` → class file; cron queue → TaskManager → task class), all validated.
- Baseline 6/6 @ 26s median (3–5 turns, surgical grep) vs index arm 5/6 @ 53s (5–8 turns). The one failure: agent stopped early without writing ANSWER.txt.
- **Honest caveat:** every hop retained a literal string anchor — multi-hop but lexically navigable. Anchorless semantic navigation remains untested (and is hard to tier-1 check).

### Pass 6 — Repo D (12,633 files), GLM-5.3, index-only, 16 runs
- 16/16 PASS; caller ground truth pre-verified to match index semantics exactly.
- Wall gap narrowed to 1.2–1.7× (lookup 60→73s, callgraph 47→75s, feature 121→169s, bugfix 65→108s).
- **Token crossover:** index arm used *fewer* input tokens on read-heavy tasks — feature 41k→26k (−37%), bugfix 41k→33k (−21%).

### Pass 7+8 — four-provider comparison, GLM-5.3, 76 runs (codegraph + graphmind added; baseline/skopos from passes 2–6)

Overall (n=38 per arm):

| arm | wall (median) | input tokens (median) | tier1 | tier1 excl. trap task |
|---|---|---|---|---|
| baseline | **41s** | 11.9k | 38/38 | 36/36 |
| Repo A | 76s | 28.3k | 35/38 | 35/36 |
| codegraph | 79s | 27.5k | 36/38 | **36/36** |
| graphmind | 95s | **11.9k** | 35/38 | 35/36 |

- **GraphMind matched baseline token economics exactly** (11.9k vs 11.9k median — half of skopos/codegraph) because its results are compact; it pays in wall time (one 582s outlier).
- **CodeGraph strongest treatment wall time** at small/medium scale (Repo A feature 74s vs repoA-arm 178s; Repo B callgraph 37s vs 43s) and **tied baseline outright on Repo D feature** (122s vs 121s) — first wall-time parity point.
- **The edge-semantics trap is universal:** Repo C callgraph failed 0/2 under *all three* providers with byte-identical answers (4 correct files + the definition file). Repo D's phrasing (which explicitly excludes the defining file) passed 2/2 under all three. Inclusive caller edges are an industry-wide index convention; the task was rephrased for future passes.

### Pass 9 — the weak-model rescue test: qwen3.6-35b, all 4 arms, Repo D, 32 runs
- 32/32 PASS. Compliance: codegraph 8/8, graphmind 6/8, skopos 4/8.

| task | baseline | Repo A | codegraph | graphmind |
|---|---|---|---|---|
| lookup | **60s** | 87s | 106s | 89s |
| callgraph | **72s** | 101s | 109s | 80s |
| feature | 162s | 169s | 146s | **105s** ✓ |
| bugfix | 114s | 156s | 182s | **113s** (tie) |

- **The sign flip** (feature cell, same tasks): GLM-5.3 with graphmind 187s vs its baseline 121s (index hurt, +55%); qwen-35b with graphmind 105s vs its baseline 162s (index helped, −35%).
- **Mechanism:** weak model + grep flails in the huge repo (8–11 turns, 358k input tokens re-reading big files); the compact index gives it structure (4 turns, 123k tokens = **−65% input**, output tokens −34%).

### Pass 10 — anchorless semantic questions, GLM-5.3, 30 runs + 5 probes
- New corpus: 5 mechanism questions on Repo D (deferred-work pipeline, permission-enforcement chain, two-phase fatal-error handler swap, CLI task-runner framework, error output-renderer family), phrased by **role, never identifier**; file-set ground truth derived by reading code, with a recorded grep-distance audit (`tasks/agents/files/repoD-anchorless-provenance.md`).
- Difficulty probes (grep-only, 1/question): 4/5 solved (42–98s), 1 **failed** (error-renderers: 11 similarly-named driver classes; which renders HTML vs JSON?).
- **First strong-model quality win for an index:** baseline 9/10 @ 74s med / 30.9k in-tok · skopos 10/10 @ 126s / 42.4k · **graphmind 10/10 @ 81s (+9% wall) / 23.2k (−25% tokens vs baseline)**.
- The discriminator: on the grep-hard question, grep attempts went 1/3 (failures picked a general-error output driver instead of the HTML driver — grep finds the directory, not the role); both index arms went 4/4 — semantic role resolution.
- Verdict: on anchorless questions the index delivers a genuine **recall** edge for a strong model; graphmind delivers it at near-parity wall and lower tokens.

### Pass 11 — skopos redeploy A/B (compact results + semantic code_find), 2 × 10 runs
- Two server redeploys shipped recommendations from this report. Verified: terse `code_search` results, `kind` labels on caller edges, workspace defaulting (unambiguous keys), question-shape steering, and `code_find` — a vector-semantic search with compact labeled results.
- Same anchorless cell, three measurements: **input tokens 42.4k → 39.1k → 36.8k (−13.2% cumulative)**; wall 126 → 130 → 117s (−7%); turns flat; tier1 10/10 → 10/10 → 9/10.
- The one v2 failure substituted `an email-queue class` for `a communication helper` — a *semantic neighbor* plausibly surfaced by vector search; same question passed under v1, so n=2 cannot attribute. Noted as the semantic-search analog of the caller-edge lesson: vector hits need the same skeptical consumption as graph edges.
- Remaining gaps vs graphmind: tokens +59% (28-tool schema surface rides every step — `plan_*`/`blackboard_*` are dead weight for code-only consumers; a code-only endpoint is the biggest unshipped lever), and the `skopos_workspaces` dead turn persists for multi-workspace keys (10/10 runs).

---

## 5. Cross-cutting findings

### 5.1 The compliance ladder (prerequisite for measuring anything)
1. Stock AGENTS.md steering: model could quote it verbatim (proven with zero-tool-call probe) yet used grep on real tasks — strong models weight trained habits over project rules.
2. Strengthened AGENTS.md rule (carve-out closed): no behavior change.
3. **Agent-level override (`.opencode/agent/build.md`, imperative, mode: primary): 100% compliance for GLM-5.3** across all three providers; partial for the local 35B (skopos 4/8, codegraph 8/8, graphmind 6/8).
4. Claude-Code's equivalent (hook nudges) is designed but untested in this series.

### 5.2 Ceremony vs index (cost attribution)
The stock skopos install bundles code intelligence with coordination ceremony (workspace discovery, session context, status reporting, blackboard writes). Measured on pass 2, ceremony accounted for a large share of the token blowout (feature 8.8k→38k full vs 16.5k→22.7k index-only at Repo B scale). All later arms strip it; `arms/repoA-full.sh` preserves the full variant.

### 5.3 Index semantics transfer to agent correctness
Inclusive caller edges (definition/type references counted as "callers") are shared by all three providers and are *documented* behavior — but a compliant agent that trusts the index answers one file too many, while a grep agent is right. Exact-answer corpora must either phrase questions to match index semantics or verify ground truth against the index before freezing it (this report's corpora do the latter; the one task that didn't — Repo C callgraph — measured the trap, and was rephrased).

### 5.4 The scale trend (input tokens, index/baseline, read-heavy tasks)

| repo | files | token ratio (index/baseline) | wall ratio |
|---|---|---|---|
| Repo A | 256 | 1.1–4.3× more | ~2× slower |
| Repo B | 657 | 1.2–1.7× more | ~2× slower |
| Repo C | 2,246 | ~parity (0.8–1.4×) | 1.4–3× slower |
| Repo D | 12,633 | 0.6–1.7× — index wins read-heavy | 1.2–1.7× slower |

Parity ≈ 2k files; index advantage emerges at 12k on read-heavy work. Wall time narrows but did not cross for the strong model.

### 5.5 Provider profiles

| provider | tools exposed | wall | tokens | notes |
|---|---|---|---|---|
| grep (baseline) | — | best at all scales (strong model) | parity→worse with scale | surgical, 3–6 turns |
| Repo A | 10 rich tools (remote MCP) | mid | worst (2.4×) | richest queries (impact trees, branch diff); ceremony stripped; remote latency negligible (35–55ms) |
| CodeGraph | 1 one-shot (`codegraph_explore`) | best treatment at small/medium; ties baseline at Repo D feature | heavy (2.3×) | chunky one-shot results |
| GraphMind | 26 tools (local) | worst for strong model; **wins for weak model** | **champion — baseline parity** | compact results + semantic search; global-store registration made overlay-safe via slug |

---

## 6. Conclusion

**Index value = f(model weakness × repo size × task read-heaviness × result compactness).**

- **Strong model + anchored tasks (any size tested):** grep wins. Indexes cost 1.5–2× wall and up to 2.4× tokens (except graphmind's token parity). No quality advantage — both sides at ceiling.
- **Large repo + read-heavy tasks:** token economics cross over ~2k files in the index's favor; wall-time parity appears at ~12k files.
- **Weak model + large repo:** the index (specifically compact-result graphmind) wins outright — −35% wall, −65% tokens on feature work; ties bugfix. This is the rescue regime, and it flipped the sign of the index effect versus the strong model on identical tasks.
- **Deployment guidance:** provision indexes for weak/local models on large repos — there they pay for themselves immediately. For strong models, an index must justify itself through what grep cannot do: anchorless semantic retrieval and transitive impact questions (untested here — the open frontier), not speed. And whatever you deploy, **verify steering compliance** (project rules alone are ignored) and **match task semantics to index semantics**.

## 7. Limitations and threats to validity

- **n=2 reps per cell** in most passes (pre-registered minimum is 5; effects observed are large relative to run-to-run spread, but confidence intervals are wide; pass-1 cells have n=5).
- **All tasks lexically anchored** — single-hop or multi-hop with literal string anchors. The index's theoretical home turf (anchorless semantic navigation, transitive impact) was not tested; tier-1 exact-answer checking of such questions is an open corpus-design problem.
- **Quality ceiling:** ~96% of runs passed tier-1 in both arms; quality barely discriminates in this corpus. Harder tasks needed.
- **One agent CLI** (opencode headless), two models, one steering strength per provider. Claude CLI + hook-nudge treatment documented but unmeasured.
- **Steering is part of the treatment** by design ("index + instructions to use it"); arms differ in both tools and instructions.
- Skopos arm queried a remote server (35–55 ms measured — negligible vs model turns); codegraph/graphmind are local.
- One graphmind run hit 582s (agent thrash) — included, not outlier-removed.

## 8. Artifacts and reproducibility

- Harness: `agent-runner.py`; arms: `arms/{baseline,skopos,repoA-full,codegraph,graphmind}.sh`; config: `agents.example.json` → `agents.json` (gitignored); corpora: `tasks/agents/*.json` + `tasks/agents/files/`; worktrees: `.local/repos/<repo>-<ref12>/`.
- Raw results: `outputs/agents-<timestamp>/{results.json,summary.json,logs/}` (per-run event streams retained).
- All findings also recorded on the skopos blackboard (workspace `github.com/martinsuchenak/llm-bench`, branch `main`).

```bash
scriptling agent-runner.py -- doctor
scriptling agent-runner.py -- validate                 # zero agent tokens
scriptling agent-runner.py -- run --reps 5 --task <id> --arm baseline,skopos,codegraph,graphmind
```

*Report generated 2026-09-15 from 224 measured runs across outputs/agents-20260914-214947 … agents-20260915-122023.*
