#!/usr/bin/env bash
# Sandbox-only end-to-end test for multi-agent ingest.
#
# Exercises the full pipeline: claude + gemini + codex ingest, agent-tagged
# search, agent filter, embedded coverage, search latency. Asserts each
# requirement and exits non-zero with a diagnostic on failure.
#
# Pre-conditions (must be set up by Phase 1):
#   - claude-sandbox container running with /work/convo-recall installed
#   - sidecar `recall serve` listening on $CONVO_RECALL_SOCK
#   - real session files exist under /root/.{claude,gemini,codex}
#
# Run:
#   docker exec claude-sandbox bash /work/convo-recall/tests/sandbox-multi-agent.sh
set -euo pipefail

: "${CONVO_RECALL_SOCK:=/tmp/convo-recall/embed.sock}"
: "${CONVO_RECALL_DB:=/root/.local/share/convo-recall/conversations.db}"
: "${CONVO_RECALL_CONFIG:=/root/.local/share/convo-recall/config.json}"
export CONVO_RECALL_SOCK CONVO_RECALL_DB CONVO_RECALL_CONFIG

VENV=/work/convo-recall/.venv
RECALL="$VENV/bin/recall"

red() { printf "\033[31m%s\033[0m\n" "$*" >&2; }
green() { printf "\033[32m%s\033[0m\n" "$*"; }
fail() { red "FAIL: $1"; exit 1; }

echo "=== sandbox multi-agent E2E ==="

# 0. Pre-flight — sidecar reachable
curl --silent --unix-socket "$CONVO_RECALL_SOCK" http://localhost/healthz \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get("dim")==1024, d' \
  || fail "sidecar /healthz did not return dim=1024"

# 1. Enable all three agents and wipe any prior DB so we start fresh
mkdir -p "$(dirname "$CONVO_RECALL_CONFIG")"
echo '{"agents": ["claude", "gemini", "codex"]}' > "$CONVO_RECALL_CONFIG"
rm -f "$CONVO_RECALL_DB" "$CONVO_RECALL_DB-wal" "$CONVO_RECALL_DB-shm"

# 2. Run ingest, capture output. No Python tracebacks allowed.
ingest_log=$(mktemp)
"$RECALL" ingest > "$ingest_log" 2>&1 || { cat "$ingest_log"; fail "recall ingest exited non-zero"; }
if grep -q "Traceback (most recent call last)" "$ingest_log"; then
  cat "$ingest_log"
  fail "recall ingest produced a Python traceback"
fi
echo "  ✅ ingest clean"

# 3. recall stats — messages > 0; per-agent counts all > 0
stats_log=$(mktemp)
"$RECALL" stats > "$stats_log"
msg_total=$(grep "^Messages" "$stats_log" | awk '{print $3}' | tr -d ',')
[[ "$msg_total" -gt 0 ]] || { cat "$stats_log"; fail "Messages == 0 after ingest"; }

for agent in claude gemini codex; do
  # Stats line shape: "  {agent}         : {count}" — fields are name, ":", count.
  count=$(awk -v a="$agent" '$1==a{print $3}' "$stats_log" | tr -d ',')
  [[ -n "$count" && "$count" -gt 0 ]] \
    || { cat "$stats_log"; fail "agent '$agent' has zero messages — check parser"; }
done
echo "  ✅ stats: $msg_total messages, all 3 agents have entries"

# 4. embedded ≥ 95%
embed_pct=$(grep "^Embedded" "$stats_log" | grep -oE "[0-9]+%" | head -1 | tr -d '%')
[[ "$embed_pct" -ge 95 ]] || fail "Embedded coverage $embed_pct% < 95%"
echo "  ✅ embedded $embed_pct%"

# 5. recall search --all-projects must show all three agent tags
all_log=$(mktemp)
"$RECALL" search "the" --all-projects -n 50 -c 0 > "$all_log" 2>&1
for agent in claude gemini codex; do
  grep -q "\[$agent\]" "$all_log" \
    || { cat "$all_log"; fail "search --all-projects missing tag [$agent]"; }
done
echo "  ✅ search --all-projects shows all 3 tags"

# 6. recall search --agent X must show ONLY agent X tag
for agent in claude gemini codex; do
  out=$(mktemp)
  "$RECALL" search "the" --agent "$agent" -n 50 -c 0 > "$out" 2>&1 || true
  if grep -E "^\[" "$out" | grep -v "^\[hybrid" | grep -v "^\[fts" \
       | grep -v "\[$agent\]" \
       | grep -qE "\[claude\]|\[gemini\]|\[codex\]"; then
    cat "$out"
    fail "search --agent $agent leaked another agent's tag"
  fi
done
echo "  ✅ search --agent filter is exclusive"

# 7. Latency — first run + warm run
warm_ns=$(python3 -c 'import time, subprocess, sys
last = 0
for _ in range(2):
    t0 = time.monotonic_ns()
    subprocess.run([sys.argv[1], "search", "the", "--all-projects", "-n", "5", "-c", "0"],
                   capture_output=True, check=True)
    last = time.monotonic_ns() - t0
print(last)' "$RECALL" 2>/dev/null || echo 9999999999)
warm_ms=$(( warm_ns / 1000000 ))
if [[ "$warm_ms" -gt 1500 ]]; then
  red "warm latency $warm_ms ms > 1500 ms"
  fail "search latency too high"
fi
echo "  ✅ warm search latency: ${warm_ms} ms"

green ""
green "=== ALL CHECKS PASSED ==="
green "messages=$msg_total embedded=$embed_pct% latency=${warm_ms}ms"
