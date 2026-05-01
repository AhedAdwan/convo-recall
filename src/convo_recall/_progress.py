"""Background-job progress tracker — multi-phase.

Long-running operations (initial ingest + embed-backfill) write a
single JSON file at `<DATA_DIR>/backfill-progress.json` containing
status for each phase. `recall stats` reads it and renders one bar
per phase at the top of its output.

Phases are pre-declared at job start so users see what's coming —
even pending phases are rendered (as a placeholder) so the user
knows two steps are queued up rather than wondering why only one
bar showed.

States per phase:
- `pending` — declared but not started yet
- `running` — currently ticking
- `done`    — completed cleanly

When all phases are `done` (or the parent calls `finish_run()`), the
file is deleted. Stale-detection (PID gone + updated_at >120 s old)
also cleans the file so a crashed chain doesn't leave a misleading
snapshot in `recall stats` forever.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path


def _progress_path() -> Path:
    db_path = Path(os.environ.get(
        "CONVO_RECALL_DB",
        Path.home() / ".local" / "share" / "convo-recall" / "conversations.db"
    ))
    return db_path.parent / "backfill-progress.json"


_STALE_SECONDS = 120

PHASE_PENDING = "pending"
PHASE_RUNNING = "running"
PHASE_DONE = "done"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Multi-phase API ──────────────────────────────────────────────────────────


def start_run(phases: list[tuple[str, int]]) -> None:
    """Begin a multi-phase run. `phases` is a list of (name, total) pairs.
    Pass total=0 if the count isn't known yet — call `set_phase_total`
    later (e.g. embed-backfill counts pending rows only after ingest
    has populated the DB)."""
    path = _progress_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": os.getpid(),
        "started_at": _now_iso(),
        "updated_at": _now_iso(),
        "phases": [
            {
                "name": name,
                "total": int(total),
                "completed": 0,
                "state": PHASE_PENDING,
            }
            for name, total in phases
        ],
    }
    _atomic_write(path, payload)


def set_phase_total(name: str, total: int) -> None:
    """Update a phase's total — useful when the count is only known after
    a previous phase completes (e.g. embed-backfill counts pending rows
    after ingest finishes)."""
    _mutate_phase(name, lambda p: p.update(total=int(total)))


def update_phase(name: str, completed: int) -> None:
    """Tick a phase's completed counter and mark it as running. Idempotent
    and lossy: if the file is missing (e.g. user purged the data dir),
    we silently no-op rather than crash the parent."""
    def _set(p):
        p["completed"] = int(completed)
        p["state"] = PHASE_RUNNING
    _mutate_phase(name, _set)


def finish_phase(name: str) -> None:
    """Mark a phase as done. Sets completed=total so the bar renders at
    100% even if intermediate ticks under-counted."""
    def _set(p):
        # Snap completed up to total so the final bar reads 100% rather
        # than 99% from an off-by-one in tick batching.
        if p.get("total", 0) > 0:
            p["completed"] = max(p.get("completed", 0), p["total"])
        p["state"] = PHASE_DONE
    _mutate_phase(name, _set)


def finish_run() -> None:
    """Remove the progress file. Called at the end of a successful run
    (and from `finally:` blocks so a crashed chain still cleans up)."""
    _progress_path().unlink(missing_ok=True)


def read_status() -> dict | None:
    """Return the current run snapshot, or None if no active run."""
    path = _progress_path()
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None

    pid = payload.get("pid")
    if isinstance(pid, int) and not _pid_alive(pid):
        try:
            updated = datetime.fromisoformat(payload.get("updated_at", ""))
            age = (datetime.now(timezone.utc) - updated).total_seconds()
        except (ValueError, TypeError):
            age = _STALE_SECONDS + 1
        if age > _STALE_SECONDS:
            path.unlink(missing_ok=True)
            return None

    return payload


# ── Internal helpers ─────────────────────────────────────────────────────────


def _mutate_phase(name: str, mutator) -> None:
    """Read-modify-write a single phase entry. No-op if the file or the
    phase isn't present — both signal a missing/finished run, which is
    not a crash condition for callers."""
    path = _progress_path()
    if not path.exists():
        return
    try:
        payload = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return
    phases = payload.get("phases") or []
    for p in phases:
        if p.get("name") == name:
            mutator(p)
            break
    else:
        return  # phase wasn't declared at start_run — silently ignore
    payload["updated_at"] = _now_iso()
    _atomic_write(path, payload)


def _atomic_write(path: Path, payload: dict) -> None:
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}.{int(time.time() * 1000)}")
    tmp.write_text(json.dumps(payload))
    tmp.replace(path)
