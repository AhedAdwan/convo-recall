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
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(os.environ.get("CONVO_RECALL_DB",
               Path.home() / ".local" / "share" / "convo-recall" / "conversations.db"))
PROJECTS_DIR = Path(os.environ.get("CONVO_RECALL_PROJECTS",
                    Path.home() / ".claude" / "projects"))
EMBED_SOCK = Path(os.environ.get("CONVO_RECALL_SOCK",
                  Path.home() / ".local" / "share" / "convo-recall" / "embed.sock"))

EMBED_DIM = 1024
MAX_QUERY_LEN = 2048
RRF_K = 60
DECAY_HALF_LIFE_DAYS = 90

# apsw + sqlite-vec for vector ops (sqlite3 lacks enable_load_extension on macOS)
_vc = None  # module-level apsw connection, initialised in open_db()


def _open_vec_con():
    try:
        import apsw, sqlite_vec
        con = apsw.Connection(str(DB_PATH))
        con.enableloadextension(True)
        sqlite_vec.load(con)
        con.enableloadextension(False)
        return con
    except Exception as e:
        print(f"[warn] sqlite-vec unavailable (FTS-only mode): {e}", file=sys.stderr)
        return None


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

def open_db() -> sqlite3.Connection:
    global _vc
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    _init_schema(con)
    _migrate_fts_porter(con)
    _vc = _open_vec_con()
    if _vc:
        _init_vec_tables(_vc)
    return con


def close_db(con: sqlite3.Connection) -> None:
    """Close the sqlite3 connection and the apsw vec connection."""
    global _vc
    con.close()
    if _vc is not None:
        _vc.close()
        _vc = None


def _init_schema(con: sqlite3.Connection) -> None:
    con.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id   TEXT PRIMARY KEY,
            project_slug TEXT NOT NULL,
            title        TEXT,
            first_seen   TEXT NOT NULL,
            last_updated TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS messages (
            uuid         TEXT PRIMARY KEY,
            session_id   TEXT NOT NULL,
            project_slug TEXT NOT NULL,
            role         TEXT NOT NULL,
            content      TEXT NOT NULL,
            timestamp    TEXT,
            model        TEXT
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
            content,
            session_id   UNINDEXED,
            project_slug UNINDEXED,
            role         UNINDEXED,
            content='messages',
            content_rowid='rowid',
            tokenize='porter unicode61'
        );

        CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, content, session_id, project_slug, role)
            VALUES (new.rowid, new.content, new.session_id, new.project_slug, new.role);
        END;

        CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, content, session_id, project_slug, role)
            VALUES ('delete', old.rowid, old.content, old.session_id, old.project_slug, old.role);
        END;

        CREATE TABLE IF NOT EXISTS ingested_files (
            file_path      TEXT PRIMARY KEY,
            session_id     TEXT NOT NULL,
            project_slug   TEXT NOT NULL,
            lines_ingested INTEGER NOT NULL DEFAULT 0,
            last_modified  REAL NOT NULL
        );
    """)
    con.commit()


def _migrate_fts_porter(con: sqlite3.Connection) -> None:
    row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='messages_fts'"
    ).fetchone()
    if row and "porter" in (row[0] or ""):
        return
    print("[migrate] Rebuilding FTS index with porter unicode61 tokenizer…", file=sys.stderr)
    con.executescript("""
        DROP TRIGGER IF EXISTS messages_ai;
        DROP TRIGGER IF EXISTS messages_ad;
        DROP TABLE IF EXISTS messages_fts;

        CREATE VIRTUAL TABLE messages_fts USING fts5(
            content,
            session_id   UNINDEXED,
            project_slug UNINDEXED,
            role         UNINDEXED,
            content='messages',
            content_rowid='rowid',
            tokenize='porter unicode61'
        );

        CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, content, session_id, project_slug, role)
            VALUES (new.rowid, new.content, new.session_id, new.project_slug, new.role);
        END;

        CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, content, session_id, project_slug, role)
            VALUES ('delete', old.rowid, old.content, old.session_id, old.project_slug, old.role);
        END;

        INSERT INTO messages_fts(messages_fts) VALUES('rebuild');
    """)
    con.commit()
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


def _extract_text(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [b.get("text", "").strip() for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(p for p in parts if p)
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


# ── Ingestion ─────────────────────────────────────────────────────────────────

def ingest_file(con: sqlite3.Connection, jsonl_path: Path,
                do_embed: bool = True) -> int:
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
                       (uuid, session_id, project_slug, role, content, timestamp, model)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (uuid, session_id, project_slug, role, text, timestamp, model),
                )
                changed = con.execute("SELECT changes()").fetchone()[0]
                if changed and do_embed and _vc is not None:
                    rowid = con.execute(
                        "SELECT rowid FROM messages WHERE uuid = ?", (uuid,)
                    ).fetchone()[0]
                    vec = embed(text)
                    if vec:
                        _vec_insert(rowid, vec)
                inserted += changed
            except sqlite3.Error:
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
                                    content, timestamp, model)
                                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                                (tr_uuid, session_id, project_slug, "tool_error",
                                 tr_text, timestamp, None),
                            )
                            tr_changed = con.execute("SELECT changes()").fetchone()[0]
                            if tr_changed and do_embed and _vc is not None:
                                tr_rowid = con.execute(
                                    "SELECT rowid FROM messages WHERE uuid = ?", (tr_uuid,)
                                ).fetchone()[0]
                                tr_vec = embed(tr_text)
                                if tr_vec:
                                    _vec_insert(tr_rowid, tr_vec)
                            inserted += tr_changed
                        except sqlite3.Error:
                            pass

    now = datetime.now(timezone.utc).isoformat()
    con.execute(
        """INSERT INTO sessions (session_id, project_slug, title, first_seen, last_updated)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(session_id) DO UPDATE SET
               title = COALESCE(excluded.title, sessions.title),
               last_updated = excluded.last_updated""",
        (session_id, project_slug, title, now, now),
    )
    con.execute(
        """INSERT INTO ingested_files
               (file_path, session_id, project_slug, lines_ingested, last_modified)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(file_path) DO UPDATE SET
               lines_ingested = excluded.lines_ingested,
               last_modified  = excluded.last_modified""",
        (file_key, session_id, project_slug, lines_read, stat.st_mtime),
    )
    con.commit()
    return inserted


def scan_all(con: sqlite3.Connection, verbose: bool = False,
             do_embed: bool = True) -> None:
    embed_live = EMBED_SOCK.exists() and do_embed
    if not embed_live and do_embed:
        print("[warn] embed socket not found — running in FTS-only mode", file=sys.stderr)

    total = 0
    files = 0
    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        for pattern in ("*.jsonl", "*/subagents/*.jsonl"):
            for jsonl_path in project_dir.glob(pattern):
                n = ingest_file(con, jsonl_path, do_embed=embed_live)
                if n > 0:
                    files += 1
                    total += n
                    if verbose:
                        slug = _slug_from_path(jsonl_path)
                        print(f"  +{n:4d} msgs  {slug}/{jsonl_path.name[:8]}…")
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

def _fetch_context(con: sqlite3.Connection, session_id: str,
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


def search(con: sqlite3.Connection, query: str, limit: int = 10,
           recent: bool = False, project: str | None = None,
           context: int = 1) -> None:
    if len(query) > MAX_QUERY_LEN:
        query = query[:MAX_QUERY_LEN]

    use_vec = _vc is not None and EMBED_SOCK.exists()
    qvec = None
    if use_vec:
        qvec = embed(query, mode="query")
        use_vec = qvec is not None

    project_rowids: set[int] | None = None
    if project:
        rows = con.execute(
            "SELECT rowid FROM messages WHERE project_slug = ?", (project,)
        ).fetchall()
        project_rowids = {r[0] for r in rows}
        if not project_rowids:
            print(f"No messages found for project '{project}'.")
            return

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
            f"""SELECT rowid, session_id, project_slug, role, timestamp,
                       SUBSTR(content, 1, 300) AS excerpt
                FROM messages WHERE rowid IN ({placeholders})""",
            scored,
        ).fetchall()
    else:
        rows = con.execute(
            """SELECT m.rowid, m.session_id, m.project_slug, m.role, m.timestamp,
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
        print(f"[{r['project_slug']}] {role_label} {ts}")
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

def embed_backfill(con: sqlite3.Connection) -> None:
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


def backfill_clean(con: sqlite3.Connection) -> None:
    rows = con.execute("SELECT rowid, content FROM messages").fetchall()
    updated = 0
    for r in rows:
        cleaned = _clean_content(r["content"])
        if cleaned != r["content"]:
            con.execute("UPDATE messages SET content = ? WHERE rowid = ?",
                        (cleaned, r["rowid"]))
            updated += 1
    con.commit()
    print(f"Cleaned {updated:,} messages. Rebuilding FTS…")
    con.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
    con.commit()
    print("Done.")


def chunk_backfill(con: sqlite3.Connection) -> None:
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


def tool_error_backfill(con: sqlite3.Connection) -> None:
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
                                    changed = con.execute("SELECT changes()").fetchone()[0]
                                    if changed and _vc is not None:
                                        tr_rowid = con.execute(
                                            "SELECT rowid FROM messages WHERE uuid = ?",
                                            (tr_uuid,),
                                        ).fetchone()[0]
                                        tr_vec = embed(tr_text)
                                        if tr_vec:
                                            _vec_insert(tr_rowid, tr_vec)
                                    indexed += changed
                                except sqlite3.Error:
                                    pass
                except OSError:
                    pass
        con.commit()
    print(f"Indexed {indexed:,} tool_result error(s).")


def stats(con: sqlite3.Connection) -> None:
    msg_count = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    session_count = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    project_count = con.execute(
        "SELECT COUNT(DISTINCT project_slug) FROM sessions"
    ).fetchone()[0]
    role_counts = con.execute(
        "SELECT role, COUNT(*) FROM messages GROUP BY role ORDER BY 2 DESC"
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
