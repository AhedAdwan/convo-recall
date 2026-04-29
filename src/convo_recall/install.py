"""recall install — one-shot setup wizard (macOS only)."""

import os
import platform
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

INGEST_LABEL = "com.convo-recall.ingest"  # legacy single-plist label (uninstall-only)
EMBED_LABEL = "com.convo-recall.embed"
LAUNCHAGENTS = Path.home() / "Library" / "LaunchAgents"
PROJECTS_DIR = Path(os.environ.get("CONVO_RECALL_PROJECTS",
                    Path.home() / ".claude" / "projects"))
GEMINI_TMP = Path(os.environ.get("CONVO_RECALL_GEMINI_TMP",
                  Path.home() / ".gemini" / "tmp"))
CODEX_SESSIONS = Path(os.environ.get("CONVO_RECALL_CODEX_SESSIONS",
                      Path.home() / ".codex" / "sessions"))
SOCK_PATH = Path(os.environ.get("CONVO_RECALL_SOCK",
                 Path.home() / ".local" / "share" / "convo-recall" / "embed.sock"))
LOG_DIR = Path.home() / "Library" / "Logs"

# Per-agent watch path (the dir whose changes should trigger ingestion).
_AGENT_WATCH_DIRS = {
    "claude": lambda: PROJECTS_DIR,
    "gemini": lambda: GEMINI_TMP,
    "codex":  lambda: CODEX_SESSIONS,
}


def _ingest_plist(label: str, recall_bin: str, db_path: str,
                  watch_dir: str, sock_path: str, log_dir: str,
                  agent: str | None = None,
                  config_path: str | None = None) -> bytes:
    """Generate the launchd plist for a (per-agent) ingest watcher.

    `agent`: when set, the plist runs `recall ingest --agent {agent}` and
             watches only that agent's source dir. When None (legacy mode),
             runs plain `recall ingest` watching the claude projects dir.
    """
    args = [recall_bin, "ingest"]
    if agent:
        args += ["--agent", agent]
    env = {
        "CONVO_RECALL_DB": db_path,
        "CONVO_RECALL_PROJECTS": str(PROJECTS_DIR),
        "CONVO_RECALL_GEMINI_TMP": str(GEMINI_TMP),
        "CONVO_RECALL_CODEX_SESSIONS": str(CODEX_SESSIONS),
        "CONVO_RECALL_SOCK": sock_path,
    }
    if config_path:
        env["CONVO_RECALL_CONFIG"] = config_path
    suffix = f"-{agent}" if agent else ""
    return plistlib.dumps({
        "Label": label,
        "ProgramArguments": args,
        "EnvironmentVariables": env,
        "WatchPaths": [watch_dir],
        "RunAtLoad": True,
        "StandardOutPath": f"{log_dir}/convo-recall-ingest{suffix}.log",
        "StandardErrorPath": f"{log_dir}/convo-recall-ingest{suffix}.error.log",
        "ThrottleInterval": 10,
    })


def _embed_plist(label: str, recall_bin: str, sock_path: str, log_dir: str) -> bytes:
    return plistlib.dumps({
        "Label": label,
        "ProgramArguments": [recall_bin, "serve", "--sock", sock_path],
        "EnvironmentVariables": {"CONVO_RECALL_SOCK": sock_path},
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": f"{log_dir}/convo-recall-embed.log",
        "StandardErrorPath": f"{log_dir}/convo-recall-embed.error.log",
    })


def _find_recall_bin() -> str:
    found = shutil.which("recall")
    if found:
        return found
    candidate = Path(sys.executable).parent / "recall"
    if candidate.exists():
        return str(candidate)
    raise RuntimeError(
        "Cannot locate the `recall` executable. "
        "Install via `pipx install convo-recall` and ensure pipx bins are on PATH."
    )


def _launchctl_load(plist: Path) -> bool:
    uid = os.getuid()
    result = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{uid}", str(plist)],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        return True
    if result.returncode in (36, 37):
        subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(plist)],
                       capture_output=True)
        r2 = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist)],
                             capture_output=True, text=True)
        return r2.returncode == 0
    return False


def _check_embeddings_installed() -> bool:
    try:
        import sentence_transformers  # noqa: F401
        import aiohttp  # noqa: F401
        return True
    except ImportError:
        return False


def _require_macos() -> None:
    if platform.system() != "Darwin":
        print(
            "error: `recall install` requires macOS (launchd).\n"
            "On Linux, trigger ingestion via cron or systemd:\n"
            "  recall ingest  # run manually or schedule with cron/systemd",
            file=sys.stderr,
        )
        sys.exit(2)


def _resolve_enabled_agents(detected: list[dict]) -> list[str]:
    """Decide which agents to enable on first install.

    Default is non-interactive: include every agent whose source dir actually
    exists with at least one session file. The user can later re-run install
    or edit `~/.local/share/convo-recall/config.json` to change the set.
    """
    return [d["name"] for d in detected if d["file_count"] > 0] or ["claude"]


def run(dry_run: bool = False, with_embeddings: bool = False) -> None:
    _require_macos()
    import convo_recall.ingest as _ingest

    db_path = _ingest.DB_PATH
    config_path = _ingest._CONFIG_PATH
    print("convo-recall install\n")

    try:
        recall_bin = _find_recall_bin()
    except RuntimeError as e:
        print(f"  ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"  recall binary : {recall_bin}")
    print(f"  DB path       : {db_path}")
    print(f"  config path   : {config_path}")
    print(f"  embed socket  : {SOCK_PATH}")
    print(f"  log dir       : {LOG_DIR}")

    detected = _ingest.detect_agents()
    print("\n  Detected agents:")
    for d in detected:
        marker = "✅" if d["file_count"] > 0 else "·"
        print(f"    {marker} {d['name']:<7} {d['file_count']} file(s)  ({d['path']})")
    enabled = _resolve_enabled_agents(detected)
    print(f"\n  Will enable: {', '.join(enabled)}")

    if with_embeddings and not _check_embeddings_installed():
        print("\n  WARNING: [embeddings] extra not installed. Run:\n"
              "    pipx install 'convo-recall[embeddings]'\n"
              "  then re-run `recall install --with-embeddings`", file=sys.stderr)
        with_embeddings = False

    print()

    if dry_run:
        print("[dry-run] would create:")
        for agent in enabled:
            print(f"  {LAUNCHAGENTS}/{INGEST_LABEL}.{agent}.plist")
        if with_embeddings:
            print(f"  {LAUNCHAGENTS}/{EMBED_LABEL}.plist")
        print(f"  config:    {config_path}  →  {{\"agents\": {enabled}}}")
        print("Skipping actual install.")
        return

    # Persist enabled set so `recall ingest` (and the watch loop) know which
    # agents to scan.
    _ingest.save_config({"agents": enabled})

    # ── One ingest launchd job per enabled agent ─────────────────────────────
    LAUNCHAGENTS.mkdir(parents=True, exist_ok=True)
    for agent in enabled:
        label = f"{INGEST_LABEL}.{agent}"
        plist_path = LAUNCHAGENTS / f"{label}.plist"
        watch_dir = _AGENT_WATCH_DIRS[agent]()
        plist_path.write_bytes(_ingest_plist(
            label=label,
            recall_bin=recall_bin,
            db_path=str(db_path),
            watch_dir=str(watch_dir),
            sock_path=str(SOCK_PATH),
            log_dir=str(LOG_DIR),
            agent=agent,
            config_path=str(config_path),
        ))
        if _launchctl_load(plist_path):
            print(f"  ✅ {agent} ingest watcher loaded ({plist_path.name})")
        else:
            print(f"  ⚠  {agent} load failed — run manually: launchctl load {plist_path}")

    # ── Embed sidecar launchd job ─────────────────────────────────────────────
    if with_embeddings:
        embed_plist_path = LAUNCHAGENTS / f"{EMBED_LABEL}.plist"
        embed_plist_path.write_bytes(_embed_plist(
            label=EMBED_LABEL,
            recall_bin=recall_bin,
            sock_path=str(SOCK_PATH),
            log_dir=str(LOG_DIR),
        ))
        if _launchctl_load(embed_plist_path):
            print(f"  ✅ embed sidecar loaded ({embed_plist_path.name})")
            print(f"     Model will download on first use (~1.3 GB). Check:")
            print(f"     tail -f {LOG_DIR}/convo-recall-embed.log")
        else:
            print(f"  ⚠  embed load failed — run manually: launchctl load {embed_plist_path}")

    # ── Initial ingest ────────────────────────────────────────────────────────
    print("\n  Running initial ingest…")
    subprocess.run([recall_bin, "ingest"])

    print("\nInstallation complete.")
    print("\nWatchers fire automatically when files change in:")
    for agent in enabled:
        print(f"  [{agent}]  {_AGENT_WATCH_DIRS[agent]()}")
    print("\nQuick start:")
    print("  recall search 'your query'            # search current project")
    print("  recall search 'query' --all-projects  # search everything")
    print("  recall stats                           # DB statistics")
    if not with_embeddings:
        print("\nFor hybrid vector+FTS search (better recall):")
        print("  pipx install 'convo-recall[embeddings]'")
        print("  recall install --with-embeddings")


def uninstall(purge_data: bool = False) -> None:
    _require_macos()
    uid = os.getuid()
    removed = []
    failed = []

    candidates = [
        (INGEST_LABEL, LAUNCHAGENTS / f"{INGEST_LABEL}.plist"),  # legacy single
        (EMBED_LABEL,  LAUNCHAGENTS / f"{EMBED_LABEL}.plist"),
    ]
    # Per-agent ingest plists added in v0.2.0 multi-agent support
    for agent in ("claude", "gemini", "codex"):
        label = f"{INGEST_LABEL}.{agent}"
        candidates.append((label, LAUNCHAGENTS / f"{label}.plist"))

    for label, plist_path in candidates:
        if not plist_path.exists():
            continue
        subprocess.run(
            ["launchctl", "bootout", f"gui/{uid}", str(plist_path)],
            capture_output=True,
        )
        try:
            plist_path.unlink()
            removed.append(plist_path.name)
        except OSError as e:
            failed.append(f"{plist_path.name}: {e}")

    if removed:
        print("  Removed launchd agents:")
        for name in removed:
            print(f"    ✅ {name}")
    else:
        print("  No launchd agents found (already uninstalled or never installed).")

    if failed:
        for msg in failed:
            print(f"  ⚠  {msg}", file=sys.stderr)

    if purge_data:
        import shutil as _shutil
        data_dir = Path(os.environ.get(
            "CONVO_RECALL_DB",
            Path.home() / ".local" / "share" / "convo-recall" / "conversations.db"
        )).parent
        if data_dir.exists():
            _shutil.rmtree(data_dir)
            print(f"  ✅ Deleted data directory: {data_dir}")
        else:
            print(f"  Data directory not found: {data_dir}")

    print("\nconvo-recall uninstalled." + (" Data purged." if purge_data else
          "\nConversation DB kept. Re-run with --purge-data to delete it."))
