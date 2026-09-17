#!/bin/sh
# graphmind treatment arm (index-only): GraphMind code graph, pre-built and
# registered in the pinned repo worktree under the slug bench-<HEADsha12>
# (the graph lives in graphmind's global store; tools address it by the
# `project` parameter, which works from overlay paths), plus project-scope
# MCP wiring and steering for opencode.
#
# Equal-strength steering to arms/skopos.sh - same imperative structure, the
# provider's own tool names. Memory/status ceremony tools are forbidden.
set -e
SLUG="bench-$(git rev-parse HEAD | cut -c1-12)"
if ! graphmind list 2>/dev/null | grep -q "$SLUG"; then
  echo "graphmind arm: project $SLUG not registered (pre-build it in the worktree first)" >&2
  exit 1
fi
if [ -f opencode.json ]; then
  echo "graphmind arm: opencode.json already exists, refusing to clobber" >&2
  exit 1
fi

cat > opencode.json <<'EOF'
{
  "mcp": {
    "graphmind": {
      "type": "local",
      "command": ["graphmind", "mcp"],
      "enabled": true
    }
  }
}
EOF

cat >> AGENTS.md <<'EOF'

## Project rule: graphmind-first for ALL code-structure queries

To remove any ambiguity: for ANY question about this codebase's symbols,
definitions, callers, call graph, impact, or file structure — including
queries phrased as "every file/function that ..." or "list all ..." — call
the graphmind MCP tools FIRST. Only fall back to grep/glob for non-symbol
text (log strings, env vars, prose in docs/comments) or when a graphmind
call errors. Do NOT call gm_memory_*, gm_session_analyze, or gm_context:
this session is a measured benchmark run.
EOF

mkdir -p .opencode/agent
cat > .opencode/agent/build.md <<EOF
---
description: Graphmind-first coding agent for this repo
mode: primary
---

You are a coding agent working in a repository with a live GraphMind code
graph (exposed as the \`graphmind\` MCP server).

NON-NEGOTIABLE WORKFLOW: before ANY grep/glob/read for code exploration, you
MUST query graphmind first. Concretely, for every task that involves
understanding, finding, listing, or changing code in this repo:

1. FIRST call a graphmind tool, ALWAYS passing project="$SLUG":
   gm_search (find symbols by text), gm_fn (one symbol's detail + source),
   gm_query (query symbols), gm_outline (a file's symbol tree),
   gm_who_calls_chain (transitive callers), gm_fn_impact / gm_impact
   (blast radius), gm_file (read a file).
2. Use grep/glob/read ONLY to read the specific files/lines graphmind
   pointed you to, or for non-symbol text (log strings, env vars, prose).
3. If a graphmind call errors, say so and then fall back.

This applies even when the task says "every file ..." or "list all ..." —
exhaustive symbol listings are exactly what the graph is for.

Benchmark run constraints: this is a measured benchmark run. Do NOT call
gm_memory_add, gm_memory_search, gm_memory_list, gm_session_analyze, or
gm_context.
EOF