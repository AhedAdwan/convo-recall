"""Scheduler ABC — single contract every concrete scheduler implements.

The wizard speaks to one of N schedulers (launchd on macOS, systemd /
cron / polling on Linux) via this interface, instead of branching on
`platform.system()`. `LaunchdScheduler` is the first implementation;
`PollingScheduler`, `SystemdUserScheduler`, `CronScheduler` follow in
sub-plans B1–B3.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Result:
    """Outcome of a scheduler operation. `path` is the unit/plist file the
    operation acted on (when relevant), so callers can surface it to the
    user without re-deriving it."""

    ok: bool
    message: str
    path: Path | None = None


class Scheduler(ABC):
    """A platform-specific way to run convo-recall watchers + sidecar in
    the background. Each concrete subclass owns one platform's mechanics
    (plists, systemd units, crontab lines, polling loops) end-to-end."""

    @abstractmethod
    def available(self) -> bool:
        """True if this scheduler can run on the current OS / shell env."""

    @abstractmethod
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
        """Install a per-agent ingest watcher."""

    @abstractmethod
    def uninstall_watcher(self, agent: str) -> Result:
        """Remove a per-agent ingest watcher."""

    @abstractmethod
    def install_sidecar(
        self, recall_bin: str, sock_path: str, log_dir: str
    ) -> Result:
        """Install the embed sidecar (long-running model service)."""

    @abstractmethod
    def uninstall_sidecar(self) -> Result:
        """Remove the embed sidecar."""

    @abstractmethod
    def describe(self) -> str:
        """One-line scheduler description shown in the wizard summary."""

    @abstractmethod
    def consequence_yes(self) -> str:
        """User-facing line: what happens if they install watchers via this scheduler."""

    @abstractmethod
    def consequence_no(self) -> str:
        """User-facing line: what they lose if they decline."""
