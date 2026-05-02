"""Tests for v4 migration: project_slug → project_id + projects table.

Builds a legacy-shape DB with raw SQL, opens it via ingest.open_db(), then
asserts the migration renamed columns, populated projects, recovered cwd
where possible, and is idempotent.
"""

import json
import os
from pathlib import Path

import apsw
import pytest

from convo_recall import ingest


# ── Helpers ──────────────────────────────────────────────────────────────────

LEGACY_SCHEMA = """
CREATE TABLE sessions (
    session_id   TEXT PRIMARY KEY,
    project_slug TEXT NOT NULL,
    title        TEXT,
    first_seen   TEXT NOT NULL,
    last_updated TEXT NOT NULL,
    agent        TEXT NOT NULL DEFAULT 'claude'
);

CREATE TABLE messages (
    uuid         TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL,
    project_slug TEXT NOT NULL,
    role         TEXT NOT NULL,
    content      TEXT NOT NULL,
    timestamp    TEXT,
    model        TEXT,
    agent        TEXT NOT NULL DEFAULT 'claude'
);

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

CREATE TABLE ingested_files (
    file_path      TEXT PRIMARY KEY,
    session_id     TEXT NOT NULL,
    project_slug   TEXT NOT NULL,
    lines_ingested INTEGER NOT NULL DEFAULT 0,
    last_modified  REAL NOT NULL,
    agent          TEXT NOT NULL DEFAULT 'claude'
);

CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

INSERT INTO schema_migrations(version, applied_at) VALUES
    (2, '2025-01-01T00:00:00+00:00'),
    (3, '2025-01-01T00:00:00+00:00');
"""


def _build_legacy_db(path: Path, sessions: list[dict]) -> None:
    """Create a legacy-shape DB with the given sessions seeded.

    Each `session` dict needs: session_id, agent, project_slug.
    Optionally: messages [(role, content), ...] for fts seed.
    """
    con = apsw.Connection(str(path))
    con.execute(LEGACY_SCHEMA)
    for s in sessions:
        con.execute(
            "INSERT INTO sessions(session_id, project_slug, title, "
            "first_seen, last_updated, agent) "
            "VALUES (?, ?, ?, '2025-01-01', '2025-01-01', ?)",
            (s["session_id"], s["project_slug"], s.get("title"), s["agent"]),
        )
        for i, (role, content) in enumerate(s.get("messages", [])):
            con.execute(
                "INSERT INTO messages(uuid, session_id, project_slug, role, "
                "content, timestamp, agent) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (f"{s['session_id']}-{i}", s["session_id"], s["project_slug"],
                 role, content, "2025-01-01T00:00:00+00:00", s["agent"]),
            )
    con.close()


@pytest.fixture
def legacy_db_path(tmp_path, monkeypatch):
    """Build a legacy DB and point ingest.DB_PATH at it. Migration NOT yet run."""
    db = tmp_path / "legacy.db"
    monkeypatch.setattr(ingest, "DB_PATH", db)
    monkeypatch.setattr(ingest, "_vc", None)
    return db


# ── Tests ────────────────────────────────────────────────────────────────────

def test_migration_creates_bak_snapshot(legacy_db_path, tmp_path):
    _build_legacy_db(legacy_db_path, [
        {"session_id": "s1", "agent": "claude", "project_slug": "alpha"},
    ])
    con = ingest.open_db()
    try:
        baks = list(legacy_db_path.parent.glob(
            f"{legacy_db_path.name}.pre-project-id.*.bak"
        ))
        assert len(baks) == 1, f"expected 1 .bak snapshot, found {len(baks)}: {baks}"
    finally:
        ingest.close_db(con)


def test_migration_renames_columns(legacy_db_path):
    _build_legacy_db(legacy_db_path, [
        {"session_id": "s1", "agent": "claude", "project_slug": "alpha"},
    ])
    con = ingest.open_db()
    try:
        for table in ("sessions", "messages", "ingested_files"):
            cols = {row["name"] for row in con.execute(
                f"PRAGMA table_info({table})"
            ).fetchall()}
            assert "project_id" in cols, f"{table} missing project_id"
            assert "project_slug" not in cols, f"{table} still has project_slug"
    finally:
        ingest.close_db(con)


def test_migration_populates_projects_table(legacy_db_path):
    """Two distinct (agent, slug) pairs → at least two projects rows."""
    _build_legacy_db(legacy_db_path, [
        {"session_id": "s1", "agent": "claude", "project_slug": "alpha"},
        {"session_id": "s2", "agent": "codex",  "project_slug": "beta"},
    ])
    con = ingest.open_db()
    try:
        n = con.execute("SELECT COUNT(*) AS n FROM projects").fetchone()["n"]
        assert n >= 2
        # Every messages.project_id must have a row in projects (FK integrity)
        orphan = con.execute(
            "SELECT COUNT(*) AS n FROM messages m WHERE NOT EXISTS "
            "(SELECT 1 FROM projects p WHERE p.project_id = m.project_id)"
        ).fetchone()["n"]
        assert orphan == 0
    finally:
        ingest.close_db(con)


def test_migration_idempotent(legacy_db_path):
    """Running open_db twice should NOT create a second snapshot or re-migrate."""
    _build_legacy_db(legacy_db_path, [
        {"session_id": "s1", "agent": "claude", "project_slug": "alpha"},
    ])
    con1 = ingest.open_db()
    ingest.close_db(con1)

    baks_after_1 = list(legacy_db_path.parent.glob(
        f"{legacy_db_path.name}.pre-project-id.*.bak"
    ))

    con2 = ingest.open_db()
    try:
        baks_after_2 = list(legacy_db_path.parent.glob(
            f"{legacy_db_path.name}.pre-project-id.*.bak"
        ))
        assert len(baks_after_2) == len(baks_after_1) == 1, \
            "second open should not re-snapshot"
        # Migration v4 recorded once
        n = con2.execute(
            "SELECT COUNT(*) AS n FROM schema_migrations WHERE version = 4"
        ).fetchone()["n"]
        assert n == 1
    finally:
        ingest.close_db(con2)


def test_migration_falls_back_for_unrecoverable_cwd(legacy_db_path):
    """Slug with no recoverable cwd → project_id derived from sha1('legacy:'+slug)."""
    _build_legacy_db(legacy_db_path, [
        {"session_id": "s1", "agent": "claude",
         "project_slug": "definitely_not_a_real_project_xyz_abc_123"},
    ])
    con = ingest.open_db()
    try:
        row = con.execute(
            "SELECT project_id FROM sessions WHERE session_id = 's1'"
        ).fetchone()
        from convo_recall.ingest import _legacy_project_id
        assert row["project_id"] == _legacy_project_id(
            "definitely_not_a_real_project_xyz_abc_123"
        )
        # display_name == old slug
        proj = con.execute(
            "SELECT display_name FROM projects WHERE project_id = ?",
            (row["project_id"],),
        ).fetchone()
        assert proj["display_name"] == "definitely_not_a_real_project_xyz_abc_123"
    finally:
        ingest.close_db(con)


def test_migration_handles_gemini_hash_only(legacy_db_path, tmp_path, monkeypatch):
    """Gemini slug with no projects.json entry → gemini-hash:<hash> id."""
    # Empty gemini aliases (point to a non-existent file)
    monkeypatch.setattr(ingest, "_load_gemini_aliases", lambda: {})
    monkeypatch.setattr(ingest, "GEMINI_TMP", tmp_path / "no_gemini")

    _build_legacy_db(legacy_db_path, [
        {"session_id": "g1", "agent": "gemini", "project_slug": "abc123hashdir"},
    ])
    con = ingest.open_db()
    try:
        row = con.execute(
            "SELECT project_id FROM sessions WHERE session_id = 'g1'"
        ).fetchone()
        from convo_recall.ingest import _gemini_hash_project_id
        assert row["project_id"] == _gemini_hash_project_id("abc123hashdir")
    finally:
        ingest.close_db(con)


def test_migration_recovers_cwd_from_codex_session_meta(legacy_db_path, tmp_path, monkeypatch):
    """When a codex rollout's payload.cwd derives the same slug, recover the real cwd."""
    # Set up a fake CODEX_SESSIONS dir with one rollout
    codex_root = tmp_path / "codex_sessions"
    rollout_dir = codex_root / "2025" / "01" / "01"
    rollout_dir.mkdir(parents=True)
    rollout = rollout_dir / "rollout-test.jsonl"

    # Build a real cwd path and compute its slug via the legacy fn
    real_cwd = tmp_path / "MyApp"
    real_cwd.mkdir()
    legacy_slug = ingest._legacy_codex_slug(str(real_cwd))

    rollout.write_text(json.dumps({
        "type": "session_meta",
        "payload": {"cwd": str(real_cwd)}
    }) + "\n")

    monkeypatch.setattr(ingest, "CODEX_SESSIONS", codex_root)

    _build_legacy_db(legacy_db_path, [
        {"session_id": "c1", "agent": "codex", "project_slug": legacy_slug},
    ])
    con = ingest.open_db()
    try:
        row = con.execute(
            "SELECT project_id FROM sessions WHERE session_id = 'c1'"
        ).fetchone()
        # Should equal _project_id of the real cwd, NOT the legacy fallback
        assert row["project_id"] == ingest._project_id(str(real_cwd))
        proj = con.execute(
            "SELECT display_name, cwd_realpath FROM projects WHERE project_id = ?",
            (row["project_id"],),
        ).fetchone()
        # display_name = basename of nearest marker ancestor; here no .git
        # so falls back to basename of real_cwd
        assert proj["display_name"] == "MyApp"
        assert proj["cwd_realpath"] == os.path.realpath(str(real_cwd))
    finally:
        ingest.close_db(con)


def test_migration_fts_rebuilt_with_project_id(legacy_db_path):
    """messages_fts must reference project_id, not project_slug, post-migration."""
    _build_legacy_db(legacy_db_path, [
        {"session_id": "s1", "agent": "claude", "project_slug": "alpha",
         "messages": [("user", "hello world")]},
    ])
    con = ingest.open_db()
    try:
        sql = con.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'messages_fts'"
        ).fetchone()["sql"]
        assert "project_id" in sql
        assert "project_slug" not in sql
        # FTS rebuild should preserve content
        n = con.execute(
            "SELECT COUNT(*) AS n FROM messages_fts WHERE messages_fts MATCH 'hello'"
        ).fetchone()["n"]
        assert n == 1
    finally:
        ingest.close_db(con)
