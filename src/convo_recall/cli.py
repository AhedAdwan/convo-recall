"""recall — CLI entry point for convo-recall."""

import argparse
import sys

from . import __version__, ingest, install as _install


def _expand_list(raw: str) -> set[int]:
    """argparse type for --expand: parses 'N[,N…]' into a set of ints.

    Validated at parse time so the CLI fails fast — before open_db() is
    called and before any DB access. Bad input → argparse rejects with
    exit code 2.
    """
    out: set[int] = set()
    for tok in (raw or "").split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            out.add(int(tok))
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"--expand expects integers (e.g. 3 or 1,4,7), got {tok!r}"
            )
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="recall",
        description="Searchable memory for Claude Code sessions",
    )
    parser.add_argument("--version", action="version", version=f"recall {__version__}")
    sub = parser.add_subparsers(dest="cmd")

    p_install = sub.add_parser(
        "install",
        help="Interactive setup wizard: watchers, embed sidecar, hooks, initial ingest",
    )
    p_install.add_argument("--dry-run", action="store_true",
                           help="Print what would happen without doing it")
    p_install.add_argument("--with-embeddings", action="store_true",
                           help="Skip the embed-sidecar question and enable it (requires [embeddings] extra)")
    p_install.add_argument("-y", "--non-interactive", action="store_true",
                           help="Accept default for every question (CI / scripted installs)")
    p_install.add_argument("--scheduler",
                           choices=["auto", "launchd", "systemd", "cron", "polling"],
                           default="auto",
                           help="Override scheduler tier (default: auto-detect).")

    p_install_hooks = sub.add_parser(
        "install-hooks",
        help="Wire convo-recall pre-prompt hooks into Claude/Codex/Gemini settings files",
    )
    p_install_hooks.add_argument("--agent", action="append",
                                  choices=["claude", "codex", "gemini"],
                                  help="Limit to one CLI; repeatable")
    p_install_hooks.add_argument("--dry-run", action="store_true",
                                  help="Print what would change without writing")
    p_install_hooks.add_argument("-y", "--non-interactive", action="store_true",
                                  help="Skip per-CLI confirmation prompt")

    p_uninstall_hooks = sub.add_parser(
        "uninstall-hooks",
        help="Remove convo-recall hook blocks from Claude/Codex/Gemini settings files",
    )
    p_uninstall_hooks.add_argument("--agent", action="append",
                                    choices=["claude", "codex", "gemini"],
                                    help="Limit to one CLI; repeatable")

    p_uninstall = sub.add_parser(
        "uninstall",
        help="Stop and remove convo-recall watchers across all schedulers",
    )
    p_uninstall.add_argument("--purge-data", action="store_true",
                             help="Also delete the conversation DB and data "
                                  "directory (DRY-RUN unless --confirm is also given)")
    p_uninstall.add_argument("--confirm", action="store_true",
                             help="Required alongside --purge-data to actually "
                                  "delete. Without it, --purge-data shows a "
                                  "preview and exits without touching anything.")

    p_serve = sub.add_parser("serve", help="Start the embedding sidecar (blocks until Ctrl-C)")
    p_serve.add_argument("--sock", default=None,
                         help="Override socket path (default: CONVO_RECALL_SOCK or ~/.local/share/convo-recall/embed.sock)")
    p_serve.add_argument("--model", default="BAAI/bge-large-en-v1.5",
                         help="Sentence-transformers model name")

    p_ingest = sub.add_parser("ingest", help="Scan and ingest new/updated conversations")
    p_ingest.add_argument("--agent", "-a", default=None,
                          help="Only ingest one agent (claude / gemini / codex). "
                               "Default: read enabled agents from config.json (or claude only).")

    p_watch = sub.add_parser("watch",
                             help="Polling watcher loop for sandbox / Linux (no launchd). "
                                  "Re-runs ingest every N seconds.")
    p_watch.add_argument("--interval", "-i", type=int, default=10, metavar="SEC",
                         help="Poll interval in seconds (default 10)")
    p_watch.add_argument("--verbose", action="store_true",
                         help="Verbose output for each tick.")
    sub.add_parser("embed-backfill", help="Generate embeddings for all un-embedded messages")
    sub.add_parser(
        "_backfill-chain",
        help=argparse.SUPPRESS,  # private; spawned by `recall install` wizard
    )
    p_bf_clean = sub.add_parser(
        "backfill-clean",
        help="Re-clean all stored messages and rebuild FTS "
             "(DRY-RUN unless --confirm)",
    )
    p_bf_clean.add_argument("--confirm", action="store_true",
                            help="Skip the interactive prompt and apply mutations.")
    p_bf_redact = sub.add_parser(
        "backfill-redact",
        help="Re-apply secret redaction to all stored messages and rebuild FTS "
             "(DRY-RUN unless --confirm)",
    )
    p_bf_redact.add_argument("--confirm", action="store_true",
                             help="Skip the interactive prompt and apply mutations.")
    p_chunk = sub.add_parser(
        "chunk-backfill",
        help="Re-embed long messages with chunked mean-pooling "
             "(DRY-RUN unless --confirm)",
    )
    p_chunk.add_argument("--confirm", action="store_true",
                         help="Skip the interactive prompt and re-embed.")
    sub.add_parser("tool-error-backfill", help="Index tool_result error blocks from all JSONL files")
    sub.add_parser("stats", help="Show DB statistics")

    p_doctor = sub.add_parser("doctor", help="Run DB health checks")
    p_doctor.add_argument("--scan-secrets", action="store_true",
                          help="Count credential-shaped tokens in existing rows")

    p_forget = sub.add_parser(
        "forget",
        help="Delete messages by scope (session, pattern, project, etc.). "
             "Dry-run by default; pass --confirm to actually delete.",
    )
    g = p_forget.add_mutually_exclusive_group(required=True)
    g.add_argument("--session", help="Delete all messages for one session_id")
    g.add_argument("--pattern", help="Delete all messages whose content matches a regex")
    g.add_argument("--before", help="Delete all messages with timestamp < YYYY-MM-DD")
    g.add_argument("--project", help="Delete all messages from one project slug")
    g.add_argument("--agent", help="Delete all messages from one agent (claude/gemini/codex)")
    g.add_argument("--uuid", help="Delete a single message by uuid")
    p_forget.add_argument("--confirm", action="store_true",
                          help="Without this flag, forget runs in dry-run mode (no deletion)")

    p_tail = sub.add_parser(
        "tail",
        help="Print the last N user/assistant messages from the most "
             "recent session (chronological, newest at the bottom)",
    )
    p_tail.add_argument("n", nargs="?", type=int, default=30,
                        help="Number of messages (default 30)")
    p_tail.add_argument("--session", default=None,
                        help="Specific session_id (default: latest matching project/agent)")
    p_tail.add_argument("--project", "-p", default=None,
                        help="Filter to a project slug. Defaults to current "
                             "directory if inside a Projects/ subtree.")
    p_tail.add_argument("--all-projects", action="store_true",
                        help="Pick the latest session across all projects")
    p_tail.add_argument("--agent", "-a", default=None,
                        help="Filter to one agent (claude / gemini / codex)")
    p_tail.add_argument("--roles", default="user,assistant",
                        help="Comma-separated roles to include (default: user,assistant; "
                             "use 'all' for user,assistant,tool_error)")
    p_tail.add_argument("--width", type=int, default=220, metavar="N",
                        help="Truncate each message body to N chars (default 220). "
                             "Bypassed for messages listed in --expand.")
    p_tail.add_argument("--cols", type=int, default=76, metavar="N",
                        help="Wrap message body to N columns (default 76)")
    p_tail.add_argument("--expand", type=_expand_list, default=set(),
                        metavar="N[,N…]",
                        help="Comma-separated turn numbers to print in full "
                             "(no truncation, no inline collapse)")
    p_tail.add_argument("--ascii", action="store_true",
                        help="Use ASCII glyphs (|, -, ->, ...) instead of "
                             "Unicode (│, ·, ↳, …) for terminals that don't render box chars")
    p_tail.add_argument("--json", action="store_true",
                        help="Emit machine-readable JSON instead of formatted text")

    p_search = sub.add_parser("search", help="Hybrid search (FTS5 + vector if available)")
    p_search.add_argument("query")
    p_search.add_argument("-n", type=int, default=10, metavar="N",
                          help="Number of results (default 10)")
    p_search.add_argument("--recent", action="store_true",
                          help="Boost recent conversations (90-day half-life decay)")
    p_search.add_argument("--project", "-p", default=None,
                          help="Filter to a project slug (e.g. apps_noema). "
                               "Defaults to current directory if inside a Projects/ subtree.")
    p_search.add_argument("--all-projects", action="store_true",
                          help="Search across all projects (overrides cwd auto-detect)")
    p_search.add_argument("--context", "-c", type=int, default=1, metavar="N",
                          help="Show N messages before/after each result (default 1, 0 to disable)")
    p_search.add_argument("--agent", "-a", default=None,
                          help="Filter to messages from a single agent (claude / gemini / codex)")
    p_search.add_argument("--json", action="store_true",
                          help="Emit machine-readable JSON instead of human-formatted text")

    args = parser.parse_args()

    if args.cmd is None:
        parser.print_help()
        sys.exit(0)

    # Commands that don't need the DB open
    if args.cmd == "install":
        _install.run(dry_run=getattr(args, "dry_run", False),
                     with_embeddings=getattr(args, "with_embeddings", False),
                     non_interactive=getattr(args, "non_interactive", False),
                     scheduler=getattr(args, "scheduler", "auto"))
        return

    if args.cmd == "uninstall":
        _install.uninstall(
            purge_data=getattr(args, "purge_data", False),
            confirm=getattr(args, "confirm", False),
        )
        return

    if args.cmd == "install-hooks":
        _install.install_hooks(
            agents=getattr(args, "agent", None),
            dry_run=getattr(args, "dry_run", False),
            non_interactive=getattr(args, "non_interactive", False),
        )
        return

    if args.cmd == "uninstall-hooks":
        _install.uninstall_hooks(agents=getattr(args, "agent", None))
        return

    if args.cmd == "serve":
        try:
            from . import embed_service
        except ImportError:
            print("error: embedding dependencies not installed.\n"
                  "Run: pipx install 'convo-recall[embeddings]'", file=sys.stderr)
            sys.exit(1)
        from pathlib import Path
        sock = Path(args.sock) if args.sock else None
        embed_service.serve(sock_path=sock, model_name=args.model)
        return

    con = ingest.open_db()
    try:
        if args.cmd == "ingest":
            if args.agent:
                ingest.scan_one_agent(con, args.agent, verbose=True)
            else:
                ingest.scan_all(con, verbose=True)
        elif args.cmd == "watch":
            ingest.watch_loop(con, interval=args.interval, verbose=args.verbose)
        elif args.cmd == "embed-backfill":
            ingest.embed_backfill(con)
        elif args.cmd == "_backfill-chain":
            # Private: spawned detached by `recall install` so the wizard
            # can return control to the user immediately. Runs ingest →
            # embed-backfill in sequence and updates the progress file
            # at each step. Both phases are pre-declared so `recall stats`
            # renders both bars (showing the user that two phases are
            # queued up — even if one ends up doing nothing).
            from . import _progress
            _progress.start_run([
                ("ingest", 0),           # total filled in by _dispatch_ingest
                ("embed-backfill", 0),   # total filled in by embed_backfill
            ])
            try:
                ingest.scan_all(con, verbose=True)
                ingest.embed_backfill(con)
            finally:
                _progress.finish_run()
        elif args.cmd == "backfill-clean":
            ingest.backfill_clean(con, confirm=getattr(args, "confirm", False))
        elif args.cmd == "backfill-redact":
            ingest.backfill_redact(con, confirm=getattr(args, "confirm", False))
        elif args.cmd == "chunk-backfill":
            ingest.chunk_backfill(con, confirm=getattr(args, "confirm", False))
        elif args.cmd == "tool-error-backfill":
            ingest.tool_error_backfill(con)
        elif args.cmd == "stats":
            ingest.stats(con)
        elif args.cmd == "doctor":
            ingest.doctor(con, scan_secrets=getattr(args, "scan_secrets", False))
        elif args.cmd == "forget":
            ingest.forget(
                con,
                session=args.session,
                pattern=args.pattern,
                before=args.before,
                project=args.project,
                agent=args.agent,
                uuid=args.uuid,
                confirm=args.confirm,
            )
        elif args.cmd == "search":
            # --all-projects skips the cwd auto-scope ONLY. An explicit
            # --project X is always honored, even alongside --all-projects.
            if args.project:
                project = args.project
            elif args.all_projects:
                project = None
            else:
                project = ingest.slug_from_cwd()
            ingest.search(con, args.query, args.n,
                          recent=args.recent,
                          project=project,
                          context=args.context,
                          agent=args.agent,
                          json_=getattr(args, "json", False))
        elif args.cmd == "tail":
            if args.project:
                project = args.project
            elif args.all_projects:
                project = None
            else:
                project = ingest.slug_from_cwd()
            roles_arg = (args.roles or "").strip().lower()
            if roles_arg == "all":
                roles = ("user", "assistant", "tool_error")
            else:
                roles = tuple(r.strip() for r in roles_arg.split(",") if r.strip())
            rc = ingest.tail(con, n=args.n,
                             session=args.session,
                             project=project,
                             agent=args.agent,
                             roles=roles,
                             width=args.width,
                             expand=args.expand,
                             ascii_only=getattr(args, "ascii", False),
                             cols=args.cols,
                             json_=getattr(args, "json", False))
            sys.exit(rc)
        else:
            parser.print_help()
            sys.exit(1)
    finally:
        ingest.close_db(con)
