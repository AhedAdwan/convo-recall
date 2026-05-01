"""Background-job progress tracker.

Long-running operations (initial ingest, embed-backfill) write a JSON
file at `<DATA_DIR>/backfill-progress.json`. `recall stats` reads it
and renders a one-shot tqdm bar at the top of its output, so the user
gets visible feedback any time they check stats while a background
job is running.

Why a file (not shared memory or a socket): the wizard spawns the
backfill as a *detached* subprocess and exits. The user runs
`recall stats` later from a different terminal. A file on disk is
the only thing that survives the wizard's exit and crosses processes
without an IPC layer we don't otherwise need.

Stale-job detection: each write stamps `pid` and `updated_at`. A
reader that sees `pid` no longer alive AND `updated_at` more than
~120 s old treats the file as stale and ignores it (returns None).
This prevents a dead job's last snapshot from misleading later
`recall stats` runs forever.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

# DATA_DIR is the same place CONVO_RECALL_DB lives.
def _progress_path() -> Path:
    db_path = Path(os.environ.get(
        "CONVO_RECALL_DB",
        Path.home() / ".local" / "share" / "convo-recall" / "conversations.db"
    ))
    return db_path.parent / "backfill-progress.json"


_STALE_SECONDS = 120


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but owned by someone else; we can't signal it
        # but for our purposes treat as alive — it's not stale.
        return True
    except OSError:
        return False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def start_job(name: str, total: int, *, phase: str | None = None) -> None:
    """Mark a new job as running. Overwrites any prior progress file —
    the assumption is one logical backfill at a time. If a previous job
    crashed mid-run, this clears its stale snapshot."""
    path = _progress_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "job": name,
        "phase": phase or name,
        "pid": os.getpid(),
        "total": int(total),
        "completed": 0,
        "started_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    _atomic_write(path, payload)


def update_job(completed: int, *, phase: str | None = None) -> None:
    """Update the running job's completed counter. Idempotent and lossy:
    if the file is missing (e.g. the user purged the data dir), we
    silently no-op rather than crash the backfill. Phase override lets
    a multi-step chain (ingest → embed-backfill) re-label the bar."""
    path = _progress_path()
    if not path.exists():
        return
    try:
        payload = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return
    payload["completed"] = int(completed)
    payload["updated_at"] = _now_iso()
    if phase is not None:
        payload["phase"] = phase
    _atomic_write(path, payload)


def finish_job() -> None:
    """Remove the progress file. Called at the end of a successful run."""
    path = _progress_path()
    path.unlink(missing_ok=True)


def read_status() -> dict | None:
    """Return the current job snapshot, or None if no active job.

    Returns None when:
    - the file doesn't exist
    - the file is unreadable / malformed
    - the recorded PID is not alive AND the snapshot is older than
      _STALE_SECONDS (i.e. the job crashed and never called finish_job)
    """
    path = _progress_path()
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None

    pid = payload.get("pid")
    if isinstance(pid, int) and not _pid_alive(pid):
        # Process is gone. Decide stale vs. just-finished by age.
        try:
            updated = datetime.fromisoformat(payload.get("updated_at", ""))
            age = (datetime.now(timezone.utc) - updated).total_seconds()
        except (ValueError, TypeError):
            age = _STALE_SECONDS + 1
        if age > _STALE_SECONDS:
            # Stale — clean up so future reads stay quiet.
            path.unlink(missing_ok=True)
            return None

    return payload


def _atomic_write(path: Path, payload: dict) -> None:
    """Write via tmp + replace so a concurrent read never sees a half-
    written JSON file."""
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}.{int(time.time() * 1000)}")
    tmp.write_text(json.dumps(payload))
    tmp.replace(path)
