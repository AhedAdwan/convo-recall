"""Setup wizard.

Talks to a `Scheduler` instance via the ABC — no hardcoded launchd /
platform branching. `scheduler="auto"` calls `detect_scheduler()`;
explicit `--scheduler X` calls `get_scheduler(X)`. The wizard's Step 1
prompt text adapts to the chosen scheduler via `consequence_yes/no()`
and `describe()`.

For SystemdUserScheduler an additional question asks the user whether
to enable lingering (so watchers survive logout); the wizard calls
`sched.enable_linger()` on yes.
"""

import shutil
import subprocess
import sys
from pathlib import Path

from .schedulers import detect_scheduler, get_scheduler
from .schedulers.systemd import SystemdUserScheduler


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


def _resolve_enabled_agents(detected: list[dict]) -> list[str]:
    """Decide which agents to enable on first install.

    Default is non-interactive: include every agent whose source dir actually
    exists with at least one session file. The user can later re-run install
    or edit `~/.local/share/convo-recall/config.json` to change the set.
    """
    return [d["name"] for d in detected if d["file_count"] > 0] or ["claude"]


def run(
    dry_run: bool = False,
    with_embeddings: bool = False,
    non_interactive: bool = False,
    scheduler: str = "auto",
) -> None:
    """Interactive setup wizard. Each decision prints what happens if YES vs
    if NO so the user opts into things knowingly. Non-interactive mode
    accepts the printed default for each question.

    `scheduler="auto"` runs `detect_scheduler()`; an explicit name
    (`launchd`/`systemd`/`cron`/`polling`) bypasses detection.
    """
    # Late imports: avoid circulars with __init__.py and ingest.
    from . import (
        LAUNCHAGENTS,  # noqa: F401 — kept on namespace for tests
        PROJECTS_DIR,  # noqa: F401
        GEMINI_TMP,    # noqa: F401
        CODEX_SESSIONS,  # noqa: F401
        SOCK_PATH,
        LOG_DIR,
        _AGENT_WATCH_DIRS,
    )
    from ._hooks import install_hooks
    import convo_recall.ingest as _ingest

    if scheduler == "auto":
        sched = detect_scheduler()
    else:
        sched = get_scheduler(scheduler)
        if not sched.available():
            print(
                f"warning: --scheduler {scheduler} reports unavailable on this "
                f"host; proceeding because you asked for it.",
                file=sys.stderr,
            )

    db_path = _ingest.DB_PATH
    config_path = _ingest._CONFIG_PATH
    print("convo-recall setup wizard\n")
    print(f"Selected scheduler: {sched.describe()}")
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
    print(f"Step 1/4: indexing watchers via {sched.describe()} for {', '.join(enabled)}")
    do_watchers = _ask(
        f"Install {sched.describe()} watchers so new sessions index automatically?",
        default=True,
        if_yes=sched.consequence_yes(),
        if_no=sched.consequence_no(),
        non_interactive=non_interactive,
    )

    # Linger opt-in — only meaningful for SystemdUserScheduler.
    do_linger = False
    if do_watchers and isinstance(sched, SystemdUserScheduler):
        do_linger = _ask(
            "Keep watchers running when logged out? (enables `loginctl enable-linger`)",
            default=True,
            if_yes=("Lingering keeps your user systemd instance running across "
                    "logout/SSH-disconnect — watchers survive."),
            if_no=("Watchers will die at logout — re-enable later with "
                   "`sudo loginctl enable-linger $USER`."),
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
    print(f"  scheduler     : {sched.describe()}")
    print(f"  watchers      : {'yes — ' + ', '.join(enabled) if do_watchers else 'no'}")
    if do_watchers and isinstance(sched, SystemdUserScheduler):
        print(f"  linger        : {'yes' if do_linger else 'no (watchers die at logout)'}")
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
            result = sched.install_watcher(
                agent=agent,
                recall_bin=recall_bin,
                watch_dir=str(watch_dir),
                db_path=str(db_path),
                sock_path=str(SOCK_PATH),
                config_path=str(config_path),
                log_dir=str(LOG_DIR),
            )
            marker = "✅" if result.ok else "⚠ "
            print(f"  {marker} [{sched.describe()}] {result.message}")
        if do_linger and isinstance(sched, SystemdUserScheduler):
            r = sched.enable_linger()
            marker = "✅" if r.ok else "⚠ "
            print(f"  {marker} {r.message}")

    # ── 2. Embed sidecar ─────────────────────────────────────────────────────
    if do_embed_sidecar:
        result = sched.install_sidecar(
            recall_bin=recall_bin,
            sock_path=str(SOCK_PATH),
            log_dir=str(LOG_DIR),
        )
        marker = "✅" if result.ok else "⚠ "
        print(f"  {marker} [{sched.describe()}] {result.message}")
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
