#!/usr/bin/env bash
# F-3: hook auto-search — when given a substantive prompt, the hook runs
# `recall search "$prompt" --json` and injects the top hits as
# additionalContext under a "## Prior context from convo-recall" heading.
#
# Tests use a temporary DB with one known row so search results are
# deterministic regardless of the user's real index.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOK="${REPO_ROOT}/src/convo_recall/hooks/conversation-memory.sh"
[[ -x "${HOOK}" ]] || chmod +x "${HOOK}"

# Resolve `recall` from a venv if one exists; else fall back to PATH.
if [[ -x "${REPO_ROOT}/.venv/bin/recall" ]]; then
    RECALL_DIR="${REPO_ROOT}/.venv/bin"
elif command -v recall >/dev/null 2>&1; then
    RECALL_DIR="$(dirname "$(command -v recall)")"
else
    echo "SKIP: recall not on PATH (editable-install required for hook auto-search test)"
    exit 0
fi

# Make a temp DB with one known message; point CONVO_RECALL_DB at it.
TEST_DB="$(mktemp -t cr-hook-test.XXXXXX.db)"
trap 'rm -f "${TEST_DB}"' EXIT

export PATH="${RECALL_DIR}:${PATH}"
export CONVO_RECALL_DB="${TEST_DB}"

# Seed: ingest a fake JSONL via the recall library so the test is
# deterministic.
SESSION_DIR="$(mktemp -d -t cr-hook-test.XXXXXX)"
trap 'rm -rf "${SESSION_DIR}"; rm -f "${TEST_DB}"' EXIT

cat > "${SESSION_DIR}/seed.jsonl" <<'EOF'
{"uuid":"u1","type":"user","timestamp":"2026-04-30T00:00:00Z","message":{"role":"user","content":"sprint plan for the moodmix product launch"}}
EOF

python3 -c "
import os, sys
os.environ['CONVO_RECALL_DB'] = '${TEST_DB}'
sys.path.insert(0, '${REPO_ROOT}/src')
from pathlib import Path
import convo_recall.ingest as ingest
ingest.PROJECTS_DIR = Path('${SESSION_DIR}').parent
con = ingest.open_db()
ingest.ingest_file(con, Path('${SESSION_DIR}/seed.jsonl'), do_embed=False)
print('seeded', con.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 'rows')
" || { echo "FAILED: seed step"; exit 1; }

# ── Test 1: substantive prompt → search runs, results injected ───────────────

echo "── F-3: hook auto-search tests ──"

substantive_payload='{"hook_event_name":"UserPromptSubmit","prompt":"sprint plan moodmix product","cwd":"/x","session_id":"s"}'
out=$(echo "${substantive_payload}" | bash "${HOOK}")
ctx=$(printf '%s' "${out}" | python3 -c 'import json,sys;print(json.load(sys.stdin)["hookSpecificOutput"]["additionalContext"])')

if ! echo "${ctx}" | grep -q "Prior context from convo-recall"; then
    echo "FAILED: substantive prompt — missing 'Prior context' heading"
    echo "got: ${ctx:0:300}..."
    exit 1
fi
if ! echo "${ctx}" | grep -q "moodmix"; then
    echo "FAILED: substantive prompt — search result snippet (containing 'moodmix') missing"
    echo "got: ${ctx:0:300}..."
    exit 1
fi
if ! echo "${ctx}" | grep -q "convo-recall"; then
    echo "FAILED: static reminder still missing after the prior context"
    exit 1
fi
echo "  ✓ substantive prompt — prior context + reminder both present"

# ── Test 2: interjection — no search runs, no prior context ──────────────────

interject_payload='{"hook_event_name":"UserPromptSubmit","prompt":"yes"}'
out=$(echo "${interject_payload}" | bash "${HOOK}")
ctx=$(printf '%s' "${out}" | python3 -c 'import json,sys;print(json.load(sys.stdin)["hookSpecificOutput"]["additionalContext"])')

if [[ -n "${ctx}" ]]; then
    echo "FAILED: interjection — should be empty, got: ${ctx:0:80}..."
    exit 1
fi
echo "  ✓ interjection — no search, no context"

# ── Test 3: opt-out env disables auto-search even for substantive prompts ────

out=$(CONVO_RECALL_HOOK_AUTO_SEARCH=off bash "${HOOK}" <<<"${substantive_payload}")
ctx=$(printf '%s' "${out}" | python3 -c 'import json,sys;print(json.load(sys.stdin)["hookSpecificOutput"]["additionalContext"])')
if [[ -n "${ctx}" ]]; then
    echo "FAILED: opt-out — additionalContext should be empty, got: ${ctx:0:80}..."
    exit 1
fi
echo "  ✓ opt-out env disables auto-search"

# ── Test 4: query with zero hits — fallback to reminder only ─────────────────

zero_hit_payload='{"hook_event_name":"UserPromptSubmit","prompt":"zorblax_definitely_no_match_anywhere"}'
out=$(echo "${zero_hit_payload}" | bash "${HOOK}")
ctx=$(printf '%s' "${out}" | python3 -c 'import json,sys;print(json.load(sys.stdin)["hookSpecificOutput"]["additionalContext"])')

if echo "${ctx}" | grep -q "Prior context from convo-recall"; then
    echo "FAILED: zero-hit query — should NOT print 'Prior context' header when no results"
    exit 1
fi
if ! echo "${ctx}" | grep -q "convo-recall"; then
    echo "FAILED: zero-hit query — static reminder missing"
    exit 1
fi
echo "  ✓ zero-hit query — falls back to reminder cleanly"

echo ""
echo "All F-3 hook auto-search tests passed."
