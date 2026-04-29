"""convo-recall: searchable memory for Claude Code sessions."""

from .ingest import (
    open_db,
    close_db,
    ingest_file,
    scan_all,
    search,
    embed,
    slug_from_cwd,
    DB_PATH,
    PROJECTS_DIR,
)

__version__ = "0.1.0"
__all__ = [
    "__version__",
    "open_db", "close_db", "ingest_file", "scan_all",
    "search", "embed", "slug_from_cwd",
    "DB_PATH", "PROJECTS_DIR",
]
