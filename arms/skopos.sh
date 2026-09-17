#!/bin/sh
# skopos treatment arm (INDEX-ONLY): project-scope skopos MCP install with
# the coordination ceremony stripped, so the A/B isolates the code index
# itself from the status/blackboard/session-cadence overhead.
#
# Measured motivation (outputs/agents-20260914-225129): with the full stock
# block, every run paid for skopos_workspaces/skopos_context/report_status/
# blackboard_write + bash 'skopos mode' checks - turns and tokens that
# contribute nothing to solving. The full-ceremony variant lives on as
# arms/skopos-full.sh for a later 3-arm pass.
#
# Runs with the overlay MOUNT PATH as working directory before the agent
# starts. SKOPOS_INSTALL_AGENT selects the agent flavor (default claude-code).
# Requires in .env: SKOPOS_MCP_URL, SKOPOS_API_KEY.
set -e
skopos install --agent "${SKOPOS_INSTALL_AGENT:-claude-code}" --scope project --url "$SKOPOS_MCP_URL" --api-key "$SKOPOS_API_KEY"

# 1. Trim the stock block to the code-exploration guidance only: drop the
#    "Shared memory" and "Session cadence" sections (keep the block markers
#    so a future install can still find/replace it).
awk '
  /<!-- skopos:begin -->/ {inblock=1}
  /## Shared memory/ && inblock {drop=1}
  /<!-- skopos:end -->/ {drop=0}
  !drop
' AGENTS.md > AGENTS.md.tmp && mv AGENTS.md.tmp AGENTS.md

# 2. Strengthen what remains: close the "exhaustive listings may grep"
#    carve-out (stock block measured insufficient on its own).
cat >> AGENTS.md <<'EOF'

## Project rule: skopos-first for ALL code-structure queries

To remove any ambiguity from the block above: for ANY question about this
codebase's symbols, definitions, callers, call graph, impact, or file
structure — including queries phrased as "every file/function that ..." or
"list all ..." — call the skopos MCP tools FIRST (code_search, code_symbol,
code_callers, code_outline). Only fall back to grep/glob for non-symbol text
(log strings, env vars, prose in docs/comments) or when a skopos call errors.
Do NOT call skopos_context, report_status, or blackboard tools: this session
is a measured benchmark run, not a coordination session.
EOF

# 3. Override the built-in "build" agent: agent-level instructions move
#    models where project rules do not (measured: GLM-5.3 quoted the AGENTS
#    block verbatim and still grepped). Command stays identical across arms -
#    only this overlay contains the override.
mkdir -p .opencode/agent
cat > .opencode/agent/build.md <<'EOF'
---
description: Skopos-first coding agent for this repo
mode: primary
---

You are a coding agent working in a repository with a live skopos code index.

NON-NEGOTIABLE WORKFLOW: before ANY grep/glob/read for code exploration, you
MUST query the skopos index first. Concretely, for every task that involves
understanding, finding, listing, or changing code in this repo:

1. FIRST call a skopos code-intelligence tool: code_search (find symbols by
   name/signature), code_symbol (exact definition + file:line),
   code_callers/code_callees (call graph), code_outline (a file's
   definitions), code_impact (blast radius). If a tool asks for workspace_id,
   get it from skopos_workspaces.
2. Use grep/glob/read ONLY to read the specific files/lines the index pointed
   you to, or for non-symbol text (log strings, env vars, prose).
3. If a skopos call errors, say so and then fall back.

This applies even when the task says "every file ..." or "list all ..." —
exhaustive symbol listings are exactly what code_search is for.

Benchmark run constraints: do NOT call skopos_context, report_status, or any
blackboard tool, and do not run the `skopos` CLI - those are coordination
overhead, not code intelligence, and they pollute the measurement. Use only
the code_* index tools (+ skopos_workspaces when a tool needs workspace_id).
EOF