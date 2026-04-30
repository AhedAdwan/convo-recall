"""Item 1 — install/_paths.py XDG-aware path resolution."""

import platform
from pathlib import Path

import pytest

from convo_recall.install import _paths


def test_is_macos_matches_platform():
    assert _paths.is_macos() is (platform.system() == "Darwin")
    assert _paths.is_linux() is (platform.system() == "Linux")


@pytest.mark.skipif(not _paths.is_macos(), reason="macOS-only path layout")
def test_scheduler_unit_dir_macos():
    assert _paths.scheduler_unit_dir() == Path.home() / "Library" / "LaunchAgents"


@pytest.mark.skipif(not _paths.is_macos(), reason="macOS-only path layout")
def test_log_dir_macos():
    assert _paths.log_dir() == Path.home() / "Library" / "Logs"


def test_scheduler_unit_dir_linux_honours_xdg_config_home(monkeypatch, tmp_path):
    """Force the Linux branch — even when running on macOS — by pinning
    `is_macos` to False. This keeps the test useful on every CI matrix
    leg (mac and linux), since the rule we care about is that XDG vars
    are read whenever Linux is the active platform."""
    monkeypatch.setattr(_paths, "is_macos", lambda: False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert _paths.scheduler_unit_dir() == tmp_path / "cfg" / "systemd" / "user"


def test_scheduler_unit_dir_linux_falls_back_to_dot_config(monkeypatch):
    monkeypatch.setattr(_paths, "is_macos", lambda: False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert _paths.scheduler_unit_dir() == Path.home() / ".config" / "systemd" / "user"


def test_log_dir_linux_honours_xdg_state_home(monkeypatch, tmp_path):
    monkeypatch.setattr(_paths, "is_macos", lambda: False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert _paths.log_dir() == tmp_path / "state" / "convo-recall"


def test_runtime_dir_linux_honours_xdg_runtime_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(_paths, "is_macos", lambda: False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    assert _paths.runtime_dir() == tmp_path / "run" / "convo-recall"


def test_runtime_dir_linux_falls_back_to_tmp(monkeypatch):
    monkeypatch.setattr(_paths, "is_macos", lambda: False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    expected = Path(f"/tmp/convo-recall-{__import__('os').getuid()}")
    assert _paths.runtime_dir() == expected
