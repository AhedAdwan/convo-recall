"""Scheduler registry + factory.

`detect_scheduler()` walks the priority list and returns the first
scheduler whose `available()` is True. `PollingScheduler` lives at the
end because its `available()` is always True — every other tier gets
a chance to claim the host first.

`get_scheduler(name)` lets callers (the `--scheduler` CLI flag,
explicit tests) bypass detection. `all_schedulers()` returns one
instance of every class — used by `install.uninstall()` so a host that
switched OS gets clean teardown across tiers.
"""

from .base import Scheduler
from .cron import CronScheduler
from .launchd import LaunchdScheduler
from .polling import PollingScheduler
from .systemd import SystemdUserScheduler


_PRIORITY: list[type[Scheduler]] = [
    LaunchdScheduler,
    SystemdUserScheduler,
    CronScheduler,
    PollingScheduler,
]

_BY_NAME: dict[str, type[Scheduler]] = {
    "launchd": LaunchdScheduler,
    "systemd": SystemdUserScheduler,
    "cron":    CronScheduler,
    "polling": PollingScheduler,
}


def detect_scheduler() -> Scheduler:
    """Return the first scheduler whose `available()` is True.

    PollingScheduler is the always-True backstop, so this never raises
    on any platform.
    """
    for cls in _PRIORITY:
        instance = cls()
        if instance.available():
            return instance
    # Unreachable in practice — PollingScheduler.available() is True.
    return PollingScheduler()


def get_scheduler(name: str) -> Scheduler:
    """Look up a scheduler by name. Raises ValueError on miss with a
    message that lists all four valid names — that error is surfaced
    by the CLI to the user."""
    cls = _BY_NAME.get(name)
    if cls is None:
        raise ValueError(
            f"unknown scheduler {name!r}; choose one of: {sorted(_BY_NAME)}"
        )
    return cls()


def all_schedulers() -> list[Scheduler]:
    """One instance of every class, in priority order."""
    return [cls() for cls in _PRIORITY]


__all__ = [
    "Scheduler",
    "LaunchdScheduler",
    "SystemdUserScheduler",
    "CronScheduler",
    "PollingScheduler",
    "detect_scheduler",
    "get_scheduler",
    "all_schedulers",
]
