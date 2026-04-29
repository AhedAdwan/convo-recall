"""
Core ingestion, search, and backfill logic for convo-recall.

Paths default to standard Claude Code locations but are configurable
via environment variables:
  CONVO_RECALL_DB       — path to SQLite DB (default ~/.claude/index/conversations.db)
  CONVO_RECALL_PROJECTS — path to Claude projects dir (default ~/.claude/projects)
  CONVO_RECALL_SOCK     — path to embed UDS socket (default ~/.midcortex/engram/embed.sock)
"""

import http.client
import json
import math
import os
import re
import socket
import struct
import sys

import apsw
from datetime import datetime, timezone
from pathlib import Path

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

EMBED_DIM = 1024
MAX_QUERY_LEN = 2048
RRF_K = 60
DECAY_HALF_LIFE_DAYS = 90

# Single apsw connection for both FTS and vector ops. _vc points at the same
# connection when sqlite-vec is available (or None when it isn't, which puts
# the library in FTS-only mode). Using a single connection avoids the
# cross-libsqlite3-version corruption that occurs when stdlib sqlite3 (e.g.
# 3.45 on Ubuntu 24.04) shares a DB file with apsw's bundled sqlite (3.53).
_vc = None  # module-level apsw connection, initialised in open_db()


class _Row:
    """sqlite3.Row-compatible wrapper around an apsw tuple — supports both
    string-key and integer-index access so existing call sites keep working."""
    __slots__ = ("_keys", "_data")

    def __init__(self, keys, data):
        self._keys = keys
        self._data = data

    def __getitem__(self, k):
        if isinstance(k, int):
            return self._data[k]
        try:
            return self._data[self._keys.index(k)]
        except ValueError:
            raise KeyError(k)

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)

    def keys(self):
        return list(self._keys)


def _row_factory(cursor, row):
    desc = cursor.getdescription()
    return _Row(tuple(d[0] for d in desc), row)


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
    text = _BLANK_LINES_RE.sub('\n\n', text)
    text = _expand_code_tokens(text)
    return text.strip()


# ── Embedding client ──────────────────────────────────────────────────────────

class _UnixHTTPConn(http.client.HTTPConnection):
    def __init__(self, sock_path: str):
        super().__init__("localhost")
        self._sock_path = sock_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self._sock_path)


def embed(text: str, mode: str = "document") -> list[float] | None:
    """POST text to the UDS embed service. Returns None if unreachable.
    Long texts are chunked and mean-pooled by the sidecar — no client-side truncation."""
    body = json.dumps({"text": text, "mode": mode}).encode()
    conn = _UnixHTTPConn(str(EMBED_SOCK))
    try:
        conn.request("POST", "/embed", body=body,
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        return json.loads(resp.read())["vector"]
    except (ConnectionRefusedError, FileNotFoundError, OSError):
        return None  # service down — expected when sidecar not running
    except Exception as e:
        print(f"[warn] embed: {type(e).__name__}: {e}", file=sys.stderr)
        return None
    finally:
        conn.close()


def _vec_bytes(v: list[float]) -> bytes:
    return struct.pack(f"{len(v)}f", *v)


# ── DB setup ──────────────────────────────────────────────────────────────────

def open_db() -> apsw.Connection:
    global _vc
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = apsw.Connection(str(DB_PATH))
    con.row_trace = _row_factory
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    try:
        import sqlite_vec
        con.enableloadextension(True)
        sqlite_vec.load(con)
        con.enableloadextension(False)
        _vc = con
    except Exception as e:
        print(f"[warn] sqlite-vec unavailable (FTS-only mode): {e}", file=sys.stderr)
        _vc = None
    _init_schema(con)
    _migrate_add_agent_column(con)
    _migrate_fts_porter(con)
    if _vc is not None:
        _init_vec_tables(_vc)
    return con


def close_db(con: apsw.Connection) -> None:
    """Close the single apsw connection."""
    global _vc
    con.close()
    _vc = None


def _init_schema(con: apsw.Connection) -> None:
    con.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id   TEXT PRIMARY KEY,
            project_slug TEXT NOT NULL,
            title        TEXT,
            first_seen   TEXT NOT NULL,
            last_updated TEXT NOT NULL,
            agent        TEXT NOT NULL DEFAULT 'claude'
        );

        CREATE TABLE IF NOT EXISTS messages (
            uuid         TEXT PRIMARY KEY,
            session_id   TEXT NOT NULL,
            project_slug TEXT NOT NULL,
            role         TEXT NOT NULL,
            content      TEXT NOT NULL,
            timestamp    TEXT,
            model        TEXT,
            agent        TEXT NOT NULL DEFAULT 'claude'
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
            content,
            session_id   UNINDEXED,
            project_slug UNINDEXED,
            role         UNINDEXED,
            agent        UNINDEXED,
            content='messages',
            content_rowid='rowid',
            tokenize='porter unicode61'
        );

        CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, content, session_id, project_slug, role, agent)
            VALUES (new.rowid, new.content, new.session_id, new.project_slug, new.role, new.agent);
        END;

        CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, content, session_id, project_slug, role, agent)
            VALUES ('delete', old.rowid, old.content, old.session_id, old.project_slug, old.role, old.agent);
        END;

        CREATE TABLE IF NOT EXISTS ingested_files (
            file_path      TEXT PRIMARY KEY,
            session_id     TEXT NOT NULL,
            project_slug   TEXT NOT NULL,
            lines_ingested INTEGER NOT NULL DEFAULT 0,
            last_modified  REAL NOT NULL,
            agent          TEXT NOT NULL DEFAULT 'claude'
        );
    """)


def _has_column(con: apsw.Connection, table: str, column: str) -> bool:
    cols = con.execute(f"PRAGMA table_info({table})").fetchall()
    names = {c["name"] if isinstance(c, _Row) else c[1] for c in cols}
    return column in names


def _migrate_add_agent_column(con: apsw.Connection) -> None:
    """Add `agent` column to legacy DBs that pre-date multi-agent support.

    Idempotent: each ALTER guarded by PRAGMA table_info check. Backfills
    existing rows with 'claude' (the only agent before this migration).
    """
    altered = []
    for table in ("sessions", "messages", "ingested_files"):
        if not _has_column(con, table, "agent"):
            con.execute(
                f"ALTER TABLE {table} ADD COLUMN agent TEXT NOT NULL DEFAULT 'claude'"
            )
            con.execute(f"UPDATE {table} SET agent='claude' WHERE agent IS NULL")
            altered.append(table)
    if altered:
        print(
            f"[migrate] Added `agent` column to: {', '.join(altered)} "
            "(backfilled to 'claude').",
            file=sys.stderr,
        )


def _migrate_fts_porter(con: apsw.Connection) -> None:
    """Migrate FTS table to porter+unicode61 tokenizer if needed AND make sure
    the FTS schema includes the `agent` UNINDEXED column. Both conditions
    trigger the same drop-rebuild flow (they share a code path)."""
    row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='messages_fts'"
    ).fetchone()
    sql = (row[0] or "") if row else ""
    needs_porter = "porter" not in sql
    needs_agent = "agent" not in sql
    if not (needs_porter or needs_agent):
        return
    why = []
    if needs_porter: why.append("porter unicode61 tokenizer")
    if needs_agent: why.append("agent column")
    print(f"[migrate] Rebuilding FTS index ({', '.join(why)})…", file=sys.stderr)
    con.execute("""
        DROP TRIGGER IF EXISTS messages_ai;
        DROP TRIGGER IF EXISTS messages_ad;
        DROP TABLE IF EXISTS messages_fts;

        CREATE VIRTUAL TABLE messages_fts USING fts5(
            content,
            session_id   UNINDEXED,
            project_slug UNINDEXED,
            role         UNINDEXED,
            agent        UNINDEXED,
            content='messages',
            content_rowid='rowid',
            tokenize='porter unicode61'
        );

        CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, content, session_id, project_slug, role, agent)
            VALUES (new.rowid, new.content, new.session_id, new.project_slug, new.role, new.agent);
        END;

        CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, content, session_id, project_slug, role, agent)
            VALUES ('delete', old.rowid, old.content, old.session_id, old.project_slug, old.role, old.agent);
        END;

        INSERT INTO messages_fts(messages_fts) VALUES('rebuild');
    """)
    print("[migrate] Done.", file=sys.stderr)


def _init_vec_tables(vc) -> None:
    vc.execute(f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS message_vecs USING vec0(
            rowid INTEGER PRIMARY KEY,
            embedding FLOAT[{EMBED_DIM}]
        )
    """)


def _vec_insert(rowid: int, vec: list[float]) -> None:
    if _vc is None:
        return
    try:
        _vc.execute(
            "INSERT OR REPLACE INTO message_vecs(rowid, embedding) VALUES (?, ?)",
            (rowid, _vec_bytes(vec)),
        )
    except Exception:
        pass


def _vec_search(qvec: list[float], k: int = 100) -> list[int]:
    if _vc is None:
        return []
    try:
        rows = _vc.execute(
            "SELECT rowid FROM message_vecs WHERE embedding MATCH ? AND k = ?",
            (_vec_bytes(qvec), k),
        ).fetchall()
        return [r[0] for r in rows]
    except Exception:
        return []


def _vec_count() -> int:
    if _vc is None:
        return 0
    try:
        return _vc.execute("SELECT COUNT(*) FROM message_vecs").fetchone()[0]
    except Exception:
        return 0


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


# ── Temporal decay ────────────────────────────────────────────────────────────

def _decay(timestamp: str | None, half_life_days: int = DECAY_HALF_LIFE_DAYS) -> float:
    if not timestamp:
        return 1.0
    try:
        ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        age_days = (datetime.now(timezone.utc) - ts).days
        return math.pow(0.5, age_days / half_life_days)
    except Exception:
        return 1.0


# ── Path helpers ──────────────────────────────────────────────────────────────

def _slug_from_path(jsonl_path: Path) -> str:
    if jsonl_path.parent.name == "subagents":
        project_dir_name = jsonl_path.parent.parent.parent.name
    else:
        project_dir_name = jsonl_path.parent.name
    parts = project_dir_name.lstrip("-").split("-")
    try:
        idx = next(i for i, p in enumerate(parts) if p.lower() == "projects")
        relevant = parts[idx + 1:]
    except StopIteration:
        relevant = parts[-2:] if len(parts) >= 2 else parts
    return "_".join(relevant) if relevant else project_dir_name


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


def slug_from_cwd() -> str | None:
    """Derive project slug from cwd, matching ingestion convention."""
    parts = Path.cwd().parts
    try:
        idx = next(i for i, p in enumerate(parts) if p.lower() == "projects")
        relevant = parts[idx + 1:]
        return "_".join(relevant) if relevant else None
    except StopIteration:
        return None


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
    """Load `~/.local/share/convo-recall/config.json` or return defaults."""
    if not _CONFIG_PATH.exists():
        return {"agents": ["claude"]}  # default — preserves pre-multi-agent behavior
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
    project_slug = _slug_from_path(jsonl_path)
    session_id = _session_id_from_path(jsonl_path)

    inserted = 0
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
            if not text:
                continue

            uuid = rec.get("uuid", f"{session_id}:{lineno}")
            timestamp = rec.get("timestamp")
            model = msg.get("model") if role == "assistant" else None

            try:
                con.execute(
                    """INSERT OR IGNORE INTO messages
                       (uuid, session_id, project_slug, role, content, timestamp, model, agent)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (uuid, session_id, project_slug, role, text, timestamp, model, agent),
                )
                changed = con.changes()
                if changed and do_embed and _vc is not None:
                    rowid = con.execute(
                        "SELECT rowid FROM messages WHERE uuid = ?", (uuid,)
                    ).fetchone()[0]
                    vec = embed(text)
                    if vec:
                        _vec_insert(rowid, vec)
                inserted += changed
            except apsw.Error:
                pass

            # Index tool_result error blocks within user messages
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
                        try:
                            con.execute(
                                """INSERT OR IGNORE INTO messages
                                   (uuid, session_id, project_slug, role,
                                    content, timestamp, model, agent)
                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                                (tr_uuid, session_id, project_slug, "tool_error",
                                 tr_text, timestamp, None, agent),
                            )
                            tr_changed = con.changes()
                            if tr_changed and do_embed and _vc is not None:
                                tr_rowid = con.execute(
                                    "SELECT rowid FROM messages WHERE uuid = ?", (tr_uuid,)
                                ).fetchone()[0]
                                tr_vec = embed(tr_text)
                                if tr_vec:
                                    _vec_insert(tr_rowid, tr_vec)
                            inserted += tr_changed
                        except apsw.Error:
                            pass

    now = datetime.now(timezone.utc).isoformat()
    con.execute(
        """INSERT INTO sessions (session_id, project_slug, title, first_seen, last_updated, agent)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(session_id) DO UPDATE SET
               title = COALESCE(excluded.title, sessions.title),
               last_updated = excluded.last_updated,
               agent = excluded.agent""",
        (session_id, project_slug, title, now, now, agent),
    )
    con.execute(
        """INSERT INTO ingested_files
               (file_path, session_id, project_slug, lines_ingested, last_modified, agent)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(file_path) DO UPDATE SET
               lines_ingested = excluded.lines_ingested,
               last_modified  = excluded.last_modified,
               agent          = excluded.agent""",
        (file_key, session_id, project_slug, lines_read, stat.st_mtime, agent),
    )
    return inserted


def _persist_message(con: apsw.Connection, agent: str, project_slug: str,
                     session_id: str, uuid: str, role: str, text: str,
                     timestamp: str | None, do_embed: bool) -> int:
    """Insert one message row + (if vec is up) embedding. Returns rows changed
    (0 or 1). Shared helper used by gemini and codex parsers; ingest_file
    keeps its own inline INSERT for now to preserve tool_error indexing
    behavior unchanged from pre-multi-agent code."""
    try:
        con.execute(
            """INSERT OR IGNORE INTO messages
               (uuid, session_id, project_slug, role, content, timestamp, model, agent)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (uuid, session_id, project_slug, role, text, timestamp, None, agent),
        )
        changed = con.changes()
        if changed and do_embed and _vc is not None:
            rowid = con.execute(
                "SELECT rowid FROM messages WHERE uuid = ?", (uuid,)
            ).fetchone()[0]
            vec = embed(text)
            if vec:
                _vec_insert(rowid, vec)
        return changed
    except apsw.Error:
        return 0


def _gemini_slug_from_path(jsonl_path: Path) -> str:
    """~/.gemini/tmp/{project}/chats/session-*.jsonl → {project}."""
    return jsonl_path.parent.parent.name


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

    project_slug = _gemini_slug_from_path(jsonl_path)
    session_id = jsonl_path.stem  # fallback if no header
    first_seen = None
    inserted = 0
    lines_read = 0

    with open(jsonl_path, "r", errors="replace") as f:
        for lineno, raw in enumerate(f):
            lines_read = lineno + 1
            if lineno < lines_already:
                continue
            try:
                rec = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                continue
            # Header record (no `type`) — extract sessionId/startTime
            if "$set" in rec:
                continue
            if "sessionId" in rec and "type" not in rec:
                session_id = rec.get("sessionId", session_id)
                first_seen = rec.get("startTime") or first_seen
                continue
            rtype = rec.get("type")
            if rtype not in ("user", "gemini"):
                continue
            role = "user" if rtype == "user" else "assistant"
            text = _clean_content(_extract_text(rec.get("content", "")))
            if not text:
                continue
            uuid = rec.get("id") or f"{session_id}:{lineno}"
            timestamp = rec.get("timestamp")
            inserted += _persist_message(con, "gemini", project_slug, session_id,
                                          uuid, role, text, timestamp, do_embed)

    now = datetime.now(timezone.utc).isoformat()
    con.execute(
        """INSERT INTO sessions (session_id, project_slug, title, first_seen, last_updated, agent)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(session_id) DO UPDATE SET
               last_updated = excluded.last_updated,
               agent        = excluded.agent""",
        (session_id, project_slug, None, first_seen or now, now, "gemini"),
    )
    con.execute(
        """INSERT INTO ingested_files
               (file_path, session_id, project_slug, lines_ingested, last_modified, agent)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(file_path) DO UPDATE SET
               lines_ingested = excluded.lines_ingested,
               last_modified  = excluded.last_modified,
               agent          = excluded.agent""",
        (file_key, session_id, project_slug, lines_read, stat.st_mtime, "gemini"),
    )
    return inserted


def _codex_slug_from_cwd(cwd: str) -> str:
    """Convert a codex session's cwd to a project slug.

    `/Users/x/Projects/mcp/Foo` → `mcp_Foo` (matches Claude's slug convention
    for paths under `Projects/`).
    `/some/random/path` → `random_path` (best-effort fallback).
    """
    parts = Path(cwd).parts
    try:
        idx = next(i for i, p in enumerate(parts) if p.lower() == "projects")
        relevant = parts[idx + 1:]
        return "_".join(relevant) if relevant else Path(cwd).name
    except StopIteration:
        # No Projects/ in path — use last 2 path components
        relevant = parts[-2:] if len(parts) >= 2 else parts
        return "_".join(p for p in relevant if p and p != "/")


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
    project_slug = "codex_unknown"
    first_seen = None
    inserted = 0
    lines_read = 0

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
                                project_slug = _codex_slug_from_cwd(cwd)
                            first_seen = payload.get("timestamp") or rec.get("timestamp")
                    except (json.JSONDecodeError, ValueError):
                        pass
                continue
            try:
                rec = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                continue
            ttype = rec.get("type")
            if ttype == "session_meta":
                payload = rec.get("payload", {})
                session_id = payload.get("id", session_id)
                cwd = payload.get("cwd")
                if cwd:
                    project_slug = _codex_slug_from_cwd(cwd)
                first_seen = payload.get("timestamp") or rec.get("timestamp")
                continue
            if ttype != "response_item":
                continue
            payload = rec.get("payload", {})
            if payload.get("type") != "message":
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
            timestamp = rec.get("timestamp")
            uuid = payload.get("id") or f"{session_id}:{lineno}"
            inserted += _persist_message(con, "codex", project_slug, session_id,
                                          uuid, role, text, timestamp, do_embed)

    now = datetime.now(timezone.utc).isoformat()
    con.execute(
        """INSERT INTO sessions (session_id, project_slug, title, first_seen, last_updated, agent)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(session_id) DO UPDATE SET
               last_updated = excluded.last_updated,
               agent        = excluded.agent""",
        (session_id, project_slug, None, first_seen or now, now, "codex"),
    )
    con.execute(
        """INSERT INTO ingested_files
               (file_path, session_id, project_slug, lines_ingested, last_modified, agent)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(file_path) DO UPDATE SET
               lines_ingested = excluded.lines_ingested,
               last_modified  = excluded.last_modified,
               agent          = excluded.agent""",
        (file_key, session_id, project_slug, lines_read, stat.st_mtime, "codex"),
    )
    return inserted


_AGENT_INGEST = {
    "claude": ingest_file,
    "gemini": ingest_gemini_file,
    "codex":  ingest_codex_file,
}


def scan_one_agent(con: apsw.Connection, agent_name: str,
                   verbose: bool = False, do_embed: bool = True) -> int:
    """Scan and ingest only the named agent's source files. Returns total
    messages inserted. Used by `recall ingest --agent {name}` and by the
    per-agent launchd plists generated at install time."""
    if agent_name not in _AGENT_INGEST:
        print(f"[error] unknown agent: {agent_name}", file=sys.stderr)
        return 0
    embed_live = EMBED_SOCK.exists() and do_embed
    ingester = _AGENT_INGEST[agent_name]
    iterator = _AGENT_ITERATORS[agent_name]
    total = 0
    files = 0
    for jsonl_path in iterator():
        kwargs = {"do_embed": embed_live}
        if agent_name == "claude":
            kwargs["agent"] = "claude"
        n = ingester(con, jsonl_path, **kwargs)
        if n > 0:
            files += 1
            total += n
            if verbose:
                slug = (_slug_from_path(jsonl_path) if agent_name == "claude"
                        else _gemini_slug_from_path(jsonl_path) if agent_name == "gemini"
                        else jsonl_path.parent.name)
                print(f"  +{n:4d} msgs  [{agent_name}] {slug}/{jsonl_path.name[:8]}…")
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
    embed_live = EMBED_SOCK.exists() and do_embed
    if not embed_live and do_embed:
        print("[warn] embed socket not found — running in FTS-only mode", file=sys.stderr)

    enabled_agents = load_config().get("agents") or ["claude"]

    total = 0
    files = 0
    for agent_name in enabled_agents:
        if agent_name not in _AGENT_INGEST:
            print(f"[warn] unknown agent in config: {agent_name}", file=sys.stderr)
            continue
        ingester = _AGENT_INGEST[agent_name]
        iterator = _AGENT_ITERATORS[agent_name]
        for jsonl_path in iterator():
            kwargs = {"do_embed": embed_live}
            if agent_name == "claude":
                kwargs["agent"] = "claude"
            n = ingester(con, jsonl_path, **kwargs)
            if n > 0:
                files += 1
                total += n
                if verbose:
                    slug = (_slug_from_path(jsonl_path) if agent_name == "claude"
                            else _gemini_slug_from_path(jsonl_path) if agent_name == "gemini"
                            else jsonl_path.parent.name)
                    print(f"  +{n:4d} msgs  [{agent_name}] {slug}/{jsonl_path.name[:8]}…")
    if verbose or total > 0:
        print(f"Ingested {total} new messages from {files} file(s).")

    # Self-healing embed pass: catch messages ingested while embed service was down
    if embed_live and _vc is not None:
        missing = _vc.execute("""
            SELECT m.rowid, m.content FROM messages m
            LEFT JOIN message_vecs v ON v.rowid = m.rowid
            WHERE v.rowid IS NULL
            ORDER BY m.rowid
            LIMIT 500
        """).fetchall()
        if missing:
            healed = 0
            for rowid, content in missing:
                vec = embed(content)
                if vec:
                    _vec_insert(rowid, vec)
                    healed += 1
            if verbose or healed > 0:
                print(f"Healed {healed} missing embedding(s).")


# ── Search ────────────────────────────────────────────────────────────────────

def _fetch_context(con: apsw.Connection, session_id: str,
                   timestamp: str | None, n: int) -> tuple[list, list]:
    if not timestamp or n <= 0:
        return [], []
    before = con.execute(
        """SELECT role, SUBSTR(content, 1, 150) AS excerpt FROM messages
           WHERE session_id = ? AND timestamp < ?
           ORDER BY timestamp DESC LIMIT ?""",
        (session_id, timestamp, n),
    ).fetchall()
    after = con.execute(
        """SELECT role, SUBSTR(content, 1, 150) AS excerpt FROM messages
           WHERE session_id = ? AND timestamp > ?
           ORDER BY timestamp ASC LIMIT ?""",
        (session_id, timestamp, n),
    ).fetchall()
    return list(reversed(before)), after


def search(con: apsw.Connection, query: str, limit: int = 10,
           recent: bool = False, project: str | None = None,
           context: int = 1, agent: str | None = None) -> None:
    if len(query) > MAX_QUERY_LEN:
        query = query[:MAX_QUERY_LEN]

    use_vec = _vc is not None and EMBED_SOCK.exists()
    qvec = None
    if use_vec:
        qvec = embed(query, mode="query")
        use_vec = qvec is not None

    # Pre-compute the rowid set for the (project, agent) filter so we can
    # narrow both FTS and vec result sets down before scoring.
    filter_rowids: set[int] | None = None
    if project or agent:
        clauses = []
        params: list = []
        if project:
            clauses.append("project_slug = ?")
            params.append(project)
        if agent:
            clauses.append("agent = ?")
            params.append(agent)
        where = " AND ".join(clauses)
        rows = con.execute(
            f"SELECT rowid FROM messages WHERE {where}", params
        ).fetchall()
        filter_rowids = {r[0] for r in rows}
        if not filter_rowids:
            label = ", ".join(filter(None, [
                f"project='{project}'" if project else None,
                f"agent='{agent}'" if agent else None,
            ]))
            print(f"No messages found for {label}.")
            return
    project_rowids = filter_rowids  # keep alias to minimize downstream churn

    # Corpus mismatch guard: fall back to FTS if vector coverage < 95%
    if use_vec and project and _vc is not None:
        cov = _vc.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN v.rowid IS NOT NULL THEN 1 ELSE 0 END) AS embedded
               FROM messages m
               LEFT JOIN message_vecs v ON v.rowid = m.rowid
               WHERE m.project_slug = ?""",
            (project,),
        ).fetchone()
        total, embedded = cov[0], cov[1] or 0
        if total > 0 and (embedded / total) < 0.95:
            pct = embedded * 100 // total
            print(f"[warn] Vector coverage {pct}% (<95%) for '{project}' — using FTS only. "
                  f"Run `recall ingest` to heal.", file=sys.stderr)
            use_vec = False

    if use_vec:
        fts_rows = con.execute(
            """SELECT m.rowid, ROW_NUMBER() OVER (ORDER BY rank) AS fts_rank
               FROM messages_fts
               JOIN messages m ON messages_fts.rowid = m.rowid
               WHERE messages_fts MATCH ?
               LIMIT 100""",
            (query,),
        ).fetchall()
        fts_map = {r["rowid"]: r["fts_rank"] for r in fts_rows
                   if project_rowids is None or r["rowid"] in project_rowids}

        vec_rowids = _vec_search(qvec, k=100)
        vec_map = {rid: rank + 1 for rank, rid in enumerate(vec_rowids)
                   if project_rowids is None or rid in project_rowids}

        all_rowids = list(set(fts_map) | set(vec_map))

        if recent and all_rowids:
            placeholders = ",".join("?" * len(all_rowids))
            ts_rows = con.execute(
                f"SELECT rowid, timestamp FROM messages WHERE rowid IN ({placeholders})",
                all_rowids,
            ).fetchall()
            ts_map = {r["rowid"]: r["timestamp"] for r in ts_rows}
        else:
            ts_map = {}

        def _score(rid: int) -> float:
            rrf = (1.0 / (RRF_K + fts_map.get(rid, 101))
                   + 1.0 / (RRF_K + vec_map.get(rid, 101)))
            if recent:
                rrf *= _decay(ts_map.get(rid))
            return rrf

        scored = sorted(all_rowids, key=_score, reverse=True)[:limit]
        placeholders = ",".join("?" * len(scored))
        rows = con.execute(
            f"""SELECT rowid, session_id, project_slug, role, timestamp, agent,
                       SUBSTR(content, 1, 300) AS excerpt
                FROM messages WHERE rowid IN ({placeholders})""",
            scored,
        ).fetchall()
    else:
        rows = con.execute(
            """SELECT m.rowid, m.session_id, m.project_slug, m.role, m.timestamp, m.agent,
                      snippet(messages_fts, 0, '[', ']', '…', 20) AS excerpt
               FROM messages_fts
               JOIN messages m ON messages_fts.rowid = m.rowid
               WHERE messages_fts MATCH ?
               ORDER BY rank
               LIMIT ?""",
            (query, limit),
        ).fetchall()

    if not rows:
        print("No results.")
        return

    mode = ("hybrid+recent" if use_vec and recent
            else "hybrid" if use_vec
            else "fts")
    print(f"[{mode} search]\n")
    for r in rows:
        ts = (r["timestamp"] or "")[:10]
        role_label = "[⚠ error]" if r["role"] == "tool_error" else f"[{r['role']}]"
        print(f"[{r['project_slug']}] [{r['agent']}] {role_label} {ts}")
        if context > 0:
            before, after = _fetch_context(con, r["session_id"], r["timestamp"], context)
            for c in before:
                print(f"  ↑ [{c['role']}] {c['excerpt']}")
        print(f"  {r['excerpt']}")
        if context > 0:
            for c in after:
                print(f"  ↓ [{c['role']}] {c['excerpt']}")
        print()


# ── Backfill commands ─────────────────────────────────────────────────────────

def embed_backfill(con: apsw.Connection) -> None:
    if _vc is None:
        print("sqlite-vec not loaded", file=sys.stderr)
        return
    if not EMBED_SOCK.exists():
        print("Embed socket not found", file=sys.stderr)
        return
    existing = {r[0] for r in _vc.execute("SELECT rowid FROM message_vecs").fetchall()}
    rows = con.execute("SELECT rowid, content FROM messages").fetchall()
    pending = [r for r in rows if r["rowid"] not in existing]
    total = len(pending)
    print(f"Embedding {total:,} messages…")
    done = 0
    for r in pending:
        vec = embed(r["content"])
        if vec:
            _vec_insert(r["rowid"], vec)
            done += 1
        if done % 500 == 0 and done > 0:
            print(f"  {done}/{total}…")
    print(f"Done. {done:,} embeddings written.")


def backfill_clean(con: apsw.Connection) -> None:
    rows = con.execute("SELECT rowid, content FROM messages").fetchall()
    updated = 0
    for r in rows:
        cleaned = _clean_content(r["content"])
        if cleaned != r["content"]:
            con.execute("UPDATE messages SET content = ? WHERE rowid = ?",
                        (cleaned, r["rowid"]))
            updated += 1
    print(f"Cleaned {updated:,} messages. Rebuilding FTS…")
    con.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
    print("Done.")


def chunk_backfill(con: apsw.Connection) -> None:
    """Re-embed long messages whose vectors may pre-date server-side chunking.
    Chunking now happens inside the sidecar — one HTTP call per message."""
    _BACKFILL_MIN_CHARS = 1800  # ≈ 450 tokens; shorter texts always fit in model window
    if _vc is None or not EMBED_SOCK.exists():
        print("Embed service not available", file=sys.stderr)
        return
    rows = con.execute(
        "SELECT rowid, content FROM messages WHERE LENGTH(content) > ?",
        (_BACKFILL_MIN_CHARS,),
    ).fetchall()
    total = len(rows)
    print(f"Re-embedding {total:,} long messages via sidecar chunking…")
    done = 0
    for r in rows:
        vec = embed(r["content"])
        if vec:
            _vec_insert(r["rowid"], vec)
            done += 1
        if done % 100 == 0 and done > 0:
            print(f"  {done}/{total}…")
    print(f"Done. {done:,} re-embedded.")


def tool_error_backfill(con: apsw.Connection) -> None:
    indexed = 0
    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        for pattern in ("*.jsonl", "*/subagents/*.jsonl"):
            for jsonl_path in project_dir.glob(pattern):
                session_id = _session_id_from_path(jsonl_path)
                project_slug = _slug_from_path(jsonl_path)
                try:
                    with open(jsonl_path, "r", errors="replace") as f:
                        for lineno, raw in enumerate(f):
                            try:
                                rec = json.loads(raw)
                            except (json.JSONDecodeError, ValueError):
                                continue
                            if rec.get("type") != "user":
                                continue
                            msg = rec.get("message", {})
                            content_blocks = msg.get("content", [])
                            if not isinstance(content_blocks, list):
                                continue
                            timestamp = rec.get("timestamp")
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
                                try:
                                    con.execute(
                                        """INSERT OR IGNORE INTO messages
                                           (uuid, session_id, project_slug, role,
                                            content, timestamp, model)
                                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                                        (tr_uuid, session_id, project_slug, "tool_error",
                                         tr_text, timestamp, None),
                                    )
                                    changed = con.changes()
                                    if changed and _vc is not None:
                                        tr_rowid = con.execute(
                                            "SELECT rowid FROM messages WHERE uuid = ?",
                                            (tr_uuid,),
                                        ).fetchone()[0]
                                        tr_vec = embed(tr_text)
                                        if tr_vec:
                                            _vec_insert(tr_rowid, tr_vec)
                                    indexed += changed
                                except apsw.Error:
                                    pass
                except OSError:
                    pass
    print(f"Indexed {indexed:,} tool_result error(s).")


def stats(con: apsw.Connection) -> None:
    msg_count = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    session_count = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    project_count = con.execute(
        "SELECT COUNT(DISTINCT project_slug) FROM sessions"
    ).fetchone()[0]
    role_counts = con.execute(
        "SELECT role, COUNT(*) FROM messages GROUP BY role ORDER BY 2 DESC"
    ).fetchall()
    agent_counts = con.execute(
        "SELECT agent, COUNT(*) FROM messages GROUP BY agent ORDER BY 2 DESC"
    ).fetchall()
    vec_count = _vec_count()
    fts_row = con.execute(
        "SELECT sql FROM sqlite_master WHERE name='messages_fts'"
    ).fetchone()
    fts_tokenizer = "porter" if fts_row and "porter" in (fts_row[0] or "") else "default"
    print(f"Messages   : {msg_count:,}")
    print(f"Embedded   : {vec_count:,}  ({vec_count * 100 // msg_count if msg_count else 0}%)")
    print(f"Sessions   : {session_count:,}")
    print(f"Projects   : {project_count}")
    print(f"FTS        : {fts_tokenizer} tokenizer")
    print("By role    :")
    for role, count in role_counts:
        print(f"  {role:14s}: {count:,}")
    print("By agent   :")
    for agent, count in agent_counts:
        print(f"  {agent:14s}: {count:,}")
