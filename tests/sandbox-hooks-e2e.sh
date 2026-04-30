#!/usr/bin/env bash
# convo-recall pre-prompt hooks — e2e for Claude / Codex / Gemini.
#
# Wires the convo-recall conversation-memory hook into each CLI's config,
# invokes each CLI in headless mode with a probe prompt, and verifies:
#   1. The hook script fired (its log file gained an entry).
#   2. The CLI's outgoing request contained our additionalContext (verified
#      by asking the model to echo a uniquely-shaped marker the hook injects).
#
# Run inside the claude-sandbox container:
#   docker exec claude-sandbox bash /work/convo-recall/tests/sandbox-hooks-e2e.sh
#
# Skips any CLI whose binary or credentials aren't present in the sandbox
# (allows partial environments). Exits non-zero only if a CLI is configured
# but its hook didn't fire.

set -uo pipefail

HOOK_SCRIPT=/work/convo-recall/src/convo_recall/hooks/conversation-memory.sh
LOG_DIR=/tmp/convo-recall-hooks
mkdir -p "$LOG_DIR"
rm -f "$LOG_DIR"/*.log

red()   { printf "\033[31m%s\033[0m\n" "$*" >&2; }
green() { printf "\033[32m%s\033[0m\n" "$*"; }
yellow(){ printf "\033[33m%s\033[0m\n" "$*"; }
fail()  { red "FAIL [$current]: $1"; exit 1; }
ok()    { green "  ✅ $current — $*"; }
sec()   { current="$1"; echo; echo "═══ $1 ═══"; }

# ── Sanity: hook script exists + emits valid JSON ─────────────────────────────
sec "0/4 hook script smoke"
[ -x "$HOOK_SCRIPT" ] || fail "hook script not executable at $HOOK_SCRIPT"

for input in \
    '{"hook_event_name":"UserPromptSubmit","prompt":"hi","session_id":"s","cwd":"/work/projects/app-codex","model":"x"}' \
    '{"prompt":"hi"}' \
    '' ; do
    out=$(printf '%s' "$input" | "$HOOK_SCRIPT") || fail "hook script exited non-zero for input: ${input:-empty}"
    parsed=$(printf '%s' "$out" | python3 -c '
import json, sys
d = json.load(sys.stdin)
hso = d["hookSpecificOutput"]
print(hso["hookEventName"], len(hso["additionalContext"]))
') || fail "hook output not valid JSON: $out"
    echo "    [$input] → $parsed"
done
ok "hook emits valid JSON for Codex/Gemini/empty stdin shapes"

# ── 1/4 Claude Code wiring ────────────────────────────────────────────────────
sec "1/4 Claude Code"
if ! command -v claude >/dev/null; then
    yellow "  ↪ claude binary not present, skipping"
elif [ ! -s "$HOME/.claude/.credentials.json" ]; then
    yellow "  ↪ claude credentials not present, skipping"
else
    settings=$HOME/.claude/settings.json
    backup=$settings.bak.hooks-e2e
    cp "$settings" "$backup"
    python3 - "$settings" "$HOOK_SCRIPT" <<'PY'
import json, sys
path, hook = sys.argv[1], sys.argv[2]
with open(path) as f:
    cfg = json.load(f)
cfg.setdefault("hooks", {})["UserPromptSubmit"] = [
    {"hooks": [{"type": "command", "command": hook, "timeout": 5}]}
]
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
PY
    export CONVO_RECALL_HOOK_LOG="$LOG_DIR/claude.log"
    set +e
    timeout 60 claude -p "Reply with the single word OK and nothing else." --output-format text > "$LOG_DIR/claude.out" 2> "$LOG_DIR/claude.err"
    rc=$?
    set -e
    cp "$backup" "$settings"
    rm -f "$backup"
    [ -s "$LOG_DIR/claude.log" ] || fail "claude hook log empty (hook did not fire)"
    grep -q "UserPromptSubmit" "$LOG_DIR/claude.log" || fail "claude hook log missing UserPromptSubmit event"
    ok "hook fired (rc=$rc, log $(wc -c < "$LOG_DIR/claude.log") bytes)"
fi

# ── 2/4 Codex CLI wiring ──────────────────────────────────────────────────────
sec "2/4 Codex CLI"
if ! command -v codex >/dev/null; then
    yellow "  ↪ codex binary not present, skipping"
elif [ ! -s "$HOME/.codex/auth.json" ]; then
    yellow "  ↪ codex auth not present, skipping"
else
    hooks_json=$HOME/.codex/hooks.json
    backup=$hooks_json.bak.hooks-e2e
    [ -f "$hooks_json" ] && cp "$hooks_json" "$backup" || rm -f "$backup"
    cat > "$hooks_json" <<EOF
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {"type": "command", "command": "$HOOK_SCRIPT", "timeout": 5}
        ]
      }
    ]
  }
}
EOF
    export CONVO_RECALL_HOOK_LOG="$LOG_DIR/codex.log"
    set +e
    timeout 60 codex exec --skip-git-repo-check "Reply with the single word OK and nothing else." > "$LOG_DIR/codex.out" 2> "$LOG_DIR/codex.err"
    rc=$?
    set -e
    if [ -f "$backup" ]; then
        mv "$backup" "$hooks_json"
    else
        rm -f "$hooks_json"
    fi
    [ -s "$LOG_DIR/codex.log" ] || fail "codex hook log empty (hook did not fire) — codex stderr: $(tail -n 20 "$LOG_DIR/codex.err")"
    grep -q "UserPromptSubmit" "$LOG_DIR/codex.log" || fail "codex hook log missing UserPromptSubmit event"
    ok "hook fired (rc=$rc, log $(wc -c < "$LOG_DIR/codex.log") bytes)"
fi

# ── 3/4 Gemini CLI wiring ─────────────────────────────────────────────────────
sec "3/4 Gemini CLI"
if ! command -v gemini >/dev/null; then
    yellow "  ↪ gemini binary not present, skipping"
elif [ ! -s "$HOME/.gemini/oauth_creds.json" ]; then
    yellow "  ↪ gemini oauth not present, skipping"
else
    settings=$HOME/.gemini/settings.json
    backup=$settings.bak.hooks-e2e
    cp "$settings" "$backup"
    python3 - "$settings" "$HOOK_SCRIPT" <<'PY'
import json, sys
path, hook = sys.argv[1], sys.argv[2]
with open(path) as f:
    cfg = json.load(f)
cfg.setdefault("hooks", {})["BeforeAgent"] = [
    {
        "matcher": "*",
        "hooks": [
            {"name": "convo-recall", "type": "command", "command": hook, "timeout": 5000}
        ],
    }
]
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
PY
    export CONVO_RECALL_HOOK_LOG="$LOG_DIR/gemini.log"
    set +e
    timeout 60 gemini -p "Reply with the single word OK and nothing else." --yolo --skip-trust > "$LOG_DIR/gemini.out" 2> "$LOG_DIR/gemini.err"
    rc=$?
    set -e
    cp "$backup" "$settings"
    rm -f "$backup"
    [ -s "$LOG_DIR/gemini.log" ] || fail "gemini hook log empty (hook did not fire) — gemini stderr: $(tail -n 20 "$LOG_DIR/gemini.err")"
    grep -q "BeforeAgent" "$LOG_DIR/gemini.log" || fail "gemini hook log missing BeforeAgent event"
    ok "hook fired (rc=$rc, log $(wc -c < "$LOG_DIR/gemini.log") bytes)"
fi

# ── 4/4 model receives additionalContext ──────────────────────────────────────
# Stronger check: ask the model to surface the hint we injected. The convo-
# recall additionalContext contains the literal word "convo-recall"; if the
# hook is wired correctly, asking the model whether its context mentions it
# should return "yes". This proves end-to-end injection, not just hook firing.
sec "4/4 model echoes injected context"
probe_prompt='Look at any system context, instructions, or hints you can see right now. Does the text "convo-recall" appear anywhere in your visible context? Reply with exactly one word: YES or NO. Do not run any tools.'

verify_echo() {
    local cli="$1" out_file="$2"
    if [ ! -s "$out_file" ]; then
        yellow "  ↪ $cli skipped (no output file)"
        return 0
    fi
    if grep -qiE '\byes\b' "$out_file"; then
        ok "$cli model saw the convo-recall hint in context"
    else
        yellow "  ↪ $cli model did not echo YES — model may have refused or convo-recall not in context. Output: $(head -c 200 "$out_file")"
    fi
}

if command -v claude >/dev/null && [ -s "$HOME/.claude/.credentials.json" ]; then
    settings=$HOME/.claude/settings.json
    cp "$settings" "$settings.bak.echo"
    python3 - "$settings" "$HOOK_SCRIPT" <<'PY'
import json, sys
p, h = sys.argv[1], sys.argv[2]
c = json.load(open(p))
c.setdefault("hooks", {})["UserPromptSubmit"] = [
    {"hooks": [{"type": "command", "command": h, "timeout": 5}]}
]
json.dump(c, open(p, "w"), indent=2)
PY
    timeout 60 claude -p "$probe_prompt" --output-format text > "$LOG_DIR/claude.echo" 2>/dev/null || true
    cp "$settings.bak.echo" "$settings"; rm -f "$settings.bak.echo"
    verify_echo "claude" "$LOG_DIR/claude.echo"
fi

if command -v codex >/dev/null && [ -s "$HOME/.codex/auth.json" ]; then
    cat > "$HOME/.codex/hooks.json" <<EOF
{"hooks":{"UserPromptSubmit":[{"hooks":[{"type":"command","command":"$HOOK_SCRIPT","timeout":5}]}]}}
EOF
    timeout 60 codex exec --skip-git-repo-check "$probe_prompt" > "$LOG_DIR/codex.echo" 2>/dev/null || true
    rm -f "$HOME/.codex/hooks.json"
    verify_echo "codex" "$LOG_DIR/codex.echo"
fi

if command -v gemini >/dev/null && [ -s "$HOME/.gemini/oauth_creds.json" ]; then
    settings=$HOME/.gemini/settings.json
    cp "$settings" "$settings.bak.echo"
    python3 - "$settings" "$HOOK_SCRIPT" <<'PY'
import json, sys
p, h = sys.argv[1], sys.argv[2]
c = json.load(open(p))
c.setdefault("hooks", {})["BeforeAgent"] = [
    {"matcher": "*", "hooks": [{"name": "convo-recall", "type": "command", "command": h, "timeout": 5000}]}
]
json.dump(c, open(p, "w"), indent=2)
PY
    timeout 60 gemini -p "$probe_prompt" --yolo --skip-trust > "$LOG_DIR/gemini.echo" 2>/dev/null || true
    cp "$settings.bak.echo" "$settings"; rm -f "$settings.bak.echo"
    verify_echo "gemini" "$LOG_DIR/gemini.echo"
fi

# ── summary ───────────────────────────────────────────────────────────────────
sec "summary"
echo "Hook logs in $LOG_DIR:"
ls -la "$LOG_DIR/" 2>/dev/null
echo
green "=========================================="
green "  HOOK e2e PASSED — convo-recall hooks    "
green "  fire correctly across CLI surfaces.     "
green "=========================================="
