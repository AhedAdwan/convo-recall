"""
Core ingestion, search, and backfill logic for convo-recall.

Paths default to standard Claude Code locations but are configurable
via environment variables:
  CONVO_RECALL_DB       — path to SQLite DB (default ~/.local/share/convo-recall/conversations.db)
  CONVO_RECALL_PROJECTS — path to Claude projects dir (default ~/.claude/projects)
  CONVO_RECALL_SOCK     — path to embed UDS socket (default ~/.local/share/convo-recall/embed.sock)
"""

import hashlib
import http.client
import json
import math
import os
import re
import socket
import struct
import sys

import apsw
import weakref
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

EMBED_DIM = 1024
MAX_QUERY_LEN = 2048
RRF_K = 60
DECAY_HALF_LIFE_DAYS = 90

# Vec-enabled state lives per-connection so multiple open_db() calls in one
# process (test runners, in-memory bench harnesses) don't clobber each other.
# A single apsw connection serves both FTS and vector ops to avoid the
# cross-libsqlite3-version corruption that occurs when stdlib sqlite3 (e.g.
# 3.45 on Ubuntu 24.04) shares a DB file with apsw's bundled sqlite (3.53).
_VEC_ENABLED: "weakref.WeakKeyDictionary[apsw.Connection, bool]" = weakref.WeakKeyDictionary()


def _vec_ok(con: apsw.Connection) -> bool:
    return _VEC_ENABLED.get(con, False)


# Backward-compat shim: tests historically monkeypatched `_vc` to None. We keep
# the name as a property-like attribute that resolves to the most recently
# opened vec-enabled connection (or None). New code should use `_vec_ok(con)`
# and pass the connection through to vec helpers.
_vc: apsw.Connection | None = None  # last vec-enabled connection (legacy)


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
    if os.environ.get("CONVO_RECALL_REDACT") != "off":
        text = _redact.redact_secrets(text)
    text = _BLANK_LINES_RE.sub('\n\n', text)
    text = _expand_code_tokens(text)
    return text.strip()


# ── Embedding client ──────────────────────────────────────────────────────────

# Sidecar timeout: the model is in-process and embedding is fast (<200ms
# warm), but a hung sidecar (deadlock, GPU stall) used to freeze ingestion
# indefinitely. Cap at 30s — generous enough for long inputs that need
# server-side chunking, short enough to surface a problem same-day.
_EMBED_TIMEOUT_S = 30.0


class _UnixHTTPConn(http.client.HTTPConnection):
    def __init__(self, sock_path: str, timeout: float = _EMBED_TIMEOUT_S):
        super().__init__("localhost", timeout=timeout)
        self._sock_path = sock_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._sock_path)


def embed(text: str, mode: str = "document") -> list[float] | None:
    """POST text to the UDS embed service. Returns None if unreachable
    or the sidecar returns a non-200 / malformed response.

    Long texts are chunked and mean-pooled by the sidecar — no client-side
    truncation. Caller falls back to FTS-only when this returns None.
    """
    body = json.dumps({"text": text, "mode": mode}).encode()
    conn = _UnixHTTPConn(str(EMBED_SOCK))
    try:
        conn.request("POST", "/embed", body=body,
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        if resp.status != 200:
            # Drain so the connection can close cleanly. Don't spam stderr —
            # a sidecar that's rate-limiting (429) or briefly unhealthy is a
            # transient condition, and the caller already degrades to FTS.
            try: resp.read()
            except Exception: pass
            return None
        return json.loads(resp.read()).get("vector")
    except (ConnectionRefusedError, FileNotFoundError, OSError, socket.timeout):
        return None  # service down or hung — expected when sidecar isn't healthy
    except Exception as e:
        print(f"[warn] embed: {type(e).__name__}: {e}", file=sys.stderr)
        return None
    finally:
        conn.close()


def _vec_bytes(v: list[float]) -> bytes:
    return struct.pack(f"{len(v)}f", *v)


def _wait_for_embed_socket(timeout_s: float = 5.0,
                           poll_interval_s: float = 0.2,
                           verbose: bool = False) -> bool:
    """Poll for `EMBED_SOCK.exists()` up to `timeout_s` seconds.

    Returns True if the socket exists by the deadline (immediately if it
    already does), False on timeout. Used by ingest + embed-backfill to
    close the race where:

      1. The wizard starts the embed sidecar systemd unit.
      2. The wizard immediately spawns the detached _backfill-chain.
      3. The chain calls `open_db()` and ingest before the sidecar has
         finished loading the model + binding the socket (~5s on Linux).
      4. `embed_live = EMBED_SOCK.exists()` was set to False at the start
         of ingest → self-heal pass + embed-backfill silently no-op.

    Pre-fix the race only manifested on truly cold installs (the bug
    pattern the user hit on a fresh Linux sandbox). On warm systems
    (e.g. macOS rerunning install with the launchd sidecar already up)
    the socket exists at step 4 → no race → coincidentally "just works".
    """
    if EMBED_SOCK.exists():
        return True
    if verbose:
        print(f"[ingest] waiting up to {timeout_s:.0f}s for embed socket "
              f"at {EMBED_SOCK} …", file=sys.stderr)
    import time as _time
    deadline = _time.time() + timeout_s
    while _time.time() < deadline:
        if EMBED_SOCK.exists():
            if verbose:
                elapsed = timeout_s - (deadline - _time.time())
                print(f"[ingest] embed socket appeared after {elapsed:.1f}s",
                      file=sys.stderr)
            return True
        _time.sleep(poll_interval_s)
    if verbose:
        print(f"[ingest] embed socket did not appear within {timeout_s:.0f}s — "
              f"running in FTS-only mode", file=sys.stderr)
    return False


# ── DB setup ──────────────────────────────────────────────────────────────────

def _harden_perms(path: Path, mode: int) -> None:
    """Set mode to {mode} if not already; ignore if file doesn't exist."""
    try:
        if path.exists() and (os.stat(path).st_mode & 0o777) != mode:
            os.chmod(path, mode)
    except OSError:
        pass


def _enable_wal_mode(con: apsw.Connection) -> None:
    """Wrapper around the WAL pragma. Extracted so tests can monkeypatch
    it to simulate the codex-style sandbox where WAL sidecar creation
    fails with apsw.CantOpenError."""
    con.execute("PRAGMA journal_mode=WAL")


def open_db(readonly: bool = False) -> apsw.Connection:
    global _vc
    # Read-only mode: open the DB without trying to create sidecars or
    # chmod the parent dir. Used by search/stats/doctor under sandboxed
    # subprocess contexts (e.g. Codex CLI restricts writes to the
    # project working dir; WAL mode creates `.db-wal` and `.db-shm`
    # outside that dir → apsw.CantOpenError on the WAL pragma).
    if readonly:
        if not DB_PATH.exists():
            # Read-only on a missing DB is a hard error — there's
            # nothing to read. Surface it before apsw does.
            raise apsw.CantOpenError(
                f"DB not found at {DB_PATH} (CONVO_RECALL_DB not set; "
                f"run `recall install` or set the env var)"
            )
        con = apsw.Connection(str(DB_PATH), flags=apsw.SQLITE_OPEN_READONLY)
        con.row_trace = _row_factory
        try:
            import sqlite_vec
            con.enableloadextension(True)
            sqlite_vec.load(con)
            con.enableloadextension(False)
            _VEC_ENABLED[con] = True
            _vc = con
        except Exception:
            pass  # FTS-only is fine for read-only callers
        return con

    # Owner-only on the parent dir AND the DB + WAL/SHM sidecars. The DB
    # contains conversation history including any secrets pasted into chats;
    # default umask 0o022 would publish it to other UIDs on a multi-user box.
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _harden_perms(DB_PATH.parent, 0o700)
    con = apsw.Connection(str(DB_PATH))
    con.row_trace = _row_factory
    try:
        _enable_wal_mode(con)
    except apsw.CantOpenError:
        # Sandboxed subprocess (codex CLI, seatbelt, landlock-restricted
        # shell) — the parent dir is readable but writes are blocked, so
        # WAL can't create its sidecars. Auto-fall back to read-only so
        # search/stats/doctor still work; write-needing operations will
        # fail clearly when they try.
        print("[warn] DB write access denied — falling back to read-only "
              "mode. `recall ingest` and friends won't work in this "
              "shell; set CONVO_RECALL_DB to a writable path or run "
              "outside the sandbox.", file=sys.stderr)
        con.close()
        return open_db(readonly=True)
    con.execute("PRAGMA synchronous=NORMAL")
    for sidecar in (DB_PATH, DB_PATH.with_suffix(DB_PATH.suffix + "-wal"),
                    DB_PATH.with_suffix(DB_PATH.suffix + "-shm")):
        _harden_perms(sidecar, 0o600)
    try:
        import sqlite_vec
        con.enableloadextension(True)
        sqlite_vec.load(con)
        con.enableloadextension(False)
        _VEC_ENABLED[con] = True
        _vc = con
    except Exception as e:
        print(f"[warn] sqlite-vec unavailable (FTS-only mode): {e}", file=sys.stderr)
        _VEC_ENABLED[con] = False
        _vc = None
    _init_schema(con)
    _ensure_migrations_table(con)
    _migrate_add_agent_column(con)
    _migrate_fts_porter(con)
    _migrate_project_id(con)
    if _vec_ok(con):
        _init_vec_tables(con)
    return con


def close_db(con: apsw.Connection) -> None:
    """Close one apsw connection. Per-connection vec state is auto-cleaned
    by the WeakKeyDictionary when `con` is garbage-collected, but we also
    clear the legacy `_vc` shim if it pointed here."""
    global _vc
    if _vc is con:
        _vc = None
    _VEC_ENABLED.pop(con, None)
    con.close()


def _init_schema(con: apsw.Connection) -> None:
    con.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id   TEXT PRIMARY KEY,
            project_id   TEXT NOT NULL,
            title        TEXT,
            first_seen   TEXT NOT NULL,
            last_updated TEXT NOT NULL,
            agent        TEXT NOT NULL DEFAULT 'claude'
        );

        CREATE TABLE IF NOT EXISTS messages (
            uuid         TEXT PRIMARY KEY,
            session_id   TEXT NOT NULL,
            project_id   TEXT NOT NULL,
            role         TEXT NOT NULL,
            content      TEXT NOT NULL,
            timestamp    TEXT,
            model        TEXT,
            agent        TEXT NOT NULL DEFAULT 'claude'
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
            content,
            session_id   UNINDEXED,
            project_id   UNINDEXED,
            role         UNINDEXED,
            agent        UNINDEXED,
            content='messages',
            content_rowid='rowid',
            tokenize='porter unicode61'
        );

        CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, content, session_id, project_id, role, agent)
            VALUES (new.rowid, new.content, new.session_id, new.project_id, new.role, new.agent);
        END;

        CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, content, session_id, project_id, role, agent)
            VALUES ('delete', old.rowid, old.content, old.session_id, old.project_id, old.role, old.agent);
        END;

        CREATE TABLE IF NOT EXISTS ingested_files (
            file_path      TEXT PRIMARY KEY,
            session_id     TEXT NOT NULL,
            project_id     TEXT NOT NULL,
            lines_ingested INTEGER NOT NULL DEFAULT 0,
            last_modified  REAL NOT NULL,
            agent          TEXT NOT NULL DEFAULT 'claude'
        );

        CREATE TABLE IF NOT EXISTS projects (
            project_id    TEXT PRIMARY KEY,
            display_name  TEXT NOT NULL,
            cwd_realpath  TEXT,
            first_seen    TEXT NOT NULL,
            last_updated  TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_projects_display_name
            ON projects(display_name);
    """)


def _upsert_project(con: apsw.Connection, project_id: str,
                    display_name: str, cwd_realpath: str | None) -> None:
    """Insert (project_id, display_name, cwd_realpath) or refresh on conflict.

    Preserves first_seen on update; bumps last_updated and overwrites
    display_name + cwd_realpath with the latest observed values.
    """
    now = datetime.now(timezone.utc).isoformat()
    con.execute(
        "INSERT INTO projects(project_id, display_name, cwd_realpath, "
        "first_seen, last_updated) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(project_id) DO UPDATE SET "
        "display_name = excluded.display_name, "
        "cwd_realpath = COALESCE(excluded.cwd_realpath, projects.cwd_realpath), "
        "last_updated = excluded.last_updated",
        (project_id, display_name, cwd_realpath, now, now),
    )


def _has_column(con: apsw.Connection, table: str, column: str) -> bool:
    cols = con.execute(f"PRAGMA table_info({table})").fetchall()
    names = {c["name"] if isinstance(c, _Row) else c[1] for c in cols}
    return column in names


def _ensure_migrations_table(con: apsw.Connection) -> None:
    con.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )


def _migration_applied(con: apsw.Connection, version: int) -> bool:
    row = con.execute(
        "SELECT 1 FROM schema_migrations WHERE version = ?", (version,)
    ).fetchone()
    return row is not None


def _record_migration(con: apsw.Connection, version: int) -> None:
    con.execute(
        "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
        (version, datetime.now(timezone.utc).isoformat()),
    )


# Schema versions. New migrations append here.
_MIGRATION_AGENT_COLUMN = 2  # v2: add agent column to sessions/messages/ingested_files
_MIGRATION_FTS_PORTER = 3    # v3: rebuild FTS with porter+unicode61 + agent column
_MIGRATION_PROJECT_ID = 4    # v4: rename project_slug → project_id; populate projects table


def _migrate_add_agent_column(con: apsw.Connection) -> None:
    """Add `agent` column to legacy DBs that pre-date multi-agent support.

    Idempotent: gated on schema_migrations version 2; falls back to PRAGMA
    table_info check for DBs that pre-date the schema_migrations table.
    Backfills existing rows with 'claude' (the only agent before).
    """
    if _migration_applied(con, _MIGRATION_AGENT_COLUMN):
        return
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
    _record_migration(con, _MIGRATION_AGENT_COLUMN)


def _migrate_fts_porter(con: apsw.Connection) -> None:
    """Migrate FTS table to porter+unicode61 tokenizer if needed AND make sure
    the FTS schema includes the `agent` UNINDEXED column. Both conditions
    trigger the same drop-rebuild flow (they share a code path)."""
    if _migration_applied(con, _MIGRATION_FTS_PORTER):
        return
    row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='messages_fts'"
    ).fetchone()
    sql = (row[0] or "") if row else ""
    needs_porter = "porter" not in sql
    needs_agent = "agent" not in sql
    if not (needs_porter or needs_agent):
        _record_migration(con, _MIGRATION_FTS_PORTER)
        return
    why = []
    if needs_porter: why.append("porter unicode61 tokenizer")
    if needs_agent: why.append("agent column")
    print(f"[migrate] Rebuilding FTS index ({', '.join(why)})…", file=sys.stderr)
    # Wrap the multi-statement migration in a single transaction so a SIGTERM
    # or crash mid-way leaves the DB in either the old or new state — never
    # a half-migrated state with no FTS table where subsequent inserts would
    # silently go un-indexed.
    try:
        con.execute("BEGIN IMMEDIATE")
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
        con.execute("COMMIT")
    except Exception:
        try: con.execute("ROLLBACK")
        except Exception: pass
        raise
    _record_migration(con, _MIGRATION_FTS_PORTER)
    print("[migrate] Done.", file=sys.stderr)


# ── v4: project_slug → project_id + projects table ───────────────────────────

def _legacy_project_id(old_slug: str) -> str:
    """Synthesize project_id for legacy slugs whose real cwd cannot be recovered."""
    return hashlib.sha1(("legacy:" + old_slug).encode("utf-8")).hexdigest()[:12]


def _gemini_hash_project_id(hash_dir: str) -> str:
    """Synthesize project_id for Gemini hash-only sessions."""
    return hashlib.sha1(("gemini-hash:" + hash_dir).encode("utf-8")).hexdigest()[:12]


def _scan_claude_cwd(slug: str) -> str | None:
    """Scan Claude jsonl files for any session matching `slug`, return cwd field.

    Claude stores its session dir as `cwd.replace('/', '-')` — lossy. We can't
    reverse the encoding without scanning record bodies. Read up to ~200 lines
    of each candidate file looking for a `cwd` key.
    """
    if not PROJECTS_DIR.exists():
        return None
    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        # _legacy_claude_slug collapses hyphens to underscores; reverse-test
        # by comparing the slug derivation. Cheap because we're not iterating
        # all files yet — just dirs.
        try:
            test_slug = _legacy_claude_slug(project_dir / "x.jsonl")
        except Exception:
            continue
        if test_slug != slug:
            continue
        for sess in list(project_dir.glob("*.jsonl"))[:5]:
            try:
                with open(sess) as fh:
                    for i, line in enumerate(fh):
                        if i > 200:
                            break
                        try:
                            d = json.loads(line)
                        except (json.JSONDecodeError, ValueError):
                            continue
                        if isinstance(d, dict) and d.get("cwd"):
                            return d["cwd"]
            except OSError:
                continue
    return None


def _scan_codex_cwd(slug: str) -> str | None:
    """Scan Codex rollouts whose session_meta payload.cwd derives `slug`."""
    if not CODEX_SESSIONS.exists():
        return None
    # Cheap: stop at the first matching cwd. Walk newest first to bias to recent.
    files = sorted(CODEX_SESSIONS.glob("*/*/*/rollout-*.jsonl"), reverse=True)
    for f in files[:200]:  # cap scan budget
        try:
            with open(f) as fh:
                first = json.loads(fh.readline())
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        cwd = (first.get("payload") or {}).get("cwd")
        if not cwd:
            continue
        if _legacy_codex_slug(cwd) == slug:
            return cwd
    return None


def _scan_gemini_cwd(slug: str) -> tuple[str | None, str | None]:
    """For Gemini, attempt to recover real cwd via ~/.gemini/projects.json.

    Returns (cwd, hash_dir_or_None). hash_dir is set when slug looks like a
    SHA-hash dir name and we can't resolve to a real path.
    """
    aliases = _load_gemini_aliases()
    # aliases is {hash_dir → real_cwd}
    for hash_dir, cwd in aliases.items():
        if _legacy_codex_slug(cwd) == slug:
            return cwd, hash_dir
    # No alias hit — slug might already be a hash_dir name
    return None, slug


def _migrate_project_id(con: apsw.Connection) -> None:
    """v4 migration: project_slug → project_id; populate projects table; rebuild FTS.

    Idempotent: gated on _MIGRATION_PROJECT_ID. Snapshots DB to .pre-project-id.<ts>.bak
    before any DDL. On a FRESH DB whose tables are already at the post-v4 shape
    (project_id columns), records the migration and only ensures the projects
    table is in sync — no rename, no FTS rebuild.
    """
    import shutil

    if _migration_applied(con, _MIGRATION_PROJECT_ID):
        return

    fresh_shape = _has_column(con, "messages", "project_id")
    if fresh_shape:
        # Fresh DB born at v4: nothing to rename, nothing to backfill,
        # FTS already correct. Just record the migration.
        _record_migration(con, _MIGRATION_PROJECT_ID)
        return

    # Legacy DB — snapshot first
    if DB_PATH.exists() and str(DB_PATH) not in (":memory:",):
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        bak = DB_PATH.with_suffix(DB_PATH.suffix + f".pre-project-id.{ts}.bak")
        try:
            shutil.copy2(str(DB_PATH), str(bak))
            print(f"[migrate] Snapshot saved: {bak}", file=sys.stderr)
        except OSError as e:
            print(f"[migrate] WARNING: snapshot failed ({e}); continuing.", file=sys.stderr)

    print("[migrate] Renaming project_slug → project_id and populating projects table…",
          file=sys.stderr)

    # Build slug → (project_id, display_name, cwd_real) map per agent.
    # Legacy slugs are agent-scoped: same slug under claude vs gemini may
    # have different real cwds. Group by (agent, project_slug).
    slug_pairs = con.execute(
        "SELECT DISTINCT agent, project_slug FROM sessions"
    ).fetchall()

    # Each row may be _Row or tuple
    def _row(r, key, idx):
        try:
            return r[key]
        except (KeyError, TypeError):
            return r[idx]

    mapping: dict[tuple[str, str], tuple[str, str, str | None]] = {}
    for row in slug_pairs:
        agent = _row(row, "agent", 0)
        slug = _row(row, "project_slug", 1)
        cwd: str | None = None
        gemini_hash: str | None = None
        if agent == "claude":
            cwd = _scan_claude_cwd(slug)
        elif agent == "codex":
            cwd = _scan_codex_cwd(slug)
        elif agent == "gemini":
            cwd, gemini_hash = _scan_gemini_cwd(slug)

        if cwd:
            pid = _project_id(cwd)
            display = _display_name(cwd)
            cwd_real = os.path.realpath(cwd)
        elif agent == "gemini" and gemini_hash:
            pid = _gemini_hash_project_id(gemini_hash)
            display = gemini_hash
            cwd_real = None
        else:
            pid = _legacy_project_id(slug)
            display = slug
            cwd_real = None
        mapping[(agent, slug)] = (pid, display, cwd_real)

    try:
        con.execute("BEGIN IMMEDIATE")

        # Populate projects table from the mapping
        now = datetime.now(timezone.utc).isoformat()
        for (_agent, _slug), (pid, display, cwd_real) in mapping.items():
            con.execute(
                "INSERT INTO projects(project_id, display_name, cwd_realpath, "
                "first_seen, last_updated) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(project_id) DO UPDATE SET "
                "display_name = excluded.display_name, "
                "cwd_realpath = COALESCE(excluded.cwd_realpath, projects.cwd_realpath), "
                "last_updated = excluded.last_updated",
                (pid, display, cwd_real, now, now),
            )

        # Rename columns. SQLite ≥3.25 supports ALTER TABLE … RENAME COLUMN;
        # apsw bundles ≥3.45.
        for table in ("sessions", "messages", "ingested_files"):
            if _has_column(con, table, "project_slug"):
                con.execute(
                    f"ALTER TABLE {table} RENAME COLUMN project_slug TO project_id"
                )

        # Backfill project_id values per (agent, old_slug)
        for (agent, slug), (pid, _display, _cwd) in mapping.items():
            con.execute(
                "UPDATE sessions SET project_id = ? "
                "WHERE agent = ? AND project_id = ?",
                (pid, agent, slug),
            )
            con.execute(
                "UPDATE messages SET project_id = ? "
                "WHERE agent = ? AND project_id = ?",
                (pid, agent, slug),
            )
            con.execute(
                "UPDATE ingested_files SET project_id = ? "
                "WHERE agent = ? AND project_id = ?",
                (pid, agent, slug),
            )

        # Rebuild FTS: drop old (with project_slug column), recreate with project_id
        print("[migrate] Rebuilding FTS index (project_id rename)…", file=sys.stderr)
        con.execute("""
            DROP TRIGGER IF EXISTS messages_ai;
            DROP TRIGGER IF EXISTS messages_ad;
            DROP TABLE IF EXISTS messages_fts;

            CREATE VIRTUAL TABLE messages_fts USING fts5(
                content,
                session_id   UNINDEXED,
                project_id   UNINDEXED,
                role         UNINDEXED,
                agent        UNINDEXED,
                content='messages',
                content_rowid='rowid',
                tokenize='porter unicode61'
            );

            CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
                INSERT INTO messages_fts(rowid, content, session_id, project_id, role, agent)
                VALUES (new.rowid, new.content, new.session_id, new.project_id, new.role, new.agent);
            END;

            CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
                INSERT INTO messages_fts(messages_fts, rowid, content, session_id, project_id, role, agent)
                VALUES ('delete', old.rowid, old.content, old.session_id, old.project_id, old.role, old.agent);
            END;

            INSERT INTO messages_fts(messages_fts) VALUES('rebuild');
        """)
        con.execute("COMMIT")
    except Exception:
        try: con.execute("ROLLBACK")
        except Exception: pass
        raise

    _record_migration(con, _MIGRATION_PROJECT_ID)
    print("[migrate] project_id migration complete.", file=sys.stderr)


def _init_vec_tables(vc) -> None:
    vc.execute(f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS message_vecs USING vec0(
            rowid INTEGER PRIMARY KEY,
            embedding FLOAT[{EMBED_DIM}]
        )
    """)


def _vec_insert(con: apsw.Connection, rowid: int, vec: list[float]) -> None:
    if not _vec_ok(con):
        return
    try:
        con.execute(
            "INSERT OR REPLACE INTO message_vecs(rowid, embedding) VALUES (?, ?)",
            (rowid, _vec_bytes(vec)),
        )
    except Exception:
        pass


def _vec_search(con: apsw.Connection, qvec: list[float], k: int = 100,
                restrict_rowids: set[int] | None = None) -> list[int]:
    """Vector KNN search. When `restrict_rowids` is set, results are limited
    to that subset. For small subsets (<500) we compute cosine in Python
    against the filtered embeddings — exact recall, sub-millisecond at this
    size. For larger sets we ask sqlite-vec for a generous top-k and let the
    caller intersect.
    """
    if not _vec_ok(con):
        return []
    try:
        if restrict_rowids is not None and len(restrict_rowids) < 500:
            placeholders = ",".join("?" * len(restrict_rowids))
            rows = con.execute(
                f"SELECT rowid, embedding FROM message_vecs "
                f"WHERE rowid IN ({placeholders})",
                tuple(restrict_rowids),
            ).fetchall()
            qbytes = _vec_bytes(qvec)
            scored: list[tuple[float, int]] = []
            qf = struct.unpack(f"{EMBED_DIM}f", qbytes)
            for r in rows:
                emb = struct.unpack(f"{EMBED_DIM}f", r[1])
                # Vectors from BAAI/bge-large-en-v1.5 are L2-normalized at the
                # sidecar, so dot product == cosine similarity. No norm needed.
                dot = sum(a * b for a, b in zip(qf, emb))
                scored.append((dot, r[0]))
            scored.sort(reverse=True)
            return [rid for _, rid in scored[:k]]

        rows = con.execute(
            "SELECT rowid FROM message_vecs WHERE embedding MATCH ? AND k = ?",
            (_vec_bytes(qvec), k),
        ).fetchall()
        return [r[0] for r in rows]
    except Exception:
        return []


def _vec_count(con: apsw.Connection) -> int:
    if not _vec_ok(con):
        return 0
    try:
        return con.execute("SELECT COUNT(*) FROM message_vecs").fetchone()[0]
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

def _legacy_claude_slug(jsonl_path: Path) -> str:
    """Lossy slug from Claude's flattened storage dir; legacy fallback only.

    Used by (a) the v4 migration to match an existing legacy slug to its
    source dir, and (b) the Claude ingest as a last-resort display_name when
    no cwd field is present in any record. New rows always carry a real
    project_id derived from cwd via _project_id().
    """
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


_ROOT_MARKERS = (
    ".git", "package.json", "Cargo.toml", "pyproject.toml",
    "go.mod", "pom.xml", "build.gradle", "build.gradle.kts",
    "deno.json", ".projectile",
)


def _project_id(cwd) -> str:
    """Stable 12-hex id from realpath(cwd). Same dir → same id forever.

    Built from os.path.realpath so symlinked paths that resolve to the same
    target collapse to one id. Hyphen-vs-slash safe because the input is
    a real path, not the lossy hyphen-encoded directory name Claude uses.
    """
    real = os.path.realpath(str(cwd))
    return hashlib.sha1(real.encode("utf-8")).hexdigest()[:12]


def _display_name(cwd) -> str:
    """basename of nearest ancestor containing a project-root marker.

    Walks up from realpath(cwd) looking for any of _ROOT_MARKERS (.git,
    package.json, Cargo.toml, pyproject.toml, go.mod, …). Returns the
    basename of that ancestor. Falls back to basename of realpath(cwd)
    when no marker is found upstream.
    """
    real = Path(os.path.realpath(str(cwd)))
    for ancestor in (real, *real.parents):
        try:
            if any((ancestor / m).exists() for m in _ROOT_MARKERS):
                return ancestor.name or "/"
        except (OSError, PermissionError):
            continue
    return real.name or "/"


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
            if not text:
                continue

            uuid = rec.get("uuid", f"{session_id}:{lineno}")
            timestamp = rec.get("timestamp")
            model = msg.get("model") if role == "assistant" else None

            inserted += _persist_message(
                con, agent, project_id, session_id, uuid, role, text,
                timestamp, do_embed, model=model,
            )

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


def _legacy_gemini_slug(jsonl_path: Path) -> str:
    """Lossy slug from a Gemini session path; legacy fallback only.

    Used by the v4 migration to match an existing Gemini legacy slug to its
    source dir. New rows derive project_id from the session header's cwd.
    """
    return jsonl_path.parent.parent.name.replace("-", "_")


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
            if rtype not in ("user", "gemini"):
                continue
            # Defer message inserts until we've decided project_id (after header).
            if project_id is None:
                project_id = _gemini_hash_project_id(hash_dir)
                display_name = hash_dir
                cwd_real = None
            role = "user" if rtype == "user" else "assistant"
            text = _clean_content(_extract_text(rec.get("content", "")))
            if not text:
                continue
            uuid = rec.get("id") or f"{session_id}:{lineno}"
            timestamp = rec.get("timestamp")
            inserted += _persist_message(con, "gemini", project_id, session_id,
                                          uuid, role, text, timestamp, do_embed)

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


def _legacy_codex_slug(cwd: str) -> str:
    """Lossy slug from a cwd; legacy fallback only.

    Used by the v4 migration to match codex/gemini legacy slugs to their
    source files. New codex rows derive project_id from session_meta.payload.cwd
    via _project_id().
    """
    parts = Path(cwd).parts
    try:
        idx = next(i for i, p in enumerate(parts) if p.lower() == "projects")
        relevant = parts[idx + 1:]
        slug = "_".join(relevant) if relevant else Path(cwd).name
    except StopIteration:
        # No Projects/ in path — use last 2 path components
        relevant = parts[-2:] if len(parts) >= 2 else parts
        slug = "_".join(p for p in relevant if p and p != "/")
    return slug.replace("-", "_")


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
            if ttype == "session_meta":
                payload = rec.get("payload", {})
                session_id = payload.get("id", session_id)
                cwd = payload.get("cwd")
                if cwd:
                    _set_project_from_cwd(cwd)
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


def _safe_fts_query(query: str) -> str:
    """Convert a free-form user query into a safe FTS5 MATCH expression.

    FTS5 treats `-`, `.`, `:`, `(`, `*`, `AND`, `OR`, `NOT`, `NEAR` as
    operators / column refs. Passing a raw user string into
    `messages_fts MATCH ?` crashes on common inputs (`app-gemini` →
    "no such column: gemini"; `.*` → "syntax error near '.'").

    Strategy: split on whitespace, wrap each token in double quotes
    (FTS5's phrase syntax — special chars inside are literal), join
    with spaces. Multiple quoted tokens are implicit-AND'ed by FTS5,
    matching the prior behavior for normal multi-word queries.

    Edge cases:
      - empty input → returns a quoted empty string, which FTS5 reads
        as a no-match (caller prints "No results.").
      - embedded double quotes are doubled (FTS5's quote-escape
        convention).
      - tokens that consist entirely of FTS5-special chars (e.g. `.*`)
        end up as empty phrases, which FTS5 also no-matches cleanly.
    """
    if not query.strip():
        return '""'
    parts = []
    for token in query.split():
        # Strip leading/trailing FTS5 specials so a token like `.*` doesn't
        # produce an empty phrase that FTS5 treats as a syntax error in
        # some contexts. Internal punctuation is preserved (the tokenizer
        # handles word-boundary splitting inside the phrase).
        cleaned = token.strip('.*:()')
        if not cleaned:
            continue
        # Double-up any embedded double quotes per FTS5's escape convention.
        escaped = cleaned.replace('"', '""')
        parts.append(f'"{escaped}"')
    if not parts:
        return '""'
    return " ".join(parts)


_DEFAULT_TAIL_N = 30
_TAIL_WIDTH = 220                # per-message char budget before truncation
_TAIL_BODY_COLS = 76             # body wrap column (right-side body width)
_TAIL_ROLES = ("user", "assistant")
_TAIL_USER_LABEL = "YOU"         # display label for the user's own role

_TAIL_GLYPHS = {
    # `pipe` is shown next to agent rows; `pipe_user` (heavier) marks YOUR rows
    # so your own messages pop visually without color.
    "unicode": {"pipe": "│", "pipe_user": "┃", "dot": "·", "ellipsis": "…", "rule": "─"},
    "ascii":   {"pipe": "|", "pipe_user": "#", "dot": "-", "ellipsis": "...", "rule": "-"},
}


def _resolve_project_ids(con: apsw.Connection, project: str,
                          exact_only: bool = False) -> tuple[list[str], list[str]]:
    """Resolve a display_name → list of project_ids.

    Strategy:
      1. Exact match on display_name (case-insensitive, NOCASE).
      2. If 0 hits AND not exact_only, fall back to LIKE %project%.
         Print a stderr warning when LIKE matched >1 project.

    Returns (project_ids, matched_display_names). Both empty when no match.
    """
    rows = con.execute(
        "SELECT project_id, display_name FROM projects "
        "WHERE display_name = ? COLLATE NOCASE",
        (project,),
    ).fetchall()
    if rows:
        return ([r["project_id"] for r in rows],
                [r["display_name"] for r in rows])
    if exact_only:
        return ([], [])
    rows = con.execute(
        "SELECT project_id, display_name FROM projects "
        "WHERE display_name LIKE ? COLLATE NOCASE",
        (f"%{project}%",),
    ).fetchall()
    if not rows:
        return ([], [])
    if len(rows) > 1:
        names = ", ".join(r["display_name"] for r in rows)
        print(f"[warn] '{project}' matched {len(rows)} projects: {names}",
              file=sys.stderr)
    return ([r["project_id"] for r in rows],
            [r["display_name"] for r in rows])


def _resolve_tail_session(con: apsw.Connection, project: str | None,
                          agent: str | None) -> tuple[str, str] | None:
    """Pick the latest session matching project/agent filters.

    Returns (session_id, project_id) or None if no session matches.
    """
    where = []
    params: list = []
    if project:
        pids, _ = _resolve_project_ids(con, project)
        if not pids:
            return None
        placeholders = ",".join("?" * len(pids))
        where.append(f"project_id IN ({placeholders})")
        params.extend(pids)
    if agent:
        where.append("agent = ?")
        params.append(agent)
    sql = "SELECT session_id, project_id FROM sessions"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY last_updated DESC LIMIT 1"
    row = con.execute(sql, params).fetchone()
    if row is None:
        return None
    return (row[0], row[1])


def _tail_parse_ts(ts: str | None) -> "datetime | None":
    if not ts:
        return None
    try:
        s = ts.replace("Z", "+00:00")
        # Strip sub-second precision past microsecond cap if present.
        return datetime.fromisoformat(s)
    except (ValueError, AttributeError):
        return None


def _tail_format_ago(ts: str | None,
                     now: "datetime | None" = None) -> str:
    """Return 'Xs ago' / 'Xm ago' / 'Xh ago' / 'Xd ago' / 'Xw ago'.

    `now` is injectable for deterministic tests; defaults to current UTC.
    Returns '' for unparseable timestamps and 'now' for sub-second elapsed.
    """
    dt = _tail_parse_ts(ts)
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    secs = int((now - dt).total_seconds())
    if secs <= 0:
        return "now"
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    if secs < 604800:
        return f"{secs // 86400}d ago"
    return f"{secs // 604800}w ago"


def _tail_clock(ts: str | None) -> str:
    """Format `ts` as HH:MM:SS, or 'unknown' if unparseable."""
    dt = _tail_parse_ts(ts)
    if dt is not None:
        return dt.strftime("%H:%M:%S")
    return (ts or "")[11:19] or "unknown"


def _tail_session_range(rows: list) -> str:
    """First-msg date + 'HH:MM→HH:MM' (or '→<date> HH:MM' across days)."""
    if not rows:
        return ""
    first = _tail_parse_ts(rows[0][1])
    last = _tail_parse_ts(rows[-1][1])
    if first is None or last is None:
        return ""
    if first.date() == last.date():
        return f"{first.date().isoformat()} {first.strftime('%H:%M')}"\
               f"→{last.strftime('%H:%M')}"
    return f"{first.date().isoformat()} {first.strftime('%H:%M')}"\
           f"→{last.date().isoformat()} {last.strftime('%H:%M')}"


def _tail_wrap(text: str, cols: int) -> list[str]:
    """Word-wrap, preserving paragraph breaks. Empty list never returned."""
    import textwrap
    out: list[str] = []
    for para in text.split("\n"):
        para = para.rstrip()
        if not para:
            out.append("")
            continue
        wrapped = textwrap.wrap(
            para, width=cols,
            break_long_words=False,   # don't split URLs/identifiers
            break_on_hyphens=False,
        )
        out.extend(wrapped or [""])
    return out or [""]


def tail(con: apsw.Connection, n: int = _DEFAULT_TAIL_N,
         session: str | None = None,
         project: str | None = None,
         agent: str | None = None,
         roles: tuple[str, ...] | None = None,
         width: int = _TAIL_WIDTH,
         expand: set[int] | None = None,
         ascii_only: bool = False,
         cols: int = _TAIL_BODY_COLS,
         json_: bool = False) -> int:
    """Print the last N messages from a session in chronological order.

    With no `session`, picks the most-recently-updated session matching
    `project` and `agent` filters. Output is oldest-first so the latest
    message appears at the bottom — matches a chat-log reading order.

    `expand` is a set of 1-based turn numbers to render in full (no
    truncation, no inline collapse). `ascii_only` swaps Unicode glyphs
    for ASCII fallbacks; useful for terminals that don't render box chars.

    Returns: 0 on success, 1 if no session/messages found.
    """
    roles = tuple(roles) if roles else _TAIL_ROLES
    expand = expand or set()
    if n <= 0:
        n = _DEFAULT_TAIL_N

    resolved_project = project
    if session is None:
        picked = _resolve_tail_session(con, project, agent)
        if picked is None:
            # "Did you mean" — surface display_names that fuzzily match the
            # passed --project. Slug variants (hyphen↔underscore) no longer
            # exist post-v4; we suggest LIKE matches from projects.display_name.
            suggestions: list[str] = []
            if project:
                like = con.execute(
                    "SELECT display_name FROM projects "
                    "WHERE display_name LIKE ? COLLATE NOCASE "
                    "  AND display_name != ? COLLATE NOCASE "
                    "ORDER BY display_name",
                    (f"%{project}%", project),
                ).fetchall()
                suggestions = [r["display_name"] for r in like[:3]]
            label = ", ".join(filter(None, [
                f"project='{project}'" if project else None,
                f"agent='{agent}'" if agent else None,
            ])) or "any project"
            if json_:
                import json as _json
                payload: dict = {
                    "session_id": None,
                    "project": project,
                    "agent": agent,
                    "n": n,
                    "messages": [],
                    "error": f"no session found for {label}",
                }
                if suggestions:
                    payload["did_you_mean"] = suggestions
                print(_json.dumps(payload))
            else:
                print(f"No sessions found for {label}.", file=sys.stderr)
                if suggestions:
                    print(f"Did you mean: {', '.join(suggestions)}?",
                          file=sys.stderr)
            return 1
        session, picked_pid = picked
        # Translate picked project_id → display_name for header rendering.
        dn = con.execute(
            "SELECT display_name FROM projects WHERE project_id = ?",
            (picked_pid,),
        ).fetchone()
        resolved_project = dn["display_name"] if dn else picked_pid
    elif resolved_project is None:
        # Explicit --session bypassed the picker — recover the project's
        # display_name from the sessions+projects join so the header still
        # shows it.
        row = con.execute(
            "SELECT p.display_name FROM sessions s "
            "LEFT JOIN projects p ON p.project_id = s.project_id "
            "WHERE s.session_id = ?",
            (session,),
        ).fetchone()
        if row is not None and row["display_name"] is not None:
            resolved_project = row["display_name"]

    placeholders = ",".join(["?"] * len(roles))
    rows = con.execute(
        f"SELECT role, timestamp, content, agent "
        f"FROM messages "
        f"WHERE session_id = ? AND role IN ({placeholders}) "
        f"ORDER BY timestamp DESC LIMIT ?",
        [session, *roles, n],
    ).fetchall()
    rows = list(reversed(rows))  # chronological — newest at the bottom

    if json_:
        import json as _json
        # Resolve display_name + project_id for the JSON envelope (item 14).
        sess_meta = con.execute(
            "SELECT s.project_id, p.display_name FROM sessions s "
            "LEFT JOIN projects p ON p.project_id = s.project_id "
            "WHERE s.session_id = ?",
            (session,),
        ).fetchone()
        sess_pid = sess_meta["project_id"] if sess_meta else None
        sess_display = (sess_meta["display_name"] if sess_meta and
                        sess_meta["display_name"] else resolved_project)
        out = {
            "session_id": session,
            "project": resolved_project,
            "project_id": sess_pid,
            "display_name": sess_display,
            # DEPRECATED alias for one release — equals display_name.
            "project_slug": sess_display,
            "agent": agent,
            "n": n,
            "messages": [
                {"role": r[0], "timestamp": r[1], "content": r[2],
                 "agent": r[3]}
                for r in rows
            ],
        }
        print(_json.dumps(out))
        return 0 if rows else 1

    if not rows:
        print(f"No messages found in session {session}.", file=sys.stderr)
        return 1

    g = _TAIL_GLYPHS["ascii" if ascii_only else "unicode"]
    short_session = session[:8] if len(session) >= 8 else session
    now = datetime.now(timezone.utc)

    # Pre-compute speaker labels and column widths so the metadata column
    # is uniform across all rows (Option-E layout). Newest message is #1
    # (reverse-numbered from the bottom up).
    total = len(rows)

    def _speaker_for(role: str, msg_agent: str | None) -> str:
        if role == "user":
            return _TAIL_USER_LABEL
        if role == "assistant" and msg_agent:
            return msg_agent
        return role

    speakers = [_speaker_for(r[0], r[3]) for r in rows]
    speaker_w = max((len(s) for s in speakers), default=4)
    num_w = max(2, len(str(total))) + 1   # +1 for the leading '#'
    # Pre-compute every metadata-column string so we know its exact width
    # and can build a matching blank for body continuation lines.
    meta_strs: list[str] = []
    for i, (role, ts, _content, _agent) in enumerate(rows):
        rev_n = total - i                    # newest = #1
        clock = _tail_clock(ts)
        ago = _tail_format_ago(ts, now=now)
        meta_strs.append(
            f"{('#' + str(rev_n)):<{num_w}} {clock}  {ago:<8}  "
            f"{speakers[i]:<{speaker_w}} "
        )
    meta_w = max((len(m) for m in meta_strs), default=0)
    blank_meta = " " * meta_w

    # ── header ───────────────────────────────────────────────────────────
    header_bits = [
        f"session {short_session}",
        resolved_project or "?",
        f"{total} messages",
    ]
    rng = _tail_session_range(rows)
    if rng:
        header_bits.append(rng)
    if rows:
        header_bits.append(f"latest {_tail_format_ago(rows[-1][1], now=now)}")
    print(f" {g['dot']} ".join(header_bits))
    print()

    # ── messages ─────────────────────────────────────────────────────────
    truncated_turns: list[int] = []

    for i, (role, ts, content_raw, msg_agent) in enumerate(rows):
        content = content_raw if content_raw is not None else ""
        rev_n = total - i
        force_full = rev_n in expand

        original_len = len(content)
        if not force_full and original_len > width:
            body = content[:width].rstrip()
            extra = original_len - width
            body += f" {g['ellipsis']} [+{extra} more]"
            truncated_turns.append(rev_n)
        else:
            body = content

        bar = g["pipe_user"] if role == "user" else g["pipe"]
        meta = meta_strs[i].ljust(meta_w)

        wrapped = _tail_wrap(body, cols)
        for j, line in enumerate(wrapped):
            prefix = meta if j == 0 else blank_meta
            print(f"{prefix}{bar}  {line}".rstrip())
        print()

    # ── footer hint ──────────────────────────────────────────────────────
    if truncated_turns and not expand:
        sample = truncated_turns[-1]   # most recent truncated turn (smallest #)
        print(f"(use `recall tail {n} --expand {sample}` "
              f"to see message #{sample} in full)")
    return 0


def search(con: apsw.Connection, query: str, limit: int = 10,
           recent: bool = False, project: str | None = None,
           context: int = 1, agent: str | None = None,
           json_: bool = False) -> None:
    if len(query) > MAX_QUERY_LEN:
        query = query[:MAX_QUERY_LEN]

    use_vec = _vec_ok(con) and EMBED_SOCK.exists()
    qvec = None
    if use_vec:
        qvec = embed(query, mode="query")
        use_vec = qvec is not None

    # FTS5 interprets `-`, `.`, `:`, `(`, `*`, etc. as query operators or
    # column refs, so passing a raw user query into `messages_fts MATCH ?`
    # crashes on common inputs (e.g. `app-gemini` → "no such column: gemini",
    # `.*` → "syntax error near '.'"). Wrap each whitespace-separated token
    # in double quotes — FTS5's phrase syntax — so special chars inside are
    # literal. Implicit-AND semantics across multiple quoted tokens preserve
    # the prior behavior for normal queries. Embedding path uses the raw
    # query (the model handles any string).
    fts_query = _safe_fts_query(query)

    # Pre-compute the rowid set for the (project, agent) filter so we can
    # narrow both FTS and vec result sets down before scoring.
    filter_rowids: set[int] | None = None
    resolved_project_ids: list[str] = []
    if project or agent:
        clauses = []
        params: list = []
        if project:
            resolved_project_ids, _ = _resolve_project_ids(con, project)
            if not resolved_project_ids:
                # No exact and no LIKE match → no rows; "did you mean" suggests
                # display_name LIKE matches.
                filter_rowids = set()
            else:
                placeholders = ",".join("?" * len(resolved_project_ids))
                clauses.append(f"project_id IN ({placeholders})")
                params.extend(resolved_project_ids)
        if agent:
            clauses.append("agent = ?")
            params.append(agent)
        if filter_rowids is None and clauses:
            where = " AND ".join(clauses)
            rows = con.execute(
                f"SELECT rowid FROM messages WHERE {where}", params
            ).fetchall()
            filter_rowids = {r[0] for r in rows}
        elif filter_rowids is None:
            filter_rowids = None  # no filter at all (shouldn't reach)
        if not filter_rowids:
            # "Did you mean" hint: surface display_names that fuzzily match
            # the passed --project. Slug variants no longer exist post-v4.
            suggestions = []
            if project:
                like = con.execute(
                    "SELECT display_name FROM projects "
                    "WHERE display_name LIKE ? COLLATE NOCASE "
                    "  AND display_name != ? COLLATE NOCASE "
                    "ORDER BY display_name",
                    (f"%{project}%", project),
                ).fetchall()
                suggestions = [r["display_name"] for r in like[:3]]
            if json_:
                import json as _json
                payload: dict = {
                    "query": query,
                    "project": project,
                    "agent": agent,
                    "n": limit,
                    "results": [],
                }
                if suggestions:
                    payload["did_you_mean"] = suggestions
                print(_json.dumps(payload))
            else:
                label = ", ".join(filter(None, [
                    f"project='{project}'" if project else None,
                    f"agent='{agent}'" if agent else None,
                ]))
                print(f"No messages found for {label}.")
                if suggestions:
                    print(f"Did you mean: {', '.join(suggestions)}?")
            return
    project_rowids = filter_rowids  # keep alias to minimize downstream churn

    # Corpus mismatch guard: fall back to FTS if vector coverage < 95%
    if use_vec and project and _vec_ok(con):
        cov = con.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN v.rowid IS NOT NULL THEN 1 ELSE 0 END) AS embedded
               FROM messages m
               LEFT JOIN message_vecs v ON v.rowid = m.rowid
               WHERE m.project_id IN ({})""".format(
                   ",".join("?" * len(resolved_project_ids))
               ),
            tuple(resolved_project_ids),
        ).fetchone()
        total, embedded = cov[0], cov[1] or 0
        if total > 0 and (embedded / total) < 0.95:
            pct = embedded * 100 // total
            print(f"[warn] Vector coverage {pct}% (<95%) for '{project}' — using FTS only. "
                  f"Run `recall ingest` to heal.", file=sys.stderr)
            use_vec = False

    # Filter-aware retrieval strategy. When the (project, agent) filter set is
    # a small fraction of the corpus, a global top-100 prefilter rarely
    # overlaps with it (recall cliff). Choose strategy by cardinality:
    #   - no filter / >= 5000 rows : global top-100 prefilter, intersect after
    #   - 500..4999                : bump prefilter to min(n*2, 1000)
    #   - < 500                    : push filter into FTS, brute-force vec
    filter_size = len(filter_rowids) if filter_rowids is not None else None
    if filter_size is None or filter_size >= 5_000:
        prefilter_k = 100
    elif filter_size >= 500:
        prefilter_k = min(filter_size * 2, 1000)
    else:
        prefilter_k = filter_size  # exact retrieval below

    if use_vec:
        # FTS side: when the filter is small, push `rowid IN (...)` into the
        # query so we don't waste a global top-100 fetch that gets filtered
        # to nothing.
        if filter_rowids is not None and filter_size < 5_000:
            placeholders = ",".join("?" * filter_size)
            fts_rows = con.execute(
                f"""SELECT m.rowid, ROW_NUMBER() OVER (ORDER BY rank) AS fts_rank
                    FROM messages_fts
                    JOIN messages m ON messages_fts.rowid = m.rowid
                    WHERE messages_fts MATCH ? AND m.rowid IN ({placeholders})
                    LIMIT ?""",
                (fts_query, *filter_rowids, prefilter_k),
            ).fetchall()
            fts_map = {r["rowid"]: r["fts_rank"] for r in fts_rows}
        else:
            fts_rows = con.execute(
                """SELECT m.rowid, ROW_NUMBER() OVER (ORDER BY rank) AS fts_rank
                   FROM messages_fts
                   JOIN messages m ON messages_fts.rowid = m.rowid
                   WHERE messages_fts MATCH ?
                   LIMIT ?""",
                (fts_query, prefilter_k),
            ).fetchall()
            fts_map = {r["rowid"]: r["fts_rank"] for r in fts_rows
                       if project_rowids is None or r["rowid"] in project_rowids}

        vec_rowids = _vec_search(con, qvec, k=prefilter_k,
                                 restrict_rowids=filter_rowids)
        if filter_rowids is None or filter_size < 500:
            # _vec_search already restricted; trust the order
            vec_map = {rid: rank + 1 for rank, rid in enumerate(vec_rowids)}
        else:
            vec_map = {rid: rank + 1 for rank, rid in enumerate(vec_rowids)
                       if rid in filter_rowids}

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
        if not scored:
            rows = []
        else:
            placeholders = ",".join("?" * len(scored))
            rows = con.execute(
                f"""SELECT rowid, session_id, project_id, role, timestamp, agent,
                           SUBSTR(content, 1, 300) AS excerpt
                    FROM messages WHERE rowid IN ({placeholders})""",
                scored,
            ).fetchall()
    else:
        # FTS-only path. When a filter is set, push `rowid IN (...)` into the
        # query so the filter is honored (without it, --agent X foo against a
        # corpus dominated by another agent silently returns 0 hits — the
        # original recall cliff).
        if filter_rowids is not None:
            placeholders = ",".join("?" * filter_size)
            rows = con.execute(
                f"""SELECT m.rowid, m.session_id, m.project_id, m.role,
                           m.timestamp, m.agent,
                           snippet(messages_fts, 0, '[', ']', '…', 20) AS excerpt
                    FROM messages_fts
                    JOIN messages m ON messages_fts.rowid = m.rowid
                    WHERE messages_fts MATCH ? AND m.rowid IN ({placeholders})
                    ORDER BY rank
                    LIMIT ?""",
                (fts_query, *filter_rowids, limit),
            ).fetchall()
        else:
            rows = con.execute(
                """SELECT m.rowid, m.session_id, m.project_id, m.role,
                          m.timestamp, m.agent,
                          snippet(messages_fts, 0, '[', ']', '…', 20) AS excerpt
                   FROM messages_fts
                   JOIN messages m ON messages_fts.rowid = m.rowid
                   WHERE messages_fts MATCH ?
                   ORDER BY rank
                   LIMIT ?""",
                (fts_query, limit),
            ).fetchall()

    if not rows:
        if json_:
            import json as _json
            print(_json.dumps({
                "query": query,
                "project": project,
                "agent": agent,
                "n": limit,
                "results": [],
            }))
        else:
            print("No results.")
        return

    mode = ("hybrid+recent" if use_vec and recent
            else "hybrid" if use_vec
            else "fts")

    # Build a project_id → display_name lookup for output formatting (item 14).
    pid_set = {r["project_id"] for r in rows}
    if pid_set:
        placeholders = ",".join("?" * len(pid_set))
        pid_to_name = {
            r["project_id"]: r["display_name"]
            for r in con.execute(
                f"SELECT project_id, display_name FROM projects "
                f"WHERE project_id IN ({placeholders})",
                tuple(pid_set),
            ).fetchall()
        }
    else:
        pid_to_name = {}

    if json_:
        import json as _json
        results = []
        for r in rows:
            display = pid_to_name.get(r["project_id"], r["project_id"])
            results.append({
                "session_id": r["session_id"],
                "project_id": r["project_id"],
                "display_name": display,
                # DEPRECATED alias for one release — equals display_name.
                "project_slug": display,
                "agent": r["agent"],
                "role": r["role"],
                "timestamp": r["timestamp"],
                "snippet": r["excerpt"],
            })
        print(_json.dumps({
            "query": query,
            "project": project,
            "agent": agent,
            "mode": mode,
            "n": limit,
            "results": results,
        }))
        return

    print(f"[{mode} search]\n")
    # Only show the agent tag when the result set actually mixes agents (or
    # the user explicitly filtered to a non-claude agent). Single-Claude
    # users — the entire pre-v0.2.0 cohort — see output identical to before.
    distinct_agents = {r["agent"] for r in rows}
    show_agent = len(distinct_agents) > 1 or distinct_agents != {"claude"}
    for r in rows:
        ts = (r["timestamp"] or "")[:10]
        role_label = "[⚠ error]" if r["role"] == "tool_error" else f"[{r['role']}]"
        agent_tag = f"[{r['agent']}] " if show_agent else ""
        display = pid_to_name.get(r["project_id"], r["project_id"])
        print(f"[{display}] {agent_tag}{role_label} {ts}")
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
    from . import _progress

    if not _vec_ok(con):
        print("sqlite-vec not loaded", file=sys.stderr)
        return
    # Same race fix as scan_all: the wizard's chain calls embed_backfill
    # right after spawning the sidecar, before the socket is bound.
    # Wait up to 30s for the socket to appear before declaring failure.
    if not _wait_for_embed_socket(timeout_s=30.0, verbose=True):
        print("Embed socket not found (waited 30s)", file=sys.stderr)
        return
    existing = {r[0] for r in con.execute("SELECT rowid FROM message_vecs").fetchall()}
    rows = con.execute("SELECT rowid, content FROM messages").fetchall()
    pending = [r for r in rows if r["rowid"] not in existing]
    total = len(pending)
    print(f"Embedding {total:,} messages…")

    # If we're called inside a multi-phase chain (e.g. _backfill-chain),
    # the parent has already created the progress run with both phases
    # pre-declared. Otherwise (standalone `recall embed-backfill`),
    # create our own single-phase run so the user still sees a bar in
    # `recall stats`.
    own_run = _progress.read_status() is None
    if own_run:
        _progress.start_run([("embed-backfill", total)])
    else:
        _progress.set_phase_total("embed-backfill", total)

    done = 0
    try:
        for r in pending:
            vec = embed(r["content"])
            if vec:
                _vec_insert(con, r["rowid"], vec)
                done += 1
            # Update the file every 100 rows — keeps the progress display
            # fresh without thrashing the disk on per-row writes.
            if done % 100 == 0 and done > 0:
                _progress.update_phase("embed-backfill", done)
            if done % 500 == 0 and done > 0:
                print(f"  {done}/{total}…")
        _progress.finish_phase("embed-backfill")
    finally:
        if own_run:
            _progress.finish_run()
    print(f"Done. {done:,} embeddings written.")


def _confirm_destructive(label: str, n_changed: int, samples: list[tuple[str, str]],
                         confirm: bool) -> bool:
    """Shared preview + confirmation gate for backfill mutations.

    Prints a danger banner, the row count, up to 3 before/after diffs, and:
    - non-TTY without confirm → refuse, return False
    - TTY without confirm → prompt 'YES', return True only on exact match
    - confirm=True → skip prompt, return True

    `samples` is a list of (before, after) string pairs to show the user.
    """
    print()
    print("🔥" * 35)
    print(f"☠️  DANGER — `{label}` will REWRITE {n_changed:,} rows IN PLACE  ☠️")
    print("🔥" * 35)
    print()
    print("This is an in-place mutation of message content. The original text")
    print("is replaced with the new text. There is NO undo and NO automatic")
    print("backup. If the cleaning/redaction logic has a bug, every changed")
    print("row carries the bug.")
    print()
    if samples:
        print(f"📊 Sample of changes (first {len(samples)} of {n_changed:,}):")
        for i, (before, after) in enumerate(samples, 1):
            b = before if len(before) <= 100 else before[:100] + "…"
            a = after  if len(after)  <= 100 else after[:100]  + "…"
            print(f"  ── #{i} ──")
            print(f"    BEFORE: {b!r}")
            print(f"    AFTER : {a!r}")
        print()
    if n_changed == 0:
        print("✅ Nothing to do — every row is already up to date.")
        return False

    if confirm:
        print("💥 --confirm passed — proceeding without prompt.\n")
        return True
    if not sys.stdin.isatty():
        print("⚠️  DRY-RUN — non-interactive shell.")
        print(f"Re-run with --confirm to apply:")
        print(f"    recall {label} --confirm")
        return False
    print("⚠️  ⚠️  ⚠️   ARE YOU SURE?   ⚠️  ⚠️  ⚠️\n")
    response = input(
        "Type 'YES' (uppercase) to apply the mutation, anything else to cancel: "
    ).strip()
    if response != "YES":
        print("\n✅ Aborted. No rows changed.")
        return False
    print()
    return True


def backfill_clean(con: apsw.Connection, confirm: bool = False) -> None:
    """Re-run content cleaning on every message.

    Defaults to DRY-RUN (preview only). Pass confirm=True (or --confirm on
    the CLI) to actually mutate rows. Pattern matches `recall forget`.
    """
    rows = con.execute("SELECT rowid, content FROM messages").fetchall()
    pending: list[tuple[int, str, str]] = []  # (rowid, old, new)
    for r in rows:
        new = _clean_content(r["content"])
        if new != r["content"]:
            pending.append((r["rowid"], r["content"], new))

    samples = [(old, new) for _, old, new in pending[:3]]
    if not _confirm_destructive("backfill-clean", len(pending), samples, confirm):
        return

    for rowid, _old, new in pending:
        con.execute("UPDATE messages SET content = ? WHERE rowid = ?",
                    (new, rowid))
    print(f"Cleaned {len(pending):,} messages. Rebuilding FTS…")
    con.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
    print("Done.")


def backfill_redact(con: apsw.Connection, confirm: bool = False) -> None:
    """Re-apply secret redaction to all existing rows + rebuild FTS.

    Use this after upgrading to a version with secret redaction, or after
    `recall doctor --scan-secrets` reports findings on legacy rows that
    were ingested before redaction was enabled.

    Defaults to DRY-RUN (preview only). Pass confirm=True (or --confirm on
    the CLI) to actually mutate rows.
    """
    rows = con.execute("SELECT rowid, content FROM messages").fetchall()
    pending: list[tuple[int, str, str]] = []
    for r in rows:
        new = _redact.redact_secrets(r["content"])
        if new != r["content"]:
            pending.append((r["rowid"], r["content"], new))

    samples = [(old, new) for _, old, new in pending[:3]]
    if not _confirm_destructive("backfill-redact", len(pending), samples, confirm):
        return

    for rowid, _old, new in pending:
        con.execute("UPDATE messages SET content = ? WHERE rowid = ?",
                    (new, rowid))
    print(f"Redacted {len(pending):,} messages. Rebuilding FTS…")
    con.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
    print("Done.")


_BAK_STALE_AGE_DAYS = 30


def _scan_stale_bak_files(db_dir: Path) -> list[tuple[Path, float, int]]:
    """Return a list of `(path, age_days, size_bytes)` for `.bak` files in
    `db_dir` older than `_BAK_STALE_AGE_DAYS`. Used by `recall doctor`."""
    if not db_dir.exists():
        return []
    out: list[tuple[Path, float, int]] = []
    now = datetime.now().timestamp()
    for p in db_dir.glob("*.bak"):
        try:
            stat = p.stat()
            age_days = (now - stat.st_mtime) / 86400.0
            if age_days >= _BAK_STALE_AGE_DAYS:
                out.append((p, age_days, stat.st_size))
        except OSError:
            continue
    return out


def doctor(con: apsw.Connection, scan_secrets: bool = False) -> None:
    """Run health checks against the DB. Subset is selected by flags.

    Default: scan for stray `.bak` files older than `_BAK_STALE_AGE_DAYS`
    in the DB directory. With `--scan-secrets`: also counts how many
    existing rows match each redaction pattern (so users discover what
    already leaked into their DB pre-redaction).
    """
    if scan_secrets:
        rows = con.execute("SELECT content FROM messages").fetchall()
        totals: dict[str, int] = {}
        affected_rows = 0
        for r in rows:
            counts = _redact.scan_secrets(r["content"])
            if counts:
                affected_rows += 1
                for label, n in counts.items():
                    totals[label] = totals.get(label, 0) + n
        if not totals:
            print("No secret-shaped tokens found.")
        else:
            print(f"Found secret-shaped tokens in {affected_rows:,} row(s):")
            for label, n in sorted(totals.items()):
                print(f"  {label:30s}  {n:,}")
            print("\nRun `recall backfill-redact` to redact existing rows.")

    # DB path drift: warn when CONVO_RECALL_DB overrides the canonical default.
    canonical_db = Path.home() / ".local" / "share" / "convo-recall" / "conversations.db"
    if os.environ.get("CONVO_RECALL_DB") and Path(os.environ["CONVO_RECALL_DB"]).resolve() != canonical_db.resolve():
        print(f"\nCONVO_RECALL_DB override in effect:")
        print(f"  configured  : {DB_PATH}")
        print(f"  canonical   : {canonical_db}")
        print("Different docs may reference different paths. If unintentional, "
              "unset the env var.")

    # Embed sidecar + coverage status. Three independent signals (extra
    # installed, sidecar reachable, coverage) so the user can act on the
    # right one.
    msg_count = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    vec_count = _vec_count(con) if _vec_ok(con) else 0
    coverage_pct = (vec_count * 100 // msg_count) if msg_count else 0
    try:
        import sentence_transformers  # noqa: F401
        extra_installed = True
    except ImportError:
        extra_installed = False
    sock_exists = EMBED_SOCK.exists()
    print(f"\nEmbed extra      : {'installed' if extra_installed else 'NOT installed'}")
    print(f"Embed sidecar    : {'reachable at ' + str(EMBED_SOCK) if sock_exists else 'down (no socket)'}")
    print(f"Embedded coverage: {vec_count:,}/{msg_count:,} ({coverage_pct}%)")
    if msg_count > 0 and vec_count == 0:
        if not extra_installed:
            print("  → install with: pipx install 'convo-recall[embeddings]'")
            print("    then re-run:  recall install --with-embeddings")
        elif not sock_exists:
            print("  → start the sidecar: recall serve")
        else:
            print("  → backfill embeddings: recall embed-backfill")
    elif msg_count > 0 and coverage_pct < 95:
        print(f"  → low coverage; run `recall embed-backfill` to heal")

    # Project-id integrity: every messages.project_id must have a row in
    # `projects`. Surfaces drift from the v4 migration or partial ingest.
    distinct_projects = con.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
    print(f"\nProjects         : {distinct_projects} (display_name index)")
    orphan_msgs = con.execute(
        "SELECT COUNT(*) FROM messages m WHERE NOT EXISTS "
        "(SELECT 1 FROM projects p WHERE p.project_id = m.project_id)"
    ).fetchone()[0]
    if orphan_msgs:
        print(f"⚠ {orphan_msgs:,} messages reference a project_id with no "
              f"projects-table row.")
        print("  → re-ingest will recreate the missing rows; otherwise file an issue.")

    # Ingest hook installation status — surfaces missing-hook state for users
    # who upgraded but didn't re-run `recall install`.
    print("\nIngest hook (response-completion driven):")
    try:
        from .install._hooks import _hook_target, _find_hook_script
        ingest_script = _find_hook_script("ingest")
        not_wired_count = 0
        for agent in ("claude", "codex", "gemini"):
            try:
                settings_path, event, label = _hook_target(agent, "ingest")
            except ValueError:
                continue
            wired = False
            if settings_path.exists():
                try:
                    data = json.loads(settings_path.read_text())
                    groups = (data.get("hooks") or {}).get(event) or []
                    for g in groups:
                        for h in g.get("hooks", []):
                            if h.get("command") == str(ingest_script):
                                wired = True
                                break
                        if wired:
                            break
                except (OSError, json.JSONDecodeError):
                    pass
            marker = "✅" if wired else "·"
            state = "wired" if wired else "NOT wired"
            print(f"  {marker} {label:<7} {event:<14} {state}  ({settings_path})")
            if not wired:
                not_wired_count += 1
        if not_wired_count:
            print("  → re-run `recall install-hooks --kind ingest` to wire missing hooks")
    except (RuntimeError, ImportError):
        print("  (could not locate conversation-ingest.sh)")

    stale = _scan_stale_bak_files(DB_PATH.parent)
    if stale:
        print(f"\nStale `.bak` files in {DB_PATH.parent} "
              f"(older than {_BAK_STALE_AGE_DAYS} days):")
        for path, age, size in sorted(stale):
            mb = size / (1024 * 1024)
            print(f"  {path.name}  {age:.0f}d old  {mb:,.1f} MB")
        print("\nReview and remove manually if no longer needed.")
    elif not scan_secrets:
        print("\nNo other issues found. "
              "Pass `--scan-secrets` to scan for credential-shaped tokens.")


def chunk_backfill(con: apsw.Connection, confirm: bool = False) -> None:
    """Re-embed long messages whose vectors may pre-date server-side chunking.
    Chunking now happens inside the sidecar — one HTTP call per message.

    Defaults to DRY-RUN. Less catastrophic than backfill-clean/redact
    (only embeddings change, message text stays intact) but still consumes
    GPU/CPU and time, so we gate it behind a confirm.
    """
    _BACKFILL_MIN_CHARS = 1800  # ≈ 450 tokens; shorter texts always fit in model window
    if not _vec_ok(con) or not EMBED_SOCK.exists():
        print("Embed service not available", file=sys.stderr)
        return
    rows = con.execute(
        "SELECT rowid, content FROM messages WHERE LENGTH(content) > ?",
        (_BACKFILL_MIN_CHARS,),
    ).fetchall()
    total = len(rows)

    print()
    print("─" * 70)
    print(f"📊 chunk-backfill: {total:,} long message(s) (>{_BACKFILL_MIN_CHARS} chars) "
          f"would be re-embedded.")
    print("─" * 70)
    print("This re-runs the embedding model — message TEXT is not touched, only")
    print("the stored vectors are replaced. Lower risk than backfill-clean/redact,")
    print("but still consumes GPU/CPU and re-downloads chunks via the sidecar.")
    print()

    if total == 0:
        print("✅ Nothing to do.")
        return

    if not confirm:
        if not sys.stdin.isatty():
            print("⚠️  DRY-RUN — non-interactive shell.")
            print("Re-run with --confirm to apply: recall chunk-backfill --confirm")
            return
        response = input(
            f"Type 'YES' (uppercase) to re-embed {total:,} messages, "
            "anything else to cancel: "
        ).strip()
        if response != "YES":
            print("\n✅ Aborted.")
            return
        print()

    print(f"Re-embedding {total:,} long messages via sidecar chunking…")
    done = 0
    for r in rows:
        vec = embed(r["content"])
        if vec:
            _vec_insert(con, r["rowid"], vec)
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
                # Recover project_id by scanning the file for cwd; fall back to
                # legacy slug if no cwd field is present.
                recovered_cwd: str | None = None
                try:
                    with open(jsonl_path, "r", errors="replace") as fh:
                        for i, line in enumerate(fh):
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
                else:
                    project_id = _legacy_project_id(_legacy_claude_slug(jsonl_path))
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
                                # tool_error_backfill currently only walks
                                # claude session files (PROJECTS_DIR is the
                                # claude root). When Phase 4b/c parsers add
                                # tool_error detection, the agent argument
                                # below should be set per source agent.
                                try:
                                    ret = con.execute(
                                        """INSERT OR IGNORE INTO messages
                                           (uuid, session_id, project_id, role,
                                            content, timestamp, model, agent)
                                           VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING rowid""",
                                        (tr_uuid, session_id, project_id, "tool_error",
                                         tr_text, timestamp, None, "claude"),
                                    ).fetchall()
                                    if ret:
                                        tr_rowid = ret[0][0]
                                        if _vec_ok(con):
                                            tr_vec = embed(tr_text)
                                            if tr_vec:
                                                _vec_insert(con, tr_rowid, tr_vec)
                                        indexed += 1
                                except apsw.Error as _e:
                                    print(f"[warn] tool_error_backfill insert failed: "
                                          f"{type(_e).__name__}: {_e}", file=sys.stderr)
                except OSError:
                    pass
    print(f"Indexed {indexed:,} tool_result error(s).")


def forget(con: apsw.Connection, *,
           session: str | None = None,
           pattern: str | None = None,
           before: str | None = None,
           project: str | None = None,
           agent: str | None = None,
           uuid: str | None = None,
           confirm: bool = False) -> int:
    """Delete messages by scope. Mutually-exclusive scope kwargs.

    Always prints a preview (count + first-3 excerpts). Without `confirm=True`
    no rows are deleted. Returns the number of rows deleted (0 in dry-run).
    """
    scopes = {"session": session, "pattern": pattern, "before": before,
              "project": project, "agent": agent, "uuid": uuid}
    set_scopes = [k for k, v in scopes.items() if v is not None]
    if len(set_scopes) != 1:
        raise ValueError(
            "exactly one scope flag is required "
            f"(--session/--pattern/--before/--project/--agent/--uuid); got {set_scopes!r}"
        )
    scope = set_scopes[0]

    where_clauses: list[str] = []
    params: list = []
    if session is not None:
        where_clauses.append("session_id = ?"); params.append(session)
    elif uuid is not None:
        where_clauses.append("uuid = ?"); params.append(uuid)
    elif project is not None:
        # Destructive op — exact display_name match only, NO LIKE fallback.
        pids, names = _resolve_project_ids(con, project, exact_only=True)
        if len(pids) == 0:
            raise ValueError(
                f"forget --project requires exact display_name match; "
                f"got 0 matches for {project!r}. "
                f"List candidates with: recall stats"
            )
        if len(pids) > 1:
            raise ValueError(
                f"forget --project requires exact display_name match; "
                f"got {len(pids)} matches for {project!r}: {', '.join(names)}. "
                f"Be more specific."
            )
        placeholders = ",".join("?" * len(pids))
        where_clauses.append(f"project_id IN ({placeholders})")
        params.extend(pids)
    elif agent is not None:
        where_clauses.append("agent = ?"); params.append(agent)
    elif before is not None:
        where_clauses.append("timestamp < ?"); params.append(before)
    elif pattern is not None:
        where_clauses.append("content REGEXP ?"); params.append(pattern)

    where = " AND ".join(where_clauses)

    # Pattern uses Python regex via apsw's createscalarfunction. Register on
    # demand so we don't pay the cost when forget() isn't called.
    if pattern is not None:
        compiled = re.compile(pattern)
        con.createscalarfunction(
            "REGEXP", lambda p, t: 1 if t and compiled.search(t) else 0, 2,
        )

    matches = con.execute(
        f"SELECT m.rowid AS rowid, m.uuid AS uuid, m.session_id AS session_id, "
        f"       m.project_id AS project_id, p.display_name AS display_name, "
        f"       m.agent AS agent, m.role AS role, "
        f"       SUBSTR(m.content, 1, 120) AS excerpt "
        f"FROM messages m LEFT JOIN projects p ON p.project_id = m.project_id "
        f"WHERE m.rowid IN (SELECT rowid FROM messages WHERE {where}) "
        f"ORDER BY m.rowid LIMIT ?",
        (*params, 3),
    ).fetchall()
    total = con.execute(
        f"SELECT COUNT(*) FROM messages WHERE {where}", params
    ).fetchone()[0]

    print(f"forget [{scope}]: {total:,} message(s) match.")
    for r in matches:
        display = r["display_name"] or r["project_id"]
        print(f"  · [{r['agent']}] [{display}] {r['role']}: {r['excerpt']}")
    if total > len(matches):
        print(f"  · … and {total - len(matches):,} more")

    if not confirm:
        print("\nDry-run. Re-run with --confirm to delete.")
        return 0

    if total == 0:
        return 0

    # Capture rowids before delete so we can prune message_vecs.
    rowids = [r[0] for r in con.execute(
        f"SELECT rowid FROM messages WHERE {where}", params
    ).fetchall()]

    con.execute("BEGIN IMMEDIATE")
    try:
        # Messages: deletion triggers messages_ad → FTS row removed.
        con.execute(f"DELETE FROM messages WHERE {where}", params)
        # message_vecs: prune by rowid (no triggers on vec0).
        if _vec_ok(con) and rowids:
            placeholders = ",".join("?" * len(rowids))
            try:
                con.execute(
                    f"DELETE FROM message_vecs WHERE rowid IN ({placeholders})",
                    rowids,
                )
            except Exception as e:
                print(f"[warn] message_vecs prune failed: {type(e).__name__}: {e}",
                      file=sys.stderr)
        # Prune sessions / ingested_files rows that lost all message refs.
        con.execute(
            "DELETE FROM sessions WHERE session_id NOT IN "
            "(SELECT DISTINCT session_id FROM messages)"
        )
        con.execute(
            "DELETE FROM ingested_files WHERE session_id NOT IN "
            "(SELECT DISTINCT session_id FROM messages)"
        )
        con.execute("COMMIT")
    except Exception:
        try: con.execute("ROLLBACK")
        except Exception: pass
        raise

    print(f"\nDeleted {total:,} message(s).")
    return total


def _render_phase_bar(phase: dict) -> None:
    """Render one phase as a single line at the top of `recall stats`.

    State-dependent:
    - `pending`         → "⏳ {name}: pending"
    - `done`, total=0   → "✅ {name}: nothing to do"
    - `done`            → 100% bar (so user sees what just finished)
    - `running`         → live snapshot bar with current/total + rate
    """
    name = phase.get("name", "phase")
    state = phase.get("state", "running")
    total = int(phase.get("total", 0))
    completed = int(phase.get("completed", 0))

    if state == "pending":
        print(f"  ⏳ {name}: pending")
        return
    if state == "done" and total == 0:
        print(f"  ✅ {name}: nothing to do")
        return

    safe_total = total or 1
    pct = min(100, completed * 100 // safe_total)
    try:
        from tqdm import tqdm  # type: ignore
        # file=sys.stdout so the bar lands in the same stream as stats.
        marker = "✅" if state == "done" else "  "
        bar = tqdm(total=safe_total, initial=completed,
                   desc=f"{marker} {name}",
                   unit="file" if name == "ingest" else "msg",
                   leave=True, dynamic_ncols=True, file=sys.stdout,
                   bar_format="{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} [{rate_fmt}]")
        bar.refresh()
        bar.close()
    except ImportError:
        bar_width = 30
        filled = bar_width * completed // safe_total
        plain = "█" * filled + "░" * (bar_width - filled)
        marker = "✅" if state == "done" else "  "
        print(f"{marker} {name}: {pct:3d}%|{plain}| {completed:,}/{total:,}")


def _render_progress_bar(status: dict) -> None:
    """Render every phase in the snapshot at the top of `recall stats`.

    No live refresh — one render per stats invocation. Phases are shown
    in declared order so the user sees the queued sequence (e.g. ingest
    first, embed-backfill second).
    """
    phases = status.get("phases") or []
    if not phases:
        return
    for phase in phases:
        _render_phase_bar(phase)
    print()  # blank line before stats body


def stats(con: apsw.Connection) -> None:
    from . import _progress

    progress = _progress.read_status()
    if progress is not None:
        _render_progress_bar(progress)

    msg_count = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    session_count = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    project_count = con.execute(
        "SELECT COUNT(*) FROM projects"
    ).fetchone()[0]
    role_counts = con.execute(
        "SELECT role, COUNT(*) FROM messages GROUP BY role ORDER BY 2 DESC"
    ).fetchall()
    agent_counts = con.execute(
        "SELECT agent, COUNT(*) FROM messages GROUP BY agent ORDER BY 2 DESC"
    ).fetchall()
    vec_count = _vec_count(con)
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

    # Hybrid-search readiness warning. Surface the most likely cause + the
    # exact command to fix it, so users don't silently run in FTS-only mode
    # without knowing the headline feature is off.
    if msg_count > 0 and vec_count == 0:
        print()
        try:
            import sentence_transformers  # noqa: F401
            extra_installed = True
        except ImportError:
            extra_installed = False
        if not extra_installed:
            print("⚠ Vector search disabled — `[embeddings]` extra not installed.")
            print("  pipx install 'convo-recall[embeddings]' && recall install --with-embeddings")
        elif not EMBED_SOCK.exists():
            print("⚠ Vector search disabled — embed sidecar not running.")
            print("  recall serve --sock " + str(EMBED_SOCK) + "  (or restart `recall install`)")
        elif progress is not None:
            # A backfill chain is currently running — re-word the message so
            # the user doesn't manually re-trigger something already in flight.
            # First-run on a 60K-msg DB takes 5-15 min; small DBs finish in
            # seconds. The progress bar at the top shows live status.
            print("ℹ First-run embedding in progress — see the progress bar above.")
            print("  Re-run `recall stats` to track. Vector search becomes "
                  "available as embeddings complete.")
        else:
            # No active chain — the user can manually start one OR wait for
            # the next watcher-driven ingest tick (which auto-heals up to
            # 2000 missing rows per call).
            print("ℹ Vector search ready but rows aren't embedded yet.")
            print("  • First-run? Embedding takes time proportional to DB size")
            print("    (~50ms per message; 60K msgs ≈ 5-15 min).")
            print("  • Track progress: re-run `recall stats` until the bar")
            print("    completes, then it disappears.")
            print("  • To kick it off now: `recall embed-backfill`")
            print("    (otherwise next watcher-driven ingest auto-heals 2000")
            print("    rows/tick — fully automatic but slower).")
