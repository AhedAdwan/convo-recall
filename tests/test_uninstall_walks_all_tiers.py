"""B4 Item 4 — `recall uninstall` walks every scheduler so a host that
switched OS still gets clean teardown."""

import io
import sys
from contextlib import redirect_stderr, redirect_stdout

import pytest

from convo_recall import install
from convo_recall.install.schedulers import (
    CronScheduler,
    LaunchdScheduler,
    PollingScheduler,
    SystemdUserScheduler,
)
from convo_recall.install.schedulers.base import Result


_ALL_CLASSES = (LaunchdScheduler, SystemdUserScheduler,
                CronScheduler, PollingScheduler)


def _stub_uninstall(monkeypatch, watcher_result_factory, sidecar_result_factory):
    """Replace every scheduler's `uninstall_watcher`/`uninstall_sidecar`
    with mocks that record calls and return canned results."""
    watcher_calls: list[tuple[type, str]] = []
    sidecar_calls: list[type] = []

    def make_uninstall_watcher(cls):
        def fn(self, agent):
            watcher_calls.append((cls, agent))
            return watcher_result_factory(cls, agent)
        return fn

    def make_uninstall_sidecar(cls):
        def fn(self):
            sidecar_calls.append(cls)
            return sidecar_result_factory(cls)
        return fn

    for cls in _ALL_CLASSES:
        monkeypatch.setattr(cls, "uninstall_watcher", make_uninstall_watcher(cls))
        monkeypatch.setattr(cls, "uninstall_sidecar", make_uninstall_sidecar(cls))

    return watcher_calls, sidecar_calls


def test_uninstall_calls_uninstall_watcher_on_every_scheduler(monkeypatch):
    watcher_calls, _ = _stub_uninstall(
        monkeypatch,
        watcher_result_factory=lambda cls, agent: Result(
            ok=True, message="watcher not installed", path=None,
        ),
        sidecar_result_factory=lambda cls: Result(
            ok=True, message="sidecar not installed", path=None,
        ),
    )
    install.uninstall(purge_data=False)

    seen_classes = {cls for cls, _ in watcher_calls}
    assert seen_classes == set(_ALL_CLASSES), seen_classes
    for cls in _ALL_CLASSES:
        per_class = [a for c, a in watcher_calls if c is cls]
        assert sorted(per_class) == ["claude", "codex", "gemini"]


def test_uninstall_calls_uninstall_sidecar_on_every_scheduler(monkeypatch):
    _, sidecar_calls = _stub_uninstall(
        monkeypatch,
        watcher_result_factory=lambda cls, agent: Result(
            ok=True, message="watcher not installed", path=None,
        ),
        sidecar_result_factory=lambda cls: Result(
            ok=True, message="sidecar not installed", path=None,
        ),
    )
    install.uninstall(purge_data=False)
    assert set(sidecar_calls) == set(_ALL_CLASSES)


def test_uninstall_does_not_print_no_op_results(monkeypatch):
    _stub_uninstall(
        monkeypatch,
        watcher_result_factory=lambda cls, agent: Result(
            ok=True, message="watcher not installed", path=None,
        ),
        sidecar_result_factory=lambda cls: Result(
            ok=True, message="sidecar not installed", path=None,
        ),
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        install.uninstall(purge_data=False)
    output = buf.getvalue()
    assert "✅" not in output, (
        f"no-op tiers should not surface success lines; got:\n{output}"
    )


def test_uninstall_surfaces_failures(monkeypatch):
    def watcher_factory(cls, agent):
        if cls is LaunchdScheduler and agent == "claude":
            return Result(ok=False, message="bootout failed", path=None)
        return Result(ok=True, message="watcher not installed", path=None)

    _stub_uninstall(
        monkeypatch,
        watcher_result_factory=watcher_factory,
        sidecar_result_factory=lambda cls: Result(
            ok=True, message="sidecar not installed", path=None,
        ),
    )

    out_buf = io.StringIO()
    err_buf = io.StringIO()
    with redirect_stdout(out_buf), redirect_stderr(err_buf):
        install.uninstall(purge_data=False)

    assert "bootout failed" in err_buf.getvalue()
