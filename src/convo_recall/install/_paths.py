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
