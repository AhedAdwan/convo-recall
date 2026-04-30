#!/usr/bin/env bash
# F-6: hook throttling — skip the static reminder on conversational
# interjections so we don't bloat context with a reminder for "yes" / "ok".
#
# Each case: pipe a userPrompt JSON into conversation-memory.sh, capture
# stdout, and inspect the additionalContext field.

set -euo pipefail

HOOK="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/src/convo_recall/hooks/conversation-memory.sh"
[[ -f "${HOOK}" ]] || { echo "FAILED: hook not found at ${HOOK}"; exit 1; }
[[ -x "${HOOK}" ]] || chmod +x "${HOOK}"

assert_skipped() {
    local name="$1"
    local prompt="$2"
    local extra_env="${3:-}"
    local payload
    payload=$(printf '{"hook_event_name":"UserPromptSubmit","prompt":%s}' "$(python3 -c 'import json,sys;print(json.dumps(sys.argv[1]))' "${prompt}")")
    local out
    out=$(env ${extra_env} bash "${HOOK}" <<<"${payload}")
    local ctx
    ctx=$(printf '%s' "${out}" | python3 -c 'import json,sys;print(json.load(sys.stdin)["hookSpecificOutput"]["additionalContext"])')
    if [[ -n "${ctx}" ]]; then
        echo "FAILED: ${name} — expected empty additionalContext, got: ${ctx:0:80}..."
        exit 1
    fi
    echo "  ✓ ${name}"
}

assert_emitted() {
    local name="$1"
    local prompt="$2"
    local payload
    payload=$(printf '{"hook_event_name":"UserPromptSubmit","prompt":%s}' "$(python3 -c 'import json,sys;print(json.dumps(sys.argv[1]))' "${prompt}")")
    local out
    out=$(bash "${HOOK}" <<<"${payload}")
    local ctx
    ctx=$(printf '%s' "${out}" | python3 -c 'import json,sys;print(json.load(sys.stdin)["hookSpecificOutput"]["additionalContext"])')
    if [[ -z "${ctx}" ]]; then
        echo "FAILED: ${name} — expected non-empty additionalContext for substantive prompt"
        exit 1
    fi
    if ! echo "${ctx}" | grep -q "convo-recall"; then
        echo "FAILED: ${name} — additionalContext missing the reminder text"
        exit 1
    fi
    echo "  ✓ ${name}"
}

echo "── F-6: hook throttling tests ──"

# Skipped: short / interjection / opt-out
assert_skipped "yes"            "yes"
assert_skipped "ok"             "ok"
assert_skipped "okay"           "okay"
assert_skipped "go"             "go"
assert_skipped "hmm"            "hmm"
assert_skipped "?"              "?"
assert_skipped "short"          "abc"
assert_skipped "11-char limit"  "abcdefghijk"
assert_skipped "opt-out env"    "this is a perfectly valid substantive prompt for searching" "CONVO_RECALL_HOOK_AUTO_SEARCH=off"

# Emitted: substantive prompts
assert_emitted "12-char threshold" "abcdefghijkl"
assert_emitted "real question"     "How does the cron scheduler avoid duplicate @reboot lines?"
assert_emitted "long interjection" "yes, but only after we verify the slug normalization landed cleanly"

echo ""
echo "All F-6 throttle tests passed."
