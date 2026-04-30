"""PollingScheduler — universal Popen-based fallback.

Spawns `recall watch` (and `recall serve` for the sidecar) as detached
background processes via `subprocess.Popen(start_new_session=True)`,
tracking each via a PID file in `_paths.runtime_dir()`. Last-resort
scheduler used when neither launchd nor systemd nor cron is available.

Lifecycle handling:
  - install: spawn → write PID file → return path. Stale PID files
    (process gone) are detected via `os.kill(pid, 0)` and overwritten;
    a live PID returns `ok=True` ("already running"), so install is
    idempotent.
  - uninstall: SIGTERM → poll for up to 5s → SIGKILL escalation if
    still alive → unlink PID file. Missing PID file is a no-op.

Caveat (consequence_no): does NOT survive reboot. Wizard surfaces this.
"""

import os
import signal
import subprocess
import time
from pathlib import Path

from .._paths import runtime_dir
from .base import Result, Scheduler


_GRACE_SECONDS = 5
_POLL_INTERVAL = 0.1


class PollingScheduler(Scheduler):
    def available(self) -> bool:
        return True

    def describe(self) -> str:
        return "polling (Popen fallback)"

    def consequence_yes(self) -> str:
        return ("Backgrounded via Popen; won't survive reboot/logout — "
                "re-run install on restart.")

    def consequence_no(self) -> str:
        return "Run `recall ingest` manually after each session."

    # ── Public ABC surface ────────────────────────────────────────────────────

    def install_watcher(
        self,
        agent: str,
        recall_bin: str,
        watch_dir: str,
        db_path: str,
        sock_path: str,
        config_path: str,
        log_dir: str,
    ) -> Result:
        return self._spawn(
            argv=[recall_bin, "watch"],
            pid_filename="watch.pid",
            log_filename="watch.log",
            log_dir=log_dir,
            already_msg="watcher already covers all agents",
            kind="watcher",
        )

    def install_sidecar(
        self, recall_bin: str, sock_path: str, log_dir: str
    ) -> Result:
        return self._spawn(
            argv=[recall_bin, "serve", "--sock", sock_path],
            pid_filename="embed.pid",
            log_filename="embed.log",
            log_dir=log_dir,
            already_msg="sidecar already running",
            kind="sidecar",
        )

    def uninstall_watcher(self, agent: str) -> Result:
        return self._terminate(pid_filename="watch.pid", kind="watcher")

    def uninstall_sidecar(self) -> Result:
        return self._terminate(pid_filename="embed.pid", kind="sidecar")

    # ── Internals ─────────────────────────────────────────────────────────────

    def _spawn(
        self,
        argv: list[str],
        pid_filename: str,
        log_filename: str,
        log_dir: str,
        already_msg: str,
        kind: str,
    ) -> Result:
        pid_dir = runtime_dir()
        pid_dir.mkdir(parents=True, exist_ok=True)
        pid_path = pid_dir / pid_filename

        # Idempotency: if PID file points at a live process, no-op.
        existing = self._read_live_pid(pid_path)
        if existing is not None:
            return Result(ok=True, message=already_msg, path=pid_path)

        log_path = Path(log_dir)
        log_path.mkdir(parents=True, exist_ok=True)
        log_file = open(log_path / log_filename, "ab")
        try:
            proc = subprocess.Popen(
                argv,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        finally:
            # Popen dups the fd; we can close ours.
            log_file.close()

        pid_path.write_text(str(proc.pid))
        return Result(
            ok=True,
            message=f"{kind} spawned (pid {proc.pid}, log {log_path / log_filename})",
            path=pid_path,
        )

    def _terminate(self, pid_filename: str, kind: str) -> Result:
        pid_path = runtime_dir() / pid_filename
        if not pid_path.exists():
            return Result(ok=True, message=f"no {kind} PID file; nothing to remove",
                          path=pid_path)
        try:
            pid = int(pid_path.read_text().strip())
        except (ValueError, OSError) as e:
            pid_path.unlink(missing_ok=True)
            return Result(ok=False, message=f"corrupt PID file removed: {e}", path=pid_path)

        if not self._pid_alive(pid):
            pid_path.unlink(missing_ok=True)
            return Result(ok=True, message=f"{kind} (pid {pid}) was already dead", path=pid_path)

        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pid_path.unlink(missing_ok=True)
            return Result(ok=True, message=f"{kind} (pid {pid}) gone before SIGTERM", path=pid_path)

        deadline = time.monotonic() + _GRACE_SECONDS
        while time.monotonic() < deadline:
            if not self._pid_alive(pid):
                pid_path.unlink(missing_ok=True)
                return Result(ok=True, message=f"{kind} (pid {pid}) terminated cleanly", path=pid_path)
            time.sleep(_POLL_INTERVAL)

        # SIGKILL escalation — bounded: only kills processes we spawned.
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        pid_path.unlink(missing_ok=True)
        return Result(ok=True, message=f"{kind} (pid {pid}) killed (SIGKILL after {_GRACE_SECONDS}s grace)",
                      path=pid_path)

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # Process exists but is owned by someone else — for our purposes,
            # alive (we won't be able to signal it anyway).
            return True

    def _read_live_pid(self, pid_path: Path) -> int | None:
        if not pid_path.exists():
            return None
        try:
            pid = int(pid_path.read_text().strip())
        except (ValueError, OSError):
            return None
        return pid if self._pid_alive(pid) else None
