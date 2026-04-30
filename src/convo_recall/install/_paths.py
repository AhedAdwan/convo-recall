"""Platform-aware path resolution shared across all schedulers.

Used by every concrete `Scheduler` to discover where it should write its
unit/agent files, log files, and runtime sockets — without each scheduler
re-implementing platform branching.
"""

import os
import platform
from pathlib import Path


def is_macos() -> bool:
    return platform.system() == "Darwin"


def is_linux() -> bool:
    return platform.system() == "Linux"


def scheduler_unit_dir() -> Path:
    """Where scheduler unit files live.

    macOS: `~/Library/LaunchAgents` (launchd plists).
    Linux: `$XDG_CONFIG_HOME/systemd/user`, falling back to
    `~/.config/systemd/user` per the XDG Base Directory spec.
    """
    if is_macos():
        return Path.home() / "Library" / "LaunchAgents"
    config_home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(config_home) / "systemd" / "user"


def log_dir() -> Path:
    """Where each scheduler writes log files.

    macOS: `~/Library/Logs`.
    Linux: `$XDG_STATE_HOME/convo-recall`, falling back to
    `~/.local/state/convo-recall` per the XDG spec.
    """
    if is_macos():
        return Path.home() / "Library" / "Logs"
    state_home = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state_home) / "convo-recall"


def runtime_dir() -> Path:
    """Where transient runtime files (e.g. embed sockets) live.

    macOS: `~/Library/Caches/convo-recall` (no XDG runtime dir on macOS).
    Linux: `$XDG_RUNTIME_DIR/convo-recall`, falling back to `/tmp/convo-recall-{uid}`
    when `$XDG_RUNTIME_DIR` is unset (e.g. cron / non-interactive sessions).
    """
    if is_macos():
        return Path.home() / "Library" / "Caches" / "convo-recall"
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / "convo-recall"
    return Path(f"/tmp/convo-recall-{os.getuid()}")


def ensure_xdg_runtime_dir() -> None:
    """Populate `XDG_RUNTIME_DIR` if unset but a user bus exists at the
    canonical location.

    `systemctl --user` resolves the user-bus at `$XDG_RUNTIME_DIR/bus`,
    and many of our other paths (runtime_dir, polling PID files) read
    `XDG_RUNTIME_DIR` too. On non-interactive shells (cron, sudo,
    ssh-without-AcceptEnv, `docker exec`) the var is often unset even
    when systemd-user is fully functional — the bus socket is at
    `/run/user/$UID/bus`. Without this fix, detection silently
    downgrades to cron/polling AND polling's PID file lands in
    `/tmp/convo-recall-{uid}` instead of the XDG path, so a clean
    install/uninstall cycle uses two different runtime dirs.

    Idempotent. Safe to call from anywhere — only mutates os.environ
    when the canonical bus path exists. macOS no-op.
    """
    if is_macos():
        return
    if "XDG_RUNTIME_DIR" in os.environ:
        return
    candidate = f"/run/user/{os.getuid()}"
    if Path(f"{candidate}/bus").exists():
        os.environ["XDG_RUNTIME_DIR"] = candidate
