"""convo-recall: searchable memory for AI coding-agent sessions.

Indexes conversations from multiple agents (Claude, Gemini, Codex) into a
single hybrid FTS5 + vector search index.
"""

from .ingest import (
    open_db,
    close_db,
    ingest_file,
    ingest_gemini_file,
    ingest_codex_file,
    scan_all,
    scan_one_agent,
    watch_loop,
    search,
    embed,
    detect_agents,
    load_config,
    save_config,
    DB_PATH,
    PROJECTS_DIR,
    GEMINI_TMP,
    CODEX_SESSIONS,
    SUPPORTED_AGENTS,
)

__version__ = "0.2.0"
__all__ = [
    "__version__",
    "open_db", "close_db",
    "ingest_file", "ingest_gemini_file", "ingest_codex_file",
    "scan_all", "scan_one_agent", "watch_loop",
    "search", "embed",
    "detect_agents", "load_config", "save_config",
    "DB_PATH", "PROJECTS_DIR", "GEMINI_TMP", "CODEX_SESSIONS",
    "SUPPORTED_AGENTS",
]
