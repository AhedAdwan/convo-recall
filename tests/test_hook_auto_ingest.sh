#!/usr/bin/env bash
# Hook auto-ingest tests — fires `recall ingest` from the response-completion
# hook on each CLI's Stop / AfterAgent event. Lock-file dedup at
# $XDG_RUNTIME_DIR/convo-recall/ingest.lock with a 5s window.
#
# Tests use a temporary lock dir so the user's real ~/.local state is never
# touched. The hook is exercised purely by piping synthetic payloads — no
# Claude/Codex/Gemini runtime required.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOK="${REPO_ROOT}/src/convo_recall/hooks/conversation-ingest.sh"
[[ -x "${HOOK}" ]] || chmod +x "${HOOK}"

# Isolated lock dir for the whole test run.
TEST_RUNTIME_DIR="$(mktemp -d -t cr-ingest-hook.XXXXXX)"
trap 'rm -rf "${TEST_RUNTIME_DIR}"' EXIT
export XDG_RUNTIME_DIR="${TEST_RUNTIME_DIR}"
LOCK="${TEST_RUNTIME_DIR}/convo-recall/ingest.lock"

# Stub recall so spawned `recall ingest` never touches the user's real DB.
# We don't care whether ingest itself runs — only that the hook returns
# valid JSON and manages the lock correctly.
STUB_DIR="$(mktemp -d -t cr-stub.XXXXXX)"
trap 'rm -rf "${TEST_RUNTIME_DIR}" "${STUB_DIR}"' EXIT
cat > "${STUB_DIR}/recall" <<'EOF'
#!/usr/bin/env bash
# Test stub — no-op; real recall would scan + ingest here.
exit 0
EOF
chmod +x "${STUB_DIR}/recall"
export PATH="${STUB_DIR}:${PATH}"

echo "── hook auto-ingest tests ──"

# ── Helper: run hook with a payload, capture stdout (the JSON response) ─────
fire_hook() {
    local payload="$1"
    echo "$payload" | bash "${HOOK}"
}

# ── Test 1: substantive Claude Stop payload fires ingest ────────────────────
# Stop hook contract: empty stdout + exit 0. Anything else (including the
# `hookSpecificOutput` shape used by UserPromptSubmit) is rejected by Claude
# Code with "Hook JSON output validation failed".
rm -f "${LOCK}"
claude_payload='{"hook_event_name":"Stop","stop_hook_active":true}'
out=$(fire_hook "${claude_payload}")
rc=$?
if [[ $rc -ne 0 ]]; then
    echo "FAILED: Claude Stop — exit code $rc (must be 0)"
    exit 1
fi
if [[ -n "${out}" ]]; then
    echo "FAILED: Claude Stop — stdout must be empty (Stop schema rejects hookSpecificOutput); got: ${out}"
    exit 1
fi
if [[ ! -f "${LOCK}" ]]; then
    echo "FAILED: Claude Stop — lock file not created at ${LOCK}"
    exit 1
fi
echo "  ✓ Claude Stop payload → silent exit 0 + lock created"

# ── Test 2: substantive Gemini AfterAgent payload fires ingest ──────────────
rm -f "${LOCK}"
gemini_payload='{"hook_event_name":"AfterAgent","agent_name":"gemini-2.0"}'
out=$(fire_hook "${gemini_payload}")
rc=$?
if [[ $rc -ne 0 || -n "${out}" ]]; then
    echo "FAILED: Gemini AfterAgent — expected silent exit 0, got rc=$rc out=${out}"
    exit 1
fi
if [[ ! -f "${LOCK}" ]]; then
    echo "FAILED: Gemini AfterAgent — lock file not created"
    exit 1
fi
echo "  ✓ Gemini AfterAgent payload → silent exit 0 + lock created"

# ── Test 3: Codex Stop payload (same shape as Claude) ───────────────────────
rm -f "${LOCK}"
codex_payload='{"hook_event_name":"Stop"}'
out=$(fire_hook "${codex_payload}")
rc=$?
if [[ $rc -ne 0 || -n "${out}" ]]; then
    echo "FAILED: Codex Stop — expected silent exit 0, got rc=$rc out=${out}"
    exit 1
fi
echo "  ✓ Codex Stop payload → silent exit 0"

# ── Test 4: two Stops within 5s — second is dedup'd (lock mtime unchanged) ──
rm -f "${LOCK}"
fire_hook "${claude_payload}" > /dev/null
mtime_first=$(stat -f %m "${LOCK}" 2>/dev/null || stat -c %Y "${LOCK}")
sleep 1
fire_hook "${claude_payload}" > /dev/null
mtime_second=$(stat -f %m "${LOCK}" 2>/dev/null || stat -c %Y "${LOCK}")
if [[ "${mtime_first}" != "${mtime_second}" ]]; then
    echo "FAILED: dedup — lock mtime changed (${mtime_first} → ${mtime_second}); should have been no-op"
    exit 1
fi
echo "  ✓ second Stop within 5s → dedup'd (lock mtime unchanged)"

# ── Test 5: lock 6s old → second hook DOES fire ingest (touches lock) ───────
rm -f "${LOCK}"
fire_hook "${claude_payload}" > /dev/null
# Backdate the lock by 6 seconds.
touch -t "$(date -v-6S +%Y%m%d%H%M.%S 2>/dev/null || date -d '6 seconds ago' +%Y%m%d%H%M.%S)" "${LOCK}" 2>/dev/null || \
    python3 -c "import os, time; os.utime('${LOCK}', (time.time()-6, time.time()-6))"
mtime_before=$(stat -f %m "${LOCK}" 2>/dev/null || stat -c %Y "${LOCK}")
fire_hook "${claude_payload}" > /dev/null
mtime_after=$(stat -f %m "${LOCK}" 2>/dev/null || stat -c %Y "${LOCK}")
if [[ "${mtime_after}" -le "${mtime_before}" ]]; then
    echo "FAILED: lock expiry — after 6s lock should refresh (mtime ${mtime_before} → ${mtime_after})"
    exit 1
fi
echo "  ✓ 6s-old lock → second hook fires (mtime refreshed)"

# ── Test 6: opt-out env disables hook entirely ──────────────────────────────
rm -f "${LOCK}"
out=$(CONVO_RECALL_INGEST_HOOK=off bash "${HOOK}" <<<"${claude_payload}")
rc=$?
if [[ $rc -ne 0 || -n "${out}" ]]; then
    echo "FAILED: opt-out — expected silent exit 0, got rc=$rc out=${out}"
    exit 1
fi
if [[ -f "${LOCK}" ]]; then
    echo "FAILED: opt-out — lock should NOT be touched"
    exit 1
fi
echo "  ✓ CONVO_RECALL_INGEST_HOOK=off → silent exit, no ingest spawn, no lock touch"

# ── Test 7: hook source handles all three event names ───────────────────────
if ! grep -qE 'hook_event_name|AfterAgent|Stop' "${HOOK}"; then
    echo "FAILED: hook source missing event-name handling"
    exit 1
fi
echo "  ✓ hook source handles Stop / AfterAgent / hook_event_name"

echo ""
echo "All hook auto-ingest tests passed."
