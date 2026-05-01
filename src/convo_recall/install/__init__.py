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


def _purge_preview(data_dir: Path, runtime_dir: Path, log_dir: Path) -> dict:
    """Inspect what `--purge-data` would delete WITHOUT touching anything.

    Returns a structured summary so callers can render a preview before
    asking for confirmation. Read-only — safe to call repeatedly.
    """
    summary: dict = {
        "data_dir": {"path": data_dir, "exists": data_dir.exists(),
                     "size_bytes": 0, "messages": 0, "sessions": 0,
                     "db_path": None, "db_size": 0},
        "runtime_dir": {"path": runtime_dir, "exists": runtime_dir.exists(),
                        "size_bytes": 0, "files": 0},
        "log_files": {"path": log_dir, "exists": log_dir.exists(),
                      "files": [], "size_bytes": 0, "rmtree_whole_dir": False},
    }

    def _dir_size(p: Path) -> int:
        total = 0
        try:
            for f in p.rglob("*"):
                if f.is_file():
                    total += f.stat().st_size
        except OSError:
            pass
        return total

    if data_dir.exists():
        summary["data_dir"]["size_bytes"] = _dir_size(data_dir)
        # Count messages + sessions if the DB is present and queryable.
        db_path = data_dir / "conversations.db"
        env_db = os.environ.get("CONVO_RECALL_DB")
        if env_db:
            db_path = Path(env_db)
        if db_path.exists():
            summary["data_dir"]["db_path"] = db_path
            summary["data_dir"]["db_size"] = db_path.stat().st_size
            try:
                import sqlite3 as _sqlite3
                con = _sqlite3.connect(
                    f"file:{db_path}?mode=ro", uri=True, timeout=2.0,
                )
                summary["data_dir"]["messages"] = con.execute(
                    "SELECT COUNT(*) FROM messages").fetchone()[0]
                summary["data_dir"]["sessions"] = con.execute(
                    "SELECT COUNT(*) FROM sessions").fetchone()[0]
                con.close()
            except Exception:
                pass  # locked / corrupt / missing tables — preview is best-effort

    if runtime_dir.exists():
        summary["runtime_dir"]["size_bytes"] = _dir_size(runtime_dir)
        try:
            summary["runtime_dir"]["files"] = sum(
                1 for _ in runtime_dir.rglob("*") if _.is_file())
        except OSError:
            pass

    if log_dir.exists():
        # If log_dir is OUR private dir (Linux: <state>/convo-recall),
        # we'd rmtree it whole. Otherwise (macOS: ~/Library/Logs shared),
        # we glob-delete only convo-recall-* files.
        if log_dir.name == "convo-recall":
            summary["log_files"]["rmtree_whole_dir"] = True
            summary["log_files"]["size_bytes"] = _dir_size(log_dir)
            try:
                summary["log_files"]["files"] = [
                    f for f in log_dir.rglob("*") if f.is_file()]
            except OSError:
                pass
        else:
            try:
                files = list(log_dir.glob("convo-recall-*.log")) + \
                        list(log_dir.glob("convo-recall-*.error.log"))
                summary["log_files"]["files"] = files
                summary["log_files"]["size_bytes"] = sum(
                    f.stat().st_size for f in files if f.is_file())
            except OSError:
                pass

    return summary


def _format_size(n: int) -> str:
    """Render a byte count as 'X.Y MB' / 'X KB' / 'N B'."""
    for unit, scale in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= scale:
            return f"{n / scale:.1f} {unit}"
    return f"{n} B"


def uninstall(purge_data: bool = False, confirm: bool = False) -> None:
    """Walk every scheduler and remove anything it owns.

    A host that installed via systemd then switched OS to macOS deserves
    to see launchd plists cleaned up too — and vice versa. Each tier's
    `uninstall_*` no-ops gracefully when nothing was installed, so this
    is safe even when most tiers have nothing to do.

    Hooks come FIRST so the package is still installed when we resolve
    the bundled `conversation-memory.sh` script path. If hooks were
    deferred to after `pipx uninstall`, the script path can no longer be
    located and entries get left dangling in each CLI's settings file.

    `purge_data=True` deletes the DB + logs + runtime dir. To prevent a
    typo (`recall uninstall --purge-data`) from wiping months of history,
    purge runs in DRY-RUN mode by default — it prints a preview and exits
    without touching files. Pass `confirm=True` to actually delete.
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

        data_dir = Path(os.environ.get(
            "CONVO_RECALL_DB",
            Path.home() / ".local" / "share" / "convo-recall" / "conversations.db"
        )).parent
        rt = _runtime_dir()
        ld = _log_dir()

        # ── PREVIEW (always shown, even with --confirm) ─────────────────────
        preview = _purge_preview(data_dir, rt, ld)
        print("\n" + "🔥" * 35)
        print("☠️  DANGER — --purge-data WILL PERMANENTLY DELETE THE FOLLOWING  ☠️")
        print("🔥" * 35)

        d = preview["data_dir"]
        if d["exists"]:
            line = f"  📁 DATA DIRECTORY : {d['path']} ({_format_size(d['size_bytes'])})"
            if d["db_path"] is not None:
                line += (f"\n     💾 DB          : {d['db_path']} "
                         f"({_format_size(d['db_size'])})")
                if d["messages"] or d["sessions"]:
                    line += (f"\n     📊 CONTENTS    : {d['messages']:,} MESSAGES, "
                             f"{d['sessions']:,} SESSIONS")
            print(line)
        else:
            print(f"  📁 DATA DIRECTORY : {d['path']} (does not exist — skip)")

        r = preview["runtime_dir"]
        if r["exists"]:
            print(f"  ⚙️  RUNTIME DIR    : {r['path']} "
                  f"({_format_size(r['size_bytes'])}, {r['files']} file(s))")
        else:
            print(f"  ⚙️  RUNTIME DIR    : {r['path']} (does not exist — skip)")

        l = preview["log_files"]
        if l["exists"] and (l["rmtree_whole_dir"] or l["files"]):
            n = len(l["files"])
            kind = "rmtree" if l["rmtree_whole_dir"] else "glob delete"
            print(f"  📜 LOG FILES      : {l['path']} "
                  f"({_format_size(l['size_bytes'])}, {n} file(s), {kind})")
        else:
            print(f"  📜 LOG FILES      : none")
        print("🔥" * 35)

        # Interactive prompt: even with `--confirm`, a TTY user gets one last
        # "ARE YOU SURE?" before the rmtree fires. Non-TTY (CI / piped) MUST
        # pass --confirm because no prompt can run there.
        if not confirm:
            if not sys.stdin.isatty():
                print("\n⚠️  ⚠️  ⚠️   DRY-RUN — NOTHING WAS DELETED   ⚠️  ⚠️  ⚠️")
                print()
                print("Non-interactive shell detected. Re-run with --confirm "
                      "to actually delete:")
                print("    recall uninstall --purge-data --confirm")
                print()
                print("convo-recall uninstalled (watchers/sidecars/hooks). "
                      "Conversation DB KEPT.")
                return
            print("\n⚠️  ⚠️  ⚠️   ARE YOU SURE?   ⚠️  ⚠️  ⚠️")
            print()
            print("There is NO undo. There is NO backup.")
            print("Every message, every session, every log file shown above is GONE.")
            print()
            response = input("Type 'YES' (uppercase) to proceed, anything else to cancel: ").strip()
            if response != "YES":
                print("\n✅ Aborted. Nothing was deleted.")
                print("convo-recall uninstalled (watchers/sidecars/hooks). "
                      "Conversation DB KEPT.")
                return

        print("\n💥💥💥  PROCEEDING WITH PERMANENT DELETION  💥💥💥\n")

        # ── ACTUAL DELETE (only with --confirm) ─────────────────────────────
        if data_dir.exists():
            _shutil.rmtree(data_dir)
            print(f"  ✅ Deleted data directory: {data_dir}")
        else:
            print(f"  Data directory not found: {data_dir}")

        # F-21/F-19: runtime dir (sockets + cron backups) — always our dir.
        if rt.exists():
            _shutil.rmtree(rt)
            print(f"  ✅ Deleted runtime directory: {rt}")

        # F-18: log files
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
