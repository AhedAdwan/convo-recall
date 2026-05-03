"""
Core ingestion, search, and backfill logic for convo-recall.

Paths default to standard Claude Code locations but are configurable
via environment variables:
  CONVO_RECALL_DB       — path to SQLite DB (default ~/.local/share/convo-recall/conversations.db)
  CONVO_RECALL_PROJECTS — path to Claude projects dir (default ~/.claude/projects)
  CONVO_RECALL_SOCK     — path to embed UDS socket (default ~/.local/share/convo-recall/embed.sock)
"""

import os
from pathlib import Path

# ── Test-monkeypatched constants (stay defined here through v0.4.0) ──────────
# Tests heavily monkeypatch `ingest.{DB_PATH, EMBED_SOCK, PROJECTS_DIR,
# GEMINI_TMP, CODEX_SESSIONS, _CONFIG_PATH, _GEMINI_ALIAS_PATH, SUPPORTED_AGENTS}`.
# Defining them on the package init means a single `monkeypatch.setattr(
# ingest, "X", ...)` is the canonical override point — submodules read these
# via lazy `from .. import ingest as _pkg; _pkg.X` inside function bodies.
# `tests/test_ingest_docstring_truth.py` reloads this module with env vars
# cleared, so each constant must come from `os.environ.get(...)` here.
# A8 finalizes the moves to `db.py` / `embed.py` / `ingest/scan.py` / etc.
# when test fixtures rewire off the back-compat shim.
DB_PATH = Path(os.environ.get("CONVO_RECALL_DB",
               Path.home() / ".local" / "share" / "convo-recall" / "conversations.db"))
PROJECTS_DIR = Path(os.environ.get("CONVO_RECALL_PROJECTS",
                    Path.home() / ".claude" / "projects"))
GEMINI_TMP = Path(os.environ.get("CONVO_RECALL_GEMINI_TMP",
                  Path.home() / ".gemini" / "tmp"))
CODEX_SESSIONS = Path(os.environ.get("CONVO_RECALL_CODEX_SESSIONS",
                      Path.home() / ".codex" / "sessions"))
EMBED_SOCK = Path(os.environ.get("CONVO_RECALL_SOCK",
                  Path.home() / ".local" / "share" / "convo-recall" / "embed.sock"))
_CONFIG_PATH = Path(os.environ.get("CONVO_RECALL_CONFIG",
                    Path.home() / ".local" / "share" / "convo-recall" / "config.json"))
_GEMINI_ALIAS_PATH = Path(os.environ.get(
    "CONVO_RECALL_GEMINI_ALIASES",
    Path.home() / ".local" / "share" / "convo-recall" / "gemini-aliases.json",
))

# Built-in agents and how to find their session files.
SUPPORTED_AGENTS = ("claude", "gemini", "codex")


# ── Project-identity helpers (extracted to identity.py in v0.4.0; TD-008) ────
from ..identity import (
    _ROOT_MARKERS,
    _project_id,
    _display_name,
    _legacy_project_id,
    _legacy_claude_slug,
    _legacy_codex_slug,
    _legacy_gemini_slug,
    _gemini_hash_project_id,
    _scan_claude_cwd,
    _scan_codex_cwd,
    _scan_gemini_cwd,
)

# ── DB / migrations / connection (extracted to db.py in v0.4.0; TD-008) ──────
from ..db import (
    EMBED_DIM,
    _VEC_ENABLED,
    _vec_ok,
    _vc,
    _Row,
    _row_factory,
    _harden_perms,
    _enable_wal_mode,
    open_db,
    close_db,
    _init_schema,
    _upsert_project,
    _has_column,
    _ensure_migrations_table,
    _migration_applied,
    _record_migration,
    _MIGRATION_AGENT_COLUMN,
    _MIGRATION_FTS_PORTER,
    _MIGRATION_PROJECT_ID,
    _migrate_add_agent_column,
    _migrate_fts_porter,
    _migrate_project_id,
    _init_vec_tables,
)

# ── Embed UDS client + vec helpers (extracted to embed.py in v0.4.0; TD-008) ─
from ..embed import (
    _EMBED_TIMEOUT_S,
    _UnixHTTPConn,
    embed,
    _vec_bytes,
    _wait_for_embed_socket,
    _vec_insert,
    _vec_search,
    _vec_count,
)

# ── Read-path: search / tail / RRF (extracted to query.py in v0.4.0; TD-008) ─
from ..query import (
    MAX_QUERY_LEN,
    RRF_K,
    DECAY_HALF_LIFE_DAYS,
    _decay,
    _safe_fts_query,
    _resolve_project_ids,
    _resolve_tail_session,
    _fetch_context,
    _DEFAULT_TAIL_N,
    _TAIL_WIDTH,
    _TAIL_BODY_COLS,
    _TAIL_ROLES,
    _TAIL_USER_LABEL,
    _TAIL_GLYPHS,
    _tail_parse_ts,
    _tail_format_ago,
    _tail_clock,
    _tail_session_range,
    _tail_wrap,
    search,
    tail,
)

# ── Backfill commands (extracted to backfill.py in v0.4.0; TD-008) ───────────
from ..backfill import (
    embed_backfill,
    _confirm_destructive,
    backfill_clean,
    backfill_redact,
    chunk_backfill,
    _backfill_insert_tool_error,
    _backfill_claude_tool_errors,
    _backfill_codex_tool_errors,
    _backfill_gemini_tool_errors,
    tool_error_backfill,
)

# ── Admin commands (extracted to admin.py in v0.4.0; TD-008) ─────────────────
from ..admin import (
    _BAK_STALE_AGE_DAYS,
    _scan_stale_bak_files,
    doctor,
    forget,
    _render_phase_bar,
    _render_progress_bar,
    stats,
)

# ── Sibling write-path modules (NEW in v0.4.0 / A7) ──────────────────────────
from .writer import (
    _ANSI_RE,
    _CR_ERASE_RE,
    _XML_PAIR_RE,
    _XML_SOLO_RE,
    _BOX_BRAILLE_RE,
    _BLANK_LINES_RE,
    _TEXT_BLOCK_TYPES,
    _expand_code_tokens,
    _clean_content,
    _extract_text,
    _upsert_session,
    _upsert_ingested_file,
    _persist_message,
)
from .claude import (
    _ERROR_PATTERNS,
    _is_error_result,
    _extract_tool_result_text,
    _session_id_from_path,
    _iter_claude_files,
    ingest_file,
)
from .gemini import (
    _gemini_record_error,
    _gemini_tool_call_error,
    _load_gemini_aliases,
    _iter_gemini_files,
    ingest_gemini_file,
)
from .codex import (
    _codex_event_msg_error,
    _codex_fco_error,
    _iter_codex_files,
    ingest_codex_file,
)
from .scan import (
    _AGENT_INGEST,
    _AGENT_ITERATORS,
    _AGENT_SOURCE_PATHS,
    detect_agents,
    load_config,
    save_config,
    _dispatch_ingest,
    scan_one_agent,
    scan_all,
    watch_loop,
)
