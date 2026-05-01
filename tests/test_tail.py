"""Tests for `recall tail` — last-N-messages reader (Option E formatter)."""

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone

import pytest

os.environ.setdefault("CONVO_RECALL_DB", ":memory:")

import convo_recall.ingest as ingest


@pytest.fixture()
def db(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(ingest, "DB_PATH", db_file)
    monkeypatch.setattr(ingest, "_vc", None)
    con = ingest.open_db()
    yield con
    con.close()


def _seed(con, *, agent="claude", project_slug="proj_a",
          session_id="s-1", first_seen="2026-04-01T00:00:00Z",
          last_updated="2026-04-01T00:00:00Z"):
    ingest._upsert_session(con, agent, project_slug, session_id, None,
                           first_seen, last_updated)


def _msg(con, *, uuid, session_id, role, text, ts, project_slug="proj_a",
         agent="claude"):
    ingest._persist_message(con, agent, project_slug, session_id, uuid,
                            role, text, ts, do_embed=False)


# ── core behaviour ─────────────────────────────────────────────────────────────

def test_tail_returns_chronological_oldest_first(db, capsys):
    """Bottom of output = newest message; numbering puts newest at #1."""
    _seed(db)
    _msg(db, uuid="u1", session_id="s-1", role="user",
         text="first message", ts="2026-04-01T00:00:01Z")
    _msg(db, uuid="u2", session_id="s-1", role="assistant",
         text="second message", ts="2026-04-01T00:00:02Z")
    _msg(db, uuid="u3", session_id="s-1", role="assistant",
         text="third message", ts="2026-04-01T00:00:03Z")

    rc = ingest.tail(db, n=10, project="proj_a")
    out = capsys.readouterr().out
    assert rc == 0
    i_first = out.index("first message")
    i_second = out.index("second message")
    i_third = out.index("third message")
    assert i_first < i_second < i_third


def test_tail_reverse_numbering_newest_is_one(db, capsys):
    """Newest message gets #1, oldest of the batch gets the highest number."""
    _seed(db)
    _msg(db, uuid="u1", session_id="s-1", role="user",
         text="OLDEST", ts="2026-04-01T00:00:01Z")
    _msg(db, uuid="u2", session_id="s-1", role="user",
         text="MIDDLE", ts="2026-04-01T00:00:02Z")
    _msg(db, uuid="u3", session_id="s-1", role="user",
         text="NEWEST", ts="2026-04-01T00:00:03Z")

    ingest.tail(db, n=3, project="proj_a")
    out = capsys.readouterr().out
    # OLDEST should be on a row with #3 (highest)
    oldest_line = [l for l in out.splitlines() if "OLDEST" in l][0]
    assert "#3" in oldest_line
    # NEWEST should be on a row with #1
    newest_line = [l for l in out.splitlines() if "NEWEST" in l][0]
    assert "#1" in newest_line


def test_tail_picks_latest_session_by_project(db, capsys):
    _seed(db, session_id="old", last_updated="2026-04-01T00:00:00Z")
    _seed(db, session_id="new", last_updated="2026-04-05T00:00:00Z")
    _msg(db, uuid="o1", session_id="old", role="user",
         text="OLD-MSG", ts="2026-04-01T00:00:00Z")
    _msg(db, uuid="n1", session_id="new", role="user",
         text="NEW-MSG", ts="2026-04-05T00:00:00Z")

    ingest.tail(db, n=10, project="proj_a")
    out = capsys.readouterr().out
    assert "NEW-MSG" in out
    assert "OLD-MSG" not in out
    assert "session new" in out


def test_tail_explicit_session_overrides_latest_pick(db, capsys):
    _seed(db, session_id="old", last_updated="2026-04-01T00:00:00Z")
    _seed(db, session_id="new", last_updated="2026-04-05T00:00:00Z")
    _msg(db, uuid="o1", session_id="old", role="user",
         text="OLD-MSG", ts="2026-04-01T00:00:00Z")
    _msg(db, uuid="n1", session_id="new", role="user",
         text="NEW-MSG", ts="2026-04-05T00:00:00Z")

    ingest.tail(db, n=10, session="old")
    out = capsys.readouterr().out
    assert "OLD-MSG" in out
    assert "NEW-MSG" not in out


def test_tail_n_bounds_results(db, capsys):
    _seed(db)
    for i in range(1, 11):
        _msg(db, uuid=f"u{i}", session_id="s-1", role="user",
             text=f"msg-{i:02d}", ts=f"2026-04-01T00:00:{i:02d}Z")
    ingest.tail(db, n=3, project="proj_a")
    out = capsys.readouterr().out
    assert "msg-10" in out
    assert "msg-09" in out
    assert "msg-08" in out
    assert "msg-07" not in out


def test_tail_role_filter_excludes_tool_error(db, capsys):
    _seed(db)
    _msg(db, uuid="u1", session_id="s-1", role="user",
         text="USER-MSG", ts="2026-04-01T00:00:01Z")
    _msg(db, uuid="t1", session_id="s-1", role="tool_error",
         text="TOOL-ERR", ts="2026-04-01T00:00:02Z")
    _msg(db, uuid="a1", session_id="s-1", role="assistant",
         text="ASSIST-MSG", ts="2026-04-01T00:00:03Z")

    ingest.tail(db, n=10, project="proj_a")
    out = capsys.readouterr().out
    assert "USER-MSG" in out
    assert "ASSIST-MSG" in out
    assert "TOOL-ERR" not in out

    ingest.tail(db, n=10, project="proj_a", roles=("tool_error",))
    out = capsys.readouterr().out
    assert "TOOL-ERR" in out
    assert "USER-MSG" not in out


# ── speaker labels ─────────────────────────────────────────────────────────────

def test_tail_user_role_displays_as_YOU(db, capsys):
    _seed(db)
    _msg(db, uuid="u1", session_id="s-1", role="user",
         text="hello there", ts="2026-04-01T00:00:01Z")
    ingest.tail(db, n=10, project="proj_a")
    out = capsys.readouterr().out
    assert "YOU" in out
    # The bare role name "user" should NOT appear as the speaker label.
    assert "user " not in out.replace("YOU", "")


def test_tail_assistant_displays_agent_name(db, capsys):
    _seed(db, agent="claude")
    _msg(db, uuid="a1", session_id="s-1", role="assistant",
         text="response from claude", ts="2026-04-01T00:00:01Z", agent="claude")
    ingest.tail(db, n=10, project="proj_a")
    out = capsys.readouterr().out
    assert "claude" in out
    # Generic "assistant" word should not appear as a speaker label.
    assert "assistant " not in out


def test_tail_assistant_uses_codex_when_agent_is_codex(db, capsys):
    _seed(db, agent="codex")
    _msg(db, uuid="a1", session_id="s-1", role="assistant",
         text="response from codex", ts="2026-04-01T00:00:01Z", agent="codex")
    ingest.tail(db, n=10, project="proj_a")
    out = capsys.readouterr().out
    assert "codex" in out


def test_tail_user_row_uses_heavy_bar(db, capsys):
    """User rows get ┃ (heavy bar); agent rows get │ (light bar)."""
    _seed(db)
    _msg(db, uuid="u1", session_id="s-1", role="user",
         text="from me", ts="2026-04-01T00:00:01Z")
    _msg(db, uuid="a1", session_id="s-1", role="assistant",
         text="from claude", ts="2026-04-01T00:00:02Z", agent="claude")
    ingest.tail(db, n=10, project="proj_a")
    out = capsys.readouterr().out
    user_line = [l for l in out.splitlines() if "from me" in l][0]
    agent_line = [l for l in out.splitlines() if "from claude" in l][0]
    assert "┃" in user_line
    assert "│" in agent_line
    assert "┃" not in agent_line
    assert "│" not in user_line


# ── time formatting ────────────────────────────────────────────────────────────

def test_tail_format_ago_thresholds():
    """Cover each granularity bucket with an injected `now`."""
    base = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
    # 30 seconds ago
    assert ingest._tail_format_ago("2026-05-01T11:59:30Z", now=base) == "30s ago"
    # 5 minutes ago
    assert ingest._tail_format_ago("2026-05-01T11:55:00Z", now=base) == "5m ago"
    # 3 hours ago
    assert ingest._tail_format_ago("2026-05-01T09:00:00Z", now=base) == "3h ago"
    # 2 days ago
    assert ingest._tail_format_ago("2026-04-29T12:00:00Z", now=base) == "2d ago"
    # 3 weeks ago
    assert ingest._tail_format_ago("2026-04-10T12:00:00Z", now=base) == "3w ago"
    # Exactly now / future timestamp
    assert ingest._tail_format_ago("2026-05-01T12:00:00Z", now=base) == "now"


def test_tail_ago_appears_in_output_relative_to_now(db, capsys):
    _seed(db)
    # Message ~5 minutes ago. We can't inject `now` into tail() directly,
    # so use a timestamp very close to now() at test time.
    now_utc = datetime.now(timezone.utc)
    near_ts = now_utc.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    _msg(db, uuid="u1", session_id="s-1", role="user",
         text="recent", ts=near_ts)
    ingest.tail(db, n=10, project="proj_a")
    out = capsys.readouterr().out
    # "now" or "Ns ago" — both acceptable depending on test latency
    assert ("now" in out) or ("s ago" in out)


def test_tail_header_includes_latest_ago(db, capsys):
    _seed(db)
    now_utc = datetime.now(timezone.utc)
    near_ts = now_utc.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    _msg(db, uuid="u1", session_id="s-1", role="user",
         text="hello", ts=near_ts)
    ingest.tail(db, n=10, project="proj_a")
    out = capsys.readouterr().out
    assert "latest" in out


# ── format details ────────────────────────────────────────────────────────────

def test_tail_truncates_long_content_with_more_marker(db, capsys):
    _seed(db)
    long_text = "X" * 3000
    _msg(db, uuid="u1", session_id="s-1", role="user",
         text=long_text, ts="2026-04-01T00:00:01Z")
    ingest.tail(db, n=1, project="proj_a", width=100)
    out = capsys.readouterr().out
    assert "[+2900 more]" in out
    assert "--expand 1" in out


def test_tail_expand_disables_truncation_for_listed_turn(db, capsys):
    _seed(db)
    long_text = "X" * 500
    _msg(db, uuid="u1", session_id="s-1", role="user",
         text=long_text, ts="2026-04-01T00:00:01Z")
    ingest.tail(db, n=1, project="proj_a", width=100, expand={1})
    out = capsys.readouterr().out
    # Must contain the full content (expand uses the reverse-numbered index).
    assert "X" * 500 in out
    assert "more]" not in out
    assert "--expand" not in out


def test_tail_ascii_swaps_glyphs(db, capsys):
    _seed(db)
    _msg(db, uuid="u1", session_id="s-1", role="user",
         text="hi", ts="2026-04-01T00:00:01Z")
    _msg(db, uuid="a1", session_id="s-1", role="assistant",
         text="hello", ts="2026-04-01T00:00:02Z", agent="claude")
    ingest.tail(db, n=10, project="proj_a", ascii_only=True)
    out = capsys.readouterr().out
    assert "│" not in out
    assert "┃" not in out
    assert "·" not in out
    # ASCII fallbacks
    assert " | " in out   # agent bar
    assert " # " in out   # user bar (heavy fallback)


def test_tail_header_includes_session_project_count_range(db, capsys):
    _seed(db, session_id="abc12345xyz")
    _msg(db, uuid="u1", session_id="abc12345xyz", role="user",
         text="hello", ts="2026-04-01T09:00:00Z")
    _msg(db, uuid="u2", session_id="abc12345xyz", role="user",
         text="bye", ts="2026-04-01T10:00:00Z")
    ingest.tail(db, n=10, session="abc12345xyz")
    out = capsys.readouterr().out
    assert "session abc12345" in out
    assert "proj_a" in out
    assert "2 messages" in out
    assert "09:00→10:00" in out


def test_tail_metadata_column_aligns_across_rows(db, capsys):
    """The pipe `│` should appear at the same column on every row."""
    _seed(db)
    _msg(db, uuid="u1", session_id="s-1", role="user",
         text="short", ts="2026-04-01T00:00:01Z")
    _msg(db, uuid="a1", session_id="s-1", role="assistant",
         text="agent reply", ts="2026-04-01T00:00:02Z", agent="claude")
    ingest.tail(db, n=10, project="proj_a")
    lines = [l for l in capsys.readouterr().out.splitlines()
             if ("┃" in l or "│" in l) and ("YOU" in l or "claude" in l)]
    assert len(lines) >= 2
    # Find the column index of the bar in each row.
    cols = []
    for line in lines:
        # bar is either ┃ or │ — pick whichever is present
        bar = "┃" if "┃" in line else "│"
        cols.append(line.index(bar))
    assert len(set(cols)) == 1, f"misaligned bars: cols={cols}, lines={lines}"


# ── JSON output ────────────────────────────────────────────────────────────────

def test_tail_json_shape_includes_agent(db, capsys):
    _seed(db, agent="claude")
    _msg(db, uuid="u1", session_id="s-1", role="user",
         text="hello", ts="2026-04-01T00:00:01Z")
    _msg(db, uuid="a1", session_id="s-1", role="assistant",
         text="world", ts="2026-04-01T00:00:02Z")

    ingest.tail(db, n=5, project="proj_a", json_=True)
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload["session_id"] == "s-1"
    assert payload["project"] == "proj_a"
    assert payload["n"] == 5
    assert len(payload["messages"]) == 2
    # JSON keeps raw role names (machine-readable).
    assert payload["messages"][0]["role"] == "user"
    assert payload["messages"][0]["content"] == "hello"
    assert payload["messages"][0]["agent"] == "claude"


# ── error paths ────────────────────────────────────────────────────────────────

def test_tail_no_session_returns_1(db, capsys):
    rc = ingest.tail(db, n=10, project="nonexistent")
    err = capsys.readouterr().err
    assert rc == 1
    assert "No sessions found" in err


def test_tail_no_messages_in_session_returns_1(db, capsys):
    _seed(db)
    rc = ingest.tail(db, n=10, session="s-1")
    assert rc == 1


def test_tail_json_no_session_includes_error(db, capsys):
    rc = ingest.tail(db, n=10, project="nonexistent", json_=True)
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert rc == 1
    assert payload["messages"] == []
    assert "error" in payload


# ── "Did you mean" hint when slug differs only by hyphen/underscore ──────────

def test_tail_suggests_hyphen_variant_when_underscored_form_misses(db, capsys):
    """Real bug from /work/projects/app-gemini in the sandbox: cwd-detection
    derives slug 'app_gemini' but gemini ingest stored 'app-gemini'. Tail
    should now hint 'Did you mean: app-gemini?' (matches search's behavior)."""
    # Seed a session/message under the hyphenated slug.
    ingest._upsert_session(db, "gemini", "app-gemini", "s-g", None,
                           "2026-04-30T00:00:00Z", "2026-04-30T00:00:00Z")
    ingest._persist_message(db, "gemini", "app-gemini", "s-g", "u1",
                            "user", "hi from gemini",
                            "2026-04-30T00:00:01Z", do_embed=False)

    rc = ingest.tail(db, n=10, project="app_gemini")  # underscored — wrong
    err = capsys.readouterr().err
    assert rc == 1
    assert "No sessions found" in err
    assert "Did you mean" in err
    assert "app-gemini" in err


def test_tail_suggests_underscore_variant_when_hyphen_form_misses(db, capsys):
    """Symmetric case: user passes hyphen form but only underscored exists."""
    ingest._upsert_session(db, "claude", "app_claude", "s-c", None,
                           "2026-04-30T00:00:00Z", "2026-04-30T00:00:00Z")
    ingest._persist_message(db, "claude", "app_claude", "s-c", "u1",
                            "user", "hi", "2026-04-30T00:00:01Z",
                            do_embed=False)

    rc = ingest.tail(db, n=10, project="app-claude")
    err = capsys.readouterr().err
    assert rc == 1
    assert "Did you mean: app_claude" in err


def test_tail_json_did_you_mean_in_payload(db, capsys):
    """JSON callers should see the suggestion under `did_you_mean`."""
    ingest._upsert_session(db, "gemini", "app-gemini", "s-g", None,
                           "2026-04-30T00:00:00Z", "2026-04-30T00:00:00Z")
    ingest._persist_message(db, "gemini", "app-gemini", "s-g", "u1",
                            "user", "hi", "2026-04-30T00:00:01Z",
                            do_embed=False)

    rc = ingest.tail(db, n=10, project="app_gemini", json_=True)
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert rc == 1
    assert payload["messages"] == []
    assert "did_you_mean" in payload
    assert "app-gemini" in payload["did_you_mean"]


def test_tail_no_suggestion_when_truly_unknown_project(db, capsys):
    """If the project genuinely doesn't exist (no near-match), no suggestion."""
    rc = ingest.tail(db, n=10, project="totally-bogus-project")
    err = capsys.readouterr().err
    assert rc == 1
    assert "No sessions found" in err
    assert "Did you mean" not in err


# ── CLI smoke tests ────────────────────────────────────────────────────────────

_RECALL = shutil.which("recall")


@pytest.mark.skipif(_RECALL is None, reason="`recall` not on PATH")
def test_cli_tail_help_lists_args():
    r = subprocess.run([_RECALL, "tail", "--help"],
                       capture_output=True, text=True, timeout=10)
    assert r.returncode == 0
    for needle in ("Number of messages", "--session", "--all-projects",
                   "--expand", "--ascii", "--cols", "--json"):
        assert needle in r.stdout, f"missing {needle} in --help"


@pytest.mark.skipif(_RECALL is None, reason="`recall` not on PATH")
def test_cli_tail_expand_rejects_non_integer():
    r = subprocess.run([_RECALL, "tail", "--expand", "abc"],
                       capture_output=True, text=True, timeout=10)
    assert r.returncode == 2
    assert "expects integers" in r.stderr
