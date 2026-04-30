"""recall — CLI entry point for convo-recall."""

import argparse
import sys

from . import __version__, ingest, install as _install


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
                             help="Also delete the conversation DB and data directory")

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
    sub.add_parser("backfill-clean", help="Re-clean all stored messages and rebuild FTS")
    sub.add_parser("backfill-redact", help="Re-apply secret redaction to all stored messages and rebuild FTS")
    sub.add_parser("chunk-backfill", help="Re-embed long messages with chunked mean-pooling")
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
        _install.uninstall(purge_data=getattr(args, "purge_data", False))
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
        elif args.cmd == "backfill-clean":
            ingest.backfill_clean(con)
        elif args.cmd == "backfill-redact":
            ingest.backfill_redact(con)
        elif args.cmd == "chunk-backfill":
            ingest.chunk_backfill(con)
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
        else:
            parser.print_help()
            sys.exit(1)
    finally:
        ingest.close_db(con)
