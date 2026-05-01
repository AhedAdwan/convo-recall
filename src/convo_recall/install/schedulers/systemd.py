"""SystemdUserScheduler — Linux native scheduler.

Generates a `.service` + `.path` unit pair per agent (file-event driven,
the systemd analogue of launchd's `WatchPaths`) and manages them via
`systemctl --user`. Pre-validates units with `systemd-analyze verify`
at install time so syntax errors fail loud.

Detection (`available()`) requires both `systemctl --user --version` AND
`systemctl --user is-system-running` to succeed with a usable state
(running / degraded / starting). A host where `systemctl` exists but
the user instance is offline returns False — those hosts must fall
through to CronScheduler or PollingScheduler.

Lingering (`loginctl enable-linger $USER`) is opt-in — exposed as
`enable_linger()` so the wizard can ask the user explicitly. Without
it, watchers die at logout.
"""

import os
import subprocess
from pathlib import Path

from .._paths import ensure_xdg_runtime_dir, scheduler_unit_dir
from .base import Result, Scheduler


_INGEST_PREFIX = "com.convo-recall.ingest"
_EMBED_LABEL = "com.convo-recall.embed"
_USABLE_STATES = ("running", "degraded", "starting")


class SystemdUserScheduler(Scheduler):
    def __init__(self) -> None:
        # Belt-and-suspenders: also runs from the wizard at startup, but
        # `recall uninstall` walks all_schedulers() without going through
        # the wizard, so make sure the env is right whenever this class
        # is instantiated. Idempotent.
        ensure_xdg_runtime_dir()

    def available(self) -> bool:
        try:
            v = subprocess.run(
                ["systemctl", "--user", "--version"],
                capture_output=True, text=True, timeout=2,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
        if v.returncode != 0:
            return False

        try:
            r = subprocess.run(
                ["systemctl", "--user", "is-system-running"],
                capture_output=True, text=True, timeout=2,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
        # is-system-running exits non-zero on `degraded`, which is still usable.
        # The state name on stdout is the authoritative signal.
        state = (r.stdout or "").strip()
        return any(s in state for s in _USABLE_STATES)

    def describe(self) -> str:
        return "systemd --user (Linux)"

    def consequence_yes(self) -> str:
        return ("A `.service` + `.path` unit pair is enabled per agent. "
                "Survives reboot if linger is enabled.")

    def consequence_no(self) -> str:
        return "Run `recall ingest` manually, or wire cron yourself."

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
        unit_dir = scheduler_unit_dir()
        unit_dir.mkdir(parents=True, exist_ok=True)
        label = f"{_INGEST_PREFIX}.{agent}"
        service_path = unit_dir / f"{label}.service"
        path_path = unit_dir / f"{label}.path"

        env = {
            "CONVO_RECALL_DB": db_path,
            "CONVO_RECALL_SOCK": sock_path,
            "CONVO_RECALL_CONFIG": config_path,
        }
        service_path.write_text(self._service_unit(
            description=f"convo-recall ingest watcher ({agent})",
            exec_start=f"{recall_bin} ingest --agent {agent}",
            env=env,
            unit_type="oneshot",
        ))
        path_path.write_text(self._path_unit(
            description=f"convo-recall path trigger ({agent})",
            target_unit=f"{label}.service",
            watch_dir=watch_dir,
        ))

        verify = self._systemd_analyze_verify([service_path, path_path])
        if not verify.ok:
            return verify

        rl = subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            capture_output=True, text=True,
        )
        if rl.returncode != 0:
            return Result(ok=False, message=f"daemon-reload failed: {rl.stderr.strip()}",
                          path=path_path)
        en = subprocess.run(
            ["systemctl", "--user", "enable", "--now", f"{label}.path"],
            capture_output=True, text=True,
        )
        if en.returncode != 0:
            return Result(ok=False, message=f"enable --now failed: {en.stderr.strip()}",
                          path=path_path)
        return Result(ok=True, message=f"{label} enabled and active", path=path_path)

    def install_sidecar(
        self, recall_bin: str, sock_path: str, log_dir: str
    ) -> Result:
        unit_dir = scheduler_unit_dir()
        unit_dir.mkdir(parents=True, exist_ok=True)
        service_path = unit_dir / f"{_EMBED_LABEL}.service"

        service_path.write_text(self._service_unit(
            description="convo-recall embed sidecar",
            exec_start=f"{recall_bin} serve --sock {sock_path}",
            env={"CONVO_RECALL_SOCK": sock_path},
            unit_type="simple",
            restart="always",
        ))

        verify = self._systemd_analyze_verify([service_path])
        if not verify.ok:
            return verify

        rl = subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            capture_output=True, text=True,
        )
        if rl.returncode != 0:
            return Result(ok=False, message=f"daemon-reload failed: {rl.stderr.strip()}",
                          path=service_path)
        en = subprocess.run(
            ["systemctl", "--user", "enable", "--now", f"{_EMBED_LABEL}.service"],
            capture_output=True, text=True,
        )
        if en.returncode != 0:
            return Result(ok=False, message=f"enable --now failed: {en.stderr.strip()}",
                          path=service_path)
        return Result(ok=True, message=f"{_EMBED_LABEL} enabled and active", path=service_path)

    def uninstall_watcher(self, agent: str) -> Result:
        unit_dir = scheduler_unit_dir()
        label = f"{_INGEST_PREFIX}.{agent}"
        service_path = unit_dir / f"{label}.service"
        path_path = unit_dir / f"{label}.path"

        # Early return when no unit files exist for this agent — e.g.
        # `recall uninstall` walks all_schedulers() including this one
        # on macOS where systemctl doesn't exist. Without this guard,
        # the systemctl call below raises FileNotFoundError on missing-
        # binary platforms and crashes the whole uninstall walk.
        if not (service_path.exists() or path_path.exists()):
            return Result(ok=True, message=f"{agent} watcher not installed",
                          path=path_path)

        try:
            subprocess.run(
                ["systemctl", "--user", "disable", "--now", f"{label}.path"],
                capture_output=True, text=True,
            )
        except FileNotFoundError:
            # Files exist but systemctl missing — treat as orphan units;
            # remove the files anyway and skip the bus call.
            pass
        path_path.unlink(missing_ok=True)
        service_path.unlink(missing_ok=True)
        try:
            subprocess.run(
                ["systemctl", "--user", "daemon-reload"],
                capture_output=True, text=True,
            )
        except FileNotFoundError:
            pass
        return Result(ok=True, message=f"{label} disabled and removed", path=path_path)

    def uninstall_sidecar(self) -> Result:
        unit_dir = scheduler_unit_dir()
        service_path = unit_dir / f"{_EMBED_LABEL}.service"

        # Same early-return guard as uninstall_watcher.
        if not service_path.exists():
            return Result(ok=True, message=f"{_EMBED_LABEL} sidecar not installed",
                          path=service_path)

        try:
            subprocess.run(
                ["systemctl", "--user", "disable", "--now", f"{_EMBED_LABEL}.service"],
                capture_output=True, text=True,
            )
        except FileNotFoundError:
            pass
        service_path.unlink(missing_ok=True)
        try:
            subprocess.run(
                ["systemctl", "--user", "daemon-reload"],
                capture_output=True, text=True,
            )
        except FileNotFoundError:
            pass
        return Result(ok=True, message=f"{_EMBED_LABEL} disabled and removed", path=service_path)

    # ── Lingering (opt-in by wizard) ─────────────────────────────────────────

    def enable_linger(self, user: str | None = None) -> Result:
        user = user or os.environ.get("USER", "")
        if not user:
            return Result(ok=False, message="USER not set; cannot enable linger")
        r = subprocess.run(
            ["loginctl", "enable-linger", user],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            return Result(ok=True, message=f"linger enabled for {user}")
        return Result(
            ok=False,
            message=("watchers will die at logout — fix with "
                     "`sudo loginctl enable-linger $USER`"),
        )

    # ── Unit-file generators ─────────────────────────────────────────────────

    def _service_unit(
        self,
        description: str,
        exec_start: str,
        env: dict[str, str],
        unit_type: str,
        restart: str | None = None,
    ) -> str:
        lines = [
            "[Unit]",
            f"Description={description}",
            "",
            "[Service]",
            f"Type={unit_type}",
            f"ExecStart={exec_start}",
        ]
        for k, v in env.items():
            lines.append(f'Environment={k}={v}')
        if restart:
            lines.append(f"Restart={restart}")
        lines.extend([
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ])
        return "\n".join(lines)

    def _path_unit(self, description: str, target_unit: str, watch_dir: str) -> str:
        return "\n".join([
            "[Unit]",
            f"Description={description}",
            "",
            "[Path]",
            f"PathChanged={watch_dir}",
            f"PathModified={watch_dir}",
            f"Unit={target_unit}",
            "",
            "[Install]",
            "WantedBy=paths.target",
            "",
        ])

    def _systemd_analyze_verify(self, files: list[Path]) -> Result:
        try:
            r = subprocess.run(
                ["systemd-analyze", "verify", *[str(f) for f in files]],
                capture_output=True, text=True,
            )
        except FileNotFoundError:
            # systemd-analyze not on PATH — skip verification rather than fail.
            # Real systemd hosts always have it; missing is only an issue on
            # CI mocks where the tests skip themselves anyway.
            return Result(ok=True, message="systemd-analyze not available; skipped verify",
                          path=files[0] if files else None)
        # exit 0 + non-empty stderr means warnings — fail loud per spec.
        if r.returncode != 0 or (r.stderr or "").strip():
            return Result(
                ok=False,
                message=f"systemd-analyze verify failed: {r.stderr.strip() or r.stdout.strip()}",
                path=files[0] if files else None,
            )
        return Result(ok=True, message="units verified",
                      path=files[0] if files else None)
