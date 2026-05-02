"""Tests for the new project_id / display_name model.

Covers items 1, 2, and 4 of the project-identity migration plan:
  - _project_id(cwd) and _display_name(cwd) helpers
  - projects table + _upsert_project
  - fresh DB has project_id columns only
"""

import hashlib
import os
import sqlite3
import sys
from pathlib import Path

import pytest

from convo_recall import ingest


# ── Item 1: _project_id + _display_name ──────────────────────────────────────

def test_project_id_stable_under_realpath(tmp_path):
    """Symlinked path resolves to the same id as its target."""
    target = tmp_path / "real_dir"
    target.mkdir()
    link = tmp_path / "link_to_real"
    link.symlink_to(target)

    assert ingest._project_id(target) == ingest._project_id(link)


def test_project_id_collision_free_for_distinct_realpaths(tmp_path):
    """Two distinct directories produce distinct ids."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()

    assert ingest._project_id(a) != ingest._project_id(b)


def test_project_id_is_12_hex_chars(tmp_path):
    pid = ingest._project_id(tmp_path)
    assert len(pid) == 12
    assert all(c in "0123456789abcdef" for c in pid)


def test_project_id_deterministic(tmp_path):
    """Calling twice on the same path returns the same id."""
    assert ingest._project_id(tmp_path) == ingest._project_id(tmp_path)


def test_project_id_matches_sha1_of_realpath(tmp_path):
    """Spec: first 12 hex chars of sha1(realpath(cwd))."""
    expected = hashlib.sha1(
        os.path.realpath(str(tmp_path)).encode("utf-8")
    ).hexdigest()[:12]
    assert ingest._project_id(tmp_path) == expected


def test_display_name_picks_git_ancestor(tmp_path):
    """display_name walks up to nearest .git ancestor."""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    (repo / ".git").mkdir()
    sub = repo / "src" / "deep" / "nested"
    sub.mkdir(parents=True)

    assert ingest._display_name(sub) == "myrepo"


def test_display_name_picks_pyproject_ancestor(tmp_path):
    """display_name detects pyproject.toml as a marker."""
    repo = tmp_path / "pylib"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname = 'pylib'\n")
    sub = repo / "src" / "pylib"
    sub.mkdir(parents=True)

    assert ingest._display_name(sub) == "pylib"


def test_display_name_falls_back_to_basename_when_no_marker(tmp_path):
    """No marker upstream → basename of cwd itself."""
    bare = tmp_path / "no_marker_anywhere"
    bare.mkdir()
    assert ingest._display_name(bare) == "no_marker_anywhere"


def test_display_name_at_marker_root_returns_marker_basename(tmp_path):
    """When cwd itself contains the marker, return cwd basename."""
    repo = tmp_path / "atroot"
    repo.mkdir()
    (repo / ".git").mkdir()
    assert ingest._display_name(repo) == "atroot"


# ── Item 2: projects table + _upsert_project ─────────────────────────────────

@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """A brand-new DB with no migrations applied — fresh schema."""
    db_path = tmp_path / "fresh.db"
    monkeypatch.setattr(ingest, "DB_PATH", db_path)
    con = ingest.open_db()
    yield con
    ingest.close_db(con)


def _col_names(rows):
    """PRAGMA table_info row may be _Row (attr access) or tuple."""
    out = set()
    for row in rows:
        try:
            out.add(row["name"])
        except (KeyError, TypeError):
            out.add(row[1])
    return out


def _index_names(rows):
    out = set()
    for row in rows:
        try:
            out.add(row["name"])
        except (KeyError, TypeError):
            out.add(row[1])
    return out


def test_projects_table_created_on_open_db(fresh_db):
    """open_db() creates the projects table with the documented schema."""
    cols = _col_names(fresh_db.execute(
        "PRAGMA table_info(projects)"
    ).fetchall())
    assert {"project_id", "display_name", "cwd_realpath",
            "first_seen", "last_updated"}.issubset(cols)


def test_projects_table_has_display_name_index(fresh_db):
    """idx_projects_display_name exists for LIKE queries."""
    indexes = _index_names(fresh_db.execute(
        "PRAGMA index_list(projects)"
    ).fetchall())
    assert "idx_projects_display_name" in indexes


def test_upsert_project_inserts_new_row(fresh_db):
    ingest._upsert_project(fresh_db, "abc123def456", "myproj", "/tmp/myproj")
    row = fresh_db.execute(
        "SELECT project_id, display_name, cwd_realpath FROM projects WHERE project_id = ?",
        ("abc123def456",),
    ).fetchone()
    assert row is not None
    assert (row["project_id"], row["display_name"], row["cwd_realpath"]) == \
           ("abc123def456", "myproj", "/tmp/myproj")


def test_upsert_project_preserves_first_seen(fresh_db):
    """Calling upsert twice keeps first_seen, updates last_updated."""
    import time

    ingest._upsert_project(fresh_db, "abc123def456", "myproj", "/tmp/myproj")
    row1 = fresh_db.execute(
        "SELECT first_seen, last_updated FROM projects WHERE project_id = ?",
        ("abc123def456",),
    ).fetchone()
    first_seen_1 = row1["first_seen"]
    last_updated_1 = row1["last_updated"]

    time.sleep(0.05)  # ensure ts differs
    ingest._upsert_project(fresh_db, "abc123def456", "myproj-renamed", "/tmp/myproj")
    row2 = fresh_db.execute(
        "SELECT first_seen, last_updated, display_name FROM projects WHERE project_id = ?",
        ("abc123def456",),
    ).fetchone()

    assert first_seen_1 == row2["first_seen"], "first_seen must NOT change on update"
    assert last_updated_1 != row2["last_updated"], "last_updated MUST change on update"
    assert row2["display_name"] == "myproj-renamed", "display_name should pick up the new value"


# ── Item 4: fresh DB has project_id columns only ─────────────────────────────

def test_fresh_db_has_project_id_columns_only(fresh_db):
    """Fresh DBs are born at the post-migration shape — no project_slug anywhere."""
    for table in ("sessions", "messages", "ingested_files"):
        cols = {row[1] for row in fresh_db.execute(
            f"PRAGMA table_info({table})"
        ).fetchall()}
        assert "project_id" in cols, f"{table} missing project_id"
        assert "project_slug" not in cols, f"{table} still has project_slug"


def test_fresh_db_messages_fts_has_project_id(fresh_db):
    """FTS5 virtual table is born with project_id (UNINDEXED)."""
    sql_row = fresh_db.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'messages_fts'"
    ).fetchone()
    assert sql_row is not None
    sql = sql_row[0]
    assert "project_id" in sql, "messages_fts missing project_id column"
    assert "project_slug" not in sql, "messages_fts still has project_slug"
