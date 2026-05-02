#!/usr/bin/env bash
# convo-recall response-completion ingest hook — works for all three CLIs.
#
# Fires on:
#   - Claude Code  →  Stop          (~/.claude/settings.json)
#   - Codex CLI    →  Stop          (~/.codex/hooks.json) — session-end only
#   - Gemini CLI   →  AfterAgent    (~/.gemini/settings.json)
#
# When an agent finishes a turn, we spawn `recall ingest` detached so the
# session JSONL is indexed within ~50ms — bypasses the systemd `.path`
# unit's non-recursive limitation on Linux/Docker.
#
# Lock-file dedup at $XDG_RUNTIME_DIR/convo-recall/ingest.lock (5s window)
# prevents stampedes on chatty turns. Always exits 0 so the agent is
# never blocked.
#
# Output contract: empty stdout + exit 0. Stop / AfterAgent / SessionEnd
# hooks do NOT accept the `hookSpecificOutput` shape that UserPromptSubmit
# uses — Claude Code rejects it with "Hook JSON output validation failed".
# The only universal "no-op success" across all three CLIs is silent exit.
#
# Opt-out: CONVO_RECALL_INGEST_HOOK=off

set -u

payload=""
if [ ! -t 0 ]; then
    payload=$(cat)
fi

if [ -n "${CONVO_RECALL_HOOK_LOG:-}" ]; then
    {
        printf '[%s] ingest_payload: ' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        printf '%s\n' "$payload"
    } >> "$CONVO_RECALL_HOOK_LOG"
fi

# Opt-out: bail before doing any work.
if [ "${CONVO_RECALL_INGEST_HOOK:-}" = "off" ]; then
    exit 0
fi

LOCK_DIR="${XDG_RUNTIME_DIR:-/tmp}/convo-recall"
LOCK="$LOCK_DIR/ingest.lock"
mkdir -p "$LOCK_DIR" 2>/dev/null || true

# Lock-file dedup: skip if last ingest fired within 5 seconds.
if [ -f "$LOCK" ]; then
    now=$(date +%s)
    lock_mtime=$(stat -c %Y "$LOCK" 2>/dev/null || stat -f %m "$LOCK" 2>/dev/null || echo 0)
    age=$(( now - lock_mtime ))
    if [ "$age" -lt 5 ]; then
        exit 0
    fi
fi

touch "$LOCK" 2>/dev/null || true

# Locate recall. PATH-based lookup is the happy path, but:
#   - Some agent CLIs invoke hooks under a minimal env (no shell rc, no
#     pipx ~/.local/bin in PATH) — `command -v` returns nothing
#   - Container deployments (docker exec without -l) inherit a PATH that
#     omits user bin dirs entirely
# Fall back to the canonical pipx and /usr/local install paths so the
# hook works regardless of how the parent agent process was launched.
recall_bin=$(command -v recall 2>/dev/null || true)
if [ -z "$recall_bin" ]; then
    for candidate in \
        "${HOME}/.local/bin/recall" \
        "/root/.local/bin/recall" \
        "/usr/local/bin/recall" \
        "/opt/homebrew/bin/recall"; do
        if [ -x "$candidate" ]; then
            recall_bin="$candidate"
            break
        fi
    done
fi

if [ -n "$recall_bin" ]; then
    ( "$recall_bin" ingest > /dev/null 2>&1 < /dev/null ) & disown 2>/dev/null || true
fi

exit 0
