"""B4 Item 3 — wizard talks to a Scheduler instance and adapts prompts."""

import io
import platform
import sys
from contextlib import redirect_stdout

import pytest

from convo_recall.install import _wizard
from convo_recall.install.schedulers import (
    CronScheduler,
    LaunchdScheduler,
    PollingScheduler,
    SystemdUserScheduler,
)


def _stub_scheduler_install_methods(monkeypatch):
    """Make every scheduler's install_* / uninstall_* a no-op that returns
    a benign Result, so dry_run tests never touch real launchd / systemd."""
    from convo_recall.install.schedulers.base import Result

    def fake_install_watcher(self, *a, **kw):
        return Result(ok=True, message="stubbed", path=None)

    def fake_install_sidecar(self, *a, **kw):
        return Result(ok=True, message="stubbed", path=None)

    for cls in (LaunchdScheduler, SystemdUserScheduler,
                CronScheduler, PollingScheduler):
        monkeypatch.setattr(cls, "install_watcher", fake_install_watcher)
        monkeypatch.setattr(cls, "install_sidecar", fake_install_sidecar)


def test_wizard_picks_polling_when_only_polling_available(monkeypatch):
    monkeypatch.setattr(LaunchdScheduler, "available", lambda self: False)
    monkeypatch.setattr(SystemdUserScheduler, "available", lambda self: False)
    monkeypatch.setattr(CronScheduler, "available", lambda self: False)
    _stub_scheduler_install_methods(monkeypatch)

    buf = io.StringIO()
    with redirect_stdout(buf):
        _wizard.run(non_interactive=True, dry_run=True, scheduler="auto")
    output = buf.getvalue()
    assert "polling (Popen fallback)" in output


def test_wizard_explicit_scheduler_override(monkeypatch):
    _stub_scheduler_install_methods(monkeypatch)
    buf = io.StringIO()
    with redirect_stdout(buf):
        _wizard.run(non_interactive=True, dry_run=True, scheduler="polling")
    output = buf.getvalue()
    assert "polling (Popen fallback)" in output
    assert "launchd (macOS)" not in output


def test_wizard_unknown_scheduler_raises_value_error(monkeypatch):
    with pytest.raises(ValueError, match="bogus"):
        _wizard.run(non_interactive=True, dry_run=True, scheduler="bogus")


def test_wizard_uses_scheduler_consequences_in_step1_prompt(monkeypatch):
    _stub_scheduler_install_methods(monkeypatch)
    buf = io.StringIO()
    with redirect_stdout(buf):
        _wizard.run(non_interactive=True, dry_run=True, scheduler="cron")
    output = buf.getvalue()
    assert "@reboot" in output
    # Launchd's consequence text MUST NOT appear when cron was selected.
    assert "indexed within ~10s" not in output


def test_wizard_systemd_path_asks_about_linger(monkeypatch):
    """Force-pick systemd via explicit name; capture every _ask question."""
    _stub_scheduler_install_methods(monkeypatch)

    asked: list[str] = []

    def recording_ask(question, *, default=True, if_yes=None, if_no=None,
                      non_interactive=False):
        asked.append(question)
        return True

    monkeypatch.setattr(_wizard, "_ask", recording_ask)
    monkeypatch.setattr(SystemdUserScheduler, "available", lambda self: True)

    buf = io.StringIO()
    with redirect_stdout(buf):
        _wizard.run(non_interactive=False, dry_run=True, scheduler="systemd")

    assert any(q.startswith("Keep watchers running when logged out") for q in asked), (
        f"linger question missing from {asked}"
    )


def test_wizard_macos_path_does_not_ask_about_linger(monkeypatch):
    _stub_scheduler_install_methods(monkeypatch)
    asked: list[str] = []

    def recording_ask(question, *, default=True, if_yes=None, if_no=None,
                      non_interactive=False):
        asked.append(question)
        return True

    monkeypatch.setattr(_wizard, "_ask", recording_ask)
    monkeypatch.setattr(LaunchdScheduler, "available", lambda self: True)

    buf = io.StringIO()
    with redirect_stdout(buf):
        _wizard.run(non_interactive=False, dry_run=True, scheduler="launchd")

    assert not any("linger" in q for q in asked), (
        f"linger question should not fire for launchd; got {asked}"
    )
