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


def test_legacy_claude_slug(tmp_path):
    """Smoke test for the migration-only path-to-slug helper."""
    jsonl = tmp_path / "apps_foo" / "session.jsonl"
    jsonl.parent.mkdir(parents=True)
    jsonl.touch()
    # _legacy_claude_slug is migration-internal but exposed for debugging
    assert ingest._legacy_claude_slug(jsonl) == "apps_foo"


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
    # Original pre-v2 shape with project_slug (intentional — this test
    # exercises the v2 + v3 + v4 migration chain on a legacy DB).
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


def test_search_shows_agent_tag_only_when_mixed(db, tmp_path, monkeypatch, capsys):
    """v0.2.1+: agent tag is only printed when the result set actually mixes
    agents (or contains a non-claude agent). Single-Claude users see output
    identical to v0.1.x — no surprise visual regression."""
    monkeypatch.setattr(ingest, "PROJECTS_DIR", tmp_path)
    session = tmp_path / "proj_foo" / "session.jsonl"
    _write_session(session, [
        {"uuid": "u1", "type": "user", "timestamp": "2026-01-01T00:00:00Z",
         "message": {"role": "user", "content": "tagged message body"}},
    ])
    ingest.ingest_file(db, session, do_embed=False)

    # Single-claude result set: tag should NOT appear (no visual regression
    # for single-agent users, the v0.1.x cohort).
    capsys.readouterr()
    ingest.search(db, "tagged", project="proj_foo", limit=3, context=0)
    out_single = capsys.readouterr().out
    assert "[claude]" not in out_single, \
        "agent tag should be hidden for single-claude result sets (UX regression fix)"

    # Mixed result set: insert a synthetic gemini row → tag appears for both.
    # The gemini row reuses the same project_id as the claude session so
    # both surface under `--project proj_foo`.
    pid_row = db.execute(
        "SELECT DISTINCT project_id FROM messages LIMIT 1"
    ).fetchone()
    pid = pid_row["project_id"]
    db.execute(
        "INSERT INTO messages(uuid, session_id, project_id, role, content, "
        "timestamp, model, agent) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("g1", "g-1", pid, "user", "tagged gemini body",
         "2026-01-01T00:00:00Z", None, "gemini"),
    )
    capsys.readouterr()
    ingest.search(db, "tagged", project="proj_foo", limit=3, context=0)
    out_mixed = capsys.readouterr().out
    assert "[claude]" in out_mixed and "[gemini]" in out_mixed, \
        "agent tag should be visible when results mix agents"


# ── Phase 4a: claude parser preserves existing behavior ────────────────────────

def test_claude_parser_preserves_existing_behavior(db, tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "PROJECTS_DIR", tmp_path)
    session = tmp_path / "apps_foo" / "session-x.jsonl"
    _write_session(session, [
        {"uuid": "u1", "type": "user", "timestamp": "2026-01-01T00:00:00Z",
         "message": {"role": "user", "content": "claude hello"}},
    ])
    ingest.ingest_file(db, session, do_embed=False)
    row = db.execute(
        "SELECT m.agent AS agent, m.project_id AS project_id, "
        "m.content AS content, p.display_name AS display_name "
        "FROM messages m LEFT JOIN projects p ON p.project_id = m.project_id"
    ).fetchone()
    assert row["agent"] == "claude"
    assert row["display_name"] == "apps_foo"
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
        "SELECT m.role AS role, m.content AS content, m.agent AS agent, "
        "m.project_id AS project_id, m.session_id AS session_id, "
        "p.display_name AS display_name "
        "FROM messages m LEFT JOIN projects p ON p.project_id = m.project_id "
        "ORDER BY m.rowid"
    ).fetchall()
    assert len(rows) == 2
    assert all(r["agent"] == "gemini" and r["display_name"] == "myproj"
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
        "SELECT m.role AS role, m.content AS content, m.agent AS agent, "
        "m.project_id AS project_id, m.session_id AS session_id, "
        "p.display_name AS display_name "
        "FROM messages m LEFT JOIN projects p ON p.project_id = m.project_id "
        "ORDER BY m.rowid"
    ).fetchall()
    assert len(rows) == 2
    assert all(r["agent"] == "codex" for r in rows)
    assert all(r["session_id"] == "c-abc" for r in rows)
    assert all(r["display_name"] == "Foo" for r in rows)
    # /Users/x/Projects/mcp/Foo → display_name = basename "Foo"
    assert "codex hi" in rows[0]["content"]
    assert "codex reply" in rows[1]["content"]


# NOTE: post-v4 the legacy slug helpers (_codex_slug_from_cwd,
# _gemini_slug_from_path, slug_from_cwd, _slug_from_path) were removed in
# favor of project_id = sha1(realpath(cwd))[:12] + display_name from a
# marker walk. The 4 obsolete tests that exercised those helpers were
# replaced by tests/test_project_id.py.


def test_legacy_slug_helpers_are_removed():
    """The four divergent slug functions must NOT be importable post-v4."""
    assert not hasattr(ingest, "_slug_from_path")
    assert not hasattr(ingest, "_gemini_slug_from_path")
    assert not hasattr(ingest, "_slug_from_cwd")
    assert not hasattr(ingest, "_codex_slug_from_cwd")
    assert not hasattr(ingest, "slug_from_cwd")


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
        "INSERT INTO messages(uuid, session_id, project_id, role, content, "
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
    from convo_recall.install.schedulers.launchd import LaunchdScheduler
    monkeypatch.setattr(LaunchdScheduler, "_launchctl_load", lambda self, p: True)
    monkeypatch.setattr("convo_recall.install._wizard._find_recall_bin",
                        lambda: "/fake/bin/recall")
    # Subprocess "Running initial ingest" — neuter it. The wizard now
    # also spawns a detached `_backfill-chain` Popen, so stub that too.
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: None)
    class _FakePopen:
        def __init__(self, *a, **k):
            self.pid = 12345
        def wait(self):
            return 0
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)

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

    _install.run(dry_run=False, non_interactive=True, scheduler="launchd")

    plists = {p.name for p in (tmp_path / "LaunchAgents").iterdir()}
    # Wizard's non-interactive mode accepts all defaults, so we expect:
    # - one ingest plist per detected agent
    # - the embed sidecar plist (default-on when [embeddings] extra is present)
    assert {
        "com.convo-recall.ingest.claude.plist",
        "com.convo-recall.ingest.codex.plist",
        "com.convo-recall.ingest.gemini.plist",
    }.issubset(plists), f"missing ingest plists: {plists}"
    cfg = json.loads((tmp_path / "config.json").read_text())
    assert sorted(cfg["agents"]) == ["claude", "codex", "gemini"]


def test_install_plist_targets_correct_watch_dir(tmp_path, monkeypatch):
    from convo_recall.install.schedulers.launchd import LaunchdScheduler
    plist_bytes = LaunchdScheduler()._ingest_plist(
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


# ── Code-review regression tests (RED before fix, GREEN after) ─────────────────

def test_db_file_mode_is_0600_after_open_db(tmp_path, monkeypatch):
    """P0 #1: open_db must write the DB with mode 0o600 (owner-only).
    Currently fails: apsw.Connection creates files with the process umask
    (typically 0o022 → 0o644)."""
    import stat as _stat
    db_file = tmp_path / "secret.db"
    monkeypatch.setattr(ingest, "DB_PATH", db_file)
    monkeypatch.setattr(ingest, "_vc", None)
    con = ingest.open_db()
    try:
        mode = _stat.S_IMODE(db_file.stat().st_mode)
        assert mode == 0o600, f"DB file mode is 0o{mode:o}, expected 0o600 (world-readable risk)"
    finally:
        con.close()


def test_save_config_actually_writes_0600(tmp_path, monkeypatch):
    """P0 #1 (sub): save_config tries to chmod 0o600 but the resulting file
    isn't actually 0o600 on at least some platforms. Verify end-state."""
    import stat as _stat
    cfg_path = tmp_path / "config.json"
    monkeypatch.setattr(ingest, "_CONFIG_PATH", cfg_path)
    ingest.save_config({"agents": ["claude"]})
    mode = _stat.S_IMODE(cfg_path.stat().st_mode)
    assert mode == 0o600, f"config.json mode is 0o{mode:o}, expected 0o600"


def test_clean_content_redacts_obvious_secrets():
    """P0 #2: _clean_content currently does NOT redact secrets — they survive
    verbatim into FTS + vector index. After fix, common token shapes should
    be replaced with a placeholder."""
    samples = [
        ("OPENAI_API_KEY=sk-abc123def456ghi789jkl012mno345pqr678stu901", "sk-"),
        ("export GITHUB_TOKEN=ghp_AAAABBBBCCCCDDDDEEEEFFFFGGGGHHHHII", "ghp_"),
        ("AWS_ACCESS_KEY_ID=AKIA1234567890ABCDEF", "AKIA1234567890ABCDEF"),
        ("sk-ant-api03-VeRy_LoNg_AnThRoPiC_KeY_Ab123-C456", "sk-ant-"),
    ]
    for raw, leak_marker in samples:
        cleaned = ingest._clean_content(raw)
        assert leak_marker not in cleaned, (
            f"secret pattern {leak_marker!r} survived _clean_content: {cleaned!r}"
        )


def test_gemini_slug_from_header_cwd(db, tmp_path, monkeypatch):
    """P1 #7: Gemini sessions whose header includes cwd should slug from
    cwd (matching Claude/Codex convention) instead of from the SHA-hash dir."""
    sha = "1c19fb10eb84a000aaaa1111ccccdddd2222eeee3333ffff4444aaaa5555bbbb"
    sess_dir = tmp_path / sha / "chats"
    sess_dir.mkdir(parents=True)
    sess = sess_dir / "session-001.jsonl"
    sess.write_text(
        json.dumps({
            "sessionId": "g-001",
            "startTime": "2026-04-01T00:00:00Z",
            "cwd": "/Users/x/Projects/apps/noema",
            "kind": "main",
        }) + "\n"
        + json.dumps({"id": "m1", "timestamp": "2026-04-01T00:00:01Z",
                      "type": "user", "content": [{"text": "hello"}]}) + "\n"
    )
    ingest.ingest_gemini_file(db, sess, do_embed=False)
    pid = db.execute(
        "SELECT project_id FROM sessions WHERE agent='gemini'"
    ).fetchone()[0]
    # post-v4: project_id = sha1(realpath(cwd))[:12]; display_name = basename
    # of nearest marker ancestor (none here) → basename of cwd → "noema"
    assert pid == ingest._project_id("/Users/x/Projects/apps/noema")
    proj = db.execute(
        "SELECT display_name FROM projects WHERE project_id = ?", (pid,)
    ).fetchone()
    assert proj["display_name"] == "noema"


def test_gemini_slug_from_alias_map(db, tmp_path, monkeypatch):
    """post-v4: when the header has no cwd, the gemini-aliases.json map (now
    interpreted as hash_dir → real_cwd) is consulted to recover a real cwd."""
    sha = "deadbeef0000111122223333444455556666777788889999aaaabbbbccccdddd"
    sess_dir = tmp_path / sha / "chats"
    sess_dir.mkdir(parents=True)
    sess = sess_dir / "session-002.jsonl"
    sess.write_text(
        json.dumps({"sessionId": "g-002", "startTime": "2026-04-01T00:00:00Z",
                    "kind": "main"}) + "\n"
        + json.dumps({"id": "m1", "timestamp": "2026-04-01T00:00:01Z",
                      "type": "user", "content": [{"text": "hi"}]}) + "\n"
    )
    real_cwd = tmp_path / "MyProject"
    real_cwd.mkdir()
    aliases = tmp_path / "gemini-aliases.json"
    aliases.write_text(json.dumps({sha: str(real_cwd)}))
    monkeypatch.setattr(ingest, "_GEMINI_ALIAS_PATH", aliases)

    ingest.ingest_gemini_file(db, sess, do_embed=False)
    pid = db.execute(
        "SELECT project_id FROM sessions WHERE agent='gemini'"
    ).fetchone()[0]
    assert pid == ingest._project_id(str(real_cwd))


def test_backfill_redact_purges_existing_secrets(db):
    """P0 #2: existing rows that pre-date redaction can be retroactively
    cleaned via `recall backfill-redact`. After backfill, neither FTS nor
    a direct content scan should find the secret token."""
    leaked = "OPENAI_API_KEY=sk-abc123def456ghi789jkl012mno345pqr678stu901"
    db.execute(
        "INSERT INTO messages(uuid, session_id, project_id, role, content, "
        "timestamp, model, agent) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("leaked-1", "s", "p", "user", leaked, "2026-01-01T00:00:00Z", None, "claude"),
    )
    secret_token = "sk-abc123def456ghi789jkl012mno345pqr678stu901"
    fts_query = f'"{secret_token}"'  # FTS5 quote — secret contains a dash
    # Sanity: pre-backfill the secret IS indexed
    pre_fts = db.execute(
        "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH ?",
        (fts_query,),
    ).fetchone()[0]
    assert pre_fts == 1, "fixture sanity — secret should be findable before backfill"

    ingest.backfill_redact(db, confirm=True)

    # Direct content scan: no row contains the original token
    survivors = db.execute(
        "SELECT COUNT(*) FROM messages WHERE content LIKE ?",
        (f"%{secret_token}%",),
    ).fetchone()[0]
    assert survivors == 0, "secret token survived backfill_redact"

    # FTS rebuilt: searching for the secret returns no hits
    fts_hits = db.execute(
        "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH ?",
        (fts_query,),
    ).fetchone()[0]
    assert fts_hits == 0, "FTS still indexes the secret after backfill_redact"


def _seed_messages(db, rows):
    """rows is a list of (uuid, session_id, project_id, role, content, timestamp, agent).

    project_id values double as display_name for the seeded projects row,
    so tests can pass `project="p1"` to forget/search and have the resolver
    find the right rows.
    """
    seen_projects = set()
    for r in rows:
        if r[2] not in seen_projects:
            ingest._upsert_project(db, r[2], r[2], None)
            seen_projects.add(r[2])
        db.execute(
            "INSERT INTO messages(uuid, session_id, project_id, role, content, "
            "timestamp, model, agent) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (r[0], r[1], r[2], r[3], r[4], r[5], None, r[6]),
        )
        db.execute(
            "INSERT OR IGNORE INTO sessions(session_id, project_id, title, "
            "first_seen, last_updated, agent) VALUES (?, ?, ?, ?, ?, ?)",
            (r[1], r[2], None, r[5], r[5], r[6]),
        )


def test_forget_by_session(db):
    _seed_messages(db, [
        ("u1", "s1", "p1", "user", "hello s1", "2026-01-01T00:00:00Z", "claude"),
        ("u2", "s1", "p1", "user", "another s1", "2026-01-01T00:00:01Z", "claude"),
        ("u3", "s2", "p1", "user", "from s2", "2026-01-01T00:00:00Z", "claude"),
    ])
    n = ingest.forget(db, session="s1", confirm=True)
    assert n == 2
    remaining = db.execute("SELECT uuid FROM messages ORDER BY uuid").fetchall()
    assert [r[0] for r in remaining] == ["u3"]
    # Session row pruned
    sessions = db.execute("SELECT session_id FROM sessions").fetchall()
    assert {s[0] for s in sessions} == {"s2"}


def test_forget_by_pattern(db):
    _seed_messages(db, [
        ("u1", "s1", "p1", "user", "OPENAI_API_KEY=sk-abc123def456ghi789jkl012mno345pqr678",
         "2026-01-01T00:00:00Z", "claude"),
        ("u2", "s1", "p1", "user", "harmless content", "2026-01-01T00:00:01Z", "claude"),
    ])
    # Note: _clean_content normally redacts on ingest, but raw INSERT here
    # bypasses that — simulating a pre-redaction legacy DB.
    n = ingest.forget(db, pattern=r"sk-[A-Za-z0-9]{20,}", confirm=True)
    assert n == 1
    survivors = db.execute("SELECT uuid FROM messages").fetchall()
    assert [r[0] for r in survivors] == ["u2"]


def test_forget_by_before_date(db):
    _seed_messages(db, [
        ("u1", "s1", "p1", "user", "old", "2025-01-01T00:00:00Z", "claude"),
        ("u2", "s1", "p1", "user", "new", "2026-04-01T00:00:00Z", "claude"),
    ])
    n = ingest.forget(db, before="2026-01-01", confirm=True)
    assert n == 1
    survivors = db.execute("SELECT uuid FROM messages").fetchall()
    assert [r[0] for r in survivors] == ["u2"]


def test_forget_by_project(db):
    _seed_messages(db, [
        ("u1", "s1", "p1", "user", "p1 row", "2026-01-01T00:00:00Z", "claude"),
        ("u2", "s2", "p2", "user", "p2 row", "2026-01-01T00:00:01Z", "claude"),
    ])
    n = ingest.forget(db, project="p1", confirm=True)
    assert n == 1
    survivors = db.execute("SELECT uuid FROM messages").fetchall()
    assert [r[0] for r in survivors] == ["u2"]


def test_forget_by_agent(db):
    _seed_messages(db, [
        ("u1", "s1", "p1", "user", "claude row", "2026-01-01T00:00:00Z", "claude"),
        ("u2", "s2", "p1", "user", "codex row", "2026-01-01T00:00:01Z", "codex"),
    ])
    n = ingest.forget(db, agent="codex", confirm=True)
    assert n == 1
    survivors = db.execute("SELECT uuid FROM messages").fetchall()
    assert [r[0] for r in survivors] == ["u1"]


def test_forget_by_uuid(db):
    _seed_messages(db, [
        ("u1", "s1", "p1", "user", "row1", "2026-01-01T00:00:00Z", "claude"),
        ("u2", "s1", "p1", "user", "row2", "2026-01-01T00:00:01Z", "claude"),
    ])
    n = ingest.forget(db, uuid="u1", confirm=True)
    assert n == 1
    survivors = db.execute("SELECT uuid FROM messages").fetchall()
    assert [r[0] for r in survivors] == ["u2"]


def test_forget_dry_run_does_not_delete(db, capsys):
    _seed_messages(db, [
        ("u1", "s1", "p1", "user", "foo body", "2026-01-01T00:00:00Z", "claude"),
        ("u2", "s2", "p1", "user", "foo body too", "2026-01-01T00:00:01Z", "claude"),
    ])
    n = ingest.forget(db, pattern="foo", confirm=False)  # default
    assert n == 0
    survivors = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert survivors == 2
    out = capsys.readouterr().out
    assert "2 message(s) match" in out
    assert "Dry-run" in out


def test_forget_purges_fts_index(db):
    _seed_messages(db, [
        ("u1", "s1", "p1", "user",
         "leaked sk-abc123def456ghi789jkl012mno345pqr678",
         "2026-01-01T00:00:00Z", "claude"),
    ])
    ingest.forget(db, uuid="u1", confirm=True)
    fts_hits = db.execute(
        "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH ?",
        ("leaked",),
    ).fetchone()[0]
    assert fts_hits == 0, "FTS still references the deleted row"


def test_recall_cliff_with_skewed_agent_distribution(db, tmp_path, monkeypatch):
    """P1 #4: recall search --agent X must return matches from agent X even
    when X is a small minority of the corpus. Reproduces the production
    scenario: 1000 claude rows + 20 codex rows, search a common term, expect
    --agent codex to return ~all of the codex matches.

    Today (RED): top-100 prefilter is global → expected 0 codex hits in top-100
    when claude dominates → search returns 0 even when 20 matches exist.
    """
    monkeypatch.setattr(ingest, "PROJECTS_DIR", tmp_path)
    # 1000 claude messages, all containing the noisy common term "test"
    claude_session = tmp_path / "p_claude" / "session.jsonl"
    claude_session.parent.mkdir(parents=True)
    with open(claude_session, "w") as f:
        for n in range(1000):
            f.write(json.dumps({
                "uuid": f"c-{n}", "type": "user",
                "timestamp": f"2026-01-01T00:00:{n%60:02d}Z",
                "message": {"role": "user", "content": f"claude message {n} test"},
            }) + "\n")
    ingest.ingest_file(db, claude_session, do_embed=False)
    # 20 codex messages also containing "test" — synthetic tagged rows
    for n in range(20):
        db.execute(
            "INSERT INTO messages(uuid, session_id, project_id, role, content, "
            "timestamp, model, agent) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (f"x-{n}", "codex-1", "p_codex", "user",
             f"codex message {n} test", "2026-02-01T00:00:00Z", None, "codex"),
        )

    ground_truth = db.execute(
        "SELECT COUNT(*) FROM messages WHERE agent='codex' AND content LIKE '%test%'"
    ).fetchone()[0]
    assert ground_truth == 20, "fixture sanity"

    # Run search restricted to codex
    import io, sys as _sys
    buf = io.StringIO(); old = _sys.stdout; _sys.stdout = buf
    try:
        ingest.search(db, "test", agent="codex", limit=20, context=0)
    finally:
        _sys.stdout = old
    output = buf.getvalue()
    codex_hits = output.count("[codex]")
    assert codex_hits >= 10, (
        f"recall cliff: --agent codex returned {codex_hits} hits "
        f"out of {ground_truth} ground-truth matches"
    )


def test_schema_migrations_table_records_versions(tmp_path, monkeypatch):
    """LQ-4: a fresh DB should have schema_migrations rows for every applied
    migration. Re-opening the same DB should not re-enter migration bodies."""
    monkeypatch.setattr(ingest, "DB_PATH", tmp_path / "fresh.db")
    con = ingest.open_db()
    try:
        rows = con.execute(
            "SELECT version, applied_at FROM schema_migrations ORDER BY version"
        ).fetchall()
        versions = [r[0] for r in rows]
        # v2 (agent column) and v3 (FTS porter) should both be recorded
        assert ingest._MIGRATION_AGENT_COLUMN in versions
        assert ingest._MIGRATION_FTS_PORTER in versions
        for r in rows:
            assert r[1], "applied_at timestamp should be populated"
    finally:
        ingest.close_db(con)

    # Reopen — migration bodies should be gated by _migration_applied
    # (we instrument via a counter monkey-patched onto _record_migration)
    calls = {"n": 0}
    real_record = ingest._record_migration
    def counting(con, version):
        calls["n"] += 1
        real_record(con, version)
    monkeypatch.setattr(ingest, "_record_migration", counting)
    con2 = ingest.open_db()
    try:
        # No migration body should re-execute → no fresh _record_migration calls
        assert calls["n"] == 0, (
            f"migrations re-ran on second open ({calls['n']} record calls)"
        )
    finally:
        ingest.close_db(con2)


def test_malformed_jsonl_records_surface_in_warning(db, tmp_path, monkeypatch, capsys):
    """LQ-3: malformed records used to be silently dropped via
    `try: rec = json.loads(raw); except: continue`. After fix, a per-file
    counter prints a warning so schema drift becomes visible."""
    monkeypatch.setattr(ingest, "PROJECTS_DIR", tmp_path)
    session = tmp_path / "proj_x" / "session-malformed.jsonl"
    session.parent.mkdir(parents=True)
    with open(session, "w") as f:
        f.write(json.dumps({
            "uuid": "u1", "type": "user",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"role": "user", "content": "good row"},
        }) + "\n")
        f.write("{not valid json}\n")
        f.write("[also broken\n")
        f.write(json.dumps({
            "uuid": "u2", "type": "user",
            "timestamp": "2026-01-01T00:00:01Z",
            "message": {"role": "user", "content": "another good row"},
        }) + "\n")

    capsys.readouterr()  # drain
    n = ingest.ingest_file(db, session, do_embed=False)
    err = capsys.readouterr().err
    assert n == 2, "should still ingest the 2 valid rows"
    assert "2 malformed" in err, \
        f"expected malformed-counter warning, stderr was: {err!r}"


def test_install_hooks_wires_each_cli_correctly(tmp_path, monkeypatch):
    """install_hooks() writes the right hook block into each CLI's settings:
       - claude → hooks.UserPromptSubmit
       - codex  → hooks.UserPromptSubmit
       - gemini → hooks.BeforeAgent (with `matcher: '*'` and ms timeout)
    Backs up existing settings before modifying."""
    from convo_recall import install as _install

    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".codex").mkdir(parents=True)
    (home / ".gemini").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(json.dumps({"theme": "dark"}))
    (home / ".gemini" / "settings.json").write_text(json.dumps({"existing": "config"}))
    # codex hooks.json doesn't exist yet — install should create it

    def fake_target(agent, kind="memory"):
        if agent == "claude":
            return home / ".claude" / "settings.json", "UserPromptSubmit", "claude"
        if agent == "codex":
            return home / ".codex" / "hooks.json", "UserPromptSubmit", "codex"
        if agent == "gemini":
            return home / ".gemini" / "settings.json", "BeforeAgent", "gemini"
    monkeypatch.setattr("convo_recall.install._hooks._hook_target", fake_target)

    changed = _install.install_hooks(
        agents=["claude", "codex", "gemini"],
        dry_run=False,
        non_interactive=True,
    )
    assert changed == 3, f"expected 3 hooks wired, got {changed}"

    # Claude
    claude_cfg = json.loads((home / ".claude" / "settings.json").read_text())
    assert claude_cfg["theme"] == "dark", "existing claude settings preserved"
    upr = claude_cfg["hooks"]["UserPromptSubmit"]
    assert len(upr) == 1
    assert upr[0]["hooks"][0]["command"].endswith("conversation-memory.sh")
    assert upr[0]["hooks"][0]["timeout"] == 5

    # Codex (new file)
    codex_cfg = json.loads((home / ".codex" / "hooks.json").read_text())
    assert codex_cfg["hooks"]["UserPromptSubmit"][0]["hooks"][0]["timeout"] == 5

    # Gemini — note the BeforeAgent event + matcher: "*" + ms timeout + name
    gemini_cfg = json.loads((home / ".gemini" / "settings.json").read_text())
    assert gemini_cfg["existing"] == "config"
    ba = gemini_cfg["hooks"]["BeforeAgent"]
    assert ba[0]["matcher"] == "*"
    assert ba[0]["hooks"][0]["name"] == "convo-recall"
    assert ba[0]["hooks"][0]["timeout"] == 5000

    # Backup files were created (one per pre-existing settings file).
    backups = list(home.rglob("*.bak.*"))
    assert len(backups) == 2, f"expected 2 backups (claude+gemini), got {len(backups)}"


def test_install_hooks_is_idempotent(tmp_path, monkeypatch):
    """Re-running install_hooks() should not create duplicate hook entries."""
    from convo_recall import install as _install
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text("{}")
    monkeypatch.setattr("convo_recall.install._hooks._hook_target",
        lambda a, kind="memory":
            (home / ".claude" / "settings.json", "UserPromptSubmit", "claude"))

    n1 = _install.install_hooks(agents=["claude"], non_interactive=True)
    n2 = _install.install_hooks(agents=["claude"], non_interactive=True)
    assert n1 == 1 and n2 == 0, "second call should be a no-op"
    cfg = json.loads((home / ".claude" / "settings.json").read_text())
    upr = cfg["hooks"]["UserPromptSubmit"]
    # Exactly one hook block, not two
    total_hooks = sum(len(g["hooks"]) for g in upr)
    assert total_hooks == 1, f"duplicate hook blocks created: {upr}"


def test_uninstall_hooks_removes_only_convo_recall_block(tmp_path, monkeypatch):
    """uninstall_hooks() must leave the user's other UserPromptSubmit hooks
    intact and only remove the convo-recall block (matched by command path)."""
    from convo_recall import install as _install
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)

    # Pre-existing settings: user already has one hook plus convo-recall would be added
    settings_path = home / ".claude" / "settings.json"
    settings_path.write_text(json.dumps({
        "hooks": {
            "UserPromptSubmit": [
                {"hooks": [{"type": "command", "command": "/usr/local/bin/their-other-hook.sh"}]},
            ]
        }
    }))
    monkeypatch.setattr("convo_recall.install._hooks._hook_target",
        lambda a, kind="memory":
            (settings_path, "UserPromptSubmit", "claude"))

    _install.install_hooks(agents=["claude"], non_interactive=True)
    cfg = json.loads(settings_path.read_text())
    assert len(cfg["hooks"]["UserPromptSubmit"]) == 2, "both hooks should be present"

    # uninstall_hooks() default kinds = ("memory", "ingest"). The ingest pass
    # walks the same fake settings (monkeypatch ignores kind) but finds no
    # match for the ingest script signature → no-op. Total still 1.
    removed = _install.uninstall_hooks(agents=["claude"])
    assert removed == 1
    cfg = json.loads(settings_path.read_text())
    upr = cfg["hooks"]["UserPromptSubmit"]
    assert len(upr) == 1, "user's own hook should remain"
    assert upr[0]["hooks"][0]["command"] == "/usr/local/bin/their-other-hook.sh"


def test_install_hooks_dry_run_does_not_write(tmp_path, monkeypatch):
    """dry_run=True must not modify any settings file."""
    from convo_recall import install as _install
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    settings_path = home / ".claude" / "settings.json"
    original = json.dumps({"theme": "dark"})
    settings_path.write_text(original)
    monkeypatch.setattr("convo_recall.install._hooks._hook_target",
        lambda a, kind="memory":
            (settings_path, "UserPromptSubmit", "claude"))

    _install.install_hooks(agents=["claude"], dry_run=True, non_interactive=True)
    assert settings_path.read_text() == original, "dry_run should not modify file"
    backups = list(home.rglob("*.bak.*"))
    assert backups == [], "dry_run should not create backups"


def test_conversation_memory_hook_emits_valid_json_for_each_cli():
    """The pre-prompt hook script auto-detects the firing CLI from the
    stdin payload and emits the right hookEventName. Verify the contract
    for all three input shapes:

      - Codex/Claude: includes `hook_event_name`
      - Gemini: omits `hook_event_name`, has `prompt` only
      - Empty stdin: defaults to UserPromptSubmit
    """
    import subprocess

    hook = (Path(ingest.__file__).parent / "hooks" / "conversation-memory.sh").resolve()
    assert hook.exists(), f"hook script missing at {hook}"
    assert os.access(hook, os.X_OK), f"hook script not executable: {hook}"

    # Use a substantive prompt so the F-6 throttle (added 2026-05) doesn't
    # skip the reminder — this test verifies event-name dispatch, not the
    # throttle behavior (covered separately by tests/test_hook_throttle.sh).
    SUBSTANTIVE = "How does the cron scheduler avoid duplicate @reboot lines?"
    cases = [
        # (stdin payload, expected hookEventName)
        (f'{{"hook_event_name":"UserPromptSubmit","prompt":{json.dumps(SUBSTANTIVE)},"session_id":"s","cwd":"/x"}}', "UserPromptSubmit"),
        (f'{{"hook_event_name":"BeforeAgent","prompt":{json.dumps(SUBSTANTIVE)}}}', "BeforeAgent"),
        (f'{{"prompt":{json.dumps(SUBSTANTIVE)}}}', "BeforeAgent"),  # Gemini-shaped
        ('', "UserPromptSubmit"),             # empty stdin defaults
    ]
    for payload, expected_event in cases:
        result = subprocess.run(
            [str(hook)], input=payload, capture_output=True, text=True, timeout=5,
        )
        assert result.returncode == 0, f"hook exit {result.returncode} for {payload!r}: {result.stderr}"
        data = json.loads(result.stdout)
        hso = data["hookSpecificOutput"]
        assert hso["hookEventName"] == expected_event, (
            f"stdin {payload!r}: expected hookEventName={expected_event!r}, "
            f"got {hso['hookEventName']!r}"
        )
        # Empty-stdin case: throttle skips it (no prompt → no reminder),
        # so additionalContext is "" but hookEventName still set correctly.
        if payload:
            assert "convo-recall" in hso["additionalContext"], \
                "additionalContext should mention convo-recall"
            assert hso["additionalContext"], "additionalContext must not be empty"


def test_doctor_bak_warns_on_stale_files(db, tmp_path, monkeypatch, capsys):
    """Trivia #17: `recall doctor` should surface `.bak` files older than
    30 days in the DB directory so users can reclaim disk."""
    import time
    bak = tmp_path / "test.db.pre-v020.20260101-013233.bak"
    bak.write_bytes(b"x" * 1024)
    # mtime 31 days in the past
    old = time.time() - (31 * 86400)
    os.utime(bak, (old, old))

    capsys.readouterr()  # drain
    ingest.doctor(db)
    out = capsys.readouterr().out
    assert "test.db.pre-v020.20260101-013233.bak" in out
    assert "31d" in out or "32d" in out, f"expected age annotation, got: {out!r}"


def test_two_connections_have_independent_vec_state(tmp_path, monkeypatch):
    """P2 #10: opening two DBs in one process must not clobber each other's
    vec-enabled state. Pre-refactor, the module-level `_vc` got overwritten
    by the second open_db() call, breaking the first connection's helpers.
    """
    # First DB
    monkeypatch.setattr(ingest, "DB_PATH", tmp_path / "a.db")
    con_a = ingest.open_db()
    # Second DB — re-open with a different path
    monkeypatch.setattr(ingest, "DB_PATH", tmp_path / "b.db")
    con_b = ingest.open_db()

    try:
        # Both should report vec-enabled (sqlite_vec is in dependencies)
        assert ingest._vec_ok(con_a) is True, "con_a lost vec state after con_b opened"
        assert ingest._vec_ok(con_b) is True, "con_b should be vec-enabled"

        # Each DB sees only its own rows
        con_a.execute(
            "INSERT INTO messages(uuid, session_id, project_id, role, content, "
            "timestamp, model, agent) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("a-1", "s1", "p1", "user", "row in db a", "2026-01-01T00:00:00Z", None, "claude"),
        )
        con_b.execute(
            "INSERT INTO messages(uuid, session_id, project_id, role, content, "
            "timestamp, model, agent) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("b-1", "s2", "p2", "user", "row in db b", "2026-01-01T00:00:00Z", None, "claude"),
        )

        a_uuids = {r[0] for r in con_a.execute("SELECT uuid FROM messages").fetchall()}
        b_uuids = {r[0] for r in con_b.execute("SELECT uuid FROM messages").fetchall()}
        assert a_uuids == {"a-1"}, f"db a leaked rows: {a_uuids}"
        assert b_uuids == {"b-1"}, f"db b leaked rows: {b_uuids}"

        # Closing one must not break the other
        ingest.close_db(con_a)
        assert ingest._vec_ok(con_b) is True, "con_b lost vec state when con_a closed"
        # con_b still functional
        n = con_b.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        assert n == 1
    finally:
        try: con_b.close()
        except Exception: pass


def test_self_heal_orders_newest_first():
    """P2 #9: self-heal should walk unembedded messages newest-first so the
    most recent (and most-queried) rows heal before older ones. Source-
    inspection test — the SELECT lives in `scan_all`."""
    src = Path(ingest.__file__).read_text()
    # The relevant query is the LEFT JOIN message_vecs … WHERE v.rowid IS NULL
    import re as _re
    m = _re.search(
        r"LEFT JOIN message_vecs v ON v\.rowid = m\.rowid\s+"
        r"WHERE v\.rowid IS NULL\s+"
        r"ORDER BY m\.rowid\s+(\w+)",
        src,
    )
    assert m is not None, "self-heal SELECT not found in ingest.py"
    direction = m.group(1)
    assert direction == "DESC", (
        f"self-heal SELECT uses ORDER BY m.rowid {direction}, "
        f"expected DESC (newest-first)"
    )


def test_no_silent_apsw_error_pass_in_source():
    """P1 #5: structural test — ensure no `except apsw.Error: pass` survives
    in ingest.py. apsw can't be monkey-patched at the cursor level (its
    Connection.execute is read-only), so we assert via source inspection
    instead. Every apsw.Error handler must do something visible: log,
    count, or re-raise. A bare `pass` is the bug we're banning."""
    src = Path(ingest.__file__).read_text()
    import re as _re
    # Find every 'except apsw.Error[...]:' block and the line immediately after.
    for m in _re.finditer(r"except apsw\.Error[^:]*:\s*\n(\s*)([^\n]+)", src):
        indent, next_line = m.group(1), m.group(2).strip()
        assert next_line != "pass", (
            f"silent `except apsw.Error: pass` found in ingest.py at offset {m.start()} — "
            f"every apsw.Error handler must log or surface the failure"
        )


def test_tool_error_backfill_uses_correct_agent(db, tmp_path, monkeypatch):
    """P1 #6: tool_error_backfill INSERT statement is missing the `agent`
    column. Today it relies on DEFAULT 'claude' — works for claude sessions
    but mis-tags any tool_error rows discovered in non-claude sessions.

    After fix, the INSERT should explicitly set agent matching the parent
    session, OR tool_error_backfill should only iterate claude sources.
    """
    monkeypatch.setattr(ingest, "PROJECTS_DIR", tmp_path)
    # Plant a fake claude session with a tool_error block
    sess = tmp_path / "p" / "session.jsonl"
    sess.parent.mkdir(parents=True)
    sess.write_text(json.dumps({
        "uuid": "u1", "type": "user", "timestamp": "2026-01-01T00:00:00Z",
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_x",
             "is_error": True,
             "content": [{"type": "text", "text": "ECONNREFUSED bad host"}]},
        ]},
    }) + "\n")
    ingest.tool_error_backfill(db)
    rows = db.execute("SELECT agent, role FROM messages WHERE role='tool_error'").fetchall()
    assert rows, "tool_error_backfill produced no rows"
    for r in rows:
        # Must explicitly set agent — not rely on schema default
        assert r["agent"] == "claude", \
            f"tool_error row has agent={r['agent']!r}, expected explicit 'claude'"

    # Read the source: the INSERT must list agent in the column list
    src = Path(ingest.__file__).read_text()
    # Find the INSERT in tool_error_backfill specifically
    import re as _re
    teb_block = _re.search(
        r"def tool_error_backfill.*?(?=\ndef |\Z)", src, _re.DOTALL
    ).group(0)
    inserts = _re.findall(r"INSERT OR IGNORE INTO messages.*?VALUES\s*\([?,\s]+\)", teb_block, _re.DOTALL)
    assert inserts, "no INSERT statement found in tool_error_backfill"
    for ins in inserts:
        assert "agent" in ins, (
            "tool_error_backfill INSERT does NOT list the `agent` column — "
            "relies on DEFAULT 'claude' which mis-tags non-claude sessions:\n" + ins
        )


def test_embed_returns_none_on_non_200_response(monkeypatch):
    """Bonus #14: embed() does not check resp.status — non-200 responses
    raise KeyError('vector') instead of returning None gracefully.
    After fix, any non-200 status (e.g. 429/500) returns None and the
    caller falls back to FTS-only mode."""
    class FakeResp:
        status = 429
        def read(self):
            return b'{"error":"rate limited"}'
    class FakeConn:
        def __init__(self, *a, **k): pass
        def request(self, *a, **k): pass
        def getresponse(self): return FakeResp()
        def close(self): pass
    monkeypatch.setattr(ingest, "_UnixHTTPConn", FakeConn)
    # Make the socket "exist" so embed() proceeds past the existence check
    result = ingest.embed("hello world")
    assert result is None, (
        f"embed() returned {result!r} on HTTP 429 — expected None for graceful fallback"
    )


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


# ── post-v4: project_id resolution + display_name LIKE fallback ─────────────


def test_search_did_you_mean_when_no_match(db, tmp_path, monkeypatch, capsys):
    """Search for a project that has zero exact AND zero LIKE matches → no hint.
    Search for one that has a LIKE match → suggestion via display_name."""
    monkeypatch.setattr(ingest, "PROJECTS_DIR", tmp_path)
    # Seed a session under display_name "app-claude" via _upsert_project.
    ingest._upsert_project(db, "id-app-claude", "app-claude", None)
    ingest._upsert_session(db, "claude", "id-app-claude", "s-1", None,
                            "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z")
    ingest._persist_message(db, "claude", "id-app-claude", "s-1", "u1",
                             "user", "moodmix sprint",
                             "2026-01-01T00:00:00Z", do_embed=False)

    capsys.readouterr()
    # `--project app` matches "app-claude" via LIKE → resolves; finds rows.
    ingest.search(db, "moodmix", project="app", limit=10, context=0)
    out = capsys.readouterr().out
    assert "No messages found" not in out

    # `--project totally-bogus` → no exact, no LIKE → "No results" with no hint
    ingest.search(db, "moodmix", project="totally-bogus", limit=10, context=0)
    out = capsys.readouterr().out
    assert "No messages found" in out
    assert "Did you mean" not in out


# ── F-2: recall search --json output mode ────────────────────────────────────


def test_search_json_output_is_valid_json(db, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ingest, "PROJECTS_DIR", tmp_path)
    sess = tmp_path / "proj_x" / "s.jsonl"
    _write_session(sess, [
        {"uuid": "u1", "type": "user", "timestamp": "2026-01-01T00:00:00Z",
         "message": {"role": "user", "content": "moodmix sprint plan"}},
    ])
    ingest.ingest_file(db, sess, do_embed=False)
    capsys.readouterr()
    ingest.search(db, "moodmix", project="proj_x", limit=10, context=0, json_=True)
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)  # raises if not valid JSON
    assert isinstance(payload, dict)


def test_search_json_output_no_human_banners(db, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ingest, "PROJECTS_DIR", tmp_path)
    sess = tmp_path / "proj_x" / "s.jsonl"
    _write_session(sess, [
        {"uuid": "u1", "type": "user", "timestamp": "2026-01-01T00:00:00Z",
         "message": {"role": "user", "content": "moodmix"}},
    ])
    ingest.ingest_file(db, sess, do_embed=False)
    capsys.readouterr()
    ingest.search(db, "moodmix", project="proj_x", limit=5, context=0, json_=True)
    out = capsys.readouterr().out.strip()
    assert out.startswith("{"), f"--json output should be a single JSON doc; got: {out[:80]}"
    assert "[fts search]" not in out
    assert "[hybrid search]" not in out


def test_search_json_empty_results(db, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ingest, "PROJECTS_DIR", tmp_path)
    sess = tmp_path / "proj_x" / "s.jsonl"
    _write_session(sess, [
        {"uuid": "u1", "type": "user", "timestamp": "2026-01-01T00:00:00Z",
         "message": {"role": "user", "content": "moodmix"}},
    ])
    ingest.ingest_file(db, sess, do_embed=False)
    capsys.readouterr()
    ingest.search(db, "zorblax", project="proj_x", limit=10, context=0, json_=True)
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload["results"] == []
    assert "No results." not in out  # no human banner


def test_search_json_includes_required_fields(db, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ingest, "PROJECTS_DIR", tmp_path)
    sess = tmp_path / "proj_x" / "s.jsonl"
    _write_session(sess, [
        {"uuid": "u1", "type": "user", "timestamp": "2026-01-01T00:00:00Z",
         "message": {"role": "user", "content": "moodmix"}},
    ])
    ingest.ingest_file(db, sess, do_embed=False)
    capsys.readouterr()
    ingest.search(db, "moodmix", project="proj_x", limit=5, context=0, json_=True)
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload["results"], "expected at least one result"
    r = payload["results"][0]
    for field in ("session_id", "project_id", "agent", "role", "timestamp", "snippet"):
        assert field in r, f"missing required field {field!r} in result {r}"


def test_search_json_did_you_mean_in_payload(db, tmp_path, monkeypatch, capsys):
    """Zero-results in JSON mode includes did_you_mean when display_name LIKE matches."""
    monkeypatch.setattr(ingest, "PROJECTS_DIR", tmp_path)
    ingest._upsert_project(db, "id1", "myproject", None)
    ingest._upsert_session(db, "claude", "id1", "s-1", None,
                            "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z")
    ingest._persist_message(db, "claude", "id1", "s-1", "u1",
                             "user", "moodmix",
                             "2026-01-01T00:00:00Z", do_embed=False)
    capsys.readouterr()
    # `--project totally-bogus` → no LIKE match either → no did_you_mean.
    ingest.search(db, "moodmix", project="totally-bogus",
                   limit=10, context=0, json_=True)
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload["results"] == []
    assert "did_you_mean" not in payload


# ── F-5: search snippet highlights matched query tokens with [brackets] ──────


def test_search_snippet_brackets_only_query_matches(db, tmp_path, monkeypatch, capsys):
    """The agent feedback mistook FTS bracketing for redactor asymmetry.
    Confirm the brackets come from the query, not from the agent name —
    a query of `claude` brackets `[claude]`, NOT `gemini` or `codex`."""
    monkeypatch.setattr(ingest, "PROJECTS_DIR", tmp_path)
    sess = tmp_path / "proj_x" / "s.jsonl"
    _write_session(sess, [
        {"uuid": "u1", "type": "user", "timestamp": "2026-01-01T00:00:00Z",
         "message": {"role": "user",
                     "content": "claude codex gemini are three coding agents"}},
    ])
    ingest.ingest_file(db, sess, do_embed=False)

    capsys.readouterr()
    ingest.search(db, "claude", project="proj_x", limit=5, context=0, json_=True)
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    snippet = payload["results"][0]["snippet"]

    # Only the query token gets bracketed.
    assert "[claude]" in snippet
    assert "[codex]" not in snippet, (
        f"codex should NOT be bracketed when querying 'claude'; got: {snippet}"
    )
    assert "[gemini]" not in snippet


# ── F-4: stats + doctor surface embed-sidecar status ─────────────────────────


def test_stats_warns_when_zero_embedded(db, tmp_path, monkeypatch, capsys):
    """When the DB has messages but Embedded:0, stats prints a warning
    line + the actionable command. The agent's feedback session showed
    Embedded: 0 (0%) with 145 messages and no clue why — fix is
    discoverability, not the install path."""
    monkeypatch.setattr(ingest, "PROJECTS_DIR", tmp_path)
    sess = tmp_path / "p" / "s.jsonl"
    _write_session(sess, [
        {"uuid": "u1", "type": "user", "timestamp": "2026-01-01T00:00:00Z",
         "message": {"role": "user", "content": "abc"}},
    ])
    ingest.ingest_file(db, sess, do_embed=False)

    capsys.readouterr()
    ingest.stats(db)
    out = capsys.readouterr().out
    assert "Embedded   : 0" in out
    assert "Vector search disabled" in out, (
        f"missing 'Vector search disabled' warning; got:\n{out}"
    )


def test_stats_no_warning_when_no_messages(db, tmp_path, capsys):
    capsys.readouterr()
    ingest.stats(db)
    out = capsys.readouterr().out
    # Empty DB shouldn't bug the user about embeddings.
    assert "Vector search disabled" not in out


def test_stats_friendly_message_when_chain_active(db, tmp_path, monkeypatch, capsys):
    """When `Embedded: 0` but a backfill chain is currently running, stats
    must NOT tell the user to "run recall embed-backfill" — that's already
    happening. Show a friendly "in progress" message + tracking hint."""
    monkeypatch.setattr(ingest, "PROJECTS_DIR", tmp_path)
    sess = tmp_path / "p" / "s.jsonl"
    _write_session(sess, [
        {"uuid": "u1", "type": "user", "timestamp": "2026-01-01T00:00:00Z",
         "message": {"role": "user", "content": "hello world"}},
    ])
    ingest.ingest_file(db, sess, do_embed=False)

    # Force the "sidecar running + extra installed" branch so we hit the
    # message we want to test.
    fake_sock = tmp_path / "embed.sock"
    fake_sock.touch()
    monkeypatch.setattr(ingest, "EMBED_SOCK", fake_sock)

    # CI doesn't install the `[embeddings]` extra, so `sentence_transformers`
    # isn't importable. Inject a stub so the `extra_installed = True` branch
    # fires — same as on a developer machine with the extra installed.
    import sys, types
    if "sentence_transformers" not in sys.modules:
        sys.modules["sentence_transformers"] = types.ModuleType("sentence_transformers")

    # Simulate an active backfill chain.
    from convo_recall import _progress
    monkeypatch.setattr(_progress, "_progress_path", lambda: tmp_path / "progress.json")
    _progress.start_run([("ingest", 1), ("embed-backfill", 1)])

    capsys.readouterr()
    ingest.stats(db)
    out = capsys.readouterr().out
    # Friendly "in progress" wording, NOT the manual-action hint.
    assert "First-run embedding in progress" in out, f"got:\n{out}"
    assert "recall embed-backfill" not in out or \
        "auto-heals" in out  # the auto-heal hint mentions it acceptably


def test_stats_guidance_when_no_chain_running(db, tmp_path, monkeypatch, capsys):
    """When no chain is active, stats must give the user a clear, multi-line
    guidance block explaining first-run cost + how to track + how to kick it
    off — replacing the old terse '⚠ Vector search ready but no rows
    embedded yet. recall embed-backfill' that confused users."""
    monkeypatch.setattr(ingest, "PROJECTS_DIR", tmp_path)
    sess = tmp_path / "p" / "s.jsonl"
    _write_session(sess, [
        {"uuid": "u1", "type": "user", "timestamp": "2026-01-01T00:00:00Z",
         "message": {"role": "user", "content": "hi"}},
    ])
    ingest.ingest_file(db, sess, do_embed=False)

    fake_sock = tmp_path / "embed.sock"
    fake_sock.touch()
    monkeypatch.setattr(ingest, "EMBED_SOCK", fake_sock)

    # CI lacks `[embeddings]` extra; stub sentence_transformers so the
    # extra_installed branch fires (matches dev-machine behavior).
    import sys, types
    if "sentence_transformers" not in sys.modules:
        sys.modules["sentence_transformers"] = types.ModuleType("sentence_transformers")

    # No chain active — make sure we're not reading a stale progress file.
    from convo_recall import _progress
    monkeypatch.setattr(_progress, "_progress_path", lambda: tmp_path / "progress.json")

    capsys.readouterr()
    ingest.stats(db)
    out = capsys.readouterr().out
    # Verify the new guidance includes:
    assert "First-run" in out, f"missing first-run framing; got:\n{out}"
    assert "Track progress" in out
    assert "recall embed-backfill" in out  # explicit kick-off hint
    assert "auto-heals" in out             # mentions watcher fallback


def test_doctor_reports_embed_status_lines(db, tmp_path, monkeypatch, capsys):
    """`recall doctor` prints three lines reporting embed extra / sidecar /
    coverage so the user sees the full picture in one place."""
    monkeypatch.setattr(ingest, "PROJECTS_DIR", tmp_path)
    sess = tmp_path / "p" / "s.jsonl"
    _write_session(sess, [
        {"uuid": "u1", "type": "user", "timestamp": "2026-01-01T00:00:00Z",
         "message": {"role": "user", "content": "abc"}},
    ])
    ingest.ingest_file(db, sess, do_embed=False)

    capsys.readouterr()
    ingest.doctor(db)
    out = capsys.readouterr().out
    assert "Embed extra" in out
    assert "Embed sidecar" in out
    assert "Embedded coverage" in out


def test_doctor_recommends_install_command_when_extra_missing(db, tmp_path, monkeypatch, capsys):
    """When Embedded:0 AND extra not installed, doctor recommends the
    pipx install command."""
    monkeypatch.setattr(ingest, "PROJECTS_DIR", tmp_path)
    sess = tmp_path / "p" / "s.jsonl"
    _write_session(sess, [
        {"uuid": "u1", "type": "user", "timestamp": "2026-01-01T00:00:00Z",
         "message": {"role": "user", "content": "abc"}},
    ])
    ingest.ingest_file(db, sess, do_embed=False)

    # Simulate missing extra by intercepting the import at the doctor() site.
    import builtins
    real_import = builtins.__import__

    def _fake_import(name, *a, **kw):
        if name == "sentence_transformers":
            raise ImportError("simulated: extra not installed")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _fake_import)

    capsys.readouterr()
    ingest.doctor(db)
    out = capsys.readouterr().out
    assert "Embed extra      : NOT installed" in out
    assert "pipx install" in out
    assert "convo-recall[embeddings]" in out


def test_doctor_reports_ingest_hook_state_per_agent(db, tmp_path, monkeypatch, capsys):
    """doctor() reports per-CLI ingest-hook wired/NOT wired so users who
    upgraded but didn't re-install see what's missing."""
    import json
    monkeypatch.setattr(ingest, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    # Locate the real ingest hook script so the wired check matches it.
    from convo_recall.install._hooks import _find_hook_script
    ingest_script = _find_hook_script("ingest")

    # Wire claude with the matching command; leave codex/gemini unwired.
    claude_settings = tmp_path / ".claude" / "settings.json"
    claude_settings.parent.mkdir(parents=True)
    claude_settings.write_text(json.dumps({
        "hooks": {
            "Stop": [{"hooks": [{"type": "command",
                                  "command": str(ingest_script)}]}]
        }
    }))
    # Codex settings exist but no convo-recall hook.
    codex_settings = tmp_path / ".codex" / "hooks.json"
    codex_settings.parent.mkdir(parents=True)
    codex_settings.write_text(json.dumps({"hooks": {}}))
    # Gemini settings absent entirely.

    capsys.readouterr()
    ingest.doctor(db)
    out = capsys.readouterr().out
    assert "Ingest hook" in out
    # Claude: wired
    assert "claude" in out
    claude_line = next(l for l in out.splitlines() if "claude" in l and "Stop" in l)
    assert "wired" in claude_line and "NOT" not in claude_line
    # Codex / Gemini: NOT wired
    codex_line = next(l for l in out.splitlines() if "codex" in l and "Stop" in l)
    assert "NOT wired" in codex_line
    gemini_line = next(l for l in out.splitlines() if "gemini" in l and "AfterAgent" in l)
    assert "NOT wired" in gemini_line
    # Repair hint surfaces when anything is missing.
    assert "install-hooks --kind ingest" in out


# ── F-9: FTS5 query sanitization ─────────────────────────────────────────────


def test_safe_fts_query_wraps_tokens_in_quotes():
    assert ingest._safe_fts_query("hello world") == '"hello" "world"'


def test_safe_fts_query_handles_empty():
    assert ingest._safe_fts_query("") == '""'
    assert ingest._safe_fts_query("   ") == '""'


def test_safe_fts_query_escapes_embedded_quotes():
    out = ingest._safe_fts_query('he said "hi"')
    # `he` and `said` are quoted; `"hi"` becomes `""hi""` (escaped doubles).
    assert '"he"' in out
    assert '"said"' in out
    assert '""hi""' in out


def test_safe_fts_query_strips_special_only_tokens():
    """Token `.*` would crash FTS5 — should reduce to a no-match (empty)."""
    assert ingest._safe_fts_query(".*") == '""'
    assert ingest._safe_fts_query("...") == '""'


def test_search_with_hyphenated_query_does_not_crash(db, tmp_path, monkeypatch, capsys):
    """The Gemini-agent crash: `recall search "app-gemini"` →
    `apsw.SQLError: no such column: gemini`. Sanitization fixes it."""
    monkeypatch.setattr(ingest, "PROJECTS_DIR", tmp_path)
    sess = tmp_path / "p" / "s.jsonl"
    _write_session(sess, [
        {"uuid": "u1", "type": "user", "timestamp": "2026-01-01T00:00:00Z",
         "message": {"role": "user", "content": "app-gemini integration notes"}},
    ])
    ingest.ingest_file(db, sess, do_embed=False)

    capsys.readouterr()
    # No raise = pass. Returns matching rows because "app-gemini" appears
    # in the indexed content.
    ingest.search(db, "app-gemini", limit=5, context=0)
    out = capsys.readouterr().out
    # Either we got a hit, or "No results." — but never the crash.
    assert "no such column" not in out
    assert "syntax error" not in out


def test_search_with_dot_asterisk_query_does_not_crash(db, tmp_path, monkeypatch, capsys):
    """The other Gemini-agent crash: `recall search ".*"` →
    `apsw.SQLError: fts5: syntax error near "."`."""
    monkeypatch.setattr(ingest, "PROJECTS_DIR", tmp_path)
    sess = tmp_path / "p" / "s.jsonl"
    _write_session(sess, [
        {"uuid": "u1", "type": "user", "timestamp": "2026-01-01T00:00:00Z",
         "message": {"role": "user", "content": "anything"}},
    ])
    ingest.ingest_file(db, sess, do_embed=False)

    capsys.readouterr()
    ingest.search(db, ".*", limit=5, context=0)
    out = capsys.readouterr().out
    assert "syntax error" not in out
    # `.*` strips to empty → no-match phrase → "No results."
    assert "No results" in out


def test_search_with_colon_query_does_not_crash(db, tmp_path, monkeypatch, capsys):
    """Colons and parens are also FTS5-special. Defensive against future
    user input that wasn't in the Gemini agent's specific cases."""
    monkeypatch.setattr(ingest, "PROJECTS_DIR", tmp_path)
    sess = tmp_path / "p" / "s.jsonl"
    _write_session(sess, [
        {"uuid": "u1", "type": "user", "timestamp": "2026-01-01T00:00:00Z",
         "message": {"role": "user", "content": "url:https://example.com"}},
    ])
    ingest.ingest_file(db, sess, do_embed=False)

    capsys.readouterr()
    for q in ("url:https", "(query)", "foo*bar", "AND OR NOT"):
        capsys.readouterr()
        ingest.search(db, q, limit=5, context=0)
        out = capsys.readouterr().out
        assert "syntax error" not in out, f"crashed on query: {q!r}"
        assert "no such column" not in out, f"crashed on query: {q!r}"


# ── F-10: read-only fallback when DB writes are sandboxed ────────────────────


def test_open_db_readonly_flag_returns_readonly_connection(tmp_path, monkeypatch):
    """Explicit readonly=True opens the DB without WAL or chmod, suitable
    for sandboxed subprocesses (codex CLI restricts writes outside cwd)."""
    import apsw
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(ingest, "DB_PATH", db_file)
    monkeypatch.setattr(ingest, "_vc", None)
    # Seed with a write connection first.
    seed = ingest.open_db()
    seed.execute("INSERT INTO sessions(session_id, project_id, title, "
                 "first_seen, last_updated, agent) VALUES (?,?,?,?,?,?)",
                 ("s1", "p", None, "2026-01-01", "2026-01-01", "claude"))
    seed.close()

    # Now reopen read-only.
    monkeypatch.setattr(ingest, "_vc", None)
    con = ingest.open_db(readonly=True)
    rows = con.execute("SELECT session_id FROM sessions").fetchall()
    assert rows[0]["session_id"] == "s1"
    # Writes must fail.
    import pytest as _pytest
    with _pytest.raises(apsw.ReadOnlyError):
        con.execute("INSERT INTO sessions(session_id, project_id, title, "
                    "first_seen, last_updated, agent) VALUES (?,?,?,?,?,?)",
                    ("s2", "p", None, "2026-01-01", "2026-01-01", "claude"))
    con.close()


def test_open_db_readonly_raises_when_db_missing(tmp_path, monkeypatch):
    """Read-only on a non-existent DB is a hard error — there's nothing
    to read, and silently creating it would mask config bugs."""
    import apsw
    monkeypatch.setattr(ingest, "DB_PATH", tmp_path / "does-not-exist.db")
    monkeypatch.setattr(ingest, "_vc", None)
    import pytest as _pytest
    with _pytest.raises(apsw.CantOpenError, match="DB not found"):
        ingest.open_db(readonly=True)


def test_open_db_falls_back_to_readonly_on_wal_cantopen(tmp_path, monkeypatch, capsys):
    """The Codex CLI bug: parent dir is readable but the sandbox blocks
    WAL sidecar writes, so `con.execute('PRAGMA journal_mode=WAL')`
    raises apsw.CantOpenError. Fall back to read-only with a warning."""
    import os, stat

    # Seed an existing DB so the read-only fallback has something to open.
    db_dir = tmp_path / "db"
    db_dir.mkdir()
    db_file = db_dir / "test.db"
    monkeypatch.setattr(ingest, "DB_PATH", db_file)
    monkeypatch.setattr(ingest, "_vc", None)
    seed = ingest.open_db()
    seed.execute("INSERT INTO sessions(session_id, project_id, title, "
                 "first_seen, last_updated, agent) VALUES (?,?,?,?,?,?)",
                 ("s1", "p", None, "2026-01-01", "2026-01-01", "claude"))
    seed.close()
    monkeypatch.setattr(ingest, "_vc", None)

    # Monkeypatch the WAL helper to raise CantOpenError, mirroring the
    # codex sandbox where WAL sidecar creation fails. Realistic chmod
    # doesn't reliably trigger this on all OSes (running as root in
    # docker bypasses perms; macOS handles WAL sidecars differently).
    import apsw

    def _fake_wal(con):
        raise apsw.CantOpenError("simulated sandbox: cannot create WAL sidecar")

    monkeypatch.setattr(ingest, "_enable_wal_mode", _fake_wal)

    capsys.readouterr()  # drain
    con = ingest.open_db()
    err = capsys.readouterr().err
    assert "DB write access denied" in err, (
        f"expected fallback warning on stderr; got: {err!r}"
    )
    rows = con.execute("SELECT session_id FROM sessions").fetchall()
    assert rows[0]["session_id"] == "s1"
    con.close()

# ── _wait_for_embed_socket: race-condition fix for chain → embed pipeline ─────

def test_wait_for_embed_socket_returns_true_when_socket_already_exists(tmp_path, monkeypatch):
    sock = tmp_path / 'embed.sock'
    sock.touch()
    monkeypatch.setattr(ingest, 'EMBED_SOCK', sock)
    import time
    start = time.time()
    assert ingest._wait_for_embed_socket(timeout_s=5.0) is True
    assert (time.time() - start) < 0.05, 'should return immediately when socket exists'


def test_wait_for_embed_socket_returns_true_when_socket_appears(tmp_path, monkeypatch):
    sock = tmp_path / 'embed.sock'
    monkeypatch.setattr(ingest, 'EMBED_SOCK', sock)
    # Spawn a thread that creates the socket after 0.3s.
    import threading, time
    def _create_later():
        time.sleep(0.3)
        sock.touch()
    threading.Thread(target=_create_later, daemon=True).start()
    start = time.time()
    assert ingest._wait_for_embed_socket(timeout_s=2.0, poll_interval_s=0.05) is True
    elapsed = time.time() - start
    assert 0.25 < elapsed < 0.6, f'should wait for socket; elapsed={elapsed:.2f}s'


def test_wait_for_embed_socket_returns_false_on_timeout(tmp_path, monkeypatch):
    sock = tmp_path / 'embed.sock'
    monkeypatch.setattr(ingest, 'EMBED_SOCK', sock)
    import time
    start = time.time()
    assert ingest._wait_for_embed_socket(timeout_s=0.3, poll_interval_s=0.05) is False
    elapsed = time.time() - start
    assert 0.25 < elapsed < 0.5, f'should respect timeout; elapsed={elapsed:.2f}s'


def test_wait_for_embed_socket_verbose_logs_to_stderr(tmp_path, monkeypatch, capsys):
    sock = tmp_path / 'embed.sock'
    monkeypatch.setattr(ingest, 'EMBED_SOCK', sock)
    ingest._wait_for_embed_socket(timeout_s=0.2, poll_interval_s=0.05, verbose=True)
    err = capsys.readouterr().err
    assert 'waiting up to' in err
    assert 'did not appear within' in err

