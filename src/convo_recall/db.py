"""
Schema, migrations, and connection lifecycle for convo-recall.

Provides:
  - DB_PATH, EMBED_DIM module-level config.
  - open_db(readonly) / close_db(con): connection lifecycle. Loads sqlite-vec,
    sets WAL, runs schema bootstrap + migration chain, hardens file perms.
  - _Row / _row_factory: sqlite3.Row-compatible wrapper for apsw cursors.
  - _init_schema, _init_vec_tables, _upsert_project: schema/projects bootstrap.
  - _has_column, _ensure_migrations_table, _migration_applied,
    _record_migration: schema_migrations table plumbing.
  - _MIGRATION_* version constants and the three migrations:
    _migrate_add_agent_column (v2), _migrate_fts_porter (v3),
    _migrate_project_id (v4).
  - _enable_wal_mode, _harden_perms: file-level helpers.
  - _VEC_ENABLED dict, _vec_ok(con), legacy _vc: per-connection vec state.
    `embed.py` (A3) reads `_VEC_ENABLED[con]` directly; `_vec_ok` is the
    public predicate.

Extracted from ingest.py in v0.4.0 (TD-008). Back-compat re-exports keep
`from convo_recall.ingest import open_db, ...` working through one release.

Test-monkeypatch contract: `tests/*.py` historically `monkeypatch.setattr`'s
`convo_recall.ingest.{DB_PATH,_enable_wal_mode,_record_migration}` — those
patches MUST flow through to the real call sites. Functions in this module
that touch those symbols read them via `from . import ingest as _ing` and
access `_ing.X` so the monkeypatched binding wins. A8 (test rewiring + shim
removal) replaces this indirection with direct imports of the canonical
homes.
"""

import os
import sys
import weakref
from datetime import datetime, timezone
from pathlib import Path

import apsw

from .identity import (
    _display_name,
    _gemini_hash_project_id,
    _legacy_project_id,
    _project_id,
    _scan_claude_cwd,
    _scan_codex_cwd,
    _scan_gemini_cwd,
)


# DB_PATH stays defined in ingest.py through v0.4.0 — the docstring-truth
# test (`tests/test_ingest_docstring_truth.py`) reloads ingest with env vars
# cleared and reads `ingest.DB_PATH`; if DB_PATH lived here, the reload
# wouldn't refresh db.DB_PATH (db isn't reloaded) and the test would see a
# stale value. A8 moves the canonical owner to db.py when tests rewire.
# db.py's connection lifecycle reads `_ing.DB_PATH` at call time below.

EMBED_DIM = 1024


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
_vc: "apsw.Connection | None" = None  # last vec-enabled connection (legacy)


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


# ── File-perm hardening + WAL ────────────────────────────────────────────────

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


# ── Connection lifecycle ─────────────────────────────────────────────────────

def open_db(readonly: bool = False) -> apsw.Connection:
    # Read DB_PATH and _enable_wal_mode through the ingest namespace so test
    # monkeypatches on ingest reach this codepath. See module docstring.
    from . import ingest as _ing
    db_path = _ing.DB_PATH
    enable_wal = _ing._enable_wal_mode

    global _vc
    # Read-only mode: open the DB without trying to create sidecars or
    # chmod the parent dir. Used by search/stats/doctor under sandboxed
    # subprocess contexts (e.g. Codex CLI restricts writes to the
    # project working dir; WAL mode creates `.db-wal` and `.db-shm`
    # outside that dir → apsw.CantOpenError on the WAL pragma).
    if readonly:
        if not db_path.exists():
            # Read-only on a missing DB is a hard error — there's
            # nothing to read. Surface it before apsw does.
            raise apsw.CantOpenError(
                f"DB not found at {db_path} (CONVO_RECALL_DB not set; "
                f"run `recall install` or set the env var)"
            )
        con = apsw.Connection(str(db_path), flags=apsw.SQLITE_OPEN_READONLY)
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
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _harden_perms(db_path.parent, 0o700)
    con = apsw.Connection(str(db_path))
    con.row_trace = _row_factory
    try:
        enable_wal(con)
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
    for sidecar in (db_path, db_path.with_suffix(db_path.suffix + "-wal"),
                    db_path.with_suffix(db_path.suffix + "-shm")):
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


# ── Schema bootstrap ─────────────────────────────────────────────────────────

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
                    display_name: str, cwd_realpath: "str | None") -> None:
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


# ── Migrations table ─────────────────────────────────────────────────────────

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
    from . import ingest as _ing
    record = _ing._record_migration

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
    record(con, _MIGRATION_AGENT_COLUMN)


def _migrate_fts_porter(con: apsw.Connection) -> None:
    """Migrate FTS table to porter+unicode61 tokenizer if needed AND make sure
    the FTS schema includes the `agent` UNINDEXED column. Both conditions
    trigger the same drop-rebuild flow (they share a code path)."""
    from . import ingest as _ing
    record = _ing._record_migration

    if _migration_applied(con, _MIGRATION_FTS_PORTER):
        return
    row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='messages_fts'"
    ).fetchone()
    sql = (row[0] or "") if row else ""
    needs_porter = "porter" not in sql
    needs_agent = "agent" not in sql
    if not (needs_porter or needs_agent):
        record(con, _MIGRATION_FTS_PORTER)
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
    record(con, _MIGRATION_FTS_PORTER)
    print("[migrate] Done.", file=sys.stderr)


# ── v4: project_slug → project_id + projects table ───────────────────────────

def _migrate_project_id(con: apsw.Connection) -> None:
    """v4 migration: project_slug → project_id; populate projects table; rebuild FTS.

    Idempotent: gated on _MIGRATION_PROJECT_ID. Snapshots DB to .pre-project-id.<ts>.bak
    before any DDL. On a FRESH DB whose tables are already at the post-v4 shape
    (project_id columns), records the migration and only ensures the projects
    table is in sync — no rename, no FTS rebuild.
    """
    import shutil
    from . import ingest as _ing
    db_path = _ing.DB_PATH
    record = _ing._record_migration

    if _migration_applied(con, _MIGRATION_PROJECT_ID):
        return

    fresh_shape = _has_column(con, "messages", "project_id")
    if fresh_shape:
        # Fresh DB born at v4: nothing to rename, nothing to backfill,
        # FTS already correct. Just record the migration.
        record(con, _MIGRATION_PROJECT_ID)
        return

    # Legacy DB — snapshot first
    if db_path.exists() and str(db_path) not in (":memory:",):
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        bak = db_path.with_suffix(db_path.suffix + f".pre-project-id.{ts}.bak")
        try:
            shutil.copy2(str(db_path), str(bak))
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

    mapping: "dict[tuple[str, str], tuple[str, str, str | None]]" = {}
    for row in slug_pairs:
        agent = _row(row, "agent", 0)
        slug = _row(row, "project_slug", 1)
        cwd: "str | None" = None
        gemini_hash: "str | None" = None
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

    record(con, _MIGRATION_PROJECT_ID)
    print("[migrate] project_id migration complete.", file=sys.stderr)


def _init_vec_tables(vc) -> None:
    vc.execute(f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS message_vecs USING vec0(
            rowid INTEGER PRIMARY KEY,
            embedding FLOAT[{EMBED_DIM}]
        )
    """)
