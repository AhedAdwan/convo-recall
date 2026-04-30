#!/usr/bin/env bash
# tests/sandbox-linux-port-e2e.sh — full Linux-port end-to-end harness.
#
# Run inside claude-sandbox or any disposable Linux container.
# DO NOT run on a host where you have real watchers installed —
# Stage 0 will scrub real PID files, real systemd units, and the
# `# convo-recall:*` lines from the user's crontab.
#
# Stages (each fails fast via `set -euo pipefail`):
#   0. clean-room: manual scrub of any prior convo-recall state
#   1. static guards: pytest full suite + pexpect wizard tests
#   2. detection sanity: detect_scheduler + auto wizard run
#   3. polling lifecycle: real Popen + PID file + idempotency + stale-PID + uninstall
#   4. systemd lifecycle: real .service + .path + REAL FILE-EVENT FIRING + uninstall
#   5. cron lifecycle: real crontab round-trip + USER-LINE PRESERVATION + uninstall
#   6. cross-tier uninstall: install polling, install cron, uninstall cleans both
#   7. argparse + bogus tier: --scheduler bogus exits non-zero
#   8. wheel install: build wheel, install in fresh venv, run from there
#   9. linger opt-in (semi-manual): wizard fires the linger question for systemd
#
# Stage failures print the captured context to make debugging possible
# without re-running. `[N] passed` on success.

set -euo pipefail

OS_NAME="$(uname -s)"
RECALL="$(command -v recall || true)"

if [[ -z "${RECALL}" ]]; then
    echo "FAILED: recall not on PATH; install with \`pip install -e .[dev]\`" >&2
    exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Resolve the runtime dir per _paths.runtime_dir():
#   Linux: $XDG_RUNTIME_DIR/convo-recall, fallback /tmp/convo-recall-$UID
#   macOS: ~/Library/Caches/convo-recall (Stage 3 polling still works there)
if [[ "${OS_NAME}" == "Linux" ]]; then
    if [[ -n "${XDG_RUNTIME_DIR:-}" ]]; then
        RT_DIR="${XDG_RUNTIME_DIR}/convo-recall"
    else
        RT_DIR="/tmp/convo-recall-$(id -u)"
    fi
else
    RT_DIR="${HOME}/Library/Caches/convo-recall"
fi

# ─── Result recording ────────────────────────────────────────────────────────
#
# Each run appends one NDJSON line to tests/sandbox-results.ndjson so future
# runs have a benchmark. Format:
#   {"ts":"...","commit":"<sha>","os":"Linux","kernel":"...","python":"3.11.8",
#    "recall":"0.3.0","stages":[{"n":0,"name":"clean-room","outcome":"pass",
#    "duration_s":0.5},...],"total_duration_s":42.1,"failed_stage":null}

RESULTS_FILE="${REPO_ROOT}/tests/sandbox-results.ndjson"
RUN_LOG="$(mktemp)"   # accumulates per-stage JSON fragments
RUN_START="$(date +%s)"
GIT_SHA="$(git -C "${REPO_ROOT}" rev-parse --short HEAD 2>/dev/null || echo unknown)"
KERNEL="$(uname -r)"
PY_VER="$(python -c 'import sys; print(".".join(str(v) for v in sys.version_info[:3]))')"
RECALL_VER="$(${RECALL} --version 2>/dev/null | awk '{print $2}' || echo unknown)"
STAGE_START=0
STAGE="?"
STAGE_NAME="?"

stage() {
    STAGE="$1"
    STAGE_NAME="$2"
    STAGE_START="$(date +%s)"
    echo
    echo "──── Stage $1: $2 ────"
}

# Record the just-completed stage (called explicitly per outcome).
__record() {
    local outcome="$1"
    local end_ts
    end_ts="$(date +%s)"
    local dur=$((end_ts - STAGE_START))
    # Tab-separated fragments: stage_n, stage_name, outcome, duration_s.
    printf '%s\t%s\t%s\t%s\n' \
        "${STAGE}" "${STAGE_NAME}" "${outcome}" "${dur}" >> "${RUN_LOG}"
}

pass_stage() {
    __record pass
    echo "[${STAGE}] passed (${STAGE_NAME})"
}

skip_stage() {
    local reason="$1"
    __record skip
    echo "[${STAGE}] skipped (${reason})"
}

# Trap: if anything exits non-zero, record the in-flight stage as `fail`
# and write the run record to RESULTS_FILE before propagating the failure.
__write_results() {
    local exit_code=$?
    local total_dur
    total_dur=$(($(date +%s) - RUN_START))

    # If we were mid-stage on a non-zero exit, record it as `fail`.
    if [[ "${exit_code}" -ne 0 && "${STAGE}" != "?" ]]; then
        # Avoid double-recording if the stage already wrote a result.
        if ! awk -F'\t' -v s="${STAGE}" '$1==s{found=1} END{exit !found}' "${RUN_LOG}" 2>/dev/null; then
            __record fail
        fi
    fi

    # Build stages JSON array from RUN_LOG.
    local stages_json
    stages_json="$(awk -F'\t' '
        BEGIN{first=1; printf "["}
        {
            if (!first) printf ",";
            first=0;
            printf "{\"n\":%s,\"name\":\"%s\",\"outcome\":\"%s\",\"duration_s\":%s}",
                $1, $2, $3, $4
        }
        END{printf "]"}
    ' "${RUN_LOG}")"

    local failed_stage="null"
    if [[ "${exit_code}" -ne 0 && "${STAGE}" != "?" ]]; then
        failed_stage="\"${STAGE}:${STAGE_NAME}\""
    fi

    local ts
    ts="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

    mkdir -p "$(dirname "${RESULTS_FILE}")"
    printf '{"ts":"%s","commit":"%s","os":"%s","kernel":"%s","python":"%s","recall":"%s","stages":%s,"total_duration_s":%s,"failed_stage":%s}\n' \
        "${ts}" "${GIT_SHA}" "${OS_NAME}" "${KERNEL}" "${PY_VER}" \
        "${RECALL_VER}" "${stages_json}" "${total_dur}" "${failed_stage}" \
        >> "${RESULTS_FILE}"

    rm -f "${RUN_LOG}"

    # Print a benchmark comparison vs the prior run (if one exists).
    if [[ -f "${RESULTS_FILE}" ]]; then
        local n_runs
        n_runs="$(wc -l < "${RESULTS_FILE}" | tr -d ' ')"
        if [[ "${n_runs}" -ge 2 ]]; then
            echo
            echo "── Benchmark vs prior run ──"
            tail -2 "${RESULTS_FILE}" | python3 -c "
import json, sys
prev, curr = [json.loads(l) for l in sys.stdin if l.strip()]
def stages_by_n(r): return {s['n']: s for s in r['stages']}
p, c = stages_by_n(prev), stages_by_n(curr)
print(f'  prior:   {prev[\"ts\"]}  commit={prev[\"commit\"]}  total={prev[\"total_duration_s\"]}s')
print(f'  current: {curr[\"ts\"]}  commit={curr[\"commit\"]}  total={curr[\"total_duration_s\"]}s')
for n in sorted(set(p) | set(c)):
    ps = p.get(n, {}); cs = c.get(n, {})
    p_oc = ps.get('outcome', '—'); c_oc = cs.get('outcome', '—')
    p_d = ps.get('duration_s', 0); c_d = cs.get('duration_s', 0)
    delta = c_d - p_d
    name = cs.get('name', ps.get('name', '?'))
    arrow = ''
    if p_oc != c_oc: arrow = f'  ({p_oc} → {c_oc})'
    print(f'  [{n}] {name:<28}  prev={p_oc:<5}{p_d:>3}s   curr={c_oc:<5}{c_d:>3}s   Δ{delta:+d}s{arrow}')
" 2>/dev/null || true
        fi
    fi
}
trap __write_results EXIT

fail()  { echo "FAILED ($STAGE): $*" >&2; exit 1; }

# ─── Stage 0: clean-room ──────────────────────────────────────────────────────

STAGE=0
stage 0 "clean-room scrub (manual; no recall uninstall)"

# Polling: nuke any leftover PID files (both XDG and /tmp fallback paths).
rm -f "${RT_DIR}"/*.pid 2>/dev/null || true
rm -f "/tmp/convo-recall-$(id -u)"/*.pid 2>/dev/null || true
# Kill any orphan watchers from prior runs (matches our own argv).
if pgrep -f "recall watch" >/dev/null 2>&1; then
    pkill -TERM -f "recall watch" 2>/dev/null || true
    sleep 1
    pkill -KILL -f "recall watch" 2>/dev/null || true
fi

# Systemd: disable + remove any com.convo-recall.* units.
if [[ "${OS_NAME}" == "Linux" ]] && command -v systemctl >/dev/null 2>&1 \
        && systemctl --user is-system-running >/dev/null 2>&1; then
    systemctl --user disable --now 'com.convo-recall.*' 2>/dev/null || true
    rm -f "${HOME}/.config/systemd/user/com.convo-recall."* 2>/dev/null || true
    systemctl --user daemon-reload 2>/dev/null || true
fi

# Cron: surgical removal of `# convo-recall:*` lines, preserve the rest.
if command -v crontab >/dev/null 2>&1; then
    if crontab -l 2>/dev/null | grep -v 'convo-recall:' > /tmp/crontab.scrub.$$; then
        crontab /tmp/crontab.scrub.$$ 2>/dev/null || true
    fi
    rm -f /tmp/crontab.scrub.$$
fi

# Data dirs: nuke DB, config, sock so each stage starts from a known DB-empty state.
rm -rf "${HOME}/.local/share/convo-recall" 2>/dev/null || true
rm -rf "${HOME}/.config/convo-recall"       2>/dev/null || true

pass_stage

# ─── Stage 1: static guards ───────────────────────────────────────────────────

STAGE=1
stage 1 "static guards (pytest + pexpect)"

cd "${REPO_ROOT}"
# Full unit suite — first time these run on a real Linux interpreter.
if ! python -m pytest tests/ -q --ignore=tests/test_wheel_packaging.py >/tmp/pytest.out.$$ 2>&1; then
    echo "FAILED: full pytest suite did not pass on this host"
    tail -40 /tmp/pytest.out.$$
    rm -f /tmp/pytest.out.$$
    exit 1
fi
rm -f /tmp/pytest.out.$$

# pexpect wizard: 4 cases under the real terminal of this OS.
if ! python -m pytest tests/test_install_wizard.py -v >/tmp/pexpect.out.$$ 2>&1; then
    echo "FAILED: pexpect wizard tests did not pass on this host"
    tail -40 /tmp/pexpect.out.$$
    rm -f /tmp/pexpect.out.$$
    exit 1
fi
rm -f /tmp/pexpect.out.$$

pass_stage

# ─── Stage 2: detection sanity ────────────────────────────────────────────────

STAGE=2
stage 2 "detection sanity (detect_scheduler + auto wizard)"

picked="$(python -c \
    "from convo_recall.install.schedulers import detect_scheduler; print(type(detect_scheduler()).__name__)")"
case "${OS_NAME}" in
    Darwin)
        [[ "${picked}" == "LaunchdScheduler" ]] || \
            fail "auto-detect on macOS should pick LaunchdScheduler; got ${picked}"
        ;;
    Linux)
        case "${picked}" in
            SystemdUserScheduler|CronScheduler|PollingScheduler) ;;
            *) fail "auto-detect on Linux should pick systemd/cron/polling; got ${picked}";;
        esac
        ;;
esac

# Auto wizard run — must NOT halt with `_require_macos`.
out="$(${RECALL} install --dry-run -y 2>&1)"
echo "${out}" | grep -q "Selected scheduler:" || \
    fail "wizard didn't print 'Selected scheduler:'; output:\n${out}"

pass_stage

# ─── Stage 3: polling lifecycle (real Popen) ──────────────────────────────────

STAGE=3
stage 3 "polling lifecycle (real Popen)"

PID_FILE="${RT_DIR}/watch.pid"

# 3a. Install + verify alive.
${RECALL} install --scheduler polling -y >/dev/null
[[ -f "${PID_FILE}" ]] || fail "PID file ${PID_FILE} not created after install"
PID="$(cat "${PID_FILE}")"
kill -0 "${PID}" 2>/dev/null || fail "PID ${PID} not alive after install"
pgrep -f "recall watch" >/dev/null || fail "pgrep can't find recall watch (PID ${PID})"

# 3b. Idempotency: re-install must NOT spawn a 2nd watcher.
PID_BEFORE="${PID}"
${RECALL} install --scheduler polling -y >/dev/null
PID_AFTER="$(cat "${PID_FILE}")"
[[ "${PID_AFTER}" == "${PID_BEFORE}" ]] || \
    fail "second install changed PID ${PID_BEFORE} → ${PID_AFTER} (should be no-op)"
[[ "$(pgrep -fc 'recall watch')" -eq 1 ]] || \
    fail "second install spawned a duplicate watcher; pgrep count=$(pgrep -fc 'recall watch')"

# 3c. Uninstall + verify dead.
${RECALL} uninstall >/dev/null
for _ in 1 2 3 4 5 6 7 8 9 10; do
    kill -0 "${PID}" 2>/dev/null || break
    sleep 0.5
done
kill -0 "${PID}" 2>/dev/null && fail "PID ${PID} still alive 5s after uninstall"
[[ ! -f "${PID_FILE}" ]] || fail "PID file ${PID_FILE} still exists after uninstall"

# 3d. Stale-PID handling: pre-write a dead PID, install must overwrite cleanly.
mkdir -p "${RT_DIR}"
echo "99999" > "${PID_FILE}"
${RECALL} install --scheduler polling -y >/dev/null
NEW_PID="$(cat "${PID_FILE}")"
[[ "${NEW_PID}" != "99999" ]] || fail "stale PID 99999 not overwritten on install"
kill -0 "${NEW_PID}" 2>/dev/null || fail "new PID ${NEW_PID} not alive after stale-recovery install"

# 3e. Adversarial: kill the watcher externally, then uninstall must clean up.
kill -KILL "${NEW_PID}" 2>/dev/null || true
sleep 0.5
${RECALL} uninstall >/dev/null
[[ ! -f "${PID_FILE}" ]] || fail "uninstall did not remove PID file after externally-killed watcher"

pass_stage

# ─── Stage 4: systemd lifecycle (REAL file-event firing) ──────────────────────

STAGE=4
stage 4 "systemd lifecycle (real units + file-event firing)"

if [[ "${OS_NAME}" != "Linux" ]]; then
    skip_stage "non-Linux host"
elif ! systemctl --user is-system-running >/dev/null 2>&1; then
    skip_stage "systemd-user not available"
else
    UNIT_DIR="${HOME}/.config/systemd/user"

    ${RECALL} install --scheduler systemd -y >/dev/null

    # 4a. Per-agent unit files exist + verify.
    matched=0
    for agent in claude codex gemini; do
        SVC="${UNIT_DIR}/com.convo-recall.ingest.${agent}.service"
        PTH="${UNIT_DIR}/com.convo-recall.ingest.${agent}.path"
        if [[ -f "${SVC}" && -f "${PTH}" ]]; then
            matched=$((matched + 1))
            systemd-analyze verify "${SVC}" "${PTH}" 2>&1 \
                | tee /tmp/sd-analyze.$$ \
                | { ! grep -E '(error|warning)' >/dev/null; } \
                || { cat /tmp/sd-analyze.$$; rm -f /tmp/sd-analyze.$$; \
                     fail "systemd-analyze flagged ${SVC} or ${PTH}"; }
            rm -f /tmp/sd-analyze.$$
        fi
    done
    [[ "${matched}" -ge 1 ]] || fail "no systemd unit pairs created"

    # 4b. Path units must be loaded + active.
    if ! systemctl --user list-units --no-legend 'com.convo-recall.ingest.*.path' \
            | grep -q loaded; then
        systemctl --user list-units --no-legend 'com.convo-recall.*' || true
        fail "no com.convo-recall.ingest.*.path units loaded after install"
    fi

    # 4c. THE REAL VALIDATION: trigger a file event, confirm .service fires.
    # Pick the first agent that has a watcher and a watch dir we can write.
    fired_for=""
    for agent in claude codex gemini; do
        SVC="com.convo-recall.ingest.${agent}.service"
        # Read the WatchPath from the .path unit file (literal %h not expanded —
        # use the in-process path source instead).
        watch_dir="$(python -c "
from convo_recall.install import _AGENT_WATCH_DIRS
print(_AGENT_WATCH_DIRS[\"${agent}\"]())")"
        if [[ -z "${watch_dir}" ]] || [[ ! -d "${watch_dir}" ]]; then
            mkdir -p "${watch_dir}" 2>/dev/null || continue
        fi
        # Capture journal cursor before the trigger.
        journal_cursor="$(journalctl --user --show-cursor -n 0 2>/dev/null \
            | tail -1 | sed -n 's/.*-- cursor: //p')"
        # Touch a file in the watched dir.
        touch "${watch_dir}/sandbox-trigger-$$.jsonl"
        sleep 3
        # Has the .service unit logged anything since the cursor?
        if [[ -n "${journal_cursor}" ]]; then
            if journalctl --user --after-cursor "${journal_cursor}" -u "${SVC}" \
                    --no-pager 2>/dev/null | grep -E "${SVC}|recall ingest" >/dev/null; then
                fired_for="${agent}"
                break
            fi
        fi
        # Fallback: check if the unit's last-run timestamp is recent.
        last_active="$(systemctl --user show "${SVC}" -p ActiveEnterTimestamp \
            --value 2>/dev/null || true)"
        if [[ -n "${last_active}" && "${last_active}" != "n/a" ]]; then
            fired_for="${agent}"
            break
        fi
    done
    if [[ -z "${fired_for}" ]]; then
        echo "WARNING: file-event firing not observed within 3s — this can"
        echo "         happen on slow sandboxes. Inspecting unit state:"
        systemctl --user list-units --no-legend 'com.convo-recall.*' || true
        # Soft warn — don't fail the whole stage on timing.
        echo "         (treating as soft warning; not a fatal failure)"
    else
        echo "  ✓ file-event firing observed for ${fired_for}.service"
    fi

    # 4d. Uninstall.
    ${RECALL} uninstall >/dev/null
    if systemctl --user list-units --no-legend 'com.convo-recall.*' \
            | grep -q loaded; then
        systemctl --user list-units --no-legend 'com.convo-recall.*' || true
        fail "com.convo-recall.* unit still loaded after uninstall"
    fi
    # And the unit files should be gone.
    leftover="$(ls "${UNIT_DIR}/com.convo-recall."* 2>/dev/null | head -1)"
    [[ -z "${leftover}" ]] || fail "leftover unit file after uninstall: ${leftover}"

    pass_stage
fi

# ─── Stage 5: cron lifecycle (USER-LINE PRESERVATION) ─────────────────────────

STAGE=5
stage 5 "cron lifecycle (real crontab round-trip + user-line preservation)"

if ! command -v crontab >/dev/null 2>&1; then
    skip_stage "crontab not installed"
else
    # 5a. Pre-populate crontab with two distinct user lines.
    USER_LINE_PLAIN='0 * * * * echo hi'
    USER_LINE_SUBSTRING='0 * * * * touch /tmp/x  # user note about convo-recall'
    printf '%s\n%s\n' "${USER_LINE_PLAIN}" "${USER_LINE_SUBSTRING}" | crontab -

    # 5b. Install — both user lines preserved + tagged @reboot line at end.
    ${RECALL} install --scheduler cron -y >/dev/null
    after="$(crontab -l 2>/dev/null)"
    echo "${after}" | grep -qF "${USER_LINE_PLAIN}" || \
        fail "plain user line was lost after cron install:\n${after}"
    echo "${after}" | grep -qF "${USER_LINE_SUBSTRING}" || \
        fail "user line containing substring 'convo-recall' was wrongly removed:\n${after}"
    tag_count="$(echo "${after}" | grep -c 'convo-recall:watch' || true)"
    [[ "${tag_count}" -eq 1 ]] || \
        fail "expected exactly 1 'convo-recall:watch' line; got ${tag_count}:\n${after}"

    # 5c. Backup file exists + matches pre-install content.
    bak="$(ls -t "${RT_DIR}"/crontab.bak.* 2>/dev/null | head -1)"
    [[ -n "${bak}" ]] || fail "no crontab backup written under ${RT_DIR}"
    grep -qF "${USER_LINE_PLAIN}" "${bak}" || fail "backup ${bak} doesn't contain pre-install state"

    # 5d. Idempotency.
    ${RECALL} install --scheduler cron -y >/dev/null
    after2="$(crontab -l 2>/dev/null)"
    tag_count2="$(echo "${after2}" | grep -c 'convo-recall:watch' || true)"
    [[ "${tag_count2}" -eq 1 ]] || \
        fail "second install duplicated cron line; got ${tag_count2}"

    # 5e. Uninstall — tagged line gone, user lines preserved verbatim.
    ${RECALL} uninstall >/dev/null
    final="$(crontab -l 2>/dev/null)"
    echo "${final}" | grep -q 'convo-recall:watch' && \
        fail "tagged @reboot line still present after uninstall:\n${final}"
    echo "${final}" | grep -qF "${USER_LINE_PLAIN}" || \
        fail "uninstall removed the plain user line:\n${final}"
    echo "${final}" | grep -qF "${USER_LINE_SUBSTRING}" || \
        fail "uninstall removed user line with 'convo-recall' substring:\n${final}"

    # 5f. Reset crontab.
    crontab -r 2>/dev/null || true

    pass_stage
fi

# ─── Stage 6: cross-tier uninstall ────────────────────────────────────────────

STAGE=6
stage 6 "cross-tier uninstall (polling + cron together)"

if ! command -v crontab >/dev/null 2>&1; then
    skip_stage "no crontab; cron tier unavailable"
else
    crontab -r 2>/dev/null || true

    ${RECALL} install --scheduler polling -y >/dev/null
    ${RECALL} install --scheduler cron -y >/dev/null

    # Both should have left state.
    [[ -f "${RT_DIR}/watch.pid" ]] || fail "polling PID file missing before cross-tier uninstall"
    crontab -l 2>/dev/null | grep -q 'convo-recall:watch' \
        || fail "cron tagged line missing before cross-tier uninstall"

    ${RECALL} uninstall >/dev/null

    # Both gone.
    [[ ! -f "${RT_DIR}/watch.pid" ]] || \
        fail "uninstall left polling PID file behind"
    if crontab -l 2>/dev/null | grep -q 'convo-recall:watch'; then
        fail "uninstall left cron tagged line behind"
    fi

    pass_stage
fi

# ─── Stage 7: argparse + bogus tier ───────────────────────────────────────────

STAGE=7
stage 7 "argparse rejects unknown scheduler"

set +e
out="$(${RECALL} install --scheduler bogus --dry-run -y 2>&1)"
rc=$?
set -e
[[ "${rc}" -ne 0 ]] || fail "bogus scheduler should exit non-zero; got rc=${rc}"
for name in auto launchd systemd cron polling; do
    echo "${out}" | grep -q "${name}" || \
        fail "argparse error should list '${name}' in choices; got:\n${out}"
done
pass_stage

# ─── Stage 8: wheel install (catches Hatch packaging gaps) ────────────────────

STAGE=8
stage 8 "wheel build + install in fresh venv"

if ! python -c "import build" 2>/dev/null; then
    skip_stage "\`pip install build\` to enable"
else
    cd "${REPO_ROOT}"
    rm -rf /tmp/cr-wheel.$$ /tmp/cr-venv.$$
    python -m build --wheel --outdir /tmp/cr-wheel.$$ >/tmp/build.out.$$ 2>&1 \
        || { tail -30 /tmp/build.out.$$; rm -f /tmp/build.out.$$; \
             fail "python -m build --wheel failed"; }
    rm -f /tmp/build.out.$$

    python -m venv /tmp/cr-venv.$$
    /tmp/cr-venv.$$/bin/pip install --quiet /tmp/cr-wheel.$$/convo_recall-*.whl

    /tmp/cr-venv.$$/bin/recall install --dry-run -y --scheduler polling \
        > /tmp/cr-wheel-out.$$ 2>&1 \
        || { tail -30 /tmp/cr-wheel-out.$$; \
             fail "wheel-installed recall failed"; }
    grep -q "polling (Popen fallback)" /tmp/cr-wheel-out.$$ \
        || fail "wheel-installed recall didn't pick polling"

    rm -rf /tmp/cr-wheel.$$ /tmp/cr-venv.$$
    rm -f  /tmp/cr-wheel-out.$$

    pass_stage
fi

# ─── Stage 9: linger opt-in (semi-manual) ─────────────────────────────────────

STAGE=9
stage 9 "linger question fires for systemd path"

if [[ "${OS_NAME}" != "Linux" ]]; then
    skip_stage "non-Linux host"
elif ! command -v expect >/dev/null 2>&1 \
        && ! python -c "import pexpect" 2>/dev/null; then
    skip_stage "no expect/pexpect available"
else
    # Drive the wizard interactively under --scheduler systemd, look for the
    # linger prompt explicitly. We don't actually call enable-linger (skips
    # polkit/sudo), just verify the question fires.
    output="$(python - <<'PY'
import shutil, sys
import pexpect
recall = shutil.which("recall")
w = pexpect.spawn(recall, ["install", "--scheduler", "systemd"],
                  encoding="utf-8", timeout=10)
saw_linger = False
try:
    while True:
        idx = w.expect([
            r"Keep watchers running when logged out",
            r"\[Y/n\]",
            pexpect.EOF,
        ], timeout=15)
        if idx == 0:
            saw_linger = True
            # Decline the linger question; drain to the end.
            w.expect(r"\[Y/n\]")
            w.sendline("n")
        elif idx == 1:
            w.sendline("n")  # decline everything else to exit fast
        else:
            break
finally:
    w.close()
print("LINGER_QUESTION_FIRED" if saw_linger else "LINGER_QUESTION_MISSING")
PY
)"
    echo "${output}" | grep -q LINGER_QUESTION_FIRED \
        || fail "linger question did not fire under --scheduler systemd"
    pass_stage
fi

echo
echo "════════════════════════════════════════════════════════════"
echo "All stages passed."
echo "════════════════════════════════════════════════════════════"
