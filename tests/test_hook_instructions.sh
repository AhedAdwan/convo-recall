#!/usr/bin/env bash
# F-8: hook reads optional instructions files and prepends their content
# to additionalContext (before the prior-context block and static reminder).
#
# Files:
#   - global: $XDG_CONFIG_HOME/convo-recall/instructions.md
#   - per-project: <cwd>/.recall-instructions.md
#
# Both optional. If both exist: global first, then per-project, then the
# rest of the hook's output.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOK="${REPO_ROOT}/src/convo_recall/hooks/conversation-memory.sh"
[[ -x "${HOOK}" ]] || chmod +x "${HOOK}"

# Use an isolated XDG_CONFIG_HOME so we don't depend on the user's real one.
TEST_XDG="$(mktemp -d -t cr-instr-test.XXXXXX)"
TEST_CWD="$(mktemp -d -t cr-instr-cwd.XXXXXX)"
trap 'rm -rf "${TEST_XDG}" "${TEST_CWD}"' EXIT

substantive_payload() {
    local cwd="$1"
    printf '{"hook_event_name":"UserPromptSubmit","prompt":"How does the cron scheduler work?","cwd":%s,"session_id":"s"}' \
        "$(python3 -c 'import json,sys;print(json.dumps(sys.argv[1]))' "${cwd}")"
}

extract_ctx() {
    python3 -c 'import json,sys;print(json.load(sys.stdin)["hookSpecificOutput"]["additionalContext"])'
}

echo "── F-8: hook custom-instructions tests ──"

# ── Test 1: no instruction files → behavior unchanged ────────────────────────
out=$(env -u CONVO_RECALL_HOOK_AUTO_SEARCH XDG_CONFIG_HOME="${TEST_XDG}" \
      bash "${HOOK}" <<<"$(substantive_payload "${TEST_CWD}")")
ctx=$(printf '%s' "${out}" | extract_ctx)
if echo "${ctx}" | grep -q "GLOBAL_MARKER"; then
    echo "FAILED: empty TEST_XDG should not surface any custom instructions"
    exit 1
fi
if echo "${ctx}" | grep -q "PROJECT_MARKER"; then
    echo "FAILED: empty TEST_CWD should not surface any custom instructions"
    exit 1
fi
echo "  ✓ no instructions files — static reminder only"

# ── Test 2: global instructions file → its content prepended ────────────────
mkdir -p "${TEST_XDG}/convo-recall"
echo "GLOBAL_MARKER: always check stats before installing." > "${TEST_XDG}/convo-recall/instructions.md"

out=$(env -u CONVO_RECALL_HOOK_AUTO_SEARCH XDG_CONFIG_HOME="${TEST_XDG}" \
      bash "${HOOK}" <<<"$(substantive_payload "${TEST_CWD}")")
ctx=$(printf '%s' "${out}" | extract_ctx)
if ! echo "${ctx}" | grep -q "GLOBAL_MARKER"; then
    echo "FAILED: global instructions content missing from additionalContext"
    echo "got: ${ctx:0:300}..."
    exit 1
fi
echo "  ✓ global instructions surfaced"

# ── Test 3: project-local file → also prepended, AFTER global ────────────────
echo "PROJECT_MARKER: this repo prefers tabs over spaces." > "${TEST_CWD}/.recall-instructions.md"

out=$(env -u CONVO_RECALL_HOOK_AUTO_SEARCH XDG_CONFIG_HOME="${TEST_XDG}" \
      bash "${HOOK}" <<<"$(substantive_payload "${TEST_CWD}")")
ctx=$(printf '%s' "${out}" | extract_ctx)
if ! echo "${ctx}" | grep -q "GLOBAL_MARKER"; then
    echo "FAILED: global instructions missing when both files present"
    exit 1
fi
if ! echo "${ctx}" | grep -q "PROJECT_MARKER"; then
    echo "FAILED: per-project instructions missing"
    exit 1
fi
# Order check: global must appear BEFORE project-local in the output.
g_pos=$(echo "${ctx}" | grep -bo "GLOBAL_MARKER" | head -1 | cut -d: -f1)
p_pos=$(echo "${ctx}" | grep -bo "PROJECT_MARKER" | head -1 | cut -d: -f1)
if [[ -z "${g_pos}" || -z "${p_pos}" ]] || (( g_pos >= p_pos )); then
    echo "FAILED: global should appear before project-local; g=${g_pos} p=${p_pos}"
    exit 1
fi
echo "  ✓ both files present, global before project-local"

# ── Test 4: 2 KB cap — oversized file truncates ──────────────────────────────
python3 -c "open('${TEST_XDG}/convo-recall/instructions.md','w').write('A'*3000)"
out=$(env -u CONVO_RECALL_HOOK_AUTO_SEARCH XDG_CONFIG_HOME="${TEST_XDG}" \
      bash "${HOOK}" <<<"$(substantive_payload "${TEST_CWD}")")
ctx=$(printf '%s' "${out}" | extract_ctx)
if ! echo "${ctx}" | grep -q "truncated"; then
    echo "FAILED: 3KB file should be truncated with a marker"
    exit 1
fi
echo "  ✓ oversized file truncates at 2 KB cap"

# ── Test 5: throttled prompt skips instructions too (consistency) ────────────
echo "GLOBAL_MARKER: always check stats first." > "${TEST_XDG}/convo-recall/instructions.md"
trivial_payload='{"hook_event_name":"UserPromptSubmit","prompt":"yes"}'
out=$(env -u CONVO_RECALL_HOOK_AUTO_SEARCH XDG_CONFIG_HOME="${TEST_XDG}" \
      bash "${HOOK}" <<<"${trivial_payload}")
ctx=$(printf '%s' "${out}" | extract_ctx)
if [[ -n "${ctx}" ]]; then
    echo "FAILED: trivial 'yes' should still skip the whole reminder block"
    echo "got: ${ctx:0:200}..."
    exit 1
fi
echo "  ✓ throttle still applies — instructions don't bypass it"

echo ""
echo "All F-8 instructions-file tests passed."
