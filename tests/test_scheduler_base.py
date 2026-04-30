"""Item 2 — install/schedulers/base.py: Scheduler ABC + Result dataclass."""

from pathlib import Path

import pytest

from convo_recall.install.schedulers.base import Result, Scheduler


def test_scheduler_cannot_be_instantiated_directly():
    with pytest.raises(TypeError):
        Scheduler()  # type: ignore[abstract]


def test_minimal_subclass_can_be_instantiated():
    class _Stub(Scheduler):
        def available(self): return True
        def install_watcher(self, agent, recall_bin, watch_dir, db_path,
                            sock_path, config_path, log_dir):
            return Result(ok=True, message="stub")
        def uninstall_watcher(self, agent): return Result(ok=True, message="stub")
        def install_sidecar(self, recall_bin, sock_path, log_dir):
            return Result(ok=True, message="stub")
        def uninstall_sidecar(self): return Result(ok=True, message="stub")
        def describe(self): return "stub"
        def consequence_yes(self): return "y"
        def consequence_no(self): return "n"

    s = _Stub()
    assert s.available() is True
    assert s.describe() == "stub"


def test_result_path_defaults_to_none():
    r = Result(ok=True, message="x")
    assert r.path is None
    r2 = Result(ok=False, message="x", path=Path("/tmp/y"))
    assert r2.path == Path("/tmp/y")


def test_abstract_methods_listed():
    expected = {
        "available",
        "install_watcher", "uninstall_watcher",
        "install_sidecar", "uninstall_sidecar",
        "describe", "consequence_yes", "consequence_no",
    }
    assert set(Scheduler.__abstractmethods__) == expected
