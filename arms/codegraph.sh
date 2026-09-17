#!/bin/sh
# codegraph treatment arm (index-only): local CodeGraph index, pre-built in
# the pinned repo worktree (rides into every overlay as .codegraph/), plus
# project-scope MCP wiring and steering for opencode.
#
# Equal-strength steering to arms/skopos.sh - same imperative structure, the
# provider's own tool names. No memory/status ceremony tools involved.
set -e
if [ ! -d .codegraph ]; then
  echo "codegraph arm: no .codegraph index in this repo (pre-build it in the worktree first)" >&2
  exit 1
fi
if [ -f opencode.json ]; then
  echo "codegraph arm: opencode.json already exists, refusing to clobber" >&2
  exit 1
fi

cat > opencode.json <<'EOF'
{
  "mcp": {
    "codegraph": {
      "type": "local",
      "command": ["codegraph", "serve", "--mcp"],
      "enabled": true
    }
  }
}
EOF

cat >> AGENTS.md <<'EOF'

## Project rule: codegraph-first for ALL code-structure queries

To remove any ambiguity: for ANY question about this codebase's symbols,
definitions, callers, call graph, impact, or file structure — including
queries phrased as "every file/function that ..." or "list all ..." — call
the codegraph MCP tool FIRST. Only fall back to grep/glob for non-symbol
text (log strings, env vars, prose in docs/comments) or when a codegraph
call errors. Do NOT call any memory, status, or session-analysis tools: this
session is a measured benchmark run.
EOF

mkdir -p .opencode/agent
cat > .opencode/agent/build.md <<'EOF'
---
description: Codegraph-first coding agent for this repo
mode: primary
---

You are a coding agent working in a repository with a live CodeGraph code
index (pre-built, exposed as the `codegraph` MCP server).

NON-NEGOTIABLE WORKFLOW: before ANY grep/glob/read for code exploration, you
MUST query codegraph first. Concretely, for every task that involves
understanding, finding, listing, or changing code in this repo:

1. FIRST call `codegraph_explore` with a focused natural-language query about
   the symbols, callers, or area you need. It returns relevant symbols'
   source and call paths in one shot.
2. Use grep/glob/read ONLY to read the specific files/lines codegraph
   pointed you to, or for non-symbol text (log strings, env vars, prose).
3. If a codegraph call errors, say so and then fall back.

This applies even when the task says "every file ..." or "list all ..." —
exhaustive symbol listings are exactly what the index is for.

Benchmark run constraints: this is a measured benchmark run. Do not call any
memory, status-reporting, or session-analysis tools.
EOF