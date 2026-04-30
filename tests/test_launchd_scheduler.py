"""Item 3 — install/schedulers/launchd.py: LaunchdScheduler."""

import platform
import plistlib

import pytest

from convo_recall import install as _install
from convo_recall.install.schedulers.launchd import LaunchdScheduler


def test_available_matches_platform():
    assert LaunchdScheduler().available() is (platform.system() == "Darwin")


def test_describe():
    assert LaunchdScheduler().describe() == "launchd (macOS)"


def test_install_watcher_writes_plist_with_correct_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(_install, "LAUNCHAGENTS", tmp_path / "LaunchAgents")
    monkeypatch.setattr(_install, "PROJECTS_DIR", tmp_path / "claude" / "projects")
    monkeypatch.setattr(_install, "GEMINI_TMP", tmp_path / "gemini" / "tmp")
    monkeypatch.setattr(_install, "CODEX_SESSIONS", tmp_path / "codex")
    monkeypatch.setattr(LaunchdScheduler, "_launchctl_load", lambda self, p: True)

    scheduler = LaunchdScheduler()
    result = scheduler.install_watcher(
        agent="gemini",
        recall_bin="/usr/local/bin/recall",
        watch_dir=str(tmp_path / "gemini" / "tmp"),
        db_path="/db",
        sock_path="/sock",
        config_path="/cfg",
        log_dir="/logs",
    )

    assert result.ok
    assert result.path is not None
    plist = plistlib.loads(result.path.read_bytes())
    assert plist["Label"] == "com.convo-recall.ingest.gemini"
    assert plist["WatchPaths"] == [str(tmp_path / "gemini" / "tmp")]
    assert plist["ProgramArguments"] == [
        "/usr/local/bin/recall", "ingest", "--agent", "gemini",
    ]
    assert plist["EnvironmentVariables"]["CONVO_RECALL_CONFIG"] == "/cfg"
    assert plist["StandardOutPath"] == "/logs/convo-recall-ingest-gemini.log"


def test_install_sidecar_writes_plist_with_keepalive(tmp_path, monkeypatch):
    monkeypatch.setattr(_install, "LAUNCHAGENTS", tmp_path / "LaunchAgents")
    monkeypatch.setattr(LaunchdScheduler, "_launchctl_load", lambda self, p: True)

    scheduler = LaunchdScheduler()
    result = scheduler.install_sidecar(
        recall_bin="/usr/local/bin/recall",
        sock_path="/sock",
        log_dir="/logs",
    )

    assert result.ok
    plist = plistlib.loads(result.path.read_bytes())
    assert plist["Label"] == "com.convo-recall.embed"
    assert plist["KeepAlive"] is True
    assert plist["ProgramArguments"] == [
        "/usr/local/bin/recall", "serve", "--sock", "/sock",
    ]


def test_install_watcher_returns_failure_when_launchctl_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(_install, "LAUNCHAGENTS", tmp_path / "LaunchAgents")
    monkeypatch.setattr(_install, "PROJECTS_DIR", tmp_path / "claude" / "projects")
    monkeypatch.setattr(_install, "GEMINI_TMP", tmp_path / "gemini" / "tmp")
    monkeypatch.setattr(_install, "CODEX_SESSIONS", tmp_path / "codex")
    monkeypatch.setattr(LaunchdScheduler, "_launchctl_load", lambda self, p: False)

    scheduler = LaunchdScheduler()
    result = scheduler.install_watcher(
        agent="claude",
        recall_bin="/usr/local/bin/recall",
        watch_dir=str(tmp_path),
        db_path="/db",
        sock_path="/sock",
        config_path="/cfg",
        log_dir="/logs",
    )
    assert result.ok is False
    assert "load failed" in result.message
