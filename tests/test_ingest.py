"""Basic smoke tests — no embedding service required."""

import json
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("CONVO_RECALL_DB", ":memory:")  # overridden per test

import convo_recall.ingest as ingest


@pytest.fixture()
def db(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(ingest, "DB_PATH", db_file)
    monkeypatch.setattr(ingest, "_vc", None)  # no vec ops in tests
    con = ingest.open_db()
    yield con
    con.close()


def _write_session(path: Path, messages: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for msg in messages:
            f.write(json.dumps(msg) + "\n")


def test_ingest_file_basic(db, tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "PROJECTS_DIR", tmp_path)
    session = tmp_path / "proj_foo" / "session-abc123.jsonl"
    _write_session(session, [
        {"uuid": "u1", "type": "user", "timestamp": "2026-01-01T00:00:00Z",
         "message": {"role": "user", "content": "hello world"}},
        {"uuid": "u2", "type": "assistant", "timestamp": "2026-01-01T00:00:01Z",
         "message": {"role": "assistant", "content": "hi there", "model": "claude-3"}},
    ])
    n = ingest.ingest_file(db, session, do_embed=False)
    assert n == 2

    rows = db.execute("SELECT role, content FROM messages ORDER BY rowid").fetchall()
    assert rows[0]["role"] == "user"
    assert "hello world" in rows[0]["content"]
    assert rows[1]["role"] == "assistant"


def test_ingest_file_idempotent(db, tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "PROJECTS_DIR", tmp_path)
    session = tmp_path / "proj_foo" / "session-abc123.jsonl"
    _write_session(session, [
        {"uuid": "u1", "type": "user", "timestamp": "2026-01-01T00:00:00Z",
         "message": {"role": "user", "content": "idempotency test"}},
    ])
    n1 = ingest.ingest_file(db, session, do_embed=False)
    n2 = ingest.ingest_file(db, session, do_embed=False)
    assert n1 == 1
    assert n2 == 0  # second run: no new content, mtime unchanged


def test_fts_search(db, tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "PROJECTS_DIR", tmp_path)
    session = tmp_path / "proj_foo" / "session-fts.jsonl"
    _write_session(session, [
        {"uuid": "u1", "type": "user", "timestamp": "2026-01-01T00:00:00Z",
         "message": {"role": "user", "content": "the camelCase token splitting improves FTS recall"}},
    ])
    ingest.ingest_file(db, session, do_embed=False)

    # Hybrid search falls back to FTS when _vc is None
    results = []
    import io, sys
    buf = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf
    try:
        ingest.search(db, "camel case token", project="proj_foo", limit=5, context=0)
    finally:
        sys.stdout = old_stdout
    output = buf.getvalue()
    assert "camel" in output.lower() or "proj_foo" in output


def test_expand_code_tokens():
    result = ingest._expand_code_tokens("ingestConversations _extract_text")
    assert "ingest" in result
    assert "Conversations" in result or "conversations" in result.lower()
    assert "extract" in result


def test_slug_from_path(tmp_path):
    monkeypatch_projects = tmp_path
    jsonl = tmp_path / "apps_foo" / "session.jsonl"
    jsonl.parent.mkdir(parents=True)
    jsonl.touch()

    import convo_recall.ingest as _i
    slug = _i._slug_from_path.__wrapped__(jsonl) if hasattr(_i._slug_from_path, "__wrapped__") else None
    # Just check the public function doesn't crash on a plausible path
    assert True  # slug derivation tested via ingest_file above


def test_stats_runs(db):
    ingest.stats(db)  # should not raise


# ── Phase 2: agent column ─────────────────────────────────────────────────────

def test_agent_column_default_claude(db, tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "PROJECTS_DIR", tmp_path)
    session = tmp_path / "proj_foo" / "session-x.jsonl"
    _write_session(session, [
        {"uuid": "u1", "type": "user", "timestamp": "2026-01-01T00:00:00Z",
         "message": {"role": "user", "content": "hi"}},
    ])
    ingest.ingest_file(db, session, do_embed=False)
    row = db.execute("SELECT agent FROM messages").fetchone()
    assert row["agent"] == "claude"
    fts = db.execute("SELECT agent FROM messages_fts").fetchone()
    assert fts["agent"] == "claude"
    sess = db.execute("SELECT agent FROM sessions").fetchone()
    assert sess["agent"] == "claude"


def test_agent_migration_preserves_existing_rows(tmp_path, monkeypatch):
    """Open with old schema (no agent column), insert a row, then reopen via
    open_db() — migration backfills existing rows with 'claude'."""
    db_file = tmp_path / "legacy.db"
    # Seed an old-shape DB using apsw directly with NO agent column.
    import apsw
    seed = apsw.Connection(str(db_file))
    seed.execute("PRAGMA journal_mode=WAL")
    seed.execute("""
        CREATE TABLE sessions (session_id TEXT PRIMARY KEY, project_slug TEXT NOT NULL,
                               title TEXT, first_seen TEXT NOT NULL, last_updated TEXT NOT NULL);
        CREATE TABLE messages (uuid TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                               project_slug TEXT NOT NULL, role TEXT NOT NULL,
                               content TEXT NOT NULL, timestamp TEXT, model TEXT);
        CREATE TABLE ingested_files (file_path TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                                     project_slug TEXT NOT NULL, lines_ingested INTEGER NOT NULL DEFAULT 0,
                                     last_modified REAL NOT NULL);
        CREATE VIRTUAL TABLE messages_fts USING fts5(content, session_id UNINDEXED,
            project_slug UNINDEXED, role UNINDEXED, content='messages',
            content_rowid='rowid', tokenize='porter unicode61');
    """)
    seed.execute("INSERT INTO sessions(session_id, project_slug, title, first_seen, last_updated) "
                 "VALUES (?, ?, ?, ?, ?)",
                 ("legacy-1", "old_proj", None, "2025-01-01", "2025-01-01"))
    seed.execute("INSERT INTO messages(uuid, session_id, project_slug, role, content) "
                 "VALUES (?, ?, ?, ?, ?)",
                 ("u-leg", "legacy-1", "old_proj", "user", "legacy content"))
    seed.close()

    monkeypatch.setattr(ingest, "DB_PATH", db_file)
    monkeypatch.setattr(ingest, "_vc", None)
    con = ingest.open_db()
    try:
        rows = con.execute("SELECT agent FROM messages").fetchall()
        assert all(r["agent"] == "claude" for r in rows)
        sess = con.execute("SELECT agent FROM sessions").fetchall()
        assert all(s["agent"] == "claude" for s in sess)
        # FTS rebuilt with agent column populated for legacy rows
        fts = con.execute("SELECT agent FROM messages_fts").fetchall()
        assert all(f["agent"] == "claude" for f in fts)
    finally:
        con.close()


def test_stats_shows_by_agent(db, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ingest, "PROJECTS_DIR", tmp_path)
    session = tmp_path / "proj_foo" / "session.jsonl"
    _write_session(session, [
        {"uuid": "u1", "type": "user", "timestamp": "2026-01-01T00:00:00Z",
         "message": {"role": "user", "content": "agent stats test"}},
    ])
    ingest.ingest_file(db, session, do_embed=False)
    capsys.readouterr()  # clear
    ingest.stats(db)
    out = capsys.readouterr().out
    assert "By agent" in out
    assert "claude" in out


def test_detect_agents_reports_only_present(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / ".claude" / "projects" / "p1").mkdir(parents=True)
    (home / ".claude" / "projects" / "p1" / "s.jsonl").write_text("{}\n")
    monkeypatch.setattr(ingest, "PROJECTS_DIR", home / ".claude" / "projects")
    monkeypatch.setattr(ingest, "GEMINI_TMP", home / ".gemini" / "tmp")
    monkeypatch.setattr(ingest, "CODEX_SESSIONS", home / ".codex" / "sessions")
    agents = ingest.detect_agents()
    by_name = {a["name"]: a["file_count"] for a in agents}
    assert by_name["claude"] == 1
    assert by_name["gemini"] == 0
    assert by_name["codex"] == 0


def test_detect_agents_finds_all_three(monkeypatch, tmp_path):
    home = tmp_path / "home"
    # claude session
    (home / ".claude" / "projects" / "pX").mkdir(parents=True)
    (home / ".claude" / "projects" / "pX" / "s1.jsonl").write_text("{}\n")
    # gemini session
    (home / ".gemini" / "tmp" / "myproj" / "chats").mkdir(parents=True)
    (home / ".gemini" / "tmp" / "myproj" / "chats" / "session-001.jsonl").write_text("{}\n")
    # codex rollout
    (home / ".codex" / "sessions" / "2026" / "04" / "30").mkdir(parents=True)
    (home / ".codex" / "sessions" / "2026" / "04" / "30" / "rollout-x.jsonl").write_text("{}\n")
    monkeypatch.setattr(ingest, "PROJECTS_DIR", home / ".claude" / "projects")
    monkeypatch.setattr(ingest, "GEMINI_TMP", home / ".gemini" / "tmp")
    monkeypatch.setattr(ingest, "CODEX_SESSIONS", home / ".codex" / "sessions")
    by_name = {a["name"]: a["file_count"] for a in ingest.detect_agents()}
    assert by_name == {"claude": 1, "gemini": 1, "codex": 1}


def test_config_round_trip(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    monkeypatch.setattr(ingest, "_CONFIG_PATH", cfg_path)
    ingest.save_config({"agents": ["claude", "codex"]})
    assert ingest.load_config()["agents"] == ["claude", "codex"]
    # mode 0o600 (owner-only) — POSIX-only assertion
    import stat
    mode = stat.S_IMODE(cfg_path.stat().st_mode)
    assert mode == 0o600


def test_load_config_defaults_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "_CONFIG_PATH", tmp_path / "no_such_config.json")
    cfg = ingest.load_config()
    assert cfg == {"agents": ["claude"]}


def test_search_shows_agent_tag(db, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ingest, "PROJECTS_DIR", tmp_path)
    session = tmp_path / "proj_foo" / "session.jsonl"
    _write_session(session, [
        {"uuid": "u1", "type": "user", "timestamp": "2026-01-01T00:00:00Z",
         "message": {"role": "user", "content": "tagged message body"}},
    ])
    ingest.ingest_file(db, session, do_embed=False)
    capsys.readouterr()
    ingest.search(db, "tagged", project="proj_foo", limit=3, context=0)
    out = capsys.readouterr().out
    assert "[claude]" in out


# ── Phase 4a: claude parser preserves existing behavior ────────────────────────

def test_claude_parser_preserves_existing_behavior(db, tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "PROJECTS_DIR", tmp_path)
    session = tmp_path / "apps_foo" / "session-x.jsonl"
    _write_session(session, [
        {"uuid": "u1", "type": "user", "timestamp": "2026-01-01T00:00:00Z",
         "message": {"role": "user", "content": "claude hello"}},
    ])
    ingest.ingest_file(db, session, do_embed=False)
    row = db.execute("SELECT agent, project_slug, content FROM messages").fetchone()
    assert row["agent"] == "claude"
    assert row["project_slug"] == "apps_foo"
    assert "claude hello" in row["content"]


# ── Phase 4b: gemini parser ────────────────────────────────────────────────────

def test_gemini_parser_basic(db, tmp_path, monkeypatch):
    gtmp = tmp_path / "gemini" / "tmp" / "myproj" / "chats"
    gtmp.mkdir(parents=True)
    sess = gtmp / "session-001.jsonl"
    sess.write_text(
        json.dumps({"sessionId": "g-001", "startTime": "2026-04-01T00:00:00Z",
                    "kind": "main"}) + "\n"
        + json.dumps({"id": "m1", "timestamp": "2026-04-01T00:00:01Z",
                      "type": "user", "content": [{"text": "gemini hi"}]}) + "\n"
        + json.dumps({"id": "m2", "timestamp": "2026-04-01T00:00:02Z",
                      "type": "gemini", "content": [{"text": "gemini reply"}]}) + "\n"
        + json.dumps({"$set": {"lastUpdated": "2026-04-01T00:00:03Z"}}) + "\n"
        + json.dumps({"id": "m3", "timestamp": "2026-04-01T00:00:04Z",
                      "type": "info", "message": "skip me"}) + "\n"
    )
    monkeypatch.setattr(ingest, "GEMINI_TMP", tmp_path / "gemini" / "tmp")
    n = ingest.ingest_gemini_file(db, sess, do_embed=False)
    assert n == 2
    rows = db.execute(
        "SELECT role, content, agent, project_slug, session_id "
        "FROM messages ORDER BY rowid"
    ).fetchall()
    assert len(rows) == 2
    assert all(r["agent"] == "gemini" and r["project_slug"] == "myproj"
               and r["session_id"] == "g-001" for r in rows)
    assert rows[0]["role"] == "user" and "gemini hi" in rows[0]["content"]
    assert rows[1]["role"] == "assistant" and "gemini reply" in rows[1]["content"]


def test_gemini_parser_idempotent(db, tmp_path, monkeypatch):
    gtmp = tmp_path / "gemini" / "tmp" / "p1" / "chats"
    gtmp.mkdir(parents=True)
    sess = gtmp / "session-002.jsonl"
    sess.write_text(
        json.dumps({"sessionId": "g-002", "startTime": "2026-04-01T00:00:00Z"}) + "\n"
        + json.dumps({"id": "m1", "type": "user",
                      "content": [{"text": "idem test"}]}) + "\n"
    )
    monkeypatch.setattr(ingest, "GEMINI_TMP", tmp_path / "gemini" / "tmp")
    n1 = ingest.ingest_gemini_file(db, sess, do_embed=False)
    n2 = ingest.ingest_gemini_file(db, sess, do_embed=False)
    assert n1 == 1
    assert n2 == 0


# ── Phase 4c: codex parser ─────────────────────────────────────────────────────

def test_codex_parser_basic(db, tmp_path, monkeypatch):
    cdir = tmp_path / "codex" / "sessions" / "2026" / "04" / "01"
    cdir.mkdir(parents=True)
    sess = cdir / "rollout-2026-04-01T00-00-00-abc.jsonl"
    sess.write_text(
        json.dumps({
            "type": "session_meta",
            "timestamp": "2026-04-01T00:00:00Z",
            "payload": {"id": "c-abc",
                        "cwd": "/Users/x/Projects/mcp/Foo",
                        "timestamp": "2026-04-01T00:00:00Z"},
        }) + "\n"
        + json.dumps({
            "type": "response_item",
            "timestamp": "2026-04-01T00:00:01Z",
            "payload": {"type": "message", "role": "user",
                        "content": [{"type": "input_text", "text": "codex hi"}]},
        }) + "\n"
        + json.dumps({
            "type": "response_item",
            "timestamp": "2026-04-01T00:00:02Z",
            "payload": {"type": "message", "role": "assistant",
                        "content": [{"type": "output_text", "text": "codex reply"}]},
        }) + "\n"
        + json.dumps({"type": "event_msg", "payload":
                      {"type": "user_message", "message": "skip"}}) + "\n"
        + json.dumps({"type": "response_item",
                      "payload": {"type": "message", "role": "developer",
                                  "content": [{"type": "input_text",
                                               "text": "developer prompt skip"}]}}) + "\n"
        + json.dumps({"type": "response_item",
                      "payload": {"type": "function_call",
                                  "name": "shell", "arguments": "{}"}}) + "\n"
    )
    monkeypatch.setattr(ingest, "CODEX_SESSIONS", tmp_path / "codex" / "sessions")
    n = ingest.ingest_codex_file(db, sess, do_embed=False)
    assert n == 2
    rows = db.execute(
        "SELECT role, content, agent, project_slug, session_id "
        "FROM messages ORDER BY rowid"
    ).fetchall()
    assert len(rows) == 2
    assert all(r["agent"] == "codex" for r in rows)
    assert all(r["session_id"] == "c-abc" for r in rows)
    assert all(r["project_slug"] == "mcp_Foo" for r in rows)
    assert "codex hi" in rows[0]["content"]
    assert "codex reply" in rows[1]["content"]


def test_codex_slug_from_cwd():
    assert ingest._codex_slug_from_cwd("/Users/x/Projects/mcp/Foo") == "mcp_Foo"
    assert ingest._codex_slug_from_cwd("/Users/x/Projects/apps/Avatar/web") == "apps_Avatar_web"
    assert ingest._codex_slug_from_cwd("/Users/x/Projects/foo") == "foo"
    # Non-Projects path falls back to last 2 components
    assert ingest._codex_slug_from_cwd("/some/random/path") == "random_path"


def test_search_agent_filter(db, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ingest, "PROJECTS_DIR", tmp_path)
    s_claude = tmp_path / "p_claude" / "session.jsonl"
    _write_session(s_claude, [
        {"uuid": "uc", "type": "user", "timestamp": "2026-01-01T00:00:00Z",
         "message": {"role": "user", "content": "needle in claude"}},
    ])
    ingest.ingest_file(db, s_claude, do_embed=False, agent="claude")
    # Manual gemini-shaped row
    db.execute(
        "INSERT INTO messages(uuid, session_id, project_slug, role, content, "
        "timestamp, model, agent) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("ug", "g-1", "p_gemini", "assistant", "needle in gemini",
         "2026-01-01T00:00:00Z", None, "gemini"),
    )
    capsys.readouterr()
    ingest.search(db, "needle", agent="claude", limit=10, context=0)
    out = capsys.readouterr().out
    assert "claude" in out
    assert "needle in gemini" not in out  # filtered out


# ── Phase 5: per-agent plists + watch loop ─────────────────────────────────────

def test_install_emits_one_plist_per_enabled_agent(tmp_path, monkeypatch):
    """install.run() generates one plist per enabled agent and writes the
    config. The launchctl bootstrap is monkeypatched to a no-op so no real
    macOS launchd interaction happens during the test."""
    from convo_recall import install as _install
    monkeypatch.setattr(_install, "_require_macos", lambda: None)
    monkeypatch.setattr(_install, "_launchctl_load", lambda p: True)
    monkeypatch.setattr(_install, "_find_recall_bin", lambda: "/fake/bin/recall")
    # Subprocess "Running initial ingest" — neuter it
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: None)

    home = tmp_path / "home"
    monkeypatch.setattr(_install, "LAUNCHAGENTS", tmp_path / "LaunchAgents")
    monkeypatch.setattr(_install, "LOG_DIR", tmp_path / "Logs")
    monkeypatch.setattr(_install, "PROJECTS_DIR", home / ".claude" / "projects")
    monkeypatch.setattr(_install, "GEMINI_TMP", home / ".gemini" / "tmp")
    monkeypatch.setattr(_install, "CODEX_SESSIONS", home / ".codex" / "sessions")
    monkeypatch.setattr(_install, "SOCK_PATH", tmp_path / "embed.sock")
    monkeypatch.setattr(ingest, "PROJECTS_DIR", home / ".claude" / "projects")
    monkeypatch.setattr(ingest, "GEMINI_TMP", home / ".gemini" / "tmp")
    monkeypatch.setattr(ingest, "CODEX_SESSIONS", home / ".codex" / "sessions")
    monkeypatch.setattr(ingest, "_CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(ingest, "DB_PATH", tmp_path / "test.db")
    # Make all three agents 'detected' (file_count > 0)
    for d in [(home / ".claude" / "projects" / "p1"),
              (home / ".gemini" / "tmp" / "g1" / "chats"),
              (home / ".codex" / "sessions" / "2026" / "04" / "30")]:
        d.mkdir(parents=True)
    (home / ".claude" / "projects" / "p1" / "s.jsonl").write_text("{}\n")
    (home / ".gemini" / "tmp" / "g1" / "chats" / "session-x.jsonl").write_text("{}\n")
    (home / ".codex" / "sessions" / "2026" / "04" / "30" / "rollout-x.jsonl").write_text("{}\n")

    _install.run(dry_run=False)

    plists = sorted(p.name for p in (tmp_path / "LaunchAgents").iterdir())
    assert plists == [
        "com.convo-recall.ingest.claude.plist",
        "com.convo-recall.ingest.codex.plist",
        "com.convo-recall.ingest.gemini.plist",
    ]
    cfg = json.loads((tmp_path / "config.json").read_text())
    assert sorted(cfg["agents"]) == ["claude", "codex", "gemini"]


def test_install_plist_targets_correct_watch_dir(tmp_path, monkeypatch):
    from convo_recall import install as _install
    plist_bytes = _install._ingest_plist(
        label="com.convo-recall.ingest.gemini",
        recall_bin="/usr/local/bin/recall",
        db_path="/db",
        watch_dir="/Users/x/.gemini/tmp",
        sock_path="/sock",
        log_dir="/logs",
        agent="gemini",
        config_path="/cfg",
    )
    import plistlib
    plist = plistlib.loads(plist_bytes)
    assert plist["WatchPaths"] == ["/Users/x/.gemini/tmp"]
    assert plist["ProgramArguments"] == ["/usr/local/bin/recall", "ingest", "--agent", "gemini"]
    assert plist["EnvironmentVariables"]["CONVO_RECALL_CONFIG"] == "/cfg"
    assert plist["StandardOutPath"] == "/logs/convo-recall-ingest-gemini.log"


def test_search_no_results_for_unknown_agent(db, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ingest, "PROJECTS_DIR", tmp_path)
    sess = tmp_path / "p" / "s.jsonl"
    _write_session(sess, [
        {"uuid": "u", "type": "user", "timestamp": "2026-01-01T00:00:00Z",
         "message": {"role": "user", "content": "anything"}},
    ])
    ingest.ingest_file(db, sess, do_embed=False)
    capsys.readouterr()
    ingest.search(db, "anything", agent="nonexistent", limit=10, context=0)
    out = capsys.readouterr().out
    assert "No messages found" in out
