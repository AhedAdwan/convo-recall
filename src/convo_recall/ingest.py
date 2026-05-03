"""
Core ingestion, search, and backfill logic for convo-recall.

Paths default to standard Claude Code locations but are configurable
via environment variables:
  CONVO_RECALL_DB       — path to SQLite DB (default ~/.local/share/convo-recall/conversations.db)
  CONVO_RECALL_PROJECTS — path to Claude projects dir (default ~/.claude/projects)
  CONVO_RECALL_SOCK     — path to embed UDS socket (default ~/.local/share/convo-recall/embed.sock)
"""

import hashlib
import json
import os
import re
import sys

import apsw
from datetime import datetime, timezone
from pathlib import Path

from . import redact as _redact

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

# Built-in agents and how to find their session files.
SUPPORTED_AGENTS = ("claude", "gemini", "codex")


# ── Project-identity helpers (extracted to identity.py in v0.4.0; TD-008) ────
# Re-exported here so legacy `from convo_recall.ingest import _project_id, ...`
# keeps working through one release. Removed in v0.5.0.
from .identity import (
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

# ── Admin commands (extracted to admin.py in v0.4.0; TD-008) ─────────────────
# Re-exported so legacy `from convo_recall.ingest import stats, doctor, forget`
# keeps working through one release. Removed in v0.5.0. doctor() and stats()
# read `_ing.DB_PATH` / `_ing.EMBED_SOCK` at call time (see admin.py docstring).
from .admin import (
    _BAK_STALE_AGE_DAYS,
    _scan_stale_bak_files,
    doctor,
    forget,
    _render_phase_bar,
    _render_progress_bar,
    stats,
)

# ── Backfill commands (extracted to backfill.py in v0.4.0; TD-008) ───────────
# Re-exported so legacy `from convo_recall.ingest import embed_backfill, ...`
# keeps working through one release. Removed in v0.5.0. backfill.py functions
# lazy-import write-path helpers (`_clean_content`, `_iter_*_files`,
# per-agent error extractors) from ingest at call time — those still live
# in ingest.py through A6 and move to ingest/{writer,scan,...}.py in A7.
from .backfill import (
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

# ── Read-path: search / tail / RRF (extracted to query.py in v0.4.0; TD-008) ─
# Re-exported so legacy `from convo_recall.ingest import search, tail, ...`
# keeps working through one release. Removed in v0.5.0. search() reads
# `_ing.EMBED_SOCK` at call time so test fixtures patching ingest.EMBED_SOCK
# reach this codepath (see query.py docstring).
from .query import (
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

# ── DB / migrations / connection (extracted to db.py in v0.4.0; TD-008) ──────
# Re-exported so `from convo_recall.ingest import open_db, DB_PATH, ...` keeps
# working through one release. Removed in v0.5.0. Tests that monkeypatch
# `ingest.DB_PATH` / `ingest._enable_wal_mode` / `ingest._record_migration`
# still take effect because db.py reads those names through the ingest
# module at call time (see db.py docstring).
# ── Embed UDS client + vec helpers (extracted to embed.py in v0.4.0; TD-008) ─
# Re-exported so legacy `from convo_recall.ingest import embed, _vec_search, ...`
# keeps working through one release. Removed in v0.5.0.
from .embed import (
    _EMBED_TIMEOUT_S,
    _UnixHTTPConn,
    embed,
    _vec_bytes,
    _wait_for_embed_socket,
    _vec_insert,
    _vec_search,
    _vec_count,
)

from .db import (
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


# ── Content cleaning ──────────────────────────────────────────────────────────

_ANSI_RE        = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')
_CR_ERASE_RE    = re.compile(r'\r\x1b\[K')
_XML_PAIR_RE    = re.compile(
    r'<(?:command-name|local-command-stdout|local-command-caveat'
    r'|command-message|command-args)(?:\s[^>]*)?>.*?'
    r'</(?:command-name|local-command-stdout|local-command-caveat'
    r'|command-message|command-args)>',
    re.DOTALL,
)
_XML_SOLO_RE    = re.compile(
    r'</?(?:command-name|local-command-stdout|local-command-caveat'
    r'|command-message|command-args)(?:\s[^>]*)?>'
)
_BOX_BRAILLE_RE = re.compile(r'[╔╗╚╝║═─│┌┐└┘├┤┬┴┼━┃⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]')
_BLANK_LINES_RE = re.compile(r'\n{3,}')


def _expand_code_tokens(text: str) -> str:
    text = re.sub(r'([a-z])([A-Z])', r'\1 \2', text)  # camelCase → camel Case
    text = re.sub(r'_([a-z])', r' \1', text)            # snake_case → snake case
    return text


def _clean_content(text: str) -> str:
    text = _CR_ERASE_RE.sub('', text)
    text = _ANSI_RE.sub('', text)
    text = _XML_PAIR_RE.sub('', text)
    text = _XML_SOLO_RE.sub('', text)
    text = _BOX_BRAILLE_RE.sub('', text)
    if os.environ.get("CONVO_RECALL_REDACT") != "off":
        text = _redact.redact_secrets(text)
    text = _BLANK_LINES_RE.sub('\n\n', text)
    text = _expand_code_tokens(text)
    return text.strip()


# ── Error detection ───────────────────────────────────────────────────────────

_ERROR_PATTERNS = re.compile(
    r'(Error:|TypeError|ECONNREFUSED|Traceback|FAILED|AssertionError|'
    r'npm ERR!|cargo error|\bat\s+\w.*:\d+|Exit code [1-9])',
    re.I,
)


def _is_error_result(content: str) -> bool:
    return bool(_ERROR_PATTERNS.search(content))


def _extract_tool_result_text(block: dict) -> str:
    c = block.get("content", "")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(b.get("text", "") for b in c
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


# ── Codex / Gemini tool_error extractors ──────────────────────────────────────
#
# The agent CLIs emit failures in agent-specific shapes, not Anthropic's
# `tool_result.is_error` schema. Each helper below is a pure function that
# decides whether one record represents a harvestable failure and returns
# the error text (truncated) if so. Used by the in-place ingesters and the
# tool_error_backfill walker.

def _codex_event_msg_error(rec: dict) -> tuple[str, str] | None:
    """Extract (kind, text) from a Codex event_msg record if it represents
    a harvestable failure, else None. Recognized shapes (sandbox-confirmed):
      - exec_command_end with non-zero exit_code (shell failures)
      - patch_apply_end with success=False (failed file edits)
      - error (stream/rate-limit/CLI errors)
      - turn_aborted (user interrupt)
    Returned text is prefixed with a bracketed source tag for FTS targeting.
    """
    if rec.get("type") != "event_msg":
        return None
    pl = rec.get("payload", {})
    if not isinstance(pl, dict):
        return None
    pt = pl.get("type")
    if pt == "exec_command_end":
        ec = pl.get("exit_code")
        if ec is None or ec == 0:
            return None
        body = pl.get("aggregated_output") or ""
        if not body:
            body = (pl.get("stdout") or "") + "\n" + (pl.get("stderr") or "")
        return ("exec", f"[exec_command_end exit={ec}]\n{body[:500]}")
    if pt == "patch_apply_end":
        if pl.get("success", True):
            return None
        body = pl.get("stderr") or pl.get("stdout") or ""
        return ("patch", f"[patch_apply_end]\n{body[:500]}")
    if pt == "error":
        msg = pl.get("message") or ""
        info = pl.get("codex_error_info") or ""
        text = msg if not info else f"{msg} ({info})"
        return ("error", f"[codex_error]\n{text[:500]}")
    if pt == "turn_aborted":
        reason = pl.get("reason", "unknown")
        dur = pl.get("duration_ms")
        suffix = f" (after {dur}ms)" if dur is not None else ""
        return ("abort", f"[turn_aborted]\nTurn aborted: {reason}{suffix}")
    return None


def _codex_fco_error(rec: dict) -> str | None:
    """Fallback extractor for Codex response_item.function_call_output records.
    Handles two output shapes:
      1. Older schema (~Sep 2025): output is JSON-string with metadata.exit_code.
      2. Newer schema (~2026): output is plain string; uses _is_error_result
         (catches "Process exited with code N").
    Returns truncated error text, else None.
    """
    if rec.get("type") != "response_item":
        return None
    pl = rec.get("payload", {})
    if not isinstance(pl, dict) or pl.get("type") != "function_call_output":
        return None
    out = pl.get("output", "")
    if not isinstance(out, str) or not out:
        return None
    try:
        obj = json.loads(out)
        if isinstance(obj, dict):
            ec = obj.get("metadata", {}).get("exit_code")
            if ec is not None and ec != 0:
                inner = obj.get("output", "")
                inner_s = inner if isinstance(inner, str) else str(inner)
                return f"[function_call_output exit={ec}]\n{inner_s[:500]}"
            return None  # Parsed cleanly, no error signal
    except (json.JSONDecodeError, ValueError):
        pass
    if _is_error_result(out):
        return f"[function_call_output]\n{out[:500]}"
    return None


def _gemini_record_error(rec: dict) -> tuple[str, str] | None:
    """Extract (kind, text) from a top-level Gemini message record if its
    type is 'error' or 'warning'. Content is always a plain string per the
    ConversationRecord schema.
    """
    rtype = rec.get("type")
    if rtype not in ("error", "warning"):
        return None
    content = rec.get("content", "")
    if not isinstance(content, str) or not content:
        return None
    kind = "cli_error" if rtype == "error" else "cli_warning"
    return (kind, f"[gemini_{rtype}]\n{content[:500]}")


def _gemini_tool_call_error(tc: dict) -> str | None:
    """Extract error text from a Gemini ToolCallRecord if its status indicates
    failure ('error' or 'cancelled'). The error string typically lives at
    tc.result[].functionResponse.response.error.
    """
    if not isinstance(tc, dict):
        return None
    status = tc.get("status")
    if status not in ("error", "cancelled"):
        return None
    name = tc.get("name", "<?>")
    err_text = ""
    result = tc.get("result")
    if isinstance(result, list):
        for r in result:
            if not isinstance(r, dict):
                continue
            fr = r.get("functionResponse", {})
            if isinstance(fr, dict):
                resp = fr.get("response", {})
                if isinstance(resp, dict):
                    err = resp.get("error")
                    if isinstance(err, str) and err:
                        err_text = err
                        break
    if not err_text:
        err_text = json.dumps(result)[:500] if result is not None else f"toolCall {status}"
    return f"[gemini_tool {name} status={status}]\n{err_text[:500]}"



# ── Path helpers ──────────────────────────────────────────────────────────────
# (_legacy_claude_slug moved to identity.py in v0.4.0; re-exported above.)


def _session_id_from_path(jsonl_path: Path) -> str:
    if jsonl_path.parent.name == "subagents":
        return jsonl_path.parent.parent.name
    return jsonl_path.stem


_TEXT_BLOCK_TYPES = {"text", "input_text", "output_text", None}


def _extract_text(content) -> str:
    """Extract human-readable text from a content payload.

    Accepts (a) a plain string, or (b) a list of dict blocks. For dict blocks,
    pulls the `text` field when the `type` is text-like (text/input_text/
    output_text) OR when there is no `type` key at all (gemini's shape: a
    bare `[{"text": "..."}]`). Tool-use blocks and reasoning blocks have
    other type strings and are intentionally excluded.
    """
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") in _TEXT_BLOCK_TYPES:
                t = b.get("text", "").strip()
                if t:
                    parts.append(t)
        return "\n".join(parts)
    return ""


# (_ROOT_MARKERS, _project_id, _display_name moved to identity.py in v0.4.0;
# re-exported above.)


# ── Agent detection + per-agent file iteration ───────────────────────────────

def _iter_claude_files(projects_dir: Path = None):
    base = Path(projects_dir) if projects_dir else PROJECTS_DIR
    if not base.exists():
        return
    for project_dir in base.iterdir():
        if not project_dir.is_dir():
            continue
        for pattern in ("*.jsonl", "*/subagents/*.jsonl"):
            yield from project_dir.glob(pattern)


def _iter_gemini_files(gemini_tmp: Path = None):
    base = Path(gemini_tmp) if gemini_tmp else GEMINI_TMP
    if not base.exists():
        return
    yield from base.glob("*/chats/session-*.jsonl")


def _iter_codex_files(codex_sessions: Path = None):
    base = Path(codex_sessions) if codex_sessions else CODEX_SESSIONS
    if not base.exists():
        return
    # Date-clustered: ~/.codex/sessions/{YYYY}/{MM}/{DD}/rollout-*.jsonl.
    # Skip ~/.codex/history.jsonl (lossy: rollout files are source of truth).
    yield from base.glob("*/*/*/rollout-*.jsonl")


_AGENT_ITERATORS = {
    "claude": _iter_claude_files,
    "gemini": _iter_gemini_files,
    "codex":  _iter_codex_files,
}

_AGENT_SOURCE_PATHS = {
    "claude": lambda: PROJECTS_DIR,
    "gemini": lambda: GEMINI_TMP,
    "codex":  lambda: CODEX_SESSIONS,
}


def detect_agents() -> list[dict]:
    """Return a list of {name, path, file_count} for each supported agent.

    Agents whose source dir doesn't exist report file_count=0 (they're 'absent'
    from this machine). Callers typically filter to file_count > 0 when
    showing a detection prompt.
    """
    result = []
    for name in SUPPORTED_AGENTS:
        path = _AGENT_SOURCE_PATHS[name]()
        if not path.exists():
            result.append({"name": name, "path": str(path), "file_count": 0})
            continue
        count = sum(1 for _ in _AGENT_ITERATORS[name](path))
        result.append({"name": name, "path": str(path), "file_count": count})
    return result


def load_config() -> dict:
    """Load `~/.local/share/convo-recall/config.json` or return defaults.

    Also re-chmod the file to 0o600 if it was created with a wider mode
    (e.g. by a shell `echo > config.json` that bypassed `save_config`).
    """
    if not _CONFIG_PATH.exists():
        return {"agents": ["claude"]}  # default — preserves pre-multi-agent behavior
    _harden_perms(_CONFIG_PATH, 0o600)
    try:
        return json.loads(_CONFIG_PATH.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f"[warn] config read failed ({e}); using defaults", file=sys.stderr)
        return {"agents": ["claude"]}


def save_config(cfg: dict) -> None:
    """Persist config atomically with mode 0o600."""
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _CONFIG_PATH.with_suffix(_CONFIG_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(_CONFIG_PATH)


# ── Ingestion ─────────────────────────────────────────────────────────────────

def ingest_file(con: apsw.Connection, jsonl_path: Path,
                do_embed: bool = True, agent: str = "claude") -> int:
    stat = jsonl_path.stat()
    file_key = str(jsonl_path)

    row = con.execute(
        "SELECT lines_ingested, last_modified FROM ingested_files WHERE file_path = ?",
        (file_key,),
    ).fetchone()

    if row and row["last_modified"] == stat.st_mtime:
        return 0

    lines_already = row["lines_ingested"] if row else 0
    session_id = _session_id_from_path(jsonl_path)

    # Pre-scan for cwd: Claude records carry a cwd field on user/attachment
    # rows. First-found wins. Falls back to the lossy slug encoding.
    recovered_cwd: str | None = None
    try:
        with open(jsonl_path, "r", errors="replace") as f:
            for i, line in enumerate(f):
                if i > 200:
                    break
                try:
                    d = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(d, dict) and d.get("cwd"):
                    recovered_cwd = d["cwd"]
                    break
    except OSError:
        pass

    if recovered_cwd:
        project_id = _project_id(recovered_cwd)
        display_name = _display_name(recovered_cwd)
        cwd_real = os.path.realpath(recovered_cwd)
    else:
        legacy = _legacy_claude_slug(jsonl_path)
        project_id = _legacy_project_id(legacy)
        display_name = legacy
        cwd_real = None
    _upsert_project(con, project_id, display_name, cwd_real)

    inserted = 0
    malformed = 0
    title = None
    lines_read = 0

    with open(jsonl_path, "r", errors="replace") as f:
        for lineno, raw in enumerate(f):
            lines_read = lineno + 1
            if lineno < lines_already:
                if lineno < 5:
                    try:
                        rec = json.loads(raw)
                        if rec.get("type") == "custom-title":
                            title = rec.get("customTitle")
                    except (json.JSONDecodeError, ValueError):
                        pass
                continue

            try:
                rec = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                malformed += 1
                continue

            rtype = rec.get("type")
            if rtype == "custom-title":
                title = rec.get("customTitle")
                continue
            if rtype not in ("user", "assistant"):
                continue
            if rec.get("isMeta"):
                continue

            msg = rec.get("message", {})
            role = msg.get("role", rtype)
            raw_text = _extract_text(msg.get("content", ""))
            text = _clean_content(raw_text)

            uuid = rec.get("uuid", f"{session_id}:{lineno}")
            timestamp = rec.get("timestamp")
            model = msg.get("model") if role == "assistant" else None

            if text:
                inserted += _persist_message(
                    con, agent, project_id, session_id, uuid, role, text,
                    timestamp, do_embed, model=model,
                )

            # Index tool_result error blocks within user messages. This runs
            # independently of `text` — modern Claude Code emits user records
            # whose content is ONLY a tool_result block (no accompanying text),
            # so an early-out on empty `text` would silently drop every
            # tool error. See TD-006.
            if rtype == "user":
                content_blocks = msg.get("content", [])
                if isinstance(content_blocks, list):
                    for block in content_blocks:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") != "tool_result":
                            continue
                        raw_tr = _extract_tool_result_text(block)
                        if not raw_tr:
                            continue
                        if not (block.get("is_error", False) or _is_error_result(raw_tr)):
                            continue
                        tool_use_id = block.get("tool_use_id", f"tr{lineno}")
                        tr_uuid = f"{session_id}:tr:{tool_use_id}"
                        tr_text = _clean_content(raw_tr[:500])
                        if not tr_text:
                            continue
                        inserted += _persist_message(
                            con, agent, project_id, session_id, tr_uuid,
                            "tool_error", tr_text, timestamp, do_embed,
                        )

    now = datetime.now(timezone.utc).isoformat()
    _upsert_session(con, agent, project_id, session_id, title, now, now)
    _upsert_ingested_file(con, agent, file_key, session_id, project_id,
                           lines_read, stat.st_mtime)
    if malformed:
        print(f"[warn] {malformed} malformed JSONL record(s) skipped in "
              f"{jsonl_path.name}", file=sys.stderr)
    return inserted


def _upsert_session(con: apsw.Connection, agent: str, project_id: str,
                    session_id: str, title: str | None,
                    first_seen: str, now: str) -> None:
    """Insert or refresh a sessions row. Title is only set if provided."""
    con.execute(
        """INSERT INTO sessions (session_id, project_id, title, first_seen,
                                 last_updated, agent)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(session_id) DO UPDATE SET
               title = COALESCE(excluded.title, sessions.title),
               last_updated = excluded.last_updated,
               agent = excluded.agent""",
        (session_id, project_id, title, first_seen, now, agent),
    )


def _upsert_ingested_file(con: apsw.Connection, agent: str, file_key: str,
                          session_id: str, project_id: str,
                          lines_read: int, mtime: float) -> None:
    """Insert or refresh an ingested_files row."""
    con.execute(
        """INSERT INTO ingested_files
               (file_path, session_id, project_id, lines_ingested,
                last_modified, agent)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(file_path) DO UPDATE SET
               lines_ingested = excluded.lines_ingested,
               last_modified  = excluded.last_modified,
               agent          = excluded.agent""",
        (file_key, session_id, project_id, lines_read, mtime, agent),
    )


def _persist_message(con: apsw.Connection, agent: str, project_id: str,
                     session_id: str, uuid: str, role: str, text: str,
                     timestamp: str | None, do_embed: bool,
                     model: str | None = None) -> int:
    """Insert one message row + (if vec is up) embedding. Returns rows changed
    (0 or 1). Shared by all per-agent parsers and the tool_error path."""
    try:
        # RETURNING is atomic: returns [(rowid,)] on insert, [] on conflict.
        # One round-trip instead of INSERT + SELECT after.
        ret = con.execute(
            """INSERT OR IGNORE INTO messages
               (uuid, session_id, project_id, role, content, timestamp, model, agent)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING rowid""",
            (uuid, session_id, project_id, role, text, timestamp, model, agent),
        ).fetchall()
        if not ret:
            return 0
        rowid = ret[0][0]
        if do_embed and _vec_ok(con):
            vec = embed(text)
            if vec:
                _vec_insert(con, rowid, vec)
        return 1
    except apsw.Error as _e:
        print(f"[warn] _persist_message failed for uuid={uuid!r}: "
              f"{type(_e).__name__}: {_e}", file=sys.stderr)
        return 0


# (_legacy_gemini_slug moved to identity.py in v0.4.0; re-exported above.)


def ingest_gemini_file(con: apsw.Connection, jsonl_path: Path,
                       do_embed: bool = True) -> int:
    """Ingest a single Gemini chat session file.

    Source format (one record per line):
      - First record: header `{sessionId, projectHash, startTime, kind}` —
        used to seed session metadata.
      - User messages: `{id, timestamp, type: "user", content: [{text}]}`
      - Gemini messages: `{id, timestamp, type: "gemini", content: [{text}]}`
      - `{$set: ...}` records: metadata patches, skipped.
      - `{type: "info", ...}` records: tool/system info, skipped.

    Tool-call records are skipped (we only index human-readable text).
    """
    stat = jsonl_path.stat()
    file_key = str(jsonl_path)

    row = con.execute(
        "SELECT lines_ingested, last_modified FROM ingested_files WHERE file_path = ?",
        (file_key,),
    ).fetchone()
    if row and row["last_modified"] == stat.st_mtime:
        return 0
    lines_already = row["lines_ingested"] if row else 0

    # Three-layer project_id resolution (in priority order):
    #   1. cwd from session header → _project_id(cwd)
    #   2. ~/.gemini/projects.json reverse-lookup of hash_dir → real cwd
    #   3. SHA-hash dir name → synthetic gemini-hash:<hash> id
    hash_dir = jsonl_path.parent.parent.name
    project_id: str | None = None
    display_name: str | None = None
    cwd_real: str | None = None

    aliases = _load_gemini_aliases()
    aliased_cwd = aliases.get(hash_dir)
    if aliased_cwd:
        project_id = _project_id(aliased_cwd)
        display_name = _display_name(aliased_cwd)
        cwd_real = os.path.realpath(aliased_cwd)

    session_id = jsonl_path.stem  # fallback if no header
    first_seen = None
    inserted = 0
    malformed = 0
    lines_read = 0

    with open(jsonl_path, "r", errors="replace") as f:
        for lineno, raw in enumerate(f):
            lines_read = lineno + 1
            if lineno < lines_already:
                continue
            try:
                rec = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                malformed += 1
                continue
            # Header record (no `type`) — extract sessionId/startTime/cwd
            if "$set" in rec:
                continue
            if "sessionId" in rec and "type" not in rec:
                session_id = rec.get("sessionId", session_id)
                first_seen = rec.get("startTime") or first_seen
                cwd = rec.get("cwd") or rec.get("projectDir")
                if cwd:
                    project_id = _project_id(cwd)
                    display_name = _display_name(cwd)
                    cwd_real = os.path.realpath(cwd)
                continue
            rtype = rec.get("type")
            if rtype not in ("user", "gemini", "error", "warning"):
                continue
            # Defer message inserts until we've decided project_id (after header).
            if project_id is None:
                project_id = _gemini_hash_project_id(hash_dir)
                display_name = hash_dir
                cwd_real = None
            timestamp = rec.get("timestamp")

            # Top-level CLI error/warning records → tool_error.
            if rtype in ("error", "warning"):
                hit = _gemini_record_error(rec)
                if hit is not None:
                    kind, tr_text = hit
                    rec_id = rec.get("id") or str(lineno)
                    tr_uuid = f"{session_id}:tr:gemini:{kind}:{rec_id}"
                    cleaned = _clean_content(tr_text)
                    if cleaned:
                        inserted += _persist_message(
                            con, "gemini", project_id, session_id, tr_uuid,
                            "tool_error", cleaned, timestamp, do_embed,
                        )
                continue

            role = "user" if rtype == "user" else "assistant"
            text = _clean_content(_extract_text(rec.get("content", "")))
            uuid = rec.get("id") or f"{session_id}:{lineno}"

            if text:
                inserted += _persist_message(con, "gemini", project_id, session_id,
                                              uuid, role, text, timestamp, do_embed)

            # toolCalls[] error/cancelled harvest. Runs independently of
            # `text` so messages that are pure tool wrappers don't drop the
            # error signal (TD-006-style invariant).
            if rtype == "gemini":
                tool_calls = rec.get("toolCalls") or []
                if isinstance(tool_calls, list):
                    for tc in tool_calls:
                        tr_text = _gemini_tool_call_error(tc)
                        if tr_text is None:
                            continue
                        tc_id = tc.get("id") or f"{lineno}-{id(tc)}"
                        tr_uuid = f"{session_id}:tr:gemini:tool:{tc_id}"
                        cleaned = _clean_content(tr_text)
                        if cleaned:
                            inserted += _persist_message(
                                con, "gemini", project_id, session_id, tr_uuid,
                                "tool_error", cleaned, timestamp, do_embed,
                            )

    # If no records produced project_id (empty file or skipped-only), fall back.
    if project_id is None:
        project_id = _gemini_hash_project_id(hash_dir)
        display_name = hash_dir
        cwd_real = None

    _upsert_project(con, project_id, display_name, cwd_real)
    now = datetime.now(timezone.utc).isoformat()
    _upsert_session(con, "gemini", project_id, session_id, None,
                    first_seen or now, now)
    _upsert_ingested_file(con, "gemini", file_key, session_id, project_id,
                          lines_read, stat.st_mtime)
    if malformed:
        print(f"[warn] {malformed} malformed JSONL record(s) skipped in "
              f"{jsonl_path.name}", file=sys.stderr)
    return inserted


# (_legacy_codex_slug moved to identity.py in v0.4.0; re-exported above.)


_GEMINI_ALIAS_PATH = Path(os.environ.get(
    "CONVO_RECALL_GEMINI_ALIASES",
    Path.home() / ".local" / "share" / "convo-recall" / "gemini-aliases.json",
))


def _load_gemini_aliases() -> dict[str, str]:
    """Read the optional `{sha-hash → human-slug}` map.

    The file is hand-editable. Returns an empty dict when missing or
    malformed; redactions/upgrades shouldn't crash on a stale file.
    """
    if not _GEMINI_ALIAS_PATH.exists():
        return {}
    try:
        return json.loads(_GEMINI_ALIAS_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def ingest_codex_file(con: apsw.Connection, jsonl_path: Path,
                      do_embed: bool = True) -> int:
    """Ingest a single Codex rollout file.

    Source format:
      ~/.codex/sessions/{YYYY}/{MM}/{DD}/rollout-{ts}-{uuid}.jsonl

    Per record:
      - First record `type='session_meta'`: extract `payload.id` (session_id)
        and `payload.cwd` (project slug source).
      - `type='response_item'` with `payload.type='message'`:
          * `payload.role='user'` → user
          * `payload.role='assistant'` → assistant
          * `payload.role='developer'` skipped (system prompt)
          * `payload.content` is list of `{type:input_text|output_text, text}`
      - All other top-level types (`event_msg`, `turn_context`, function
        calls, reasoning blocks) are skipped — we only index human-readable
        user/assistant turns.

    `~/.codex/history.jsonl` is NOT touched here (rollouts are source of
    truth; the iter helper already excludes it by glob pattern).
    """
    stat = jsonl_path.stat()
    file_key = str(jsonl_path)

    row = con.execute(
        "SELECT lines_ingested, last_modified FROM ingested_files WHERE file_path = ?",
        (file_key,),
    ).fetchone()
    if row and row["last_modified"] == stat.st_mtime:
        return 0
    lines_already = row["lines_ingested"] if row else 0

    session_id = jsonl_path.stem  # fallback if session_meta missing
    project_id = _legacy_project_id("codex_unknown")
    display_name = "codex_unknown"
    cwd_real = None
    first_seen = None
    inserted = 0
    malformed = 0
    lines_read = 0

    def _set_project_from_cwd(cwd: str) -> None:
        nonlocal project_id, display_name, cwd_real
        project_id = _project_id(cwd)
        display_name = _display_name(cwd)
        cwd_real = os.path.realpath(cwd)

    with open(jsonl_path, "r", errors="replace") as f:
        for lineno, raw in enumerate(f):
            lines_read = lineno + 1
            if lineno < lines_already:
                # Re-scan first record on resume to pick up session_meta even
                # when ingestion previously stopped mid-file.
                if lineno == 0:
                    try:
                        rec = json.loads(raw)
                        if rec.get("type") == "session_meta":
                            payload = rec.get("payload", {})
                            session_id = payload.get("id", session_id)
                            cwd = payload.get("cwd")
                            if cwd:
                                _set_project_from_cwd(cwd)
                            first_seen = payload.get("timestamp") or rec.get("timestamp")
                    except (json.JSONDecodeError, ValueError):
                        pass
                continue
            try:
                rec = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                malformed += 1
                continue
            ttype = rec.get("type")
            timestamp = rec.get("timestamp")
            if ttype == "session_meta":
                payload = rec.get("payload", {})
                session_id = payload.get("id", session_id)
                cwd = payload.get("cwd")
                if cwd:
                    _set_project_from_cwd(cwd)
                first_seen = payload.get("timestamp") or rec.get("timestamp")
                continue

            # Tool-error harvesting (event_msg branch). Independent of the
            # message branch — codex emits failures as event_msg payloads,
            # not as `tool_result` blocks inside user messages.
            if ttype == "event_msg":
                hit = _codex_event_msg_error(rec)
                if hit is not None:
                    kind, tr_text = hit
                    payload_evt = rec.get("payload", {})
                    key = (payload_evt.get("call_id")
                           or payload_evt.get("turn_id")
                           or str(lineno))
                    tr_uuid = f"{session_id}:tr:codex:{kind}:{key}"
                    cleaned = _clean_content(tr_text)
                    if cleaned:
                        inserted += _persist_message(
                            con, "codex", project_id, session_id, tr_uuid,
                            "tool_error", cleaned, timestamp, do_embed,
                        )
                continue

            if ttype != "response_item":
                continue
            payload = rec.get("payload", {})
            payload_type = payload.get("type")

            # Tool-error fallback (function_call_output branch). Catches the
            # older Sep-2025 schema where exit_code lives inside output JSON,
            # plus modern plain-string outputs that match the error regex.
            if payload_type == "function_call_output":
                fco_text = _codex_fco_error(rec)
                if fco_text is not None:
                    call_id = payload.get("call_id") or str(lineno)
                    tr_uuid = f"{session_id}:tr:codex:fco:{call_id}"
                    cleaned = _clean_content(fco_text)
                    if cleaned:
                        inserted += _persist_message(
                            con, "codex", project_id, session_id, tr_uuid,
                            "tool_error", cleaned, timestamp, do_embed,
                        )
                continue

            if payload_type != "message":
                continue
            role_in = payload.get("role")
            if role_in == "user":
                role = "user"
            elif role_in == "assistant":
                role = "assistant"
            else:
                continue  # skip developer / system prompts
            text = _clean_content(_extract_text(payload.get("content", "")))
            if not text:
                continue
            uuid = payload.get("id") or f"{session_id}:{lineno}"
            inserted += _persist_message(con, "codex", project_id, session_id,
                                          uuid, role, text, timestamp, do_embed)

    _upsert_project(con, project_id, display_name, cwd_real)
    now = datetime.now(timezone.utc).isoformat()
    _upsert_session(con, "codex", project_id, session_id, None,
                    first_seen or now, now)
    _upsert_ingested_file(con, "codex", file_key, session_id, project_id,
                          lines_read, stat.st_mtime)
    if malformed:
        print(f"[warn] {malformed} malformed JSONL record(s) skipped in "
              f"{jsonl_path.name}", file=sys.stderr)
    return inserted


_AGENT_INGEST = {
    "claude": ingest_file,
    "gemini": ingest_gemini_file,
    "codex":  ingest_codex_file,
}


def _dispatch_ingest(con: apsw.Connection, agents: list[str], *,
                     embed_live: bool, verbose: bool) -> tuple[int, int]:
    """Run the ingest pipeline for the named agents in order.

    Returns (total_messages_inserted, total_files_with_changes). Shared by
    `scan_one_agent` and `scan_all` so the per-agent dispatch logic lives
    in one place.

    Pre-pass counts total session files across all enabled agents and
    publishes that as the `ingest` phase total via the _progress tracker
    (no-op if no active run, e.g. the watcher loop). Each file processed
    ticks the counter so `recall stats` shows a live bar during ingest.
    """
    from . import _progress

    # Build the work list once so we can both count and process from it.
    # File-path lists are tiny (a few KB even at 10K files) — well worth
    # the visibility win.
    work: list[tuple[str, Path]] = []
    for agent_name in agents:
        if agent_name not in _AGENT_INGEST:
            print(f"[warn] unknown agent: {agent_name}", file=sys.stderr)
            continue
        for jsonl_path in _AGENT_ITERATORS[agent_name]():
            work.append((agent_name, jsonl_path))

    _progress.set_phase_total("ingest", len(work))

    total = 0
    files = 0
    for processed, (agent_name, jsonl_path) in enumerate(work, start=1):
        ingester = _AGENT_INGEST[agent_name]
        kwargs = {"do_embed": embed_live}
        if agent_name == "claude":
            kwargs["agent"] = "claude"
        n = ingester(con, jsonl_path, **kwargs)
        if n > 0:
            files += 1
            total += n
            if verbose:
                slug = (_legacy_claude_slug(jsonl_path) if agent_name == "claude"
                        else _legacy_gemini_slug(jsonl_path) if agent_name == "gemini"
                        else jsonl_path.parent.name)
                print(f"  +{n:4d} msgs  [{agent_name}] {slug}/{jsonl_path.name[:8]}…")
        # Tick on every file so the bar advances at human-perceptible
        # cadence even when most files have no new messages (the common
        # case on a re-ingest of an already-populated DB).
        _progress.update_phase("ingest", processed)
    return total, files


def scan_one_agent(con: apsw.Connection, agent_name: str,
                   verbose: bool = False, do_embed: bool = True) -> int:
    """Scan and ingest only the named agent's source files. Returns total
    messages inserted. Used by `recall ingest --agent {name}` and by the
    per-agent launchd plists generated at install time."""
    if agent_name not in _AGENT_INGEST:
        print(f"[error] unknown agent: {agent_name}", file=sys.stderr)
        return 0
    embed_live = EMBED_SOCK.exists() and do_embed
    total, files = _dispatch_ingest(con, [agent_name],
                                     embed_live=embed_live, verbose=verbose)
    if verbose or total > 0:
        print(f"Ingested {total} new [{agent_name}] message(s) from {files} file(s).")
    return total


def watch_loop(con: apsw.Connection, interval: int = 10,
               verbose: bool = False) -> None:
    """Polling watcher used inside the sandbox / on Linux (no launchd).

    Calls `scan_all` every `interval` seconds. Exits cleanly on SIGINT/SIGTERM.
    On macOS, prefer per-agent launchd plists generated by `recall install` —
    they are file-system event driven (no polling) and integrate with login
    sessions cleanly.
    """
    import signal, time
    stop = {"flag": False}
    def _handler(signum, frame):
        stop["flag"] = True
    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)
    print(f"[watch] starting loop (interval={interval}s). Ctrl-C to stop.",
          flush=True)
    tick = 0
    while not stop["flag"]:
        tick += 1
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            scan_all(con, verbose=verbose)
        except Exception as e:
            print(f"[watch] tick={tick} {ts} ERROR: {type(e).__name__}: {e}",
                  flush=True, file=sys.stderr)
        else:
            print(f"[watch] tick={tick} {ts} ok", flush=True)
        # Wait for `interval` seconds OR until stop flag set, whichever sooner
        for _ in range(interval):
            if stop["flag"]:
                break
            time.sleep(1)
    print("[watch] stopping.", flush=True)


def scan_all(con: apsw.Connection, verbose: bool = False,
             do_embed: bool = True) -> None:
    from . import _progress

    # Close the race where the embed sidecar systemd unit was started
    # moments ago but hasn't bound its socket yet (~5s Linux, can be longer
    # for first-ever model download). Without this, embed_live=False here
    # → self-heal pass below silently skips → DB stays at 0% embedded.
    # On warm systems the socket already exists, so the wait is a no-op.
    if do_embed and _vec_ok(con):
        _wait_for_embed_socket(timeout_s=30.0, verbose=verbose)

    embed_live = EMBED_SOCK.exists() and do_embed
    if not embed_live and do_embed:
        print("[warn] embed socket not found — running in FTS-only mode", file=sys.stderr)

    enabled_agents = load_config().get("agents") or ["claude"]

    # Same own-run pattern as embed_backfill: if a multi-phase chain is
    # already active (e.g. the wizard's _backfill-chain), we participate
    # in it and let the parent finish_run. Otherwise create a single-
    # phase run so standalone `recall ingest` shows a bar in stats.
    own_run = _progress.read_status() is None
    if own_run:
        _progress.start_run([("ingest", 0)])
    try:
        total, files = _dispatch_ingest(con, enabled_agents,
                                         embed_live=embed_live, verbose=verbose)
        _progress.finish_phase("ingest")
    finally:
        if own_run:
            _progress.finish_run()
    if verbose or total > 0:
        print(f"Ingested {total} new messages from {files} file(s).")

    # Self-healing embed pass: catch messages ingested while embed service was down.
    # Order DESC so the most recent (and most-queried) messages heal first
    # after a fresh install against an existing DB. Cap bumped to 2000 — at
    # ~200ms/embedding warm this fits well inside the 10s watch tick.
    if embed_live and _vec_ok(con):
        missing = con.execute("""
            SELECT m.rowid, m.content FROM messages m
            LEFT JOIN message_vecs v ON v.rowid = m.rowid
            WHERE v.rowid IS NULL
            ORDER BY m.rowid DESC
            LIMIT 2000
        """).fetchall()
        if missing:
            healed = 0
            for rowid, content in missing:
                vec = embed(content)
                if vec:
                    _vec_insert(con, rowid, vec)
                    healed += 1
            if verbose or healed > 0:
                print(f"Healed {healed} missing embedding(s).")

