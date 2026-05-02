"""Tests for items 5-7: Claude/Gemini/Codex ingest paths writing project_id.

Builds synthetic source jsonl files with cwd-recovery hints, ingests them,
asserts the resulting messages.project_id matches _project_id(cwd) and that
the projects table row exists.
"""

import json
import os
from pathlib import Path

import apsw
import pytest

from convo_recall import ingest


@pytest.fixture
def db(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(ingest, "DB_PATH", db_file)
    monkeypatch.setattr(ingest, "_vc", None)
    con = ingest.open_db()
    yield con
    ingest.close_db(con)


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


# ── Item 5: Claude ───────────────────────────────────────────────────────────

def test_claude_ingest_writes_project_id_from_session_cwd(db, tmp_path):
    """Claude session with a cwd field on a user record → project_id derived from cwd."""
    real_cwd = tmp_path / "MyApp"
    real_cwd.mkdir()
    (real_cwd / ".git").mkdir()  # marker → display_name = "MyApp"

    proj_dir = tmp_path / "claude-projects" / "-tmp-MyApp"
    sess = proj_dir / "abc-123.jsonl"
    _write_jsonl(sess, [
        {"type": "user", "uuid": "u1", "cwd": str(real_cwd),
         "message": {"role": "user", "content": "hello"},
         "timestamp": "2026-01-01T00:00:00+00:00"},
        {"type": "assistant", "uuid": "a1",
         "message": {"role": "assistant", "content": "hi back"},
         "timestamp": "2026-01-01T00:00:01+00:00"},
    ])

    inserted = ingest.ingest_file(db, sess, do_embed=False, agent="claude")
    assert inserted == 2

    row = db.execute(
        "SELECT project_id FROM messages WHERE uuid = 'u1'"
    ).fetchone()
    assert row["project_id"] == ingest._project_id(str(real_cwd))

    proj = db.execute(
        "SELECT display_name, cwd_realpath FROM projects WHERE project_id = ?",
        (row["project_id"],),
    ).fetchone()
    assert proj["display_name"] == "MyApp"
    assert proj["cwd_realpath"] == os.path.realpath(str(real_cwd))


def test_claude_ingest_falls_back_to_legacy_when_no_cwd(db, tmp_path):
    """Claude session with NO cwd in any record → legacy project_id from slug."""
    proj_dir = tmp_path / "claude-projects" / "-Users-x-Projects-rare"
    sess = proj_dir / "noc-999.jsonl"
    _write_jsonl(sess, [
        {"type": "user", "uuid": "u1",
         "message": {"role": "user", "content": "no cwd here"},
         "timestamp": "2026-01-01T00:00:00+00:00"},
    ])

    ingest.ingest_file(db, sess, do_embed=False, agent="claude")

    row = db.execute(
        "SELECT project_id FROM messages WHERE uuid = 'u1'"
    ).fetchone()
    legacy = ingest._legacy_claude_slug(sess)
    assert row["project_id"] == ingest._legacy_project_id(legacy)
    proj = db.execute(
        "SELECT display_name FROM projects WHERE project_id = ?",
        (row["project_id"],),
    ).fetchone()
    assert proj["display_name"] == legacy


# ── Item 6: Gemini ───────────────────────────────────────────────────────────

def test_gemini_cwd_yields_consistent_project_id(db, tmp_path, monkeypatch):
    """Gemini header with cwd → project_id derived from cwd."""
    real_cwd = tmp_path / "GeminiApp"
    real_cwd.mkdir()

    monkeypatch.setattr(ingest, "_load_gemini_aliases", lambda: {})

    sess = tmp_path / "gemini" / "tmp" / "somehash" / "chats" / "session-1.jsonl"
    _write_jsonl(sess, [
        {"sessionId": "g-1", "startTime": "2026-01-01T00:00:00+00:00",
         "cwd": str(real_cwd)},
        {"id": "u1", "type": "user", "timestamp": "2026-01-01T00:00:01+00:00",
         "content": [{"text": "ping"}]},
        {"id": "a1", "type": "gemini", "timestamp": "2026-01-01T00:00:02+00:00",
         "content": [{"text": "pong"}]},
    ])

    ingest.ingest_gemini_file(db, sess, do_embed=False)

    row = db.execute(
        "SELECT project_id FROM messages WHERE uuid = 'u1'"
    ).fetchone()
    assert row["project_id"] == ingest._project_id(str(real_cwd))

    # Re-ingest should yield same project_id (stable)
    sess2 = tmp_path / "gemini" / "tmp" / "somehash" / "chats" / "session-2.jsonl"
    _write_jsonl(sess2, [
        {"sessionId": "g-2", "startTime": "2026-01-02T00:00:00+00:00",
         "cwd": str(real_cwd)},
        {"id": "u2", "type": "user", "timestamp": "2026-01-02T00:00:01+00:00",
         "content": [{"text": "again"}]},
    ])
    ingest.ingest_gemini_file(db, sess2, do_embed=False)
    row2 = db.execute(
        "SELECT project_id FROM messages WHERE uuid = 'u2'"
    ).fetchone()
    assert row["project_id"] == row2["project_id"]


def test_gemini_hash_only_fallback_uses_synthetic_id(db, tmp_path, monkeypatch):
    """No cwd in header AND no projects.json alias → gemini-hash:<hash> id."""
    monkeypatch.setattr(ingest, "_load_gemini_aliases", lambda: {})

    hash_dir = "abcdef0123456789"
    sess = tmp_path / "gemini" / "tmp" / hash_dir / "chats" / "session-1.jsonl"
    _write_jsonl(sess, [
        {"sessionId": "g-1", "startTime": "2026-01-01T00:00:00+00:00"},
        {"id": "u1", "type": "user", "timestamp": "2026-01-01T00:00:01+00:00",
         "content": [{"text": "no cwd"}]},
    ])

    ingest.ingest_gemini_file(db, sess, do_embed=False)

    row = db.execute(
        "SELECT project_id FROM messages WHERE uuid = 'u1'"
    ).fetchone()
    assert row["project_id"] == ingest._gemini_hash_project_id(hash_dir)
    proj = db.execute(
        "SELECT display_name, cwd_realpath FROM projects WHERE project_id = ?",
        (row["project_id"],),
    ).fetchone()
    assert proj["display_name"] == hash_dir
    assert proj["cwd_realpath"] is None


# ── Item 7: Codex ────────────────────────────────────────────────────────────

def test_codex_writes_project_id_from_session_meta_cwd(db, tmp_path):
    """Codex session_meta with payload.cwd → project_id from that cwd."""
    real_cwd = tmp_path / "WorkFoo"
    real_cwd.mkdir()

    sess = tmp_path / "codex" / "2026" / "01" / "01" / "rollout-test.jsonl"
    _write_jsonl(sess, [
        {"type": "session_meta", "timestamp": "2026-01-01T00:00:00+00:00",
         "payload": {"id": "codex-sess-1", "cwd": str(real_cwd),
                     "timestamp": "2026-01-01T00:00:00+00:00"}},
        {"type": "response_item", "timestamp": "2026-01-01T00:00:01+00:00",
         "payload": {"id": "u1", "type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "hello codex"}]}},
        {"type": "response_item", "timestamp": "2026-01-01T00:00:02+00:00",
         "payload": {"id": "a1", "type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "hi"}]}},
    ])

    inserted = ingest.ingest_codex_file(db, sess, do_embed=False)
    assert inserted == 2

    row = db.execute(
        "SELECT project_id FROM messages WHERE uuid = 'u1'"
    ).fetchone()
    assert row["project_id"] == ingest._project_id(str(real_cwd))

    proj = db.execute(
        "SELECT display_name, cwd_realpath FROM projects WHERE project_id = ?",
        (row["project_id"],),
    ).fetchone()
    assert proj["display_name"] == "WorkFoo"
    assert proj["cwd_realpath"] == os.path.realpath(str(real_cwd))


def test_codex_no_session_meta_uses_legacy_unknown(db, tmp_path):
    """Codex with no session_meta → falls back to legacy_project_id('codex_unknown')."""
    sess = tmp_path / "codex" / "2026" / "01" / "01" / "rollout-nometa.jsonl"
    _write_jsonl(sess, [
        {"type": "response_item", "timestamp": "2026-01-01T00:00:01+00:00",
         "payload": {"id": "u1", "type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "no meta"}]}},
    ])

    ingest.ingest_codex_file(db, sess, do_embed=False)

    row = db.execute(
        "SELECT project_id FROM messages WHERE uuid = 'u1'"
    ).fetchone()
    assert row["project_id"] == ingest._legacy_project_id("codex_unknown")
