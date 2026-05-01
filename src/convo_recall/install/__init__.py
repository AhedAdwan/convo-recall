"""recall install — facade.

Public API (`run`, `uninstall`, `install_hooks`, `uninstall_hooks`)
re-exports from the focused submodules:
- `_wizard.run` — interactive setup, scheduler-aware
- `_hooks.install_hooks` / `uninstall_hooks` — pre-prompt hook wiring
- `uninstall()` here — walks `all_schedulers()` so a host that switched
  OS gets clean teardown across tiers.

The cross-cutting path constants (`PROJECTS_DIR`, `GEMINI_TMP`, …)
live here so tests can monkeypatch `convo_recall.install.PROJECTS_DIR`
without touching submodules.
"""

import os
import sys
from pathlib import Path

# Re-exported. Constants live here so tests can monkeypatch them.
from .schedulers.launchd import (
    INGEST_LABEL,
    EMBED_LABEL,
    LAUNCHAGENTS,
    LaunchdScheduler,
)

PROJECTS_DIR = Path(os.environ.get("CONVO_RECALL_PROJECTS",
                    Path.home() / ".claude" / "projects"))
GEMINI_TMP = Path(os.environ.get("CONVO_RECALL_GEMINI_TMP",
                  Path.home() / ".gemini" / "tmp"))
CODEX_SESSIONS = Path(os.environ.get("CONVO_RECALL_CODEX_SESSIONS",
                      Path.home() / ".codex" / "sessions"))
SOCK_PATH = Path(os.environ.get("CONVO_RECALL_SOCK",
                 Path.home() / ".local" / "share" / "convo-recall" / "embed.sock"))
LOG_DIR = Path.home() / "Library" / "Logs"

# Per-agent watch path. Lambdas read module-level globals at call-time
# so `monkeypatch.setattr(_install, "PROJECTS_DIR", ...)` works.
_AGENT_WATCH_DIRS = {
    "claude": lambda: PROJECTS_DIR,
    "gemini": lambda: GEMINI_TMP,
    "codex":  lambda: CODEX_SESSIONS,
}


# `_ask` is referenced by `_hooks.install_hooks` via `from . import _ask`,
# so it must be defined here even though `_wizard.py` also has a copy
# (the wizard's _ask was moved with `run()` for cohesion).
from ._wizard import _ask  # noqa: E402,F401  — late re-export for _hooks


# Public API re-exports
from ._wizard import run  # noqa: E402,F401
from ._hooks import install_hooks, uninstall_hooks  # noqa: E402


def uninstall(purge_data: bool = False) -> None:
    """Walk every scheduler and remove anything it owns.

    A host that installed via systemd then switched OS to macOS deserves
    to see launchd plists cleaned up too — and vice versa. Each tier's
    `uninstall_*` no-ops gracefully when nothing was installed, so this
    is safe even when most tiers have nothing to do.

    Hooks come FIRST so the package is still installed when we resolve
    the bundled `conversation-memory.sh` script path. If hooks were
    deferred to after `pipx uninstall`, the script path can no longer be
    located and entries get left dangling in each CLI's settings file.
    """
    from .schedulers import all_schedulers

    def _surface(sched_describe: str, r) -> None:
        # No-op outcomes (nothing was installed for this tier) are noisy —
        # only surface meaningful changes and failures.
        msg = r.message
        is_noop = (
            "not installed" in msg
            or "nothing to remove" in msg
            or "already" in msg
        )
        if r.ok and not is_noop:
            print(f"  ✅ [{sched_describe}] {msg}")
        elif not r.ok:
            print(f"  ⚠  [{sched_describe}] {msg}", file=sys.stderr)

    # ── Pre-prompt hooks ────────────────────────────────────────────────────
    # Walk all three CLIs so a host that previously installed for a different
    # subset still gets cleaned. uninstall_hooks() prints per-CLI status and
    # no-ops on agents with no settings file or no convo-recall block.
    uninstall_hooks(agents=None)

    # ── Watchers + sidecars across every tier ───────────────────────────────
    for sched in all_schedulers():
        for agent in ("claude", "gemini", "codex"):
            _surface(sched.describe(), sched.uninstall_watcher(agent))
        _surface(sched.describe(), sched.uninstall_sidecar())

    if purge_data:
        import shutil as _shutil
        from ._paths import log_dir as _log_dir, runtime_dir as _runtime_dir

        # ── DB + config (~/.local/share/convo-recall) ────────────────────────
        data_dir = Path(os.environ.get(
            "CONVO_RECALL_DB",
            Path.home() / ".local" / "share" / "convo-recall" / "conversations.db"
        )).parent
        if data_dir.exists():
            _shutil.rmtree(data_dir)
            print(f"  ✅ Deleted data directory: {data_dir}")
        else:
            print(f"  Data directory not found: {data_dir}")

        # ── F-21/F-19: runtime dir (sockets + cron backups) ──────────────────
        # Always our directory on both platforms — safe to rmtree.
        rt = _runtime_dir()
        if rt.exists():
            _shutil.rmtree(rt)
            print(f"  ✅ Deleted runtime directory: {rt}")

        # ── F-18: log files ──────────────────────────────────────────────────
        # On Linux, log_dir() returns <state>/convo-recall (our dir → rmtree).
        # On macOS, log_dir() returns ~/Library/Logs (shared dir → glob delete).
        ld = _log_dir()
        if ld.exists():
            if ld.name == "convo-recall":
                _shutil.rmtree(ld)
                print(f"  ✅ Deleted log directory: {ld}")
            else:
                removed = 0
                for log in ld.glob("convo-recall-*.log"):
                    log.unlink(missing_ok=True)
                    removed += 1
                for elog in ld.glob("convo-recall-*.error.log"):
                    elog.unlink(missing_ok=True)
                    removed += 1
                if removed:
                    print(f"  ✅ Removed {removed} log file(s) from {ld}")

    print("\nconvo-recall uninstalled." + (" Data purged." if purge_data else
          "\nConversation DB kept. Re-run with --purge-data to delete it."))


__all__ = [
    "run",
    "uninstall",
    "install_hooks",
    "uninstall_hooks",
    "LaunchdScheduler",
    "LAUNCHAGENTS",
    "INGEST_LABEL",
    "EMBED_LABEL",
    "PROJECTS_DIR",
    "GEMINI_TMP",
    "CODEX_SESSIONS",
    "SOCK_PATH",
    "LOG_DIR",
]
