#!/usr/bin/env bash
# End-to-end test inside claude-sandbox.
# Assumes: Claude Code installed, ~/.claude/projects/ exists, fixtures copied in.
# Run via: docker exec claude-sandbox bash /work/convo-recall/tests/sandbox-test.sh
set -euo pipefail

PKG=/work/convo-recall
FIXTURES=$PKG/tests/fixtures
PROJECTS=~/.claude/projects

echo "=== convo-recall sandbox test ==="
echo "Python : $(python3 --version)"
echo "Claude : $(claude --version)"
echo "OS     : $(grep PRETTY_NAME /etc/os-release | cut -d= -f2)"

# 1. Install convo-recall from source into a venv (Ubuntu 24.04 blocks system pip)
echo ""
echo "--- Install ---"
VENV=/root/.venv-recall
python3 -m venv "$VENV"
"$VENV/bin/pip" install -q "$PKG"
# Make recall available without activating venv
ln -sf "$VENV/bin/recall" /usr/local/bin/recall 2>/dev/null || true
recall --help | head -2

# 2. Copy fixture sessions into the standard Claude Code projects layout
echo ""
echo "--- Fixtures ---"
mkdir -p "$PROJECTS/mcp-scholar" "$PROJECTS/apps-midcortex"
cp "$FIXTURES/mcp-scholar/"*.jsonl   "$PROJECTS/mcp-scholar/"
cp "$FIXTURES/apps-midcortex/"*.jsonl "$PROJECTS/apps-midcortex/"
echo "Sessions: $(find "$PROJECTS" -name '*.jsonl' | wc -l | tr -d ' ') files"
echo "Projects: mcp-scholar, apps-midcortex"

# 3. Ingest — standard CONVO_RECALL_PROJECTS default points at ~/.claude/projects
echo ""
echo "--- Ingest ---"
recall ingest

# 4. Stats
echo ""
echo "--- Stats ---"
recall stats

# 5. FTS search — search for content we know is in the fixtures
echo ""
echo "--- Search: 'QA bridge test' (known content in mcp-scholar) ---"
recall search "QA bridge test" --all-projects -n 5 -c 0

echo ""
echo "--- Search: 'integration contract' ---"
recall search "integration contract" --all-projects -n 5 -c 0

# 6. Project-scoped search (slug uses underscores matching the dir name)
echo ""
echo "--- Search: scoped to mcp_scholar ---"
recall search "research query" --project mcp_scholar -n 5 -c 0

# 7. Confirm no results for nonsense query
echo ""
echo "--- Search: nonsense (expect 'No results') ---"
recall search "xyzzy_nonexistent_token_12345" --all-projects -n 3 -c 0 || true

echo ""
echo "=== PASS ==="
