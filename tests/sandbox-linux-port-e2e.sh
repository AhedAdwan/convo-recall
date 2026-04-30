#!/usr/bin/env bash
# tests/sandbox-linux-port-e2e.sh — sandbox-only Linux port e2e
#
# Run inside claude-sandbox or any disposable Linux container.
# DO NOT run on a host where you have real watchers installed —
# Sections 2 and 4 will kill them.
#
# Six sections cover every scheduler tier the wizard can pick:
#   1. polling --dry-run        (no spawn; smoke-test stdout)
#   2. polling -y full lifecycle (real Popen + PID file + uninstall)
#   3. systemd --dry-run         (Linux-only; smoke-test unit gen)
#   4. systemd -y full lifecycle (Linux-only; real .service+.path)
#   5. cron --dry-run            (smoke-test @reboot line shape)
#   6. auto-detection            (no --scheduler; verifies the picker)
#
# Each section prints `[N] passed` on success or fails fast via
# `set -euo pipefail`. Exit 0 only if all six pass.

set -euo pipefail

# ── Helpers ───────────────────────────────────────────────────────────────────

OS_NAME="$(uname -s)"
RECALL="$(command -v recall || true)"

if [[ -z "${RECALL}" ]]; then
    echo "FAILED: recall not on PATH; install with \`pip install -e .[dev]\`" >&2
    exit 1
fi

# Resolve the runtime dir per _paths.runtime_dir():
#   Linux: $XDG_RUNTIME_DIR/convo-recall, else /tmp/convo-recall-$UID
#   macOS: ~/Library/Caches/convo-recall (sections 2 polling lifecycle still works)
runtime_dir() {
    if [[ "${OS_NAME}" == "Linux" ]]; then
        echo "${XDG_RUNTIME_DIR:-/tmp/convo-recall-$(id -u)}/convo-recall"
        if [[ -z "${XDG_RUNTIME_DIR:-}" ]]; then
            # The fallback per polling.py is /tmp/convo-recall-$UID directly,
            # NOT under it — adjust:
            echo "/tmp/convo-recall-$(id -u)"
        fi
    else
        echo "${HOME}/Library/Caches/convo-recall"
    fi
}

# Single source of truth for runtime dir (matches _paths.runtime_dir):
RT_DIR=""
if [[ "${OS_NAME}" == "Linux" ]]; then
    if [[ -n "${XDG_RUNTIME_DIR:-}" ]]; then
        RT_DIR="${XDG_RUNTIME_DIR}/convo-recall"
    else
        RT_DIR="/tmp/convo-recall-$(id -u)"
    fi
else
    RT_DIR="${HOME}/Library/Caches/convo-recall"
fi

section() {
    echo
    echo "──── Section $1: $2 ────"
}

# ── Section 1 — polling --dry-run ─────────────────────────────────────────────

section 1 "polling --dry-run"
out="$(${RECALL} install --scheduler polling --dry-run -y 2>&1)"
echo "${out}" | grep -q "polling (Popen fallback)" || {
    echo "FAILED: missing 'polling (Popen fallback)' in stdout"; exit 1; }
echo "${out}" | grep -q "Backgrounded via Popen" || {
    echo "FAILED: missing 'Backgrounded via Popen' consequence text"; exit 1; }
echo "[1] passed"

# ── Section 2 — polling -y full lifecycle ─────────────────────────────────────

section 2 "polling -y full lifecycle (real Popen)"
# Clean any stale PID files from prior runs.
rm -f "${RT_DIR}/watch.pid" "${RT_DIR}/embed.pid" 2>/dev/null || true

${RECALL} install --scheduler polling -y >/dev/null

PID_FILE="${RT_DIR}/watch.pid"
if [[ ! -f "${PID_FILE}" ]]; then
    echo "FAILED: PID file ${PID_FILE} not created"
    exit 1
fi
PID="$(cat "${PID_FILE}")"
if ! kill -0 "${PID}" 2>/dev/null; then
    echo "FAILED: PID ${PID} from ${PID_FILE} is not alive"
    exit 1
fi
if ! pgrep -f "recall watch" >/dev/null; then
    echo "FAILED: pgrep can't find recall watch (PID ${PID})"
    exit 1
fi

${RECALL} uninstall >/dev/null

# Allow up to 5s for SIGTERM grace.
for _ in 1 2 3 4 5 6 7 8 9 10; do
    if ! kill -0 "${PID}" 2>/dev/null; then
        break
    fi
    sleep 0.5
done
if kill -0 "${PID}" 2>/dev/null; then
    echo "FAILED: PID ${PID} still alive after uninstall + 5s grace"
    kill -KILL "${PID}" 2>/dev/null || true
    exit 1
fi
if [[ -f "${PID_FILE}" ]]; then
    echo "FAILED: PID file ${PID_FILE} still exists after uninstall"
    exit 1
fi
echo "[2] passed"

# ── Section 3 — systemd --dry-run (Linux-only) ────────────────────────────────

section 3 "systemd --dry-run"
if [[ "${OS_NAME}" != "Linux" ]]; then
    echo "[3] skipped (non-Linux host)"
else
    out="$(${RECALL} install --scheduler systemd --dry-run -y 2>&1 || true)"
    echo "${out}" | grep -q "systemd --user (Linux)" || {
        echo "FAILED: missing 'systemd --user (Linux)' in stdout"
        echo "stdout was:"
        echo "${out}"
        exit 1
    }
    echo "[3] passed"
fi

# ── Section 4 — systemd -y full lifecycle (Linux-only) ────────────────────────

section 4 "systemd -y full lifecycle"
if [[ "${OS_NAME}" != "Linux" ]]; then
    echo "[4] skipped (non-Linux host)"
elif ! systemctl --user is-system-running >/dev/null 2>&1; then
    echo "[4] skipped (systemd-user not available in this sandbox)"
else
    ${RECALL} install --scheduler systemd -y >/dev/null

    # At least one .path unit should be loaded for one of the three agents.
    matched=0
    for agent in claude codex gemini; do
        if systemctl --user list-units --no-legend "com.convo-recall.ingest.${agent}.path" \
                | grep -q loaded; then
            matched=$((matched + 1))
        fi
    done
    if [[ "${matched}" -eq 0 ]]; then
        echo "FAILED: no com.convo-recall.ingest.*.path units loaded after install"
        systemctl --user list-units --no-legend 'com.convo-recall.*' || true
        exit 1
    fi

    ${RECALL} uninstall >/dev/null

    # After uninstall: no com.convo-recall.* units should remain loaded.
    if systemctl --user list-units --no-legend 'com.convo-recall.*' \
            | grep -q loaded; then
        echo "FAILED: com.convo-recall.* unit still loaded after uninstall"
        systemctl --user list-units --no-legend 'com.convo-recall.*' || true
        exit 1
    fi
    echo "[4] passed"
fi

# ── Section 5 — cron --dry-run ────────────────────────────────────────────────

section 5 "cron --dry-run"
out="$(${RECALL} install --scheduler cron --dry-run -y 2>&1)"
echo "${out}" | grep -q "cron (Linux fallback)" || {
    echo "FAILED: missing 'cron (Linux fallback)' in stdout"; exit 1; }
echo "${out}" | grep -q "@reboot" || {
    echo "FAILED: missing '@reboot' consequence text"; exit 1; }
echo "[5] passed"

# ── Section 6 — auto-detection ────────────────────────────────────────────────

section 6 "auto-detection (no --scheduler)"
out="$(${RECALL} install --dry-run -y 2>&1)"
case "${OS_NAME}" in
    Darwin)
        echo "${out}" | grep -q "launchd (macOS)" || {
            echo "FAILED: macOS auto-detect should pick launchd"; exit 1; }
        ;;
    Linux)
        # Prefer systemd → cron → polling. Whichever is reachable wins.
        if echo "${out}" | grep -q "systemd --user (Linux)"; then
            : # ok
        elif echo "${out}" | grep -q "cron (Linux fallback)"; then
            : # ok (no systemd-user)
        elif echo "${out}" | grep -q "polling (Popen fallback)"; then
            : # ok (no systemd, no cron — last-resort)
        else
            echo "FAILED: Linux auto-detect should pick one of systemd/cron/polling"
            echo "stdout was:"
            echo "${out}"
            exit 1
        fi
        ;;
    *)
        echo "FAILED: unsupported OS: ${OS_NAME}"
        exit 1
        ;;
esac
echo "[6] passed"

echo
echo "All sections passed."
