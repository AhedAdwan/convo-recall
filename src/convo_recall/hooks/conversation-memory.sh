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
import os
import re
import sys

payload_raw = sys.argv[1] if len(sys.argv) > 1 else ""
_bash_cwd = sys.argv[2] if len(sys.argv) > 2 else ""

# Determine which event we're answering.
event = "UserPromptSubmit"
try:
    data = json.loads(payload_raw) if payload_raw else {}
except Exception:
    data = {}

# JSON-supplied cwd is more reliable than the bash-side pwd — the hook
# might be invoked with a different working dir than the agent's actual
# cwd. Fall back to bash pwd only when the payload doesn't carry it.
cwd = data.get("cwd") or _bash_cwd
name = data.get("hook_event_name")
if name:
    event = name
elif "prompt" in data and "session_id" not in data:
    # Gemini's BeforeAgent payload has `prompt` but no session_id.
    event = "BeforeAgent"

# ── Throttling — skip the reminder for trivial conversational turns ──────────
#
# The reminder fires on every user turn. For one-word interjections ("yes",
# "ok", "hmm") it's pure context noise — there's nothing to search for. Skip
# in those cases so the model isn't paying for the reminder on every "yes".
#
# Opt-out: set CONVO_RECALL_HOOK_AUTO_SEARCH=off in the env to disable
# entirely.
prompt = data.get("prompt") or data.get("user_prompt") or ""

_INTERJECTION_RE = re.compile(
    r'^\s*(yes|no|ok|okay|sure|yep|nope|hmm+|continue|go|stop|wait|hi|hello|y|n|\.|!|\?)\.?\s*$',
    re.IGNORECASE,
)


def _should_skip(prompt: str) -> bool:
    if os.environ.get("CONVO_RECALL_HOOK_AUTO_SEARCH", "").lower() == "off":
        return True
    if not prompt or len(prompt.strip()) < 12:
        return True
    if _INTERJECTION_RE.match(prompt):
        return True
    return False


if _should_skip(prompt):
    # Empty additionalContext = valid hook response, zero token bloat.
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": "",
        }
    }))
    sys.exit(0)

# ── Auto-search — actually run the search and inject results as context ──────
#
# Project identity is delegated entirely to convo_recall via the --cwd flag.
# The hook no longer derives a slug locally; instead it passes the agent's
# raw cwd and lets `recall search --cwd` resolve the display_name → project_id
# (single source of truth — eliminates the old `/Projects/` substring hack).
#
# Hard-cap latency at ~3s (subprocess timeout) so a slow embedding sidecar
# doesn't stall every keystroke. On any failure, fall back to the static
# reminder — never block the user.
import shutil
import subprocess

_RECALL_SEARCH_TIMEOUT_S = 3.0
_RECALL_SEARCH_LIMIT = 3
_SNIPPET_CHAR_CAP = 200

prior_block = ""
results: list = []
recall_bin = shutil.which("recall")
if recall_bin:
    args = [recall_bin, "search", prompt, "-n", str(_RECALL_SEARCH_LIMIT),
            "--context", "0", "--json", "--cwd", cwd]
    try:
        res = subprocess.run(
            args, capture_output=True, text=True,
            timeout=_RECALL_SEARCH_TIMEOUT_S,
        )
        if res.returncode == 0 and res.stdout.strip():
            payload = json.loads(res.stdout)
            results = payload.get("results", [])
            # If project-scoped came back empty, retry once with --all-projects.
            if not results:
                fallback_args = [recall_bin, "search", prompt,
                                 "-n", str(_RECALL_SEARCH_LIMIT),
                                 "--context", "0", "--json", "--all-projects"]
                res2 = subprocess.run(
                    fallback_args, capture_output=True, text=True,
                    timeout=_RECALL_SEARCH_TIMEOUT_S,
                )
                if res2.returncode == 0 and res2.stdout.strip():
                    payload = json.loads(res2.stdout)
                    results = payload.get("results", [])
            if results:
                lines = ["## Prior context from convo-recall\n"]
                for r in results[:_RECALL_SEARCH_LIMIT]:
                    snip = (r.get("snippet") or "").replace("\n", " ")
                    if len(snip) > _SNIPPET_CHAR_CAP:
                        snip = snip[:_SNIPPET_CHAR_CAP] + "…"
                    proj = r.get("display_name") or r.get("project_slug") or ""
                    role = r.get("role", "")
                    ts = (r.get("timestamp") or "")[:10]
                    agent_tag = r.get("agent", "")
                    lines.append(
                        f"- [{proj}] [{agent_tag}/{role}] {ts}: {snip}"
                    )
                prior_block = "\n".join(lines) + "\n\n"
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        # Fall through to static reminder. Don't fail the hook over a
        # slow / missing recall binary.
        prior_block = ""

# Derive the project hint line from the resolved display_name in the search
# result (if any). Falls back silently when no results came back.
project_line = ""
if results:
    display = results[0].get("display_name") or results[0].get("project_slug")
    if display:
        project_line = (
            f'Search current project: recall search "<query>" '
            f'--project {display}\n'
        )

# ── Custom instructions — global + per-project ad-hoc guidance ───────────────
#
# Two optional files. If present, their contents are prepended to the
# additionalContext (before prior-context + static reminder), so users can
# add per-machine and per-repo guidance without editing the shipped hook.
#
#   1. Global:        $XDG_CONFIG_HOME/convo-recall/instructions.md
#                     (default ~/.config/convo-recall/instructions.md)
#   2. Project-local: <cwd>/.recall-instructions.md
#
# Order: global → project-local → prior-context → static reminder.
# Each file is capped at 2 KB to prevent runaway context bloat from a
# hand-edited file; excess is truncated with a `…` marker.

_INSTRUCTIONS_BYTE_CAP = 2048


def _read_instructions(path):
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, PermissionError, OSError):
        return ""
    if not text.strip():
        return ""
    if len(text.encode("utf-8")) > _INSTRUCTIONS_BYTE_CAP:
        text = text[: _INSTRUCTIONS_BYTE_CAP] + "\n…[truncated]"
    return text.rstrip() + "\n\n"


import pathlib
xdg_config = os.environ.get("XDG_CONFIG_HOME") or str(pathlib.Path.home() / ".config")
global_instr = _read_instructions(pathlib.Path(xdg_config) / "convo-recall" / "instructions.md")

project_instr = ""
if cwd:
    project_instr = _read_instructions(pathlib.Path(cwd) / ".recall-instructions.md")

context_text = (
    f"{global_instr}"
    f"{project_instr}"
    f"{prior_block}"
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
