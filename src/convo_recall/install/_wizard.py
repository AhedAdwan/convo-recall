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
from .._spinner import BouncingSpinner


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

    # ── Non-interactive warning ─────────────────────────────────────────────
    # `recall install -y` auto-accepts every prompt — useful for CI but
    # dangerous when copy-pasted from a tutorial. Print a loud warning and
    # give the user 5 seconds to Ctrl-C. Skipped in dry-run (no side effects).
    if non_interactive and not dry_run:
        import time
        print()
        print("⚠️  ⚠️  ⚠️   NON-INTERACTIVE MODE — AUTO-ACCEPTING EVERY PROMPT   ⚠️  ⚠️  ⚠️")
        print()
        print("This will run `recall install` WITHOUT asking for confirmation on:")
        print("  • Installing watchers (launchd/systemd/cron) for detected agents")
        print("  • Wiring pre-prompt hooks into claude/codex/gemini settings files")
        print("  • Starting the embed sidecar (~1.3 GB model download if --with-embeddings)")
        print("  • Persisting config to ~/.local/share/convo-recall/")
        print()
        print("   ⏱️  Press Ctrl-C in the next 5 seconds to abort, or wait to proceed…")
        try:
            for i in range(5, 0, -1):
                print(f"      {i}…", end="", flush=True)
                time.sleep(1)
            print()
        except KeyboardInterrupt:
            print("\n✅ Aborted by user. Nothing changed.")
            return
        print()

    # Populate XDG_RUNTIME_DIR if unset but the user bus is reachable —
    # gives every downstream subprocess (systemctl --user, recall watch
    # spawn, runtime_dir() for PID files) a consistent rendezvous point.
    from ._paths import ensure_xdg_runtime_dir
    ensure_xdg_runtime_dir()

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

    # Apply order matters — three constraints conspire:
    #   1. Initial ingest must NOT race the watcher's first scan for the WAL
    #      writer lock, so the watcher install goes LAST (after the DB is
    #      already populated). Polling tier hits this deterministically;
    #      launchd / systemd are async-bootstrap and may or may not race.
    #   2. Embed-backfill needs the sidecar reachable, so the sidecar must
    #      be installed AND warmed BEFORE backfill runs.
    #   3. Initial ingest CAN run with the sidecar still warming up — it
    #      tolerates a missing socket and queues those rows for backfill.
    #
    # Resulting order: sidecar → ingest → wait-for-sidecar → backfill →
    # watchers → hooks.

    # ── 1. Embed sidecar (start it FIRST so it can warm up in parallel) ──────
    if do_embed_sidecar:
        with BouncingSpinner(f"Installing embed sidecar ({sched.describe()})"):
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

    # ── 2-3. Backfill (DETACHED — runs in the background after wizard exits) ─
    # Pre-fix this block ran ingest + embed-backfill SYNCHRONOUSLY, blocking
    # the wizard for the entire 10-30 min on a large corpus. For users with
    # 60K+ messages that's a UX disaster. New flow: spawn a detached
    # `recall _backfill-chain` subprocess that runs ingest → embed-backfill
    # in sequence, writing progress to <DATA_DIR>/backfill-progress.json.
    # `recall stats` renders a one-shot tqdm bar from that file any time
    # the user checks state. Wizard exits immediately with guidance.
    backfill_log = Path(LOG_DIR) / "convo-recall-wizard-backfill.log"
    backfill_proc = None
    if do_initial_ingest or do_initial_embed_backfill:
        Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
        try:
            log_fh = open(backfill_log, "ab")
        except OSError as e:
            print(f"⚠ Could not open backfill log {backfill_log}: {e}")
            log_fh = subprocess.DEVNULL  # type: ignore
        backfill_proc = subprocess.Popen(
            [recall_bin, "_backfill-chain"],
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            # `start_new_session` detaches: the child survives wizard exit
            # and is not killed by Ctrl-C in the wizard's session.
            start_new_session=True,
            close_fds=True,
        )
        print(f"\n📦 Initial ingest + embed-backfill running in background "
              f"(pid {backfill_proc.pid}).")
        print(f"   Logs:  tail -f {backfill_log}")
        print(f"   Progress: run `recall stats` (shows a one-shot progress bar "
              f"while the job is active).")
        if do_initial_embed_backfill and not do_embed_sidecar:
            print("⚠ embed-backfill will be skipped inside the chain — no "
                  "sidecar configured. Re-run install with --with-embeddings.")

    # ── 4. Watchers (LAST — DB is populated, sidecar is up, no race) ─────────
    if do_watchers:
        for agent in enabled:
            watch_dir = _AGENT_WATCH_DIRS[agent]()
            with BouncingSpinner(f"Installing {agent} watcher"):
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
            with BouncingSpinner("Enabling systemd linger"):
                r = sched.enable_linger()
            marker = "✅" if r.ok else "⚠ "
            print(f"  {marker} {r.message}")

    # ── 5. Pre-prompt hooks ──────────────────────────────────────────────────
    if do_hooks:
        print()
        with BouncingSpinner(f"Wiring pre-prompt hooks ({', '.join(enabled)})"):
            # User already consented at the wizard level; suppress per-CLI prompts.
            install_hooks(agents=enabled, dry_run=False, non_interactive=True)

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

    # ── Final stats snapshot ─────────────────────────────────────────────────
    # Show the user the current DB state — and, if a backfill chain just
    # spawned and beat us to writing the progress file, render the
    # one-shot tqdm bar at the top so they see it kicking off.
    print("\n" + "─" * 70)
    print("Current DB state:")
    print()
    try:
        subprocess.run([recall_bin, "stats"], check=False)
    except OSError as e:
        print(f"  (couldn't run `recall stats`: {e})")
