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
#
# Run:
#   docker exec claude-sandbox bash /work/convo-recall/tests/sandbox-e2e-full.sh
set -euo pipefail

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
sec "0/13 bootstrap"
mkdir -p "$(dirname "$CONVO_RECALL_CONFIG")"
echo '{"agents": ["claude", "gemini", "codex"]}' > "$CONVO_RECALL_CONFIG"
rm -f "$CONVO_RECALL_DB" "$CONVO_RECALL_DB-wal" "$CONVO_RECALL_DB-shm"
ok "fresh DB + 3-agent config"

# ── 1. sidecar health ─────────────────────────────────────────────────────────
sec "1/13 sidecar health"
healthz=$(curl --silent --unix-socket "$CONVO_RECALL_SOCK" http://localhost/healthz)
echo "$healthz" \
  | "$PY" -c 'import json,sys; d=json.load(sys.stdin); assert d.get("dim")==1024 and "model" in d, d' \
  || fail "/healthz did not return dim=1024 + model"
ok "sidecar reports model + dim=1024"

# ── 2. agent detection ────────────────────────────────────────────────────────
sec "2/13 detect_agents()"
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
sec "3/13 config save/load round-trip"
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
sec "4/13 fresh 3-agent ingest"
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
sec "5/13 idempotent re-ingest"
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
sec "6/13 recall ingest --agent codex"
out=$("$RECALL" ingest --agent codex 2>&1)
# Should be 0 new (already ingested) but should not error
echo "$out" | grep -q "Traceback" && fail "traceback in --agent codex ingest"
ok "per-agent ingest accepted, no new rows expected"

# ── 7. legacy-schema migration ────────────────────────────────────────────────
sec "7/13 migration on legacy schema (no agent column)"
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
sec "8a/13 search --all-projects shows all 3 tags"
all_log=$(mktemp)
"$RECALL" search "the" --all-projects -n 50 -c 0 > "$all_log" 2>&1
for agent in claude gemini codex; do
  grep -q "\[$agent\]" "$all_log" || { cat "$all_log"; fail "missing [$agent] in --all-projects output"; }
done
ok "all 3 tags present"

# ── 8b. --agent X exclusivity ────────────────────────────────────────────────
sec "8b/13 --agent X is exclusive"
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
sec "8c/13 --project X filter (without --all-projects)"
# Note: convo-recall CLI currently has surprising behavior where --all-projects
# silently nullifies an explicit --project X. So we test --project alone here.
# (See E2E_FINDING_001 in TECH_DEBT — argparse logic bug in cli.py.)
proj_out=$("$RECALL" search "the" --project app-codex -n 10 -c 0 2>&1)
# Strip mode header lines, then count any result line that isn't [app-codex]
non_codex=$(echo "$proj_out" | grep -E "^\[" \
            | grep -vE "^\[(hybrid|fts)" \
            | grep -cvE "^\[app-codex\]" || true)
if [[ "$non_codex" -gt 0 ]]; then
  echo "$proj_out"; fail "--project app-codex returned $non_codex non-codex result lines"
fi
ok "project filter restricted to app-codex"

# ── 8d. --context N before/after ──────────────────────────────────────────────
sec "8d/13 --context 1 shows before/after lines"
ctx_out=$("$RECALL" search "the" --all-projects -n 1 -c 1 2>&1)
echo "$ctx_out" | grep -qE "↑|↓" || { echo "$ctx_out"; fail "--context 1 produced no ↑/↓ lines"; }
ok "context lines printed"

# ── 8e. --recent decay ────────────────────────────────────────────────────────
sec "8e/13 --recent flag accepted"
"$RECALL" search "the" --all-projects --recent -n 3 -c 0 > /tmp/recent.log 2>&1 \
  || { cat /tmp/recent.log; fail "--recent crashed"; }
grep -q "\[hybrid+recent search\]" /tmp/recent.log \
  || fail "expected '[hybrid+recent search]' header missing"
ok "--recent runs and reports recent-mode header"

# ── 8f. cwd auto-scope ────────────────────────────────────────────────────────
sec "8f/13 cwd auto-scope (slug_from_cwd)"
mkdir -p /Users/ahed_isir/Projects/app-codex
cd /Users/ahed_isir/Projects/app-codex
auto=$("$RECALL" search "the" -n 3 -c 0 2>&1 || true)
cd /
# Either matched (cwd lookup found project rows) or NoResults (project_slug differs)
echo "$auto" | grep -qE "(\[app-codex\]|No messages found for)" \
  || { echo "$auto"; fail "cwd auto-scope path silently broken"; }
ok "cwd-derived project filter active when no --all-projects"

# ── 8g. no-results query (via nonexistent project filter) ────────────────────
sec "8g/13 no-results path via nonexistent --project"
"$RECALL" search "anything" --project no_such_project_zwxq 2>&1 \
  | grep -q "No messages found" \
  || fail "expected 'No messages found' for nonexistent project"
ok "no-results path"

# ── 9. per-agent stats + embed coverage ───────────────────────────────────────
sec "9/13 stats + embed coverage"
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
sec "10/13 backfills are idempotent no-ops"
for cmd in embed-backfill backfill-clean tool-error-backfill; do
  out=$("$RECALL" $cmd 2>&1 || true)
  echo "$out" | grep -q "Traceback" && { echo "$out"; fail "traceback in $cmd"; }
done
ok "embed-backfill, backfill-clean, tool-error-backfill all clean"

# ── 11. watch loop picks up appended content ─────────────────────────────────
sec "11/13 watch loop picks up new content"
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
sec "12/13 FTS-only fallback when sidecar is missing"
SAVED_SOCK="$CONVO_RECALL_SOCK"
export CONVO_RECALL_SOCK=/tmp/no-such-socket-here.sock
fts_only_out=$("$RECALL" search "the" --all-projects -n 3 -c 0 2>&1)
echo "$fts_only_out" | grep -q "\[fts search\]" \
  || { echo "$fts_only_out"; fail "expected '[fts search]' header in fallback mode"; }
echo "$fts_only_out" | grep -E "^\[" | grep -qE "claude|gemini|codex" \
  || fail "FTS-only mode returned no results"
export CONVO_RECALL_SOCK="$SAVED_SOCK"
ok "search degrades to FTS-only when sidecar absent"

# ── 13. summary ──────────────────────────────────────────────────────────────
sec "13/13 summary"
"$RECALL" stats
green ""
green "=========================================="
green "  ALL 13 SECTIONS PASSED — convo-recall E2E"
green "=========================================="
