#!/bin/sh
# skopos treatment arm: project-scope skopos install into the overlay.
#
# Runs with the overlay MOUNT PATH as working directory before the agent
# starts. Writes the MCP config, AGENTS steering block, and (claude-code
# only) hook suite into the overlay (upper layer), so the agent sees skopos
# code-intelligence tools with zero HOME-config changes.
#
# SKOPOS_INSTALL_AGENT selects the agent flavor to install for (claude-code,
# opencode, ...; default claude-code) - set it in .env to match the agent the
# experiment drives.
#
# Requires in the environment (put them in .env):
#   SKOPOS_MCP_URL       remote skopos MCP endpoint, e.g. https://host/mcp
#   SKOPOS_API_KEY       key sent as Authorization: Bearer
set -e
skopos install --agent "${SKOPOS_INSTALL_AGENT:-claude-code}" --scope project --url "$SKOPOS_MCP_URL" --api-key "$SKOPOS_API_KEY"

# Strengthen the steering: the stock block carves out "exhaustive find-ALL
# listings" for grep, and strong models route "every file that defines X"
# questions through that carve-out - measured: GLM-5.3 and qwen3.6-35b
# ignored the index entirely. This closes the carve-out for symbol/structure
# queries, which is the behavior this arm exists to measure.
cat >> AGENTS.md <<'EOF'

## Project rule: skopos-first for ALL code-structure queries

To remove any ambiguity from the block above: for ANY question about this
codebase's symbols, definitions, callers, call graph, impact, or file
structure — including queries phrased as "every file/function that ..." or
"list all ..." — call the skopos MCP tools FIRST (code_search, code_symbol,
code_callers, code_outline). Only fall back to grep/glob for non-symbol text
(log strings, env vars, prose in docs/comments) or when a skopos call errors.
EOF

# Override the built-in "build" agent with a skopos-first variant. AGENTS.md
# steering alone measured insufficient (GLM-5.3 read it, quoted it, and still
# grepped); agent-level instructions carry more weight, and overriding "build"
# keeps the agent command identical across arms - only this overlay contains
# the override.
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

1. FIRST call a skopos MCP tool: code_search (find symbols by name/signature),
   code_symbol (exact definition + file:line), code_callers/code_callees
   (call graph), code_outline (a file's definitions), code_impact (blast
   radius). Pass workspace_id from skopos_workspaces if a tool asks for it.
2. Use grep/glob/read ONLY to read the specific files/lines the index pointed
   you to, or for non-symbol text (log strings, env vars, prose).
3. If a skopos call errors, say so and then fall back.

This applies even when the task says "every file ..." or "list all ..." —
exhaustive symbol listings are exactly what code_search is for.
EOF
