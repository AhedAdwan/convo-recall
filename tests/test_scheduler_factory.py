"""B4 Item 1 — scheduler factory."""

import platform

import pytest

from convo_recall.install.schedulers import (
    CronScheduler,
    LaunchdScheduler,
    PollingScheduler,
    SystemdUserScheduler,
    all_schedulers,
    detect_scheduler,
    get_scheduler,
)


def test_detect_scheduler_returns_polling_when_others_unavailable(monkeypatch):
    monkeypatch.setattr(LaunchdScheduler, "available", lambda self: False)
    monkeypatch.setattr(SystemdUserScheduler, "available", lambda self: False)
    monkeypatch.setattr(CronScheduler, "available", lambda self: False)
    assert isinstance(detect_scheduler(), PollingScheduler)


def test_detect_scheduler_returns_first_available(monkeypatch):
    monkeypatch.setattr(LaunchdScheduler, "available", lambda self: False)
    monkeypatch.setattr(SystemdUserScheduler, "available", lambda self: True)
    monkeypatch.setattr(CronScheduler, "available", lambda self: True)
    assert isinstance(detect_scheduler(), SystemdUserScheduler)


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS-only")
def test_detect_scheduler_on_macos_returns_launchd():
    assert isinstance(detect_scheduler(), LaunchdScheduler)


@pytest.mark.parametrize(
    "name,cls",
    [
        ("launchd", LaunchdScheduler),
        ("systemd", SystemdUserScheduler),
        ("cron",    CronScheduler),
        ("polling", PollingScheduler),
    ],
)
def test_get_scheduler_named(name, cls):
    assert isinstance(get_scheduler(name), cls)


def test_get_scheduler_unknown_name_lists_choices():
    with pytest.raises(ValueError, match="launchd"):
        get_scheduler("bogus")
    with pytest.raises(ValueError, match="systemd"):
        get_scheduler("bogus")
    with pytest.raises(ValueError, match="cron"):
        get_scheduler("bogus")
    with pytest.raises(ValueError, match="polling"):
        get_scheduler("bogus")


def test_all_schedulers_returns_one_per_class():
    instances = all_schedulers()
    assert len(instances) == 4
    types = [type(s) for s in instances]
    assert types == [LaunchdScheduler, SystemdUserScheduler,
                     CronScheduler, PollingScheduler]
