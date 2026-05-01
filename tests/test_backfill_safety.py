"""Safety-gate tests for backfill-clean / backfill-redact / chunk-backfill.

Each backfill mutates rows in place. They must all default to DRY-RUN with
a preview, prompt 'YES' on a TTY, refuse in non-TTY without --confirm, and
proceed on confirm=True. Pattern matches `recall forget` and
`recall uninstall --purge-data`.
"""

import os
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


def _msg(con, *, uuid, text, ts="2026-04-01T00:00:01Z",
         session_id="s-1", role="user"):
    ingest._persist_message(con, "claude", "proj_a", session_id, uuid,
                            role, text, ts, do_embed=False)


# ── backfill_clean ────────────────────────────────────────────────────────────

def test_backfill_clean_non_tty_default_is_dry_run(db, capsys, monkeypatch):
    """Non-TTY without --confirm must NOT mutate rows."""
    _seed(db)
    # Inject content that _clean_content WILL change. Wrap in tool_result
    # block which the cleaner unwraps.
    raw = '[{"type": "tool_result", "content": "expected change"}]'
    _msg(db, uuid="u1", text=raw)

    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    ingest.backfill_clean(db)  # confirm defaults to False

    out = capsys.readouterr().out
    assert "DRY-RUN" in out or "Nothing to do" in out
    # Row content unchanged
    after = db.execute("SELECT content FROM messages WHERE uuid='u1'").fetchone()[0]
    assert after == raw, "non-TTY dry-run must NOT mutate content"


def test_backfill_clean_tty_prompts_and_aborts_on_no(db, capsys, monkeypatch):
    """TTY user typing anything but 'YES' must abort without mutation."""
    _seed(db)
    raw = '[{"type": "tool_result", "content": "would-be-cleaned"}]'
    _msg(db, uuid="u1", text=raw)

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "yes")  # lowercase

    ingest.backfill_clean(db)

    out = capsys.readouterr().out
    assert "ARE YOU SURE" in out
    assert "Aborted" in out
    after = db.execute("SELECT content FROM messages WHERE uuid='u1'").fetchone()[0]
    assert after == raw


def test_backfill_clean_tty_proceeds_on_uppercase_YES(db, capsys, monkeypatch):
    _seed(db)
    raw = '[{"type": "tool_result", "content": "to-be-cleaned"}]'
    _msg(db, uuid="u1", text=raw)

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "YES")

    ingest.backfill_clean(db)

    after = db.execute("SELECT content FROM messages WHERE uuid='u1'").fetchone()[0]
    assert after != raw, "YES must trigger the cleaning mutation"


def test_backfill_clean_confirm_skips_prompt(db, capsys, monkeypatch):
    """confirm=True bypasses both the TTY prompt and the non-TTY refusal."""
    _seed(db)
    raw = '[{"type": "tool_result", "content": "scripted-clean"}]'
    _msg(db, uuid="u1", text=raw)

    # Simulate non-TTY — confirm should still proceed without prompting.
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    # If input() is called, this would raise (proves it isn't called).
    monkeypatch.setattr("builtins.input",
                        lambda _p="": (_ for _ in ()).throw(
                            AssertionError("input() must NOT be called when confirm=True")))

    ingest.backfill_clean(db, confirm=True)

    after = db.execute("SELECT content FROM messages WHERE uuid='u1'").fetchone()[0]
    assert after != raw


def test_backfill_clean_no_op_when_nothing_changes(db, capsys, monkeypatch):
    """If every row is already clean, the function must short-circuit
    BEFORE the gate (no prompt, no mutation, friendly 'nothing to do' msg)."""
    _seed(db)
    # Plain prose with no special tokens — passes through the cleaner unchanged.
    _msg(db, uuid="u1", text="hello world this is a clean message")

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    # input() must NOT be called.
    monkeypatch.setattr("builtins.input",
                        lambda _p="": (_ for _ in ()).throw(
                            AssertionError("no prompt expected when nothing changes")))

    ingest.backfill_clean(db)

    out = capsys.readouterr().out
    assert "Nothing to do" in out


# ── backfill_redact ───────────────────────────────────────────────────────────

def test_backfill_redact_non_tty_default_is_dry_run(db, capsys, monkeypatch):
    _seed(db)
    secret = "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    _msg(db, uuid="u1", text=f"my key is {secret} please don't leak")

    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    ingest.backfill_redact(db)

    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    after = db.execute("SELECT content FROM messages WHERE uuid='u1'").fetchone()[0]
    assert secret in after, "non-TTY dry-run must NOT redact"


def test_backfill_redact_confirm_actually_redacts(db, monkeypatch):
    _seed(db)
    secret = "sk-ant-api03-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
    _msg(db, uuid="u1", text=f"key is {secret} thanks")

    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    ingest.backfill_redact(db, confirm=True)

    after = db.execute("SELECT content FROM messages WHERE uuid='u1'").fetchone()[0]
    assert secret not in after, "secret should be redacted after confirm"


# ── chunk_backfill ────────────────────────────────────────────────────────────

def test_chunk_backfill_non_tty_default_is_dry_run(db, capsys, monkeypatch):
    """chunk-backfill is lower risk (only embeddings, no text mutation) but
    still requires confirm to avoid silently spending GPU/CPU."""
    _seed(db)
    long_text = "x " * 1500  # ≈ 3000 chars, exceeds the 1800 threshold
    _msg(db, uuid="u1", text=long_text)

    # Force vec_ok to be True so the function reaches the gate.
    monkeypatch.setattr(ingest, "_vec_ok", lambda con: True)
    # Pretend the embed socket exists.
    fake_sock = type("S", (), {"exists": lambda self: True})()
    monkeypatch.setattr(ingest, "EMBED_SOCK", fake_sock)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    # Should NOT call embed() — refusal happens at the gate.
    monkeypatch.setattr(ingest, "embed",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("embed() must NOT be called in dry-run")))

    ingest.chunk_backfill(db)
    out = capsys.readouterr().out
    assert "DRY-RUN" in out


def test_chunk_backfill_confirm_skips_prompt_and_runs_embed(db, capsys, monkeypatch):
    _seed(db)
    long_text = "y " * 1500
    _msg(db, uuid="u1", text=long_text)

    monkeypatch.setattr(ingest, "_vec_ok", lambda con: True)
    fake_sock = type("S", (), {"exists": lambda self: True})()
    monkeypatch.setattr(ingest, "EMBED_SOCK", fake_sock)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    embed_calls: list = []
    monkeypatch.setattr(ingest, "embed", lambda text, **_: embed_calls.append(text) or [0.0])
    monkeypatch.setattr(ingest, "_vec_insert", lambda *a, **k: None)

    # input() must NOT be called when confirm=True.
    monkeypatch.setattr("builtins.input",
                        lambda _p="": (_ for _ in ()).throw(
                            AssertionError("input() must NOT be called when confirm=True")))

    ingest.chunk_backfill(db, confirm=True)
    assert len(embed_calls) == 1, "confirm=True should run embed() on each long row"
