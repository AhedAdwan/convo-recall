"""Launchd scheduler — macOS implementation of the `Scheduler` ABC.

Wraps the existing per-agent ingest plists, the embed sidecar plist, and
`launchctl bootstrap`/`bootout` calls. All plist generation lived as
module-level helpers in `install.py` before Phase A; they are now
class-private methods so the wizard talks to a `Scheduler` object instead
of platform branching.
"""

import os
import platform
import plistlib
import subprocess
from pathlib import Path

from .base import Result, Scheduler


INGEST_LABEL = "com.convo-recall.ingest"
EMBED_LABEL = "com.convo-recall.embed"
LAUNCHAGENTS = Path.home() / "Library" / "LaunchAgents"


class LaunchdScheduler(Scheduler):
    def available(self) -> bool:
        return platform.system() == "Darwin"

    def describe(self) -> str:
        return "launchd (macOS)"

    def consequence_yes(self) -> str:
        return ("convo-recall watches each session dir; new content "
                "indexed within ~10s.")

    def consequence_no(self) -> str:
        return ("Indexing won't happen automatically. You'll need to run "
                "`recall ingest` manually (or wire cron / systemd yourself).")

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
        # Late import to avoid a cycle: install/__init__ imports this module.
        from convo_recall import install as _install_pkg

        label = f"{INGEST_LABEL}.{agent}"
        agents_dir = _install_pkg.LAUNCHAGENTS
        agents_dir.mkdir(parents=True, exist_ok=True)
        plist_path = agents_dir / f"{label}.plist"
        plist_path.write_bytes(self._ingest_plist(
            label=label,
            recall_bin=recall_bin,
            db_path=db_path,
            watch_dir=watch_dir,
            sock_path=sock_path,
            log_dir=log_dir,
            agent=agent,
            config_path=config_path,
        ))
        if self._launchctl_load(plist_path):
            return Result(ok=True, message=f"{agent} watcher loaded ({plist_path.name})", path=plist_path)
        return Result(
            ok=False,
            message=f"{agent} load failed — run manually: launchctl load {plist_path}",
            path=plist_path,
        )

    def install_sidecar(
        self, recall_bin: str, sock_path: str, log_dir: str
    ) -> Result:
        from convo_recall import install as _install_pkg

        agents_dir = _install_pkg.LAUNCHAGENTS
        agents_dir.mkdir(parents=True, exist_ok=True)
        plist_path = agents_dir / f"{EMBED_LABEL}.plist"
        plist_path.write_bytes(self._embed_plist(
            label=EMBED_LABEL,
            recall_bin=recall_bin,
            sock_path=sock_path,
            log_dir=log_dir,
        ))
        if self._launchctl_load(plist_path):
            return Result(ok=True, message=f"embed sidecar loaded ({plist_path.name})", path=plist_path)
        return Result(
            ok=False,
            message=f"embed load failed — run manually: launchctl load {plist_path}",
            path=plist_path,
        )

    def uninstall_watcher(self, agent: str) -> Result:
        from convo_recall import install as _install_pkg

        label = f"{INGEST_LABEL}.{agent}"
        plist_path = _install_pkg.LAUNCHAGENTS / f"{label}.plist"
        if not plist_path.exists():
            return Result(ok=True, message=f"{agent} watcher not installed", path=plist_path)
        uid = os.getuid()
        subprocess.run(
            ["launchctl", "bootout", f"gui/{uid}", str(plist_path)],
            capture_output=True,
        )
        try:
            plist_path.unlink()
            return Result(ok=True, message=f"{plist_path.name}", path=plist_path)
        except OSError as e:
            return Result(ok=False, message=f"{plist_path.name}: {e}", path=plist_path)

    def uninstall_sidecar(self) -> Result:
        from convo_recall import install as _install_pkg

        plist_path = _install_pkg.LAUNCHAGENTS / f"{EMBED_LABEL}.plist"
        if not plist_path.exists():
            return Result(ok=True, message="embed sidecar not installed", path=plist_path)
        uid = os.getuid()
        subprocess.run(
            ["launchctl", "bootout", f"gui/{uid}", str(plist_path)],
            capture_output=True,
        )
        try:
            plist_path.unlink()
            return Result(ok=True, message=f"{plist_path.name}", path=plist_path)
        except OSError as e:
            return Result(ok=False, message=f"{plist_path.name}: {e}", path=plist_path)

    # ── launchd primitives (formerly module-level helpers in install.py) ──────

    def _ingest_plist(
        self,
        label: str,
        recall_bin: str,
        db_path: str,
        watch_dir: str,
        sock_path: str,
        log_dir: str,
        agent: str | None = None,
        config_path: str | None = None,
    ) -> bytes:
        """Generate the launchd plist for a (per-agent) ingest watcher.

        `agent`: when set, the plist runs `recall ingest --agent {agent}` and
                 watches only that agent's source dir. When None (legacy mode),
                 runs plain `recall ingest` watching the claude projects dir.
        """
        from convo_recall import install as _install_pkg

        args = [recall_bin, "ingest"]
        if agent:
            args += ["--agent", agent]
        env = {
            "CONVO_RECALL_DB": db_path,
            "CONVO_RECALL_PROJECTS": str(_install_pkg.PROJECTS_DIR),
            "CONVO_RECALL_GEMINI_TMP": str(_install_pkg.GEMINI_TMP),
            "CONVO_RECALL_CODEX_SESSIONS": str(_install_pkg.CODEX_SESSIONS),
            "CONVO_RECALL_SOCK": sock_path,
        }
        if config_path:
            env["CONVO_RECALL_CONFIG"] = config_path
        suffix = f"-{agent}" if agent else ""
        return plistlib.dumps({
            "Label": label,
            "ProgramArguments": args,
            "EnvironmentVariables": env,
            "WatchPaths": [watch_dir],
            "RunAtLoad": True,
            "StandardOutPath": f"{log_dir}/convo-recall-ingest{suffix}.log",
            "StandardErrorPath": f"{log_dir}/convo-recall-ingest{suffix}.error.log",
            "ThrottleInterval": 10,
        })

    def _embed_plist(
        self, label: str, recall_bin: str, sock_path: str, log_dir: str
    ) -> bytes:
        return plistlib.dumps({
            "Label": label,
            "ProgramArguments": [recall_bin, "serve", "--sock", sock_path],
            "EnvironmentVariables": {"CONVO_RECALL_SOCK": sock_path},
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": f"{log_dir}/convo-recall-embed.log",
            "StandardErrorPath": f"{log_dir}/convo-recall-embed.error.log",
        })

    def _launchctl_load(self, plist: Path) -> bool:
        uid = os.getuid()
        result = subprocess.run(
            ["launchctl", "bootstrap", f"gui/{uid}", str(plist)],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            return True
        if result.returncode in (36, 37):
            subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(plist)],
                           capture_output=True)
            r2 = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist)],
                                 capture_output=True, text=True)
            return r2.returncode == 0
        return False
