#!/usr/bin/env bash
# Full-feature end-to-end test for convo-recall inside claude-sandbox.
#
# Asserts every major user-facing feature:
#   1. sidecar health
#   2. agent detection
#   3. config round-trip
#   4. fresh ingest (all 3 agents) — no tracebacks
#   5. idempotent re-ingest (0 new)
#   6. per-agent ingest (--agent X)
#   7. legacy-schema migration
#   8. search: --all-projects, --agent, --project, --context, --recent, no-results
#   9. cwd auto-scope
#  10. per-agent stats + embed coverage ≥ 95%
#  11. backfills are no-ops when fully populated
#  12. watch loop picks up appended content
#  13. FTS-only fallback when sidecar is gone
#  14. recall tail: reverse numbering, agent label, ago deltas, --json, --expand, --ascii
#  15. safety gates: --purge-data, backfill-*, sandbox-script guard all default-deny
#
# Run:
#   docker exec claude-sandbox bash /work/convo-recall/tests/sandbox-e2e-full.sh
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


: "${CONVO_RECALL_SOCK:=/tmp/convo-recall/embed.sock}"
: "${CONVO_RECALL_DB:=/root/.local/share/convo-recall/conversations.db}"
: "${CONVO_RECALL_CONFIG:=/root/.local/share/convo-recall/config.json}"
export CONVO_RECALL_SOCK CONVO_RECALL_DB CONVO_RECALL_CONFIG

# Repo root resolved from this script's location so the harness works
# regardless of where the repo is mounted (was hardcoded to /work/...
# in v1; current claude-sandbox uses /workspace). Override with
# REPO_ROOT or VENV env vars if needed.
REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
VENV="${VENV:-${REPO_ROOT}/.venv}"
RECALL="$VENV/bin/recall"
PY="$VENV/bin/python3"

red()   { printf "\033[31m%s\033[0m\n" "$*" >&2; }
green() { printf "\033[32m%s\033[0m\n" "$*"; }
fail()  { red "FAIL [$current]: $1"; exit 1; }
ok()    { green "  ✅ $current — $*"; }
sec()   { current="$1"; echo; echo "═══ $1 ═══"; }

# ── Shared fresh-state bootstrap ───────────────────────────────────────────────
sec "0/16 bootstrap"
mkdir -p "$(dirname "$CONVO_RECALL_CONFIG")"
echo '{"agents": ["claude", "gemini", "codex"]}' > "$CONVO_RECALL_CONFIG"
rm -f "$CONVO_RECALL_DB" "$CONVO_RECALL_DB-wal" "$CONVO_RECALL_DB-shm"
ok "fresh DB + 3-agent config"

# ── 1. sidecar health ─────────────────────────────────────────────────────────
sec "1/16 sidecar health"
healthz=$(curl --silent --unix-socket "$CONVO_RECALL_SOCK" http://localhost/healthz)
echo "$healthz" \
  | "$PY" -c 'import json,sys; d=json.load(sys.stdin); assert d.get("dim")==1024 and "model" in d, d' \
  || fail "/healthz did not return dim=1024 + model"
ok "sidecar reports model + dim=1024"

# ── 2. agent detection ────────────────────────────────────────────────────────
sec "2/16 detect_agents()"
detect_out=$("$PY" -c "
import sys; sys.path.insert(0, '${REPO_ROOT}/src')
import convo_recall.ingest as i
for a in i.detect_agents(): print(a['name'], a['file_count'])
")
echo "$detect_out"
echo "$detect_out" | grep -qE '^claude\s+[1-9]' || fail "claude not detected"
echo "$detect_out" | grep -qE '^gemini\s+[1-9]' || fail "gemini not detected"
echo "$detect_out" | grep -qE '^codex\s+[1-9]'  || fail "codex not detected"
ok "all three agents detected with file_count > 0"

# ── 3. config round-trip ──────────────────────────────────────────────────────
sec "3/16 config save/load round-trip"
"$PY" -c "
import os, sys
sys.path.insert(0, '${REPO_ROOT}/src')
os.environ['CONVO_RECALL_CONFIG']='/tmp/cr-roundtrip.json'
import convo_recall.ingest as i
i._CONFIG_PATH = __import__('pathlib').Path(os.environ['CONVO_RECALL_CONFIG'])
i.save_config({'agents': ['claude', 'gemini']})
got = i.load_config()
assert got == {'agents': ['claude', 'gemini']}, got
print('round-trip ok:', got)
" || fail "config round-trip"
rm -f /tmp/cr-roundtrip.json
ok "save_config + load_config preserve {'agents': [...]} exactly"

# ── 4. fresh ingest of all three agents ───────────────────────────────────────
sec "4/16 fresh 3-agent ingest"
ingest_log=$(mktemp)
"$RECALL" ingest > "$ingest_log" 2>&1 || { cat "$ingest_log"; fail "exit nonzero"; }
grep -q "Traceback (most recent call last)" "$ingest_log" \
  && { cat "$ingest_log"; fail "Python traceback in stderr"; }
total_inserted=$("$PY" -c "
import os, sys
sys.path.insert(0, '${REPO_ROOT}/src')
os.environ['CONVO_RECALL_DB']='$CONVO_RECALL_DB'
import convo_recall.ingest as i
con = i.open_db()
print(con.execute('SELECT COUNT(*) FROM messages').fetchone()[0])
")
[[ "$total_inserted" -gt 0 ]] || fail "0 messages inserted"
ok "$total_inserted messages from 3 agents, no tracebacks"

# ── 5. idempotent re-ingest ───────────────────────────────────────────────────
sec "5/16 idempotent re-ingest"
before=$total_inserted
"$RECALL" ingest > /dev/null 2>&1
after=$("$PY" -c "
import os, sys
sys.path.insert(0, '${REPO_ROOT}/src')
os.environ['CONVO_RECALL_DB']='$CONVO_RECALL_DB'
import convo_recall.ingest as i
con = i.open_db()
print(con.execute('SELECT COUNT(*) FROM messages').fetchone()[0])
")
[[ "$before" -eq "$after" ]] || fail "re-ingest grew DB ($before → $after) — mtime guard broken"
ok "second ingest added 0 rows ($after total unchanged)"

# ── 6. --agent X scoped ingest ────────────────────────────────────────────────
sec "6/16 recall ingest --agent codex"
out=$("$RECALL" ingest --agent codex 2>&1)
# Should be 0 new (already ingested) but should not error
echo "$out" | grep -q "Traceback" && fail "traceback in --agent codex ingest"
ok "per-agent ingest accepted, no new rows expected"

# ── 7. legacy-schema migration ────────────────────────────────────────────────
sec "7/16 migration on legacy schema (no agent column)"
LEGACY_DB=/tmp/cr-legacy-test.db
rm -f "$LEGACY_DB"*
"$PY" -c "
import apsw
con = apsw.Connection('$LEGACY_DB')
con.execute('PRAGMA journal_mode=WAL')
con.execute('''
  CREATE TABLE sessions (session_id TEXT PRIMARY KEY, project_slug TEXT NOT NULL,
                         title TEXT, first_seen TEXT NOT NULL, last_updated TEXT NOT NULL);
  CREATE TABLE messages (uuid TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                         project_slug TEXT NOT NULL, role TEXT NOT NULL,
                         content TEXT NOT NULL, timestamp TEXT, model TEXT);
  CREATE TABLE ingested_files (file_path TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                               project_slug TEXT NOT NULL, lines_ingested INTEGER NOT NULL DEFAULT 0,
                               last_modified REAL NOT NULL);
  CREATE VIRTUAL TABLE messages_fts USING fts5(content, session_id UNINDEXED,
       project_slug UNINDEXED, role UNINDEXED, content=\"messages\",
       content_rowid=\"rowid\", tokenize=\"porter unicode61\");
''')
con.execute('INSERT INTO messages(uuid, session_id, project_slug, role, content) VALUES (?, ?, ?, ?, ?)',
            ('legacy-1', 's1', 'old_proj', 'user', 'legacy content'))
"
CONVO_RECALL_DB=$LEGACY_DB "$PY" -c "
import os, sys
os.environ['CONVO_RECALL_DB']='$LEGACY_DB'
sys.path.insert(0, '${REPO_ROOT}/src')
import convo_recall.ingest as i
con = i.open_db()
agent = con.execute(\"SELECT agent FROM messages\").fetchone()[0]
fts_agent = con.execute(\"SELECT agent FROM messages_fts\").fetchone()[0]
assert agent == 'claude', agent
assert fts_agent == 'claude', fts_agent
print('migration backfilled correctly')
" || fail "legacy migration"
rm -f "$LEGACY_DB"*
ok "ALTER TABLE + FTS rebuild + agent backfill all worked"

# ── 8a. search --all-projects with all 3 tags ────────────────────────────────
sec "8a/16 search --all-projects shows all 3 tags"
all_log=$(mktemp)
"$RECALL" search "the" --all-projects -n 50 -c 0 > "$all_log" 2>&1
for agent in claude gemini codex; do
  grep -q "\[$agent\]" "$all_log" || { cat "$all_log"; fail "missing [$agent] in --all-projects output"; }
done
ok "all 3 tags present"

# ── 8b. --agent X exclusivity ────────────────────────────────────────────────
sec "8b/16 --agent X is exclusive"
for agent in claude gemini codex; do
  out=$(mktemp)
  "$RECALL" search "the" --agent "$agent" --all-projects -n 50 -c 0 > "$out" 2>&1 || true
  leaked=$(grep -E "^\[" "$out" \
            | grep -vE "^\[(hybrid|fts)" \
            | grep -vE "\[$agent\]" \
            | grep -cE "\[claude\]|\[gemini\]|\[codex\]" || true)
  if [[ "$leaked" -gt 0 ]]; then
    cat "$out"; fail "--agent $agent leaked $leaked result(s) with another agent's tag"
  fi
done
ok "no agent tag leakage with --agent X"

# ── 8c. --project filter ──────────────────────────────────────────────────────
sec "8c/16 --project X filter (display_name resolution post-v4)"
# Discover an existing display_name from the projects table — names are
# now derived from cwd basenames or marker walks, not from path-flatten slugs.
discovered_display=$("$PY" -c "
import os, sys; sys.path.insert(0, '${REPO_ROOT}/src')
os.environ['CONVO_RECALL_DB']='$CONVO_RECALL_DB'
import convo_recall.ingest as i
con = i.open_db(readonly=True)
row = con.execute('SELECT display_name FROM projects ORDER BY first_seen LIMIT 1').fetchone()
print(row['display_name'] if row else '')
" 2>/dev/null)
[[ -n "$discovered_display" ]] || fail "no projects rows present — ingest did not populate the table"
proj_out=$("$RECALL" search "the" --project "$discovered_display" -n 10 -c 0 2>&1)
# Result lines are now prefixed by [display_name]; verify the filter restricted output.
non_match=$(echo "$proj_out" | grep -E "^\[" \
            | grep -vE "^\[(hybrid|fts)" \
            | grep -cvE "^\[${discovered_display}\]" || true)
if [[ "$non_match" -gt 0 ]]; then
  echo "$proj_out"; fail "--project $discovered_display returned $non_match non-matching result lines"
fi
ok "project filter restricted to $discovered_display"

# Verify --json output carries project_id + display_name
json_out=$("$RECALL" search "the" --project "$discovered_display" --json -n 1 -c 0 2>&1 || true)
echo "$json_out" | "$PY" -c "
import json, sys
p = json.loads(sys.stdin.read())
results = p.get('results') or []
if results:
    r = results[0]
    assert 'project_id' in r and 'display_name' in r, f'missing project_id/display_name: {r.keys()}'
    assert r['display_name'] == '$discovered_display'
" || fail "--json result missing project_id/display_name"
ok "JSON output includes project_id + display_name"

# ── 8d. --context N before/after ──────────────────────────────────────────────
sec "8d/16 --context 1 shows before/after lines"
ctx_out=$("$RECALL" search "the" --all-projects -n 1 -c 1 2>&1)
echo "$ctx_out" | grep -qE "↑|↓" || { echo "$ctx_out"; fail "--context 1 produced no ↑/↓ lines"; }
ok "context lines printed"

# ── 8e. --recent decay ────────────────────────────────────────────────────────
sec "8e/16 --recent flag accepted"
"$RECALL" search "the" --all-projects --recent -n 3 -c 0 > /tmp/recent.log 2>&1 \
  || { cat /tmp/recent.log; fail "--recent crashed"; }
grep -q "\[hybrid+recent search\]" /tmp/recent.log \
  || fail "expected '[hybrid+recent search]' header missing"
ok "--recent runs and reports recent-mode header"

# ── 8f. cwd auto-scope (post-v4: --cwd flag + display_name resolution) ──────
sec "8f/16 cwd auto-scope via --cwd flag (post-v4)"
# Build a temp dir with a .git marker → display_name = basename of the dir.
auto_dir=$(mktemp -d -t cr-cwd-test.XXXXXX)
mkdir -p "$auto_dir/.git"
auto_display=$(basename "$auto_dir")

# Pass --cwd explicitly so the test is deterministic regardless of process cwd.
# Expected: search either returns no rows (display_name has no sessions) OR
# resolves to the right display_name and reports "No messages found for project='$auto_display'".
auto=$("$RECALL" search "the" --cwd "$auto_dir" -n 3 -c 0 2>&1 || true)
echo "$auto" | grep -qE "(No messages found for|\[$auto_display\])" \
  || { echo "$auto"; fail "--cwd flag did not resolve to display_name $auto_display"; }
rm -rf "$auto_dir"
ok "--cwd flag drives display_name resolution"

# ── 8g. no-results query (via nonexistent project filter) ────────────────────
sec "8g/16 no-results path via nonexistent --project"
"$RECALL" search "anything" --project no_such_project_zwxq 2>&1 \
  | grep -q "No messages found" \
  || fail "expected 'No messages found' for nonexistent project"
ok "no-results path"

# ── 9. per-agent stats + embed coverage ───────────────────────────────────────
sec "9/16 stats + embed coverage"
stats_log=$(mktemp)
"$RECALL" stats > "$stats_log"
embed_pct=$(grep "^Embedded" "$stats_log" | grep -oE "[0-9]+%" | head -1 | tr -d '%')
[[ "$embed_pct" -ge 95 ]] || { cat "$stats_log"; fail "embed coverage $embed_pct% < 95%"; }
for agent in claude gemini codex; do
  cnt=$(awk -v a="$agent" '$1==a{print $3}' "$stats_log" | tr -d ',')
  [[ -n "$cnt" && "$cnt" -gt 0 ]] || { cat "$stats_log"; fail "stats: 0 msgs for $agent"; }
done
ok "embed=$embed_pct%, all 3 agents counted"

# ── 10. backfill commands are no-ops when populated ───────────────────────────
sec "10/16 backfills are idempotent no-ops"
for cmd in embed-backfill backfill-clean tool-error-backfill; do
  out=$("$RECALL" $cmd 2>&1 || true)
  echo "$out" | grep -q "Traceback" && { echo "$out"; fail "traceback in $cmd"; }
done
ok "embed-backfill, backfill-clean, tool-error-backfill all clean"

# ── 11. watch loop picks up appended content ─────────────────────────────────
sec "11/16 watch loop picks up new content"
"$RECALL" watch --interval 3 --verbose > /tmp/watch.log 2>&1 &
WATCH_PID=$!
trap 'kill $WATCH_PID 2>/dev/null || true' EXIT
sleep 4
# Append a new line to an existing claude session file
target_file=$(find /root/.claude/projects -name '*.jsonl' | head -1)
[[ -n "$target_file" ]] || fail "no claude jsonl file to append to"
echo "{\"uuid\":\"e2e-watch-probe\",\"type\":\"user\",\"timestamp\":\"2030-01-01T00:00:00Z\",\"message\":{\"role\":\"user\",\"content\":\"e2e-watch-needle-tetraquark\"}}" >> "$target_file"
sleep 8
"$RECALL" search "tetraquark" --all-projects -n 1 -c 0 2>&1 | grep -q "tetraquark" \
  || { tail -10 /tmp/watch.log; fail "watch loop did not pick up appended line"; }
kill $WATCH_PID 2>/dev/null || true
trap - EXIT
ok "watch loop ingested appended content within 8s"

# ── 12. FTS-only fallback when sidecar gone ──────────────────────────────────
sec "12/16 FTS-only fallback when sidecar is missing"
SAVED_SOCK="$CONVO_RECALL_SOCK"
export CONVO_RECALL_SOCK=/tmp/no-such-socket-here.sock
fts_only_out=$("$RECALL" search "the" --all-projects -n 3 -c 0 2>&1)
echo "$fts_only_out" | grep -q "\[fts search\]" \
  || { echo "$fts_only_out"; fail "expected '[fts search]' header in fallback mode"; }
echo "$fts_only_out" | grep -E "^\[" | grep -qE "claude|gemini|codex" \
  || fail "FTS-only mode returned no results"
export CONVO_RECALL_SOCK="$SAVED_SOCK"
ok "search degrades to FTS-only when sidecar absent"

# ── 13. recall tail ───────────────────────────────────────────────────────────
sec "13/16 recall tail (reverse numbering, ago deltas, agent labels)"

# Pick a session_id known to have at least one user + one assistant message.
# The fresh ingest in section 4 imported real fixtures from all three agents,
# so any session with mixed roles will do.
tail_session=$("$PY" -c "
import os, sys
sys.path.insert(0, '${REPO_ROOT}/src')
os.environ['CONVO_RECALL_DB']='$CONVO_RECALL_DB'
import convo_recall.ingest as i
con = i.open_db()
row = con.execute('''
    SELECT m.session_id
    FROM messages m
    GROUP BY m.session_id
    HAVING COUNT(DISTINCT m.role) >= 2
    ORDER BY MAX(m.timestamp) DESC
    LIMIT 1
''').fetchone()
print(row[0] if row else '')
")
[[ -n "$tail_session" ]] || fail "no session has mixed user+assistant messages"

# 13a — default formatted output: header + at least one row, no traceback
tail_out=$("$RECALL" tail 5 --session "$tail_session" 2>&1) \
  || { echo "$tail_out"; fail "recall tail exited non-zero"; }
echo "$tail_out" | grep -q "Traceback" && { echo "$tail_out"; fail "traceback in tail output"; }
echo "$tail_out" | grep -q "^session " || { echo "$tail_out"; fail "missing session header line"; }
echo "$tail_out" | grep -qE "messages " || { echo "$tail_out"; fail "header missing 'messages' count"; }

# 13b — reverse numbering: #1 must appear, and at least one row prefix must use it
echo "$tail_out" | grep -qE "^#1 " || { echo "$tail_out"; fail "expected '#1 ' row (newest = #1)"; }

# 13c — agent label: 'assistant' must NOT appear as a speaker; the actual
# agent name (claude / codex / gemini) MUST.
echo "$tail_out" | grep -qE "(claude|codex|gemini)" \
  || { echo "$tail_out"; fail "no agent name in tail output"; }
# A speaker label of "assistant" would appear with surrounding whitespace
# and the bar; a substring match is enough to flag it.
echo "$tail_out" | grep -qE " assistant +[│|]" \
  && { echo "$tail_out"; fail "raw 'assistant' role label leaked into output"; }

# 13d — 'ago' delta string from current time
echo "$tail_out" | grep -qE "(now|[0-9]+(s|m|h|d|w) ago)" \
  || { echo "$tail_out"; fail "no 'X ago' / 'now' delta in tail output"; }

# 13e — JSON shape exposes role, content, agent, timestamp
tail_json=$("$RECALL" tail 3 --session "$tail_session" --json 2>&1) \
  || { echo "$tail_json"; fail "recall tail --json exited non-zero"; }
echo "$tail_json" | "$PY" -c "
import json, sys
data = json.loads(sys.stdin.read())
assert data['session_id'], 'missing session_id'
msgs = data['messages']
assert len(msgs) >= 1, 'no messages in JSON'
for m in msgs:
    for k in ('role', 'content', 'timestamp', 'agent'):
        assert k in m, f'JSON message missing {k}: {m}'
print('json-ok')
" | grep -q "^json-ok$" || fail "JSON shape missing required keys"

# 13f — --width forces truncation; --expand bypasses it.
# Force --width 50 so even short messages truncate (removes dependency on the
# longest message in any particular session). Then verify --expand 1 on a
# single-message tail produces output WITH NO truncation marker anywhere.
truncated_out=$("$RECALL" tail 5 --session "$tail_session" --width 50 2>&1)
echo "$truncated_out" | grep -q "more]" \
  || { echo "$truncated_out"; fail "expected '[+N more]' marker with --width 50"; }
expanded_out=$("$RECALL" tail 1 --session "$tail_session" --width 50 --expand 1 2>&1)
echo "$expanded_out" | grep -q "more]" \
  && { echo "$expanded_out"; fail "--expand 1 should bypass truncation for the only turn shown"; }
ok "tail truncates with --width 50; --expand 1 bypasses truncation"

# 13g — --ascii swaps Unicode glyphs for ASCII fallbacks
ascii_out=$("$RECALL" tail 3 --session "$tail_session" --ascii 2>&1)
echo "$ascii_out" | grep -q "│" \
  && { echo "$ascii_out"; fail "--ascii output still contains Unicode pipe"; }
echo "$ascii_out" | grep -q " | " \
  || { echo "$ascii_out"; fail "--ascii output missing ASCII pipe"; }

# 13h — --all-projects picks the latest session globally (no project filter)
all_out=$("$RECALL" tail 1 --all-projects 2>&1) \
  || { echo "$all_out"; fail "--all-projects exited non-zero"; }
echo "$all_out" | grep -q "^session " \
  || { echo "$all_out"; fail "--all-projects missing header"; }

ok "tail: header / reverse-# / agent label / ago delta / JSON / ascii / --all-projects"

# ── 14. safety gates: every destructive command default-denies ─────────────────
sec "14/16 safety gates default-deny without --confirm"

# 14a — `recall uninstall --purge-data` with no TTY and no --confirm MUST
#       leave the DB intact and print DRY-RUN.
db_size_before=$(stat -c%s "$CONVO_RECALL_DB" 2>/dev/null || stat -f%z "$CONVO_RECALL_DB")
purge_out=$("$RECALL" uninstall --purge-data </dev/null 2>&1) \
  || { echo "$purge_out"; fail "uninstall --purge-data exited non-zero"; }
echo "$purge_out" | grep -qiE "dry.?run" \
  || { echo "$purge_out"; fail "expected 'DRY-RUN' marker in --purge-data output"; }
[[ -f "$CONVO_RECALL_DB" ]] \
  || fail "DB was DELETED in dry-run! Safety gate broken."
db_size_after=$(stat -c%s "$CONVO_RECALL_DB" 2>/dev/null || stat -f%z "$CONVO_RECALL_DB")
[[ "$db_size_before" -eq "$db_size_after" ]] \
  || fail "DB size changed during dry-run (before=$db_size_before, after=$db_size_after)"

# Side-effect: --purge-data walks schedulers + hooks BEFORE the purge gate
# (existing design). Re-install so future re-runs of this script start clean.
"$RECALL" install -y > /dev/null 2>&1 || true
ok "14a uninstall --purge-data without --confirm did NOT delete the DB"

# 14b — backfill-clean default-deny
msg_count_before=$("$PY" -c "
import os, sys; sys.path.insert(0, '${REPO_ROOT}/src')
os.environ['CONVO_RECALL_DB']='$CONVO_RECALL_DB'
import convo_recall.ingest as i
con = i.open_db()
print(con.execute('SELECT COUNT(*) FROM messages').fetchone()[0])
")
"$RECALL" backfill-clean </dev/null > /dev/null 2>&1 \
  || fail "backfill-clean exited non-zero in dry-run"
msg_count_after=$("$PY" -c "
import os, sys; sys.path.insert(0, '${REPO_ROOT}/src')
os.environ['CONVO_RECALL_DB']='$CONVO_RECALL_DB'
import convo_recall.ingest as i
con = i.open_db()
print(con.execute('SELECT COUNT(*) FROM messages').fetchone()[0])
")
[[ "$msg_count_before" -eq "$msg_count_after" ]] \
  || fail "backfill-clean changed row count in dry-run ($msg_count_before → $msg_count_after)"
ok "14b backfill-clean without --confirm did NOT mutate row count"

# 14c — backfill-redact default-deny
"$RECALL" backfill-redact </dev/null > /dev/null 2>&1 \
  || fail "backfill-redact exited non-zero in dry-run"
ok "14c backfill-redact without --confirm exited cleanly"

# 14d — chunk-backfill default-deny
"$RECALL" chunk-backfill </dev/null > /dev/null 2>&1 \
  || fail "chunk-backfill exited non-zero in dry-run"
ok "14d chunk-backfill without --confirm exited cleanly"

# 14e — sandbox-script guard block must be present in every destructive script.
# (We can't directly test the refusal here because /.dockerenv exists in the
# sandbox; structural check ensures the guard isn't accidentally removed.)
guard_pattern='DESTRUCTIVE-SCRIPT GUARD'
for f in tests/sandbox-e2e-full.sh tests/sandbox-hooks-e2e.sh \
         tests/sandbox-linux-port-e2e.sh tests/sandbox-multi-agent.sh \
         tests/sandbox-test.sh; do
    grep -q "$guard_pattern" "${REPO_ROOT}/$f" \
      || fail "guard block missing from $f — destructive script unprotected"
done
ok "14e sandbox-script guard block present in all 5 destructive scripts"

# 14f — --confirm bypasses the prompt. If argparse plumbing for --confirm
# regresses, the command hangs on stdin read; the timeout catches that.
timeout 30 "$RECALL" chunk-backfill --confirm </dev/null > /dev/null 2>&1
rc=$?
[[ $rc -ne 124 ]] || fail "chunk-backfill --confirm hung — argparse plumbing for --confirm regressed"
[[ $rc -eq 0 ]]   || fail "chunk-backfill --confirm crashed (rc=$rc)"
ok "14f --confirm bypasses the prompt (no hang on stdin)"

# 14g — `recall forget --project X --confirm` requires EXACT display_name match.
# A non-existent display_name must exit non-zero and delete nothing.
forget_before=$("$PY" -c "
import os, sys; sys.path.insert(0, '${REPO_ROOT}/src')
os.environ['CONVO_RECALL_DB']='$CONVO_RECALL_DB'
import convo_recall.ingest as i
con = i.open_db()
print(con.execute('SELECT COUNT(*) FROM messages').fetchone()[0])
")
set +e
"$RECALL" forget --project nonexistent_project_xyz --confirm </dev/null > /dev/null 2>&1
rc=$?
set -e
[[ "$rc" -ne 0 ]] || fail "forget --project nonexistent --confirm should exit non-zero"
forget_after=$("$PY" -c "
import os, sys; sys.path.insert(0, '${REPO_ROOT}/src')
os.environ['CONVO_RECALL_DB']='$CONVO_RECALL_DB'
import convo_recall.ingest as i
con = i.open_db()
print(con.execute('SELECT COUNT(*) FROM messages').fetchone()[0])
")
[[ "$forget_before" -eq "$forget_after" ]] \
  || fail "forget --project nonexistent deleted rows ($forget_before → $forget_after)"
ok "14g forget --project nonexistent did NOT delete rows (exact-only enforced)"

# ── 15. projects table + project_id integrity (post-v4) ─────────────────────
sec "15/16 projects table + project_id integrity"
"$PY" -c "
import os, sys; sys.path.insert(0, '${REPO_ROOT}/src')
os.environ['CONVO_RECALL_DB']='$CONVO_RECALL_DB'
import convo_recall.ingest as i
con = i.open_db(readonly=True)
orphan = con.execute('SELECT COUNT(*) FROM messages m WHERE NOT EXISTS '
                     '(SELECT 1 FROM projects p WHERE p.project_id = m.project_id)'
).fetchone()[0]
assert orphan == 0, f'{orphan} orphan messages reference unknown project_ids'
" || fail "orphan messages.project_id without projects row"
ok "every messages.project_id has a projects-table row"

"$PY" -c "
import os, re, sys; sys.path.insert(0, '${REPO_ROOT}/src')
os.environ['CONVO_RECALL_DB']='$CONVO_RECALL_DB'
import convo_recall.ingest as i
con = i.open_db(readonly=True)
rows = con.execute('SELECT project_id FROM projects').fetchall()
bad = [r['project_id'] for r in rows
       if not (re.fullmatch(r'[0-9a-f]{12}', r['project_id'])
               or r['project_id'].startswith('gemini-hash:')
               or r['project_id'].startswith('legacy:')
               or r['project_id'] == 'codex_unknown')]
assert not bad, f'malformed project_id values: {bad[:5]}'
" || fail "malformed project_id in projects table"
ok "every projects.project_id is 12-hex or recognized synthetic prefix"

# ── 16. summary ──────────────────────────────────────────────────────────────
sec "16/16 summary"
"$RECALL" stats
green ""
green "=========================================="
green "  ALL 16 SECTIONS PASSED — convo-recall E2E"
green "=========================================="
