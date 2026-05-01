"""B4 Item 4 — `recall uninstall` walks every scheduler so a host that
switched OS still gets clean teardown."""

import io
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

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


def _stub_uninstall(monkeypatch, watcher_result_factory, sidecar_result_factory,
                    hooks_recorder: list[str] | None = None):
    """Replace every scheduler's `uninstall_watcher`/`uninstall_sidecar`
    with mocks that record calls and return canned results.

    Also stubs `install.uninstall_hooks` to a no-op (with optional
    recording) so tests don't mutate the dev machine's real
    `~/.claude/settings.json` / `~/.codex/hooks.json` / `~/.gemini/...`."""
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

    def fake_uninstall_hooks(agents=None):
        if hooks_recorder is not None:
            hooks_recorder.append(f"uninstall_hooks(agents={agents})")
        return 0

    monkeypatch.setattr(install, "uninstall_hooks", fake_uninstall_hooks)

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


# ── F-16: hooks must be removed alongside watchers + sidecars ────────────────


def test_uninstall_calls_uninstall_hooks(monkeypatch):
    """Pre-fix `recall uninstall` walked schedulers but never touched the
    pre-prompt hook entries, so each CLI's settings.json was left with a
    dangling reference to a (now-uninstalled) conversation-memory.sh.
    Lock that uninstall_hooks() runs as part of the walk."""
    hooks_calls: list[str] = []
    _stub_uninstall(
        monkeypatch,
        watcher_result_factory=lambda cls, agent: Result(
            ok=True, message="watcher not installed", path=None,
        ),
        sidecar_result_factory=lambda cls: Result(
            ok=True, message="sidecar not installed", path=None,
        ),
        hooks_recorder=hooks_calls,
    )

    install.uninstall(purge_data=False)

    assert hooks_calls, (
        "uninstall_hooks was never called; F-16 fix missing — see "
        "src/convo_recall/install/__init__.py:uninstall()"
    )
    # agents=None means "walk every CLI", which is what we want for
    # uninstall (clean across whatever subset was wired previously).
    assert "agents=None" in hooks_calls[0], hooks_calls


def test_uninstall_runs_hooks_before_scheduler_walk(monkeypatch):
    """Hooks must be removed BEFORE the package's bundled hook script
    becomes unresolvable (e.g. after a subsequent `pipx uninstall`).
    Capture call ordering so a regression to "hooks at the end" fails."""
    order: list[str] = []

    def fake_uninstall_hooks(agents=None):
        order.append("hooks")
        return 0

    def make_uninstall_watcher(cls):
        def fn(self, agent):
            order.append(f"watcher({cls.__name__},{agent})")
            return Result(ok=True, message="watcher not installed", path=None)
        return fn

    def make_uninstall_sidecar(cls):
        def fn(self):
            order.append(f"sidecar({cls.__name__})")
            return Result(ok=True, message="sidecar not installed", path=None)
        return fn

    for cls in _ALL_CLASSES:
        monkeypatch.setattr(cls, "uninstall_watcher", make_uninstall_watcher(cls))
        monkeypatch.setattr(cls, "uninstall_sidecar", make_uninstall_sidecar(cls))
    monkeypatch.setattr(install, "uninstall_hooks", fake_uninstall_hooks)

    install.uninstall(purge_data=False)

    assert order, "uninstall did nothing"
    assert order[0] == "hooks", (
        f"hooks must be removed first; got order: {order[:3]}…"
    )


# ── F-17/F-18/F-19/F-21: purge-data sweeps logs + runtime dir ───────────────


def test_purge_data_removes_runtime_dir_and_logs(monkeypatch, tmp_path):
    """`recall uninstall --purge-data` must clean up log files (F-18),
    cron backups in the runtime dir (F-19), and the runtime dir itself
    (F-21) — not just the DB. Pre-fix only the DB's data directory was
    removed, leaving stale logs accumulating across reinstall cycles."""
    fake_home = tmp_path
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.setenv("CONVO_RECALL_DB", str(fake_home / ".local/share/convo-recall/conversations.db"))

    # Stub schedulers + uninstall_hooks so the test only exercises the
    # purge_data branch.
    _stub_uninstall(
        monkeypatch,
        watcher_result_factory=lambda cls, agent: Result(
            ok=True, message="watcher not installed", path=None,
        ),
        sidecar_result_factory=lambda cls: Result(
            ok=True, message="sidecar not installed", path=None,
        ),
    )

    # Patch the path helpers to point inside tmp_path
    fake_runtime = fake_home / "Library" / "Caches" / "convo-recall"
    fake_runtime.mkdir(parents=True)
    (fake_runtime / "crontab.bak.1234567890").write_text("* * * * * old\n")
    (fake_runtime / "embed.sock").touch()

    fake_logs = fake_home / "Library" / "Logs"
    fake_logs.mkdir(parents=True)
    (fake_logs / "convo-recall-embed.log").write_text("warmup\n")
    (fake_logs / "convo-recall-ingest-claude.error.log").write_text("err\n")
    (fake_logs / "system.log").write_text("unrelated\n")  # MUST survive

    fake_data = fake_home / ".local" / "share" / "convo-recall"
    fake_data.mkdir(parents=True)
    (fake_data / "conversations.db").write_text("fake-db")
    (fake_data / "config.json").write_text('{"agents":["claude"]}')

    # Re-route the helpers in the purge_data branch
    from convo_recall.install import _paths
    monkeypatch.setattr(_paths, "log_dir", lambda: fake_logs)
    monkeypatch.setattr(_paths, "runtime_dir", lambda: fake_runtime)

    install.uninstall(purge_data=True)

    # F-21: runtime dir gone
    assert not fake_runtime.exists(), "runtime dir should have been removed"
    # F-18: convo-recall log files gone
    assert not (fake_logs / "convo-recall-embed.log").exists()
    assert not (fake_logs / "convo-recall-ingest-claude.error.log").exists()
    # Critical: unrelated files in shared log dir are NOT touched
    assert (fake_logs / "system.log").exists(), (
        "purge_data must not glob-delete unrelated files in ~/Library/Logs"
    )
    # F-20 sanity: data dir gone (DB + config.json)
    assert not fake_data.exists()


def test_purge_data_no_op_when_nothing_to_purge(monkeypatch, tmp_path):
    """If runtime/log/data dirs don't exist, purge_data must not crash."""
    fake_home = tmp_path
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.setenv("CONVO_RECALL_DB", str(fake_home / ".local/share/convo-recall/conversations.db"))

    _stub_uninstall(
        monkeypatch,
        watcher_result_factory=lambda cls, agent: Result(
            ok=True, message="watcher not installed", path=None,
        ),
        sidecar_result_factory=lambda cls: Result(
            ok=True, message="sidecar not installed", path=None,
        ),
    )

    from convo_recall.install import _paths
    monkeypatch.setattr(_paths, "log_dir", lambda: fake_home / "nonexistent-logs")
    monkeypatch.setattr(_paths, "runtime_dir", lambda: fake_home / "nonexistent-runtime")

    # Should not raise
    install.uninstall(purge_data=True)


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
