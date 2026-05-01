"""Tests for the multi-phase progress tracker (`_progress.py`) and its
integration with `recall stats`."""

import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from convo_recall import _progress


# ── _progress primitive: lifecycle ───────────────────────────────────────────


def _set_db(tmp_path, monkeypatch):
    """Point CONVO_RECALL_DB at a tmp file so the progress JSON lives
    next to it. Also patch the imported DB_PATH so `ingest.open_db()`
    uses the same location."""
    db = tmp_path / "conversations.db"
    monkeypatch.setenv("CONVO_RECALL_DB", str(db))
    from convo_recall import ingest as _ing
    monkeypatch.setattr(_ing, "DB_PATH", db)
    return db


def test_start_run_creates_phases_in_pending_state(tmp_path, monkeypatch):
    _set_db(tmp_path, monkeypatch)
    _progress.start_run([("ingest", 0), ("embed-backfill", 0)])
    s = _progress.read_status()
    assert s is not None
    assert [p["name"] for p in s["phases"]] == ["ingest", "embed-backfill"]
    for p in s["phases"]:
        assert p["state"] == "pending"
        assert p["completed"] == 0


def test_set_phase_total_updates_total(tmp_path, monkeypatch):
    _set_db(tmp_path, monkeypatch)
    _progress.start_run([("ingest", 0), ("embed-backfill", 0)])
    _progress.set_phase_total("ingest", 4248)
    s = _progress.read_status()
    ingest_phase = next(p for p in s["phases"] if p["name"] == "ingest")
    assert ingest_phase["total"] == 4248


def test_update_phase_marks_running(tmp_path, monkeypatch):
    _set_db(tmp_path, monkeypatch)
    _progress.start_run([("ingest", 100)])
    _progress.update_phase("ingest", 42)
    s = _progress.read_status()
    p = s["phases"][0]
    assert p["completed"] == 42
    assert p["state"] == "running"


def test_finish_phase_snaps_to_total(tmp_path, monkeypatch):
    """finish_phase sets completed = total so the bar renders 100% even
    if the last batch tick under-counted (e.g. update was every 100 but
    the loop ended at 4250)."""
    _set_db(tmp_path, monkeypatch)
    _progress.start_run([("ingest", 4250)])
    _progress.update_phase("ingest", 4200)  # last tick was at 4200
    _progress.finish_phase("ingest")
    s = _progress.read_status()
    p = s["phases"][0]
    assert p["state"] == "done"
    assert p["completed"] == 4250


def test_finish_run_removes_file(tmp_path, monkeypatch):
    _set_db(tmp_path, monkeypatch)
    _progress.start_run([("ingest", 0)])
    assert (tmp_path / "backfill-progress.json").exists()
    _progress.finish_run()
    assert not (tmp_path / "backfill-progress.json").exists()


def test_read_status_returns_none_when_absent(tmp_path, monkeypatch):
    _set_db(tmp_path, monkeypatch)
    assert _progress.read_status() is None


def test_update_unknown_phase_is_silent(tmp_path, monkeypatch):
    """Calling update_phase on a name that wasn't declared at start_run
    must not crash — useful when scan_all is reused across contexts
    (chain vs. watcher) and the chain only declared one of the phases."""
    _set_db(tmp_path, monkeypatch)
    _progress.start_run([("ingest", 0)])
    _progress.update_phase("embed-backfill", 50)  # not declared
    s = _progress.read_status()
    # Only ingest exists; no embed-backfill silently appeared.
    assert [p["name"] for p in s["phases"]] == ["ingest"]


def test_no_active_run_makes_updates_noops(tmp_path, monkeypatch):
    """All mutators are safe to call when no run exists. This is the
    invariant that lets ingest/embed_backfill instrumentation always
    fire without checking first."""
    _set_db(tmp_path, monkeypatch)
    # No start_run.
    _progress.set_phase_total("ingest", 100)
    _progress.update_phase("ingest", 50)
    _progress.finish_phase("ingest")
    _progress.finish_run()
    assert _progress.read_status() is None


def test_stale_dead_pid_cleaned_on_read(tmp_path, monkeypatch):
    _set_db(tmp_path, monkeypatch)
    p = tmp_path / "backfill-progress.json"
    p.write_text(json.dumps({
        "pid": 2**31 - 1,  # guaranteed-dead
        "started_at": "2020-01-01T00:00:00+00:00",
        "updated_at": "2020-01-01T00:00:00+00:00",
        "phases": [{"name": "ingest", "total": 100, "completed": 50, "state": "running"}],
    }))
    assert _progress.read_status() is None
    assert not p.exists(), "stale file should be deleted on read"


# ── stats() integration ─────────────────────────────────────────────────────


def test_stats_renders_one_bar_per_phase(tmp_path, monkeypatch):
    """When the chain pre-declared two phases, stats must render both —
    even if one is still pending. This is the visibility gap the user
    asked for: 'I want two bars, ingest AND embed.'"""
    _set_db(tmp_path, monkeypatch)
    _progress.start_run([("ingest", 4248), ("embed-backfill", 0)])
    _progress.update_phase("ingest", 1234)

    from convo_recall import ingest
    con = ingest.open_db()

    buf = io.StringIO()
    with redirect_stdout(buf):
        ingest.stats(con)
    out = buf.getvalue()

    assert "ingest" in out
    assert "embed-backfill" in out
    # Ingest is partway through — current count visible.
    assert "1,234" in out or "1234" in out
    assert "4,248" in out or "4248" in out
    # embed-backfill still pending — show that, not a fake bar.
    assert "pending" in out


def test_stats_pending_phase_does_not_show_fake_bar(tmp_path, monkeypatch):
    _set_db(tmp_path, monkeypatch)
    _progress.start_run([("embed-backfill", 0)])
    # Don't mark running. Should render "pending" placeholder, not 0/0 bar.

    from convo_recall import ingest
    con = ingest.open_db()

    buf = io.StringIO()
    with redirect_stdout(buf):
        ingest.stats(con)
    out = buf.getvalue()

    assert "embed-backfill" in out
    assert "pending" in out
    # No "0/0" bar — the pending placeholder is the entire output for
    # this phase. (`(0%)` from the Embedded coverage line is fine.)
    assert "0/0" not in out
    assert "█" not in out


def test_stats_done_phase_with_zero_total_says_nothing_to_do(tmp_path, monkeypatch):
    """The user's exact case: existing DB, ingest had nothing new, embed
    had nothing to embed. Both phases finish with total=0. We don't show
    a 0/0 bar — we say 'nothing to do' so the user understands."""
    _set_db(tmp_path, monkeypatch)
    _progress.start_run([("ingest", 0), ("embed-backfill", 0)])
    _progress.finish_phase("ingest")
    _progress.finish_phase("embed-backfill")

    from convo_recall import ingest
    con = ingest.open_db()

    buf = io.StringIO()
    with redirect_stdout(buf):
        ingest.stats(con)
    out = buf.getvalue()

    assert "ingest: nothing to do" in out
    assert "embed-backfill: nothing to do" in out


def test_stats_no_progress_bar_when_idle(tmp_path, monkeypatch):
    """No active run → stats output unchanged from pre-feature."""
    _set_db(tmp_path, monkeypatch)

    from convo_recall import ingest
    con = ingest.open_db()

    buf = io.StringIO()
    with redirect_stdout(buf):
        ingest.stats(con)
    out = buf.getvalue()

    assert "ingest:" not in out
    assert "embed-backfill:" not in out
    assert "█" not in out
