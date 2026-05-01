#!/usr/bin/env bash
# End-to-end test inside claude-sandbox.
# Assumes: Claude Code installed, ~/.claude/projects/ exists, fixtures copied in.
# Run via: docker exec claude-sandbox bash /work/convo-recall/tests/sandbox-test.sh
set -euo pipefail

# ── DESTRUCTIVE-SCRIPT GUARD ─────────────────────────────────────────────────
# This script wipes convo-recall state to test from a clean slate. Refuse to
# run unless we're inside a sandbox: either a Docker container (has /.dockerenv)
# or an explicit CONVO_RECALL_SANDBOX=1 override. Without this guard, a typo
# like `bash tests/sandbox-test.sh` on the live host destroys real state.
if [[ ! -f /.dockerenv && "${CONVO_RECALL_SANDBOX:-}" != "1" ]]; then
    cat <<'WARN' >&2

🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥
☠️  DESTRUCTIVE SCRIPT — NOT INSIDE A SANDBOX  ☠️
🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥

This script wipes convo-recall state to test from a clean slate. It WILL:
  📁 Delete the conversation DB at \$CONVO_RECALL_DB
  ⚙️  Remove launchd / systemd / cron watchers
  🪝 Strip convo-recall hook entries from claude / codex / gemini settings
  💥 Kill any running embed sidecar

If you are NOT inside the claude-sandbox Docker container, you WILL lose
state on THIS host. There is NO undo.

To run anyway (e.g. you have a one-off VM you don't care about):
    CONVO_RECALL_SANDBOX=1 bash $0
WARN
    if [[ -t 0 ]]; then
        printf '\n⚠️  Type "YES" (uppercase) to proceed anyway, anything else to abort: ' >&2
        read -r _sandbox_guard_response
        if [[ "$_sandbox_guard_response" != "YES" ]]; then
            echo "" >&2
            echo "✅ Aborted. Nothing changed." >&2
            exit 0
        fi
    else
        echo "" >&2
        echo "✗ Refusing in non-interactive shell. Set CONVO_RECALL_SANDBOX=1 to override." >&2
        exit 1
    fi
fi


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
