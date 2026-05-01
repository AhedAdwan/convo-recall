"""Tests for the background-job progress tracker (`_progress.py`) and
its integration with `recall stats`."""

import io
import json
import os
import time
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from convo_recall import _progress


# ── _progress primitive ──────────────────────────────────────────────────────


def test_start_writes_progress_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CONVO_RECALL_DB", str(tmp_path / "conversations.db"))
    _progress.start_job("embed-backfill", total=12345)
    p = tmp_path / "backfill-progress.json"
    assert p.exists()
    payload = json.loads(p.read_text())
    assert payload["job"] == "embed-backfill"
    assert payload["total"] == 12345
    assert payload["completed"] == 0
    assert payload["pid"] == os.getpid()
    assert "started_at" in payload
    assert "updated_at" in payload


def test_update_increments_counter(tmp_path, monkeypatch):
    monkeypatch.setenv("CONVO_RECALL_DB", str(tmp_path / "conversations.db"))
    _progress.start_job("embed-backfill", total=100)
    _progress.update_job(42)
    payload = json.loads((tmp_path / "backfill-progress.json").read_text())
    assert payload["completed"] == 42


def test_finish_removes_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CONVO_RECALL_DB", str(tmp_path / "conversations.db"))
    _progress.start_job("embed-backfill", total=10)
    assert (tmp_path / "backfill-progress.json").exists()
    _progress.finish_job()
    assert not (tmp_path / "backfill-progress.json").exists()


def test_read_status_returns_none_when_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("CONVO_RECALL_DB", str(tmp_path / "conversations.db"))
    assert _progress.read_status() is None


def test_read_status_returns_snapshot_when_alive(tmp_path, monkeypatch):
    monkeypatch.setenv("CONVO_RECALL_DB", str(tmp_path / "conversations.db"))
    _progress.start_job("embed-backfill", total=200)
    _progress.update_job(50)
    s = _progress.read_status()
    assert s is not None
    assert s["completed"] == 50
    assert s["total"] == 200
    assert s["pid"] == os.getpid()


def test_read_status_cleans_stale_dead_pid(tmp_path, monkeypatch):
    """A snapshot from a process that's gone AND older than _STALE_SECONDS
    must be deleted on read so future stats invocations stay quiet."""
    monkeypatch.setenv("CONVO_RECALL_DB", str(tmp_path / "conversations.db"))
    p = tmp_path / "backfill-progress.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    # PID 1 is init/launchd; non-zero chance someone else has it but
    # we're just exercising the "alive" branch here. Use 2**31-1 instead
    # for a guaranteed-dead PID on every Unix.
    dead_pid = 2**31 - 1
    p.write_text(json.dumps({
        "job": "embed-backfill",
        "phase": "embed-backfill",
        "pid": dead_pid,
        "total": 100,
        "completed": 50,
        "started_at": "2020-01-01T00:00:00+00:00",
        "updated_at": "2020-01-01T00:00:00+00:00",  # very old
    }))
    assert _progress.read_status() is None
    assert not p.exists(), "stale file should have been deleted on read"


def test_read_status_keeps_recent_dead_pid_snapshot(tmp_path, monkeypatch):
    """A finished job whose process exited <_STALE_SECONDS ago is NOT
    cleaned — finish_job() handles the happy-path cleanup; stale-detection
    is a fallback for crashes. Recent-but-orphan files are left for the
    user to clear (or they will be in finish_job() called by the parent)."""
    monkeypatch.setenv("CONVO_RECALL_DB", str(tmp_path / "conversations.db"))
    p = tmp_path / "backfill-progress.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    dead_pid = 2**31 - 1
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    p.write_text(json.dumps({
        "job": "embed-backfill",
        "phase": "embed-backfill",
        "pid": dead_pid,
        "total": 100,
        "completed": 100,
        "started_at": now,
        "updated_at": now,
    }))
    # Snapshot is recent — read returns the payload (caller can decide
    # what to do, e.g. show a final completed bar before stats).
    s = _progress.read_status()
    assert s is not None
    assert s["completed"] == 100


def test_atomic_write_does_not_leave_partial_file(tmp_path, monkeypatch):
    """A reader hitting the file mid-write must NOT see a half-written
    JSON object. The atomic-rename strategy guarantees this — verify by
    asserting the .tmp.* sidecar isn't visible after the call."""
    monkeypatch.setenv("CONVO_RECALL_DB", str(tmp_path / "conversations.db"))
    _progress.start_job("test", total=1)
    leftovers = list(tmp_path.glob("backfill-progress.json.tmp.*"))
    assert leftovers == [], leftovers


# ── stats() integration ─────────────────────────────────────────────────────


def _patch_db_to_tmp(tmp_path, monkeypatch):
    """DB_PATH is captured at module import so monkeypatch.setenv after
    import has no effect — patch the attribute directly on the module."""
    db = tmp_path / "conversations.db"
    monkeypatch.setenv("CONVO_RECALL_DB", str(db))
    from convo_recall import ingest as _ing
    monkeypatch.setattr(_ing, "DB_PATH", db)
    return db


def test_stats_renders_progress_bar_when_active(tmp_path, monkeypatch):
    """`recall stats` must render the one-shot progress bar at the top
    when an active job's progress file is present, then print stats."""
    _patch_db_to_tmp(tmp_path, monkeypatch)
    _progress.start_job("embed-backfill", total=1000)
    _progress.update_job(425)

    from convo_recall import ingest
    con = ingest.open_db()

    buf = io.StringIO()
    with redirect_stdout(buf):
        ingest.stats(con)
    out = buf.getvalue()

    # tqdm or plain fallback — both contain the phase + counts.
    assert "embed-backfill" in out
    assert "425" in out
    assert "1,000" in out or "1000" in out
    # Stats body still printed.
    assert "Messages" in out


def test_stats_no_progress_bar_when_idle(tmp_path, monkeypatch):
    """No progress file present → stats output must NOT include the bar
    framing characters. Idle stats should look identical to pre-feature."""
    _patch_db_to_tmp(tmp_path, monkeypatch)
    # No start_job call.

    from convo_recall import ingest
    con = ingest.open_db()

    buf = io.StringIO()
    with redirect_stdout(buf):
        ingest.stats(con)
    out = buf.getvalue()

    assert "embed-backfill" not in out
    assert "█" not in out
