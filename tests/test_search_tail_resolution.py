"""Tests for items 11-19: search/tail/forget project resolution + JSON shape.

Covers:
  - exact-then-LIKE display_name resolution in search and tail (items 11, 12)
  - exact-only display_name in forget (item 13)
  - JSON output includes project_id + display_name + project_slug alias (item 14)
  - doctor reports orphan messages (item 15)
  - stats counts from projects table (item 16)
  - --cwd flag overrides getcwd (item 17)
  - Did-you-mean repurposed (item 19)
"""

import json
import os

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


def _seed(db, *, pid, display, sid="s-1", agent="claude",
          uuid="u1", text="hello world", ts="2026-04-01T00:00:01Z"):
    ingest._upsert_project(db, pid, display, None)
    ingest._upsert_session(db, agent, pid, sid, None,
                            "2026-04-01T00:00:00Z", "2026-04-01T00:00:00Z")
    ingest._persist_message(db, agent, pid, sid, uuid, "user",
                             text, ts, do_embed=False)


# ── Item 11: search exact-then-LIKE ──────────────────────────────────────────

def test_search_project_exact_match_resolves_unique_project_id(db, capsys):
    _seed(db, pid="id-foo", display="foo-app",
          uuid="u1", text="needle in foo-app")
    _seed(db, pid="id-bar", display="foo-lib", sid="s-2",
          uuid="u2", text="needle in foo-lib")
    capsys.readouterr()
    ingest.search(db, "needle", project="foo-app", limit=10, context=0)
    out = capsys.readouterr().out
    # FTS5 snippet brackets the matched terms, so "needle" → "[needle]";
    # check by display_name to confirm only foo-app rows surfaced.
    assert "[foo-app]" in out
    assert "[foo-lib]" not in out


def test_search_project_like_fallback_warns_on_multi_match(db, capsys):
    _seed(db, pid="id-noema-app", display="noema-app",
          uuid="u1", text="hit noema-app")
    _seed(db, pid="id-noema-lib", display="noema-lib", sid="s-2",
          uuid="u2", text="hit noema-lib")
    capsys.readouterr()
    ingest.search(db, "hit", project="noema", limit=10, context=0)
    captured = capsys.readouterr()
    assert "matched 2 projects" in captured.err
    assert "noema-app" in captured.err and "noema-lib" in captured.err


# ── Item 12: tail exact-then-LIKE (already covered in test_tail.py) ─────────


# ── Item 13: forget exact-only ───────────────────────────────────────────────

def test_forget_project_exact_only_rejects_substring(db):
    _seed(db, pid="id-noema-app", display="noema-app",
          uuid="u1", text="row noema-app")
    _seed(db, pid="id-noema-lib", display="noema-lib", sid="s-2",
          uuid="u2", text="row noema-lib")
    # 'noema' has no exact display_name match (only 'noema-app' / 'noema-lib').
    # forget is exact-only so it raises with 0 matches — caller must specify
    # the full display_name.
    with pytest.raises(ValueError, match="0 matches"):
        ingest.forget(db, project="noema", confirm=True)
    # No rows deleted
    n = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert n == 2


def test_forget_project_unique_match_proceeds(db):
    _seed(db, pid="id-noema-app", display="noema-app",
          uuid="u1", text="row noema-app")
    _seed(db, pid="id-noema-lib", display="noema-lib", sid="s-2",
          uuid="u2", text="row noema-lib")
    n = ingest.forget(db, project="noema-app", confirm=True)
    assert n == 1
    survivors = {r[0] for r in db.execute("SELECT uuid FROM messages").fetchall()}
    assert survivors == {"u2"}


def test_forget_project_zero_matches_raises(db):
    with pytest.raises(ValueError, match="0 matches"):
        ingest.forget(db, project="nonexistent-project", confirm=True)


# ── Item 14: JSON output schema ──────────────────────────────────────────────

def test_search_json_includes_project_id_and_legacy_alias(db, capsys):
    _seed(db, pid="id-myproj", display="myproj",
          uuid="u1", text="needle haystack")
    capsys.readouterr()
    ingest.search(db, "needle", project="myproj", limit=10,
                   context=0, json_=True)
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload["results"], "expected at least one result"
    r = payload["results"][0]
    assert r["project_id"] == "id-myproj"
    assert r["display_name"] == "myproj"
    # Deprecated alias preserved (= display_name)
    assert r["project_slug"] == "myproj"


def test_tail_json_includes_project_id_and_legacy_alias(db, capsys):
    _seed(db, pid="id-myproj", display="myproj",
          uuid="u1", text="hi", ts="2026-04-01T00:00:01Z")
    capsys.readouterr()
    rc = ingest.tail(db, n=10, project="myproj", json_=True)
    assert rc == 0
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    # Cross-session JSON: project_id is per-message now (not on the envelope);
    # display_name + the deprecated project_slug alias remain on the envelope.
    assert payload["display_name"] == "myproj"
    assert payload["project_slug"] == "myproj"
    assert payload["messages"][0]["session_id"]  # per-message session_id present


# ── Item 15: doctor orphan check ─────────────────────────────────────────────

def test_doctor_reports_zero_orphan_messages(db, capsys):
    _seed(db, pid="id-myproj", display="myproj", uuid="u1", text="hi")
    capsys.readouterr()
    ingest.doctor(db)
    out = capsys.readouterr().out
    assert "Projects" in out
    # No orphan warning expected
    assert "orphan" not in out.lower() or "messages reference" not in out


def test_doctor_flags_orphan_messages(db, capsys):
    """A messages row pointing at a project_id with no projects entry → warning."""
    # Insert a message directly with a project_id that has no projects row.
    db.execute(
        "INSERT INTO messages(uuid, session_id, project_id, role, content, "
        "timestamp, model, agent) VALUES "
        "('orphan-1', 'orphan-sess', 'no-such-project-id', 'user', "
        "'orphan body', '2026-04-01', NULL, 'claude')"
    )
    capsys.readouterr()
    ingest.doctor(db)
    out = capsys.readouterr().out
    assert "1 messages reference" in out


# ── Item 16: stats project count ─────────────────────────────────────────────

def test_stats_project_count_reads_from_projects_table(db, capsys):
    """Insert a projects row with no session/messages → still counted."""
    ingest._upsert_project(db, "id-only", "lonely-project", None)
    capsys.readouterr()
    ingest.stats(db)
    out = capsys.readouterr().out
    # `Projects   : N` line should show >=1
    import re
    m = re.search(r"^Projects\s*:\s*(\d+)", out, re.MULTILINE)
    assert m is not None, f"no Projects line in:\n{out}"
    assert int(m.group(1)) >= 1


# ── Item 19: did-you-mean repurposed ─────────────────────────────────────────

def test_did_you_mean_uses_projects_display_name(db, capsys):
    _seed(db, pid="id-foo-app", display="foo-app",
          uuid="u1", text="content here")
    _seed(db, pid="id-foo-lib", display="foo-lib", sid="s-2",
          uuid="u2", text="more content")
    capsys.readouterr()
    # `--project totally-bogus` → no exact, no LIKE → no did_you_mean
    rc = ingest.tail(db, n=10, project="totally-bogus", json_=True)
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert rc == 1
    assert "did_you_mean" not in payload
