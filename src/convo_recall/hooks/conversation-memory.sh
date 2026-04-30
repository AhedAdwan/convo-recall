#!/usr/bin/env bash
# convo-recall pre-prompt hook — works for all three coding-agent CLIs.
#
# Fires on:
#   - Claude Code  →  UserPromptSubmit  (~/.claude/settings.json)
#   - Codex CLI    →  UserPromptSubmit  (~/.codex/hooks.json)
#   - Gemini CLI   →  BeforeAgent       (~/.gemini/settings.json)
#
# Each CLI sends a JSON payload on stdin that includes a `hook_event_name`
# (Codex/Claude) or implicitly maps to `BeforeAgent` (Gemini). We echo the
# right hookEventName back so all three accept the response.
#
# stdout MUST be a single JSON object (the hooks contract). Logs go to stderr.
# Exit 0 → success, JSON parsed; exit 2 → block; non-zero → warning, agent
# continues without our context.

set -u

# Read stdin if any (CLIs always send a JSON payload; defensive for tests).
payload=""
if [ ! -t 0 ]; then
    payload=$(cat)
fi

# Optional logging for e2e tests + debugging.
if [ -n "${CONVO_RECALL_HOOK_LOG:-}" ]; then
    {
        printf '[%s] event_payload: ' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        printf '%s\n' "$payload"
    } >> "$CONVO_RECALL_HOOK_LOG"
fi

raw_pwd=$(pwd)

# Compose the response in Python so JSON escaping (quotes, newlines) is
# correct regardless of the project name or path.
python3 - "$payload" "$raw_pwd" <<'PY'
import json
import sys

payload_raw = sys.argv[1] if len(sys.argv) > 1 else ""
cwd = sys.argv[2] if len(sys.argv) > 2 else ""

# Determine which event we're answering.
event = "UserPromptSubmit"
try:
    data = json.loads(payload_raw) if payload_raw else {}
except Exception:
    data = {}
name = data.get("hook_event_name")
if name:
    event = name
elif "prompt" in data and "session_id" not in data:
    # Gemini's BeforeAgent payload has `prompt` but no session_id.
    event = "BeforeAgent"

# Derive a slug for the current project, matching convo-recall's convention.
slug = ""
if "/Projects/" in cwd:
    tail = cwd.split("/Projects/", 1)[1]
    slug = tail.replace("/", "_").lower()

if slug:
    project_line = f'Search current project: recall search "<query>" --project {slug}\n'
else:
    project_line = ""

context_text = (
    "Before searching the web, guessing, or reinventing something already "
    "solved — this project's full conversation history is searchable via "
    "convo-recall.\n\n"
    f"{project_line}"
    'Search all projects: recall search "<query>" --all-projects\n\n'
    "IMPORTANT: If current-project results do not directly answer the "
    "question, escalate to --all-projects before concluding the topic was "
    "never discussed."
)

print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": event,
        "additionalContext": context_text,
    }
}))
PY
