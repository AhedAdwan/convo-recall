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
        help="Set up launchd watchers and run initial ingest (macOS)",
    )
    p_install.add_argument("--dry-run", action="store_true",
                           help="Print what would happen without doing it")
    p_install.add_argument("--with-embeddings", action="store_true",
                           help="Also install the embedding sidecar (requires [embeddings] extra)")

    p_uninstall = sub.add_parser(
        "uninstall",
        help="Stop and remove launchd agents (macOS)",
    )
    p_uninstall.add_argument("--purge-data", action="store_true",
                             help="Also delete the conversation DB and data directory")

    p_serve = sub.add_parser("serve", help="Start the embedding sidecar (blocks until Ctrl-C)")
    p_serve.add_argument("--sock", default=None,
                         help="Override socket path (default: CONVO_RECALL_SOCK or ~/.local/share/convo-recall/embed.sock)")
    p_serve.add_argument("--model", default="BAAI/bge-large-en-v1.5",
                         help="Sentence-transformers model name")

    sub.add_parser("ingest", help="Scan and ingest new/updated conversations")
    sub.add_parser("embed-backfill", help="Generate embeddings for all un-embedded messages")
    sub.add_parser("backfill-clean", help="Re-clean all stored messages and rebuild FTS")
    sub.add_parser("chunk-backfill", help="Re-embed long messages with chunked mean-pooling")
    sub.add_parser("tool-error-backfill", help="Index tool_result error blocks from all JSONL files")
    sub.add_parser("stats", help="Show DB statistics")

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

    args = parser.parse_args()

    if args.cmd is None:
        parser.print_help()
        sys.exit(0)

    # Commands that don't need the DB open
    if args.cmd == "install":
        _install.run(dry_run=getattr(args, "dry_run", False),
                     with_embeddings=getattr(args, "with_embeddings", False))
        return

    if args.cmd == "uninstall":
        _install.uninstall(purge_data=getattr(args, "purge_data", False))
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
            ingest.scan_all(con, verbose=True)
        elif args.cmd == "embed-backfill":
            ingest.embed_backfill(con)
        elif args.cmd == "backfill-clean":
            ingest.backfill_clean(con)
        elif args.cmd == "chunk-backfill":
            ingest.chunk_backfill(con)
        elif args.cmd == "tool-error-backfill":
            ingest.tool_error_backfill(con)
        elif args.cmd == "stats":
            ingest.stats(con)
        elif args.cmd == "search":
            project = None
            if not args.all_projects:
                project = args.project or ingest.slug_from_cwd()
            ingest.search(con, args.query, args.n,
                          recent=args.recent,
                          project=project,
                          context=args.context)
        else:
            parser.print_help()
            sys.exit(1)
    finally:
        ingest.close_db(con)
