"""recall install — interactive setup wizard.

The launchd watcher install is macOS-only (uses launchd APIs). The
pre-prompt hook wiring is cross-platform — it just edits the per-CLI
settings files (`~/.claude/settings.json`, `~/.codex/hooks.json`,
`~/.gemini/settings.json`). `recall install-hooks` exposes that
standalone for Linux users and for after-the-fact wiring.
"""

import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Embedded import for type hints / hook script lookup at runtime.
import importlib.resources as _resources

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

# Per-agent watch path (the dir whose changes should trigger ingestion).
_AGENT_WATCH_DIRS = {
    "claude": lambda: PROJECTS_DIR,
    "gemini": lambda: GEMINI_TMP,
    "codex":  lambda: CODEX_SESSIONS,
}

# ── Wizard helpers ────────────────────────────────────────────────────────────

def _ask(question: str, *, default: bool = True,
         if_yes: str | None = None, if_no: str | None = None,
         non_interactive: bool = False) -> bool:
    """Prompt the user with a yes/no question. Always print the consequence
    of each answer first so the user knows what they're opting into / out of.

    `non_interactive=True` accepts the default without prompting (used for
    CI / scripted installs). The consequence callouts still print so the
    install log captures what was decided.
    """
    print(f"\n? {question}")
    if if_yes:
        print(f"   ↪ if YES: {if_yes}")
    if if_no:
        print(f"   ↪ if NO:  {if_no}")
    if non_interactive:
        chosen = "yes" if default else "no"
        print(f"   [non-interactive: {chosen}]")
        return default
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        try:
            response = input(f"   {suffix} ").strip().lower()
        except EOFError:
            return default
        if not response:
            return default
        if response in ("y", "yes"):
            return True
        if response in ("n", "no"):
            return False
        print("   Please answer y or n.")


# ── Pre-prompt hook wiring ────────────────────────────────────────────────────
#
# Each CLI uses a slightly different settings file and a slightly different
# wrapper shape. We wire one hook per CLI; the same shell script handles all
# three (it auto-detects the firing event from stdin).

# Maps agent → (settings file path, hook event name, wrapper builder).
def _hook_target(agent: str) -> tuple[Path, str, str]:
    """Return (settings_path, event_name, agent_label) for a given agent."""
    if agent == "claude":
        return Path.home() / ".claude" / "settings.json", "UserPromptSubmit", "claude"
    if agent == "codex":
        return Path.home() / ".codex" / "hooks.json", "UserPromptSubmit", "codex"
    if agent == "gemini":
        return Path.home() / ".gemini" / "settings.json", "BeforeAgent", "gemini"
    raise ValueError(f"unknown agent: {agent}")


def _hook_block(agent: str, hook_script: Path) -> dict:
    """Build the hook block to insert under settings.hooks[event]."""
    if agent == "gemini":
        # Gemini uses millisecond timeouts and requires a `name` field.
        return {
            "matcher": "*",
            "hooks": [{
                "name": "convo-recall",
                "type": "command",
                "command": str(hook_script),
                "timeout": 5000,
            }],
        }
    # Claude and Codex share the same shape; timeout is in seconds.
    return {
        "hooks": [{
            "type": "command",
            "command": str(hook_script),
            "timeout": 5,
        }],
    }


def _hook_block_signature(agent: str, hook_script: Path) -> str:
    """Stable identifier for an existing convo-recall hook entry — lets
    `install-hooks` skip already-wired CLIs and `uninstall-hooks` find
    only the convo-recall block among the user's other hooks."""
    return str(hook_script)


def _find_hook_script() -> Path:
    """Locate the bundled `conversation-memory.sh`. Tries the editable-install
    path first (works in dev), falls back to importlib.resources (works
    after pipx install)."""
    here = Path(__file__).resolve().parent.parent / "hooks" / "conversation-memory.sh"
    if here.is_file():
        return here
    # importlib.resources path for installed wheel
    try:
        with _resources.path("convo_recall.hooks", "conversation-memory.sh") as p:
            return Path(p).resolve()
    except (ModuleNotFoundError, FileNotFoundError):
        pass
    raise RuntimeError(
        "Cannot locate conversation-memory.sh. "
        "Reinstall convo-recall and try again."
    )


def _backup_path(p: Path) -> Path:
    """Atomic-ish backup filename: <name>.bak.<unix-ts>."""
    return p.with_name(p.name + f".bak.{int(time.time())}")


def _wire_hook(agent: str, hook_script: Path,
               *, dry_run: bool = False) -> tuple[bool, str]:
    """Wire the convo-recall pre-prompt hook into one CLI's settings file.

    Returns (changed, message). Idempotent: if a hook block with the same
    command path already exists for the right event, no-op.
    """
    settings_path, event, label = _hook_target(agent)
    existing: dict = {}
    if settings_path.exists():
        try:
            existing = json.loads(settings_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            return False, f"  ⚠  {label}: settings unreadable ({e}); skipping"

    new_block = _hook_block(agent, hook_script)
    sig = _hook_block_signature(agent, hook_script)

    hooks_root = existing.setdefault("hooks", {}) if not dry_run else (
        # Build a copy for diffing; never mutate `existing` in dry-run.
        json.loads(json.dumps(existing.get("hooks", {})))
    )
    event_groups = hooks_root.setdefault(event, [])

    # Idempotency: scan existing groups for a hook command that matches.
    for group in event_groups:
        for entry in group.get("hooks", []):
            if entry.get("command") == sig:
                return False, f"  · {label}: hook already wired ({settings_path})"

    if dry_run:
        return True, (
            f"  + {label}: would add convo-recall hook to "
            f"{settings_path} → hooks.{event}"
        )

    event_groups.append(new_block)

    # Atomic write with mode 0o600 (settings can include API keys).
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    if settings_path.exists():
        backup = _backup_path(settings_path)
        backup.write_bytes(settings_path.read_bytes())
        backup_msg = f" (backup: {backup.name})"
    else:
        backup_msg = " (new file)"
    tmp = settings_path.with_name(settings_path.name + ".tmp")
    tmp.write_text(json.dumps(existing, indent=2))
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(settings_path)
    return True, f"  ✅ {label}: hook wired into {settings_path}{backup_msg}"


def _unwire_hook(agent: str, hook_script: Path) -> tuple[bool, str]:
    """Remove the convo-recall hook block from a CLI's settings (matched
    by command path). Leaves user's other hooks untouched."""
    settings_path, event, label = _hook_target(agent)
    if not settings_path.exists():
        return False, f"  · {label}: no settings file; nothing to remove"
    try:
        existing = json.loads(settings_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        return False, f"  ⚠  {label}: settings unreadable ({e}); skipping"
    sig = _hook_block_signature(agent, hook_script)
    hooks_root = existing.get("hooks") or {}
    event_groups = hooks_root.get(event) or []
    new_groups = []
    removed = 0
    for group in event_groups:
        kept_hooks = [h for h in group.get("hooks", []) if h.get("command") != sig]
        if not kept_hooks:
            removed += 1
            continue
        if len(kept_hooks) != len(group.get("hooks", [])):
            removed += 1
        if kept_hooks:
            new_group = dict(group)
            new_group["hooks"] = kept_hooks
            new_groups.append(new_group)
    if not removed:
        return False, f"  · {label}: no convo-recall hook found; nothing to remove"
    if new_groups:
        hooks_root[event] = new_groups
    else:
        hooks_root.pop(event, None)
    if not hooks_root:
        existing.pop("hooks", None)
    backup = _backup_path(settings_path)
    backup.write_bytes(settings_path.read_bytes())
    tmp = settings_path.with_name(settings_path.name + ".tmp")
    tmp.write_text(json.dumps(existing, indent=2))
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(settings_path)
    return True, f"  ✅ {label}: hook removed from {settings_path} (backup: {backup.name})"


def install_hooks(agents: list[str] | None = None,
                  *, dry_run: bool = False,
                  non_interactive: bool = False) -> int:
    """Standalone hook-wiring entry point. Used both by `recall install-hooks`
    and as one stage of the full `recall install` wizard.

    Returns the count of CLIs actually changed. Skips agents with no
    detectable source dir unless `agents` is passed explicitly.
    """
    import convo_recall.ingest as _ingest

    try:
        hook_script = _find_hook_script()
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 0

    if agents is None:
        detected = _ingest.detect_agents()
        agents = [d["name"] for d in detected if d["file_count"] > 0]
        if not agents:
            print("No agent source directories detected. Nothing to wire.")
            return 0

    print(f"Pre-prompt hook script: {hook_script}\n")
    if dry_run:
        print("[dry-run] showing what would change:\n")

    changed = 0
    for agent in agents:
        if agent not in ("claude", "codex", "gemini"):
            print(f"  ⚠  unknown agent {agent!r}, skipping")
            continue
        if not non_interactive and not dry_run:
            settings_path, event, label = _hook_target(agent)
            consent = _ask(
                f"Wire convo-recall hook for {label.title()} ({settings_path})?",
                default=True,
                if_yes=f"{label.title()} will see a 'search history first' hint on every prompt.",
                if_no=f"{label.title()} won't know convo-recall exists. "
                      f"Re-run `recall install-hooks --agent {label}` later to wire it.",
                non_interactive=False,
            )
            if not consent:
                print(f"  · {label}: skipped by user")
                continue
        did_change, msg = _wire_hook(agent, hook_script, dry_run=dry_run)
        print(msg)
        if did_change and not dry_run:
            changed += 1
    return changed


def uninstall_hooks(agents: list[str] | None = None) -> int:
    """Remove convo-recall hook blocks from each CLI's settings file."""
    try:
        hook_script = _find_hook_script()
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 0
    if agents is None:
        agents = ["claude", "codex", "gemini"]
    removed = 0
    for agent in agents:
        if agent not in ("claude", "codex", "gemini"):
            continue
        did_remove, msg = _unwire_hook(agent, hook_script)
        print(msg)
        if did_remove:
            removed += 1
    return removed


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


def run(dry_run: bool = False, with_embeddings: bool = False,
        non_interactive: bool = False) -> None:
    """Interactive setup wizard. Each decision prints what happens if YES vs
    if NO so the user opts into things knowingly. Non-interactive mode
    accepts the printed default for each question — used by CI scripts and
    by users who pass `--with-embeddings -y` for one-shot setup.
    """
    _require_macos()
    import convo_recall.ingest as _ingest

    scheduler = LaunchdScheduler()

    db_path = _ingest.DB_PATH
    config_path = _ingest._CONFIG_PATH
    print("convo-recall setup wizard\n")
    print("This walks through 4 decisions. Each is opt-in; defaults are safe.\n")

    try:
        recall_bin = _find_recall_bin()
    except RuntimeError as e:
        print(f"  ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    print("Environment:")
    print(f"  recall binary : {recall_bin}")
    print(f"  DB path       : {db_path}")
    print(f"  config path   : {config_path}")
    print(f"  embed socket  : {SOCK_PATH}")
    print(f"  log dir       : {LOG_DIR}")

    detected = _ingest.detect_agents()
    print("\nDetected agents:")
    for d in detected:
        marker = "✅" if d["file_count"] > 0 else "·"
        print(f"  {marker} {d['name']:<7} {d['file_count']} file(s)  ({d['path']})")
    enabled = _resolve_enabled_agents(detected)
    if not enabled:
        print("\nNo agent session files found. Nothing to index. "
              "Re-run after using one of the supported CLIs.")
        return

    embeddings_extra_present = _check_embeddings_installed()
    print(f"\n[embeddings] extra installed: {'yes' if embeddings_extra_present else 'no'}")

    # ── Q1. Indexing watchers ────────────────────────────────────────────────
    print("\n" + "─" * 70)
    print(f"Step 1/4: indexing watchers for {', '.join(enabled)}")
    do_watchers = _ask(
        "Install launchd watchers so new sessions index automatically?",
        default=True,
        if_yes=scheduler.consequence_yes(),
        if_no=scheduler.consequence_no(),
        non_interactive=non_interactive,
    )

    # ── Q2. Embed sidecar ────────────────────────────────────────────────────
    print("\n" + "─" * 70)
    print("Step 2/4: hybrid vector + FTS search")
    if not embeddings_extra_present:
        print("  · Skipping: [embeddings] extra not installed.")
        print("    To enable later: `pipx install 'convo-recall[embeddings]' && recall install`")
        do_embed_sidecar = False
    elif with_embeddings:
        # Honor the explicit flag without re-prompting.
        print("  · --with-embeddings flag set; enabling sidecar without prompting.")
        do_embed_sidecar = True
    else:
        do_embed_sidecar = _ask(
            "Install the embed sidecar for hybrid vector+FTS search?",
            default=True,
            if_yes=("Downloads BAAI/bge-large-en-v1.5 (~1.3 GB) on first run, "
                    "then keeps the model warm in the background for fast queries."),
            if_no=("FTS-only mode: keyword search works, but `recall search` won't "
                   "do semantic matching. Re-run install with --with-embeddings later."),
            non_interactive=non_interactive,
        )

    # ── Q3. Pre-prompt hooks ─────────────────────────────────────────────────
    print("\n" + "─" * 70)
    print(f"Step 3/4: pre-prompt hooks for {', '.join(enabled)}")
    print("  Without these, your AI agents (Claude/Codex/Gemini) won't know")
    print("  convo-recall exists and will keep guessing/web-searching despite")
    print("  the indexed history sitting right there.")
    do_hooks = _ask(
        "Wire pre-prompt hooks now?",
        default=True,
        if_yes=("Each detected CLI's settings file gets a hook block pointing at "
                "the bundled conversation-memory.sh. Existing hooks are preserved; "
                "we back up the original settings file with a timestamp."),
        if_no=("Agents won't see convo-recall hints. To wire later, run "
               "`recall install-hooks` (any subset with `--agent`). "
               "Or copy-paste from the README into each settings file."),
        non_interactive=non_interactive,
    )

    # ── Q4. Initial ingest + backfill ────────────────────────────────────────
    print("\n" + "─" * 70)
    print("Step 4/4: initial ingest")
    do_initial_ingest = _ask(
        "Run initial ingest now? (synchronous; may take 10-30 min on a large corpus)",
        default=True,
        if_yes="Existing sessions are indexed before this command exits.",
        if_no=("DB stays empty until first new session is written. "
               "Run `recall ingest` later to backfill manually."),
        non_interactive=non_interactive,
    )
    do_initial_embed_backfill = False
    if do_embed_sidecar and do_initial_ingest:
        do_initial_embed_backfill = _ask(
            "Embed all messages in one pass after ingest? (5-30 min on a large DB)",
            default=True,
            if_yes="Vector search ready immediately; no warmup ramp.",
            if_no=("Self-heal pass embeds 2000 rows per ingest tick; vector recall "
                   "ramps up over the next few hours."),
            non_interactive=non_interactive,
        )

    # ── Summary + confirmation ───────────────────────────────────────────────
    print("\n" + "═" * 70)
    print("Summary:")
    print(f"  watchers      : {'yes — ' + ', '.join(enabled) if do_watchers else 'no'}")
    print(f"  embed sidecar : {'yes (downloads ~1.3 GB)' if do_embed_sidecar else 'no'}")
    print(f"  hooks         : {'yes — ' + ', '.join(enabled) if do_hooks else 'no'}")
    print(f"  initial ingest: {'yes' if do_initial_ingest else 'no'}")
    if do_embed_sidecar:
        print(f"  embed backfill: {'yes' if do_initial_embed_backfill else 'no (self-heal will catch up)'}")
    print("═" * 70)

    if dry_run:
        print("\n[dry-run] would apply the above; exiting without changes.")
        return

    if not non_interactive and not _ask(
        "Apply these settings now?",
        default=True,
        non_interactive=False,
    ):
        print("Aborted. No changes made.")
        return

    # Persist enabled set so `recall ingest` (and the watch loop) know which
    # agents to scan. Always write — needed by `recall ingest` even if no
    # watchers are installed.
    _ingest.save_config({"agents": enabled})

    # ── 1. Watchers ──────────────────────────────────────────────────────────
    if do_watchers:
        for agent in enabled:
            watch_dir = _AGENT_WATCH_DIRS[agent]()
            result = scheduler.install_watcher(
                agent=agent,
                recall_bin=recall_bin,
                watch_dir=str(watch_dir),
                db_path=str(db_path),
                sock_path=str(SOCK_PATH),
                config_path=str(config_path),
                log_dir=str(LOG_DIR),
            )
            marker = "✅" if result.ok else "⚠ "
            print(f"  {marker} {result.message}")

    # ── 2. Embed sidecar ─────────────────────────────────────────────────────
    if do_embed_sidecar:
        result = scheduler.install_sidecar(
            recall_bin=recall_bin,
            sock_path=str(SOCK_PATH),
            log_dir=str(LOG_DIR),
        )
        marker = "✅" if result.ok else "⚠ "
        print(f"  {marker} {result.message}")
        if result.ok:
            print(f"     Model will download on first use (~1.3 GB). Check:")
            print(f"     tail -f {LOG_DIR}/convo-recall-embed.log")

    # ── 3. Pre-prompt hooks ──────────────────────────────────────────────────
    if do_hooks:
        print("\nWiring pre-prompt hooks…")
        # User already consented at the wizard level; suppress per-CLI prompts.
        install_hooks(agents=enabled, dry_run=False, non_interactive=True)

    # ── 4. Initial ingest + backfill ─────────────────────────────────────────
    if do_initial_ingest:
        print("\nRunning initial ingest…")
        subprocess.run([recall_bin, "ingest"])
        if do_initial_embed_backfill:
            print("\nRunning initial embed-backfill…")
            subprocess.run([recall_bin, "embed-backfill"])

    # ── Final summary ────────────────────────────────────────────────────────
    print("\nInstallation complete.")
    if do_watchers:
        print("\nWatchers fire automatically when files change in:")
        for agent in enabled:
            print(f"  [{agent}]  {_AGENT_WATCH_DIRS[agent]()}")
    else:
        print("\nWatchers were skipped. Run `recall ingest` manually after each session, "
              "or set up cron/systemd yourself.")
    if not do_hooks:
        print("\nPre-prompt hooks were skipped. Wire them later with:")
        print(f"  recall install-hooks                # all detected CLIs")
        print(f"  recall install-hooks --agent claude # one CLI")
    print("\nQuick start:")
    print("  recall search 'your query'            # search current project")
    print("  recall search 'query' --all-projects  # search everything")
    print("  recall stats                           # DB statistics")
    if not do_embed_sidecar and embeddings_extra_present:
        print("\nFor hybrid vector+FTS search (better recall):")
        print("  recall install --with-embeddings")


def uninstall(purge_data: bool = False) -> None:
    _require_macos()
    scheduler = LaunchdScheduler()
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
