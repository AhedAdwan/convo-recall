"""B1/B2/B3 — schedulers/{polling,systemd,cron}.py.

All tests stub external commands (`os.kill`, `subprocess.Popen`,
`subprocess.run`) so the suite is platform-agnostic and never spawns
a real long-lived child or modifies a real crontab.
"""

import os
import shutil
import signal
import subprocess
from pathlib import Path

import pytest

from convo_recall.install import _paths
from convo_recall.install.schedulers.cron import CronScheduler
from convo_recall.install.schedulers.polling import PollingScheduler
from convo_recall.install.schedulers.systemd import SystemdUserScheduler


# ─────────────────────────────────────────────────────────────────────────────
# B1 — PollingScheduler
# ─────────────────────────────────────────────────────────────────────────────


class _FakePopen:
    last: "_FakePopen | None" = None
    captured_kwargs: dict = {}

    def __init__(self, argv, stdout=None, stderr=None,
                 start_new_session=False, close_fds=False, **kwargs):
        self.argv = argv
        type(self).captured_kwargs = {
            "stdout": stdout,
            "stderr": stderr,
            "start_new_session": start_new_session,
            "close_fds": close_fds,
        }
        self.pid = 12345
        type(self).last = self


def _force_runtime_dir(monkeypatch, path):
    monkeypatch.setattr(
        "convo_recall.install.schedulers.polling.runtime_dir",
        lambda: path,
    )


def test_polling_scheduler_available_always_true():
    assert PollingScheduler().available() is True


def test_polling_scheduler_describe():
    assert PollingScheduler().describe() == "polling (Popen fallback)"


def test_polling_scheduler_install_watcher_spawns_and_writes_pid(tmp_path, monkeypatch):
    _force_runtime_dir(monkeypatch, tmp_path / "run")
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    # Ensure no leftover live PID is detected for the (non-existent) PID 12345
    monkeypatch.setattr(PollingScheduler, "_pid_alive",
                        staticmethod(lambda pid: False))

    log_dir = tmp_path / "logs"
    scheduler = PollingScheduler()
    result = scheduler.install_watcher(
        agent="claude",
        recall_bin="/usr/local/bin/recall",
        watch_dir=str(tmp_path / "watch"),
        db_path="/db",
        sock_path="/sock",
        config_path="/cfg",
        log_dir=str(log_dir),
    )

    pid_path = (tmp_path / "run") / "watch.pid"
    assert result.ok
    assert pid_path.read_text() == "12345"
    assert _FakePopen.last.argv == ["/usr/local/bin/recall", "watch"]
    assert _FakePopen.captured_kwargs["start_new_session"] is True


def test_polling_scheduler_install_watcher_idempotent(tmp_path, monkeypatch):
    _force_runtime_dir(monkeypatch, tmp_path / "run")
    pid_dir = tmp_path / "run"
    pid_dir.mkdir()
    (pid_dir / "watch.pid").write_text("99999")
    monkeypatch.setattr(PollingScheduler, "_pid_alive",
                        staticmethod(lambda pid: True))

    call_count = {"n": 0}

    def fake_popen(*a, **kw):
        call_count["n"] += 1
        return _FakePopen(*a, **kw)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    scheduler = PollingScheduler()
    result = scheduler.install_watcher(
        agent="claude", recall_bin="/r", watch_dir="/w",
        db_path="/db", sock_path="/s", config_path="/c",
        log_dir=str(tmp_path / "logs"),
    )
    assert result.ok
    assert "already" in result.message
    assert call_count["n"] == 0


def test_polling_scheduler_stale_pid_overwritten(tmp_path, monkeypatch):
    _force_runtime_dir(monkeypatch, tmp_path / "run")
    pid_dir = tmp_path / "run"
    pid_dir.mkdir()
    (pid_dir / "watch.pid").write_text("99999")
    monkeypatch.setattr(PollingScheduler, "_pid_alive",
                        staticmethod(lambda pid: False))
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)

    scheduler = PollingScheduler()
    result = scheduler.install_watcher(
        agent="claude", recall_bin="/r", watch_dir="/w",
        db_path="/db", sock_path="/s", config_path="/c",
        log_dir=str(tmp_path / "logs"),
    )
    assert result.ok
    assert (pid_dir / "watch.pid").read_text() == "12345"


def test_polling_scheduler_uninstall_sends_sigterm(tmp_path, monkeypatch):
    _force_runtime_dir(monkeypatch, tmp_path / "run")
    pid_dir = tmp_path / "run"
    pid_dir.mkdir()
    (pid_dir / "watch.pid").write_text("12345")

    sent = []
    alive_calls = {"n": 0}

    def fake_kill(pid, sig):
        sent.append((pid, sig))

    def fake_alive(pid):
        # Process is alive on the first liveness check (before SIGTERM),
        # then dies during the grace poll.
        alive_calls["n"] += 1
        return alive_calls["n"] <= 1

    monkeypatch.setattr(os, "kill", fake_kill)
    monkeypatch.setattr(PollingScheduler, "_pid_alive",
                        staticmethod(fake_alive))

    scheduler = PollingScheduler()
    result = scheduler.uninstall_watcher(agent="claude")

    assert result.ok
    assert (12345, signal.SIGTERM) in sent
    assert all(s != signal.SIGKILL for _, s in sent)
    assert not (pid_dir / "watch.pid").exists()


def test_polling_scheduler_uninstall_escalates_to_sigkill(tmp_path, monkeypatch):
    _force_runtime_dir(monkeypatch, tmp_path / "run")
    pid_dir = tmp_path / "run"
    pid_dir.mkdir()
    (pid_dir / "watch.pid").write_text("12345")

    sent = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(PollingScheduler, "_pid_alive",
                        staticmethod(lambda pid: True))
    # Speed up the grace loop so the test isn't slow.
    monkeypatch.setattr(
        "convo_recall.install.schedulers.polling._GRACE_SECONDS", 0.05
    )
    monkeypatch.setattr(
        "convo_recall.install.schedulers.polling._POLL_INTERVAL", 0.01
    )

    scheduler = PollingScheduler()
    result = scheduler.uninstall_watcher(agent="claude")

    assert result.ok
    assert (12345, signal.SIGTERM) in sent
    assert (12345, signal.SIGKILL) in sent
    assert not (pid_dir / "watch.pid").exists()


def test_polling_scheduler_log_redirection(tmp_path, monkeypatch):
    _force_runtime_dir(monkeypatch, tmp_path / "run")
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(PollingScheduler, "_pid_alive",
                        staticmethod(lambda pid: False))

    log_dir = tmp_path / "logs"
    PollingScheduler().install_watcher(
        agent="claude", recall_bin="/r", watch_dir="/w",
        db_path="/db", sock_path="/s", config_path="/c",
        log_dir=str(log_dir),
    )
    # The fake Popen captured the file object; we check it points at watch.log
    fobj = _FakePopen.captured_kwargs["stdout"]
    assert Path(fobj.name) == log_dir / "watch.log"


def test_polling_scheduler_install_sidecar(tmp_path, monkeypatch):
    _force_runtime_dir(monkeypatch, tmp_path / "run")
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(PollingScheduler, "_pid_alive",
                        staticmethod(lambda pid: False))

    result = PollingScheduler().install_sidecar(
        recall_bin="/usr/local/bin/recall",
        sock_path="/sock",
        log_dir=str(tmp_path / "logs"),
    )
    assert result.ok
    assert _FakePopen.last.argv == [
        "/usr/local/bin/recall", "serve", "--sock", "/sock",
    ]
    assert (tmp_path / "run" / "embed.pid").read_text() == "12345"


# ─────────────────────────────────────────────────────────────────────────────
# B2 — SystemdUserScheduler
# ─────────────────────────────────────────────────────────────────────────────


class _RunRecorder:
    """Deterministic stub for `subprocess.run` — yields canned responses
    based on the leading argv tokens, and records each call."""

    def __init__(self, responses: dict[tuple[str, ...], subprocess.CompletedProcess]):
        self.responses = responses
        self.calls: list[list[str]] = []

    def __call__(self, argv, *args, **kwargs):
        self.calls.append(list(argv))
        # Match by prefix tokens, longest-first.
        for prefix in sorted(self.responses, key=len, reverse=True):
            if tuple(argv[: len(prefix)]) == prefix:
                return self.responses[prefix]
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


def _ok(stdout="", stderr=""):
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr=stderr)


def _err(returncode=1, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def test_systemd_available_true_when_systemctl_user_running(monkeypatch):
    rec = _RunRecorder({
        ("systemctl", "--user", "--version"): _ok(stdout="systemd 254"),
        ("systemctl", "--user", "is-system-running"): _ok(stdout="running\n"),
    })
    monkeypatch.setattr(subprocess, "run", rec)
    assert SystemdUserScheduler().available() is True


def test_systemd_available_false_when_systemctl_missing(monkeypatch):
    def boom(*a, **kw):
        raise FileNotFoundError("no systemctl")
    monkeypatch.setattr(subprocess, "run", boom)
    assert SystemdUserScheduler().available() is False


def test_systemd_available_false_when_offline(monkeypatch):
    rec = _RunRecorder({
        ("systemctl", "--user", "--version"): _ok(stdout="systemd 254"),
        ("systemctl", "--user", "is-system-running"): _err(returncode=1, stdout="offline\n"),
    })
    monkeypatch.setattr(subprocess, "run", rec)
    assert SystemdUserScheduler().available() is False


def test_systemd_available_false_on_timeout(monkeypatch):
    def boom(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="systemctl", timeout=2)
    monkeypatch.setattr(subprocess, "run", boom)
    assert SystemdUserScheduler().available() is False


@pytest.mark.skipif(not shutil.which("systemd-analyze"),
                    reason="systemd-analyze not on PATH (likely macOS)")
def test_systemd_service_unit_passes_systemd_analyze_verify(tmp_path):
    s = SystemdUserScheduler()
    unit = s._service_unit(
        description="probe",
        exec_start="/bin/true",
        env={"FOO": "bar"},
        unit_type="oneshot",
    )
    p = tmp_path / "com.convo-recall.probe.service"
    p.write_text(unit)
    r = subprocess.run(["systemd-analyze", "verify", "--no-pager", str(p)],
                       capture_output=True, text=True)
    assert r.returncode == 0
    assert (r.stderr or "").strip() == ""


@pytest.mark.skipif(not shutil.which("systemd-analyze"),
                    reason="systemd-analyze not on PATH (likely macOS)")
def test_systemd_path_unit_passes_systemd_analyze_verify(tmp_path):
    s = SystemdUserScheduler()
    service = s._service_unit(
        description="probe", exec_start="/bin/true", env={}, unit_type="oneshot",
    )
    path_unit = s._path_unit(
        description="probe path",
        target_unit="com.convo-recall.probe.service",
        watch_dir="/tmp",
    )
    sp = tmp_path / "com.convo-recall.probe.service"
    pp = tmp_path / "com.convo-recall.probe.path"
    sp.write_text(service)
    pp.write_text(path_unit)
    r = subprocess.run(
        ["systemd-analyze", "verify", "--no-pager", str(pp)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0
    assert (r.stderr or "").strip() == ""


def test_systemd_install_watcher_writes_two_files_and_calls_daemon_reload(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        "convo_recall.install.schedulers.systemd.scheduler_unit_dir",
        lambda: tmp_path / "units",
    )
    rec = _RunRecorder({
        ("systemd-analyze", "verify"): _ok(),
        ("systemctl", "--user", "daemon-reload"): _ok(),
        ("systemctl", "--user", "enable", "--now"): _ok(),
    })
    monkeypatch.setattr(subprocess, "run", rec)

    s = SystemdUserScheduler()
    result = s.install_watcher(
        agent="claude",
        recall_bin="/usr/local/bin/recall",
        watch_dir="/home/u/.claude/projects",
        db_path="/db", sock_path="/sock", config_path="/cfg",
        log_dir="/logs",
    )

    unit_dir = tmp_path / "units"
    service_path = unit_dir / "com.convo-recall.ingest.claude.service"
    path_path = unit_dir / "com.convo-recall.ingest.claude.path"

    assert result.ok
    assert service_path.exists() and path_path.exists()

    # Order assertion: verify → daemon-reload → enable --now
    cmds = [tuple(c[:3]) for c in rec.calls]
    reload_idx = cmds.index(("systemctl", "--user", "daemon-reload"))
    enable_idx = next(i for i, c in enumerate(rec.calls)
                      if c[:4] == ["systemctl", "--user", "enable", "--now"])
    verify_idx = next(i for i, c in enumerate(rec.calls)
                      if c[:2] == ["systemd-analyze", "verify"])
    assert verify_idx < reload_idx < enable_idx


def test_systemd_uninstall_watcher_removes_both_files(tmp_path, monkeypatch):
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    monkeypatch.setattr(
        "convo_recall.install.schedulers.systemd.scheduler_unit_dir",
        lambda: unit_dir,
    )
    (unit_dir / "com.convo-recall.ingest.claude.service").write_text("")
    (unit_dir / "com.convo-recall.ingest.claude.path").write_text("")

    rec = _RunRecorder({})
    monkeypatch.setattr(subprocess, "run", rec)

    result = SystemdUserScheduler().uninstall_watcher(agent="claude")

    assert result.ok
    assert not (unit_dir / "com.convo-recall.ingest.claude.service").exists()
    assert not (unit_dir / "com.convo-recall.ingest.claude.path").exists()
    assert any(c[:3] == ["systemctl", "--user", "daemon-reload"] for c in rec.calls)


def test_systemd_unit_uses_literal_percent_h_for_home(tmp_path, monkeypatch):
    """`%h` is a systemd specifier expanded at unit-load time. We must
    NOT pre-resolve `Path.home()` in the generated unit text — otherwise
    units written on one user's account won't be portable, and lingering
    invocations may run with the wrong HOME context."""
    monkeypatch.setattr(
        "convo_recall.install.schedulers.systemd.scheduler_unit_dir",
        lambda: tmp_path / "units",
    )
    rec = _RunRecorder({
        ("systemd-analyze", "verify"): _ok(),
        ("systemctl", "--user", "daemon-reload"): _ok(),
        ("systemctl", "--user", "enable", "--now"): _ok(),
    })
    monkeypatch.setattr(subprocess, "run", rec)

    s = SystemdUserScheduler()
    s.install_watcher(
        agent="claude",
        recall_bin="/usr/local/bin/recall",
        watch_dir="%h/.claude/projects",  # caller passes the literal
        db_path="%h/.local/share/convo-recall/db.sqlite",
        sock_path="%h/.local/share/convo-recall/embed.sock",
        config_path="%h/.config/convo-recall/config.json",
        log_dir="%h/.local/state/convo-recall",
    )

    service = (tmp_path / "units" / "com.convo-recall.ingest.claude.service").read_text()
    path_unit = (tmp_path / "units" / "com.convo-recall.ingest.claude.path").read_text()

    assert "%h" in service
    assert "%h" in path_unit
    # And `Path.home()`'s literal value must not have leaked into either file.
    assert str(Path.home()) not in service
    assert str(Path.home()) not in path_unit


def test_systemd_enable_linger_returns_failure_when_loginctl_errors(monkeypatch):
    rec = _RunRecorder({
        ("loginctl", "enable-linger"): _err(returncode=1, stderr="permission denied"),
    })
    monkeypatch.setattr(subprocess, "run", rec)
    monkeypatch.setenv("USER", "tester")

    result = SystemdUserScheduler().enable_linger()
    assert result.ok is False
    assert "loginctl enable-linger" in result.message


def test_systemd_enable_linger_falls_back_to_getpass_when_USER_unset(monkeypatch):
    """Regression: docker exec / cron / fresh systemd units don't export USER.
    Without the getpass fallback, linger silently fails with "USER not set",
    leaving watchers vulnerable to dying at session end. The fix uses the
    pwd database via UID — works without any env vars."""
    import getpass
    monkeypatch.delenv("USER", raising=False)
    monkeypatch.setattr(getpass, "getuser", lambda: "from-pwd")

    rec = _RunRecorder({
        ("loginctl", "enable-linger"): _ok(),
    })
    monkeypatch.setattr(subprocess, "run", rec)

    result = SystemdUserScheduler().enable_linger()
    assert result.ok is True, f"expected success, got: {result.message}"
    assert "from-pwd" in result.message
    # Verify loginctl was actually called with the pwd-derived username.
    call = next(c for c in rec.calls if c[0] == "loginctl")
    assert "from-pwd" in call


def test_systemd_enable_linger_surfaces_clear_error_when_no_user_resolvable(monkeypatch):
    """If both USER env AND getpass.getuser() fail, surface a useful message."""
    import getpass
    monkeypatch.delenv("USER", raising=False)
    def _raise_keyerror():
        raise KeyError("no pwd entry")
    monkeypatch.setattr(getpass, "getuser", _raise_keyerror)

    result = SystemdUserScheduler().enable_linger()
    assert result.ok is False
    assert "could not derive current user" in result.message


def test_systemd_install_watcher_fails_loud_when_verify_warns(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "convo_recall.install.schedulers.systemd.scheduler_unit_dir",
        lambda: tmp_path / "units",
    )
    rec = _RunRecorder({
        ("systemd-analyze", "verify"): _err(returncode=0, stderr="warning: weird unit"),
    })
    monkeypatch.setattr(subprocess, "run", rec)

    result = SystemdUserScheduler().install_watcher(
        agent="claude", recall_bin="/r", watch_dir="/w",
        db_path="/db", sock_path="/s", config_path="/c",
        log_dir="/l",
    )
    assert result.ok is False
    assert "systemd-analyze verify failed" in result.message


# ─────────────────────────────────────────────────────────────────────────────
# B3 — CronScheduler
# ─────────────────────────────────────────────────────────────────────────────


class _CrontabFake:
    """Stub `crontab -l` (read) and `crontab -` (write). The current
    crontab content is held in `state["crontab"]`."""

    def __init__(self, initial: str = ""):
        self.state = {"crontab": initial}
        self.write_calls: list[str] = []
        self.read_calls = 0

    def __call__(self, argv, *args, capture_output=False, text=False,
                 input=None, **kwargs):
        if argv[:2] == ["crontab", "-l"]:
            self.read_calls += 1
            content = self.state["crontab"]
            if not content:
                return subprocess.CompletedProcess(
                    argv, 1, stdout="", stderr="no crontab for user",
                )
            return subprocess.CompletedProcess(argv, 0, stdout=content, stderr="")
        if argv[:2] == ["crontab", "-"]:
            self.write_calls.append(input or "")
            self.state["crontab"] = input or ""
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


def _force_cron_runtime_dir(monkeypatch, path):
    monkeypatch.setattr(
        "convo_recall.install.schedulers.cron.runtime_dir",
        lambda: path,
    )


def test_cron_available_when_crontab_exits_zero(monkeypatch):
    fake = _CrontabFake(initial="0 * * * * echo hi\n")
    monkeypatch.setattr(subprocess, "run", fake)
    assert CronScheduler().available() is True


def test_cron_available_when_crontab_exits_one(monkeypatch):
    fake = _CrontabFake(initial="")
    monkeypatch.setattr(subprocess, "run", fake)
    assert CronScheduler().available() is True


def test_cron_unavailable_when_crontab_missing(monkeypatch):
    def boom(*a, **kw):
        raise FileNotFoundError("no crontab")
    monkeypatch.setattr(subprocess, "run", boom)
    assert CronScheduler().available() is False


def test_cron_install_watcher_appends_tagged_line(tmp_path, monkeypatch):
    _force_cron_runtime_dir(monkeypatch, tmp_path / "run")
    fake = _CrontabFake(initial="# user line\n0 * * * * echo hi\n")
    monkeypatch.setattr(subprocess, "run", fake)

    result = CronScheduler().install_watcher(
        agent="claude", recall_bin="/usr/local/bin/recall",
        watch_dir="/w", db_path="/db", sock_path="/s",
        config_path="/c", log_dir="/var/log",
    )
    assert result.ok
    assert len(fake.write_calls) == 1
    written = fake.write_calls[0]
    assert written.startswith("# user line\n0 * * * * echo hi\n")
    last_line = written.rstrip("\n").splitlines()[-1]
    assert last_line.endswith("# convo-recall:watch")
    assert "@reboot /usr/local/bin/recall watch" in last_line


def test_cron_install_watcher_idempotent_second_call(tmp_path, monkeypatch):
    _force_cron_runtime_dir(monkeypatch, tmp_path / "run")
    fake = _CrontabFake(initial="")
    monkeypatch.setattr(subprocess, "run", fake)

    s = CronScheduler()
    args = dict(
        agent="claude", recall_bin="/r", watch_dir="/w",
        db_path="/db", sock_path="/s", config_path="/c", log_dir="/l",
    )
    s.install_watcher(**args)
    s.install_watcher(**args)

    assert len(fake.write_calls) == 1  # second call is a no-op
    written = fake.state["crontab"]
    assert written.count("# convo-recall:watch") == 1


def test_cron_install_sidecar_appends_embed_line(tmp_path, monkeypatch):
    _force_cron_runtime_dir(monkeypatch, tmp_path / "run")
    fake = _CrontabFake(initial="")
    monkeypatch.setattr(subprocess, "run", fake)

    result = CronScheduler().install_sidecar(
        recall_bin="/r", sock_path="/sock", log_dir="/l",
    )
    assert result.ok
    written = fake.write_calls[0]
    assert "# convo-recall:embed" in written
    assert "@reboot nohup /r serve --sock /sock" in written


def test_cron_uninstall_removes_only_tagged_line(tmp_path, monkeypatch):
    _force_cron_runtime_dir(monkeypatch, tmp_path / "run")
    initial = (
        "# user line\n"
        "0 * * * * echo hi\n"
        "@reboot /usr/local/bin/recall watch >> /l/watch.log 2>&1  # convo-recall:watch\n"
    )
    fake = _CrontabFake(initial=initial)
    monkeypatch.setattr(subprocess, "run", fake)

    result = CronScheduler().uninstall_watcher(agent="claude")
    assert result.ok
    written = fake.state["crontab"]
    assert "# convo-recall:watch" not in written
    assert "# user line" in written
    assert "0 * * * * echo hi" in written


def test_cron_uninstall_no_op_message_matches_silence_filter(tmp_path, monkeypatch):
    """F-15: when there's nothing in the crontab to remove, the message must
    contain one of the substrings the install.uninstall() `_surface()` filter
    treats as a noop ("not installed", "nothing to remove", "already") — else
    cron's four no-op tiers print four ✅ lines on every uninstall on a host
    that's never used cron."""
    _force_cron_runtime_dir(monkeypatch, tmp_path / "run")
    fake = _CrontabFake(initial="# only user lines\n0 * * * * echo hi\n")
    monkeypatch.setattr(subprocess, "run", fake)

    NOOP_SUBSTRINGS = ("not installed", "nothing to remove", "already")

    for agent in ("claude", "gemini", "codex"):
        result = CronScheduler().uninstall_watcher(agent=agent)
        assert result.ok
        assert any(sub in result.message for sub in NOOP_SUBSTRINGS), (
            f"cron uninstall_watcher noop message must match the silence "
            f"filter; got: {result.message!r}"
        )

    sidecar_result = CronScheduler().uninstall_sidecar()
    assert sidecar_result.ok
    assert any(sub in sidecar_result.message for sub in NOOP_SUBSTRINGS), (
        f"cron uninstall_sidecar noop message must match silence filter; "
        f"got: {sidecar_result.message!r}"
    )


def test_cron_uninstall_preserves_user_lines_with_convo_recall_substring(
    tmp_path, monkeypatch,
):
    _force_cron_runtime_dir(monkeypatch, tmp_path / "run")
    user_line = (
        "0 * * * * touch /tmp/convo-recall-test  # user note about convo-recall"
    )
    initial = (
        f"{user_line}\n"
        "@reboot /usr/local/bin/recall watch >> /l/watch.log 2>&1  # convo-recall:watch\n"
    )
    fake = _CrontabFake(initial=initial)
    monkeypatch.setattr(subprocess, "run", fake)

    CronScheduler().uninstall_watcher(agent="claude")
    written = fake.state["crontab"]
    assert user_line in written  # substring presence MUST NOT be filtered
    assert "convo-recall:watch" not in written


def test_cron_writes_backup_before_modification(tmp_path, monkeypatch):
    bak_dir = tmp_path / "run"
    _force_cron_runtime_dir(monkeypatch, bak_dir)
    fake = _CrontabFake(initial="# pre-existing\n")
    monkeypatch.setattr(subprocess, "run", fake)

    CronScheduler().install_watcher(
        agent="claude", recall_bin="/r", watch_dir="/w",
        db_path="/db", sock_path="/s", config_path="/c", log_dir="/l",
    )
    backups = list(bak_dir.glob("crontab.bak.*"))
    assert backups, "backup file not written"
    assert backups[0].read_text() == "# pre-existing\n"


# ── F-14: SystemdUserScheduler.uninstall_* must be safe on macOS ─────────────


def test_systemd_uninstall_watcher_no_op_when_units_absent(tmp_path, monkeypatch):
    """`recall uninstall` walks all_schedulers() including SystemdUserScheduler
    on macOS. Without the guard, systemctl FileNotFoundError crashed the
    whole walk after launchd uninstalled successfully. Early-return
    when no unit files exist for the agent."""
    monkeypatch.setattr(
        "convo_recall.install.schedulers.systemd.scheduler_unit_dir",
        lambda: tmp_path / "units-empty",
    )

    # Sentinel that fires if we accidentally call systemctl
    called = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: called.append(a) or _ok())

    result = SystemdUserScheduler().uninstall_watcher(agent="claude")

    assert result.ok
    assert "not installed" in result.message
    assert called == [], (
        f"systemctl should NOT have been called when no units exist; "
        f"calls: {called}"
    )


def test_systemd_uninstall_watcher_survives_missing_systemctl(tmp_path, monkeypatch):
    """Pathological case: unit files exist but systemctl binary doesn't.
    Could happen on macOS if someone manually copied unit files, or on
    Linux during a failed package upgrade. Should remove the orphan
    files cleanly, not crash."""
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    (unit_dir / "com.convo-recall.ingest.claude.service").write_text("[Service]")
    (unit_dir / "com.convo-recall.ingest.claude.path").write_text("[Path]")
    monkeypatch.setattr(
        "convo_recall.install.schedulers.systemd.scheduler_unit_dir",
        lambda: unit_dir,
    )

    def boom(*a, **kw):
        raise FileNotFoundError("systemctl: no such file")
    monkeypatch.setattr(subprocess, "run", boom)

    result = SystemdUserScheduler().uninstall_watcher(agent="claude")
    assert result.ok
    # Files removed despite systemctl being missing
    assert not (unit_dir / "com.convo-recall.ingest.claude.service").exists()
    assert not (unit_dir / "com.convo-recall.ingest.claude.path").exists()


def test_systemd_uninstall_sidecar_no_op_when_unit_absent(tmp_path, monkeypatch):
    """Same guard for sidecar."""
    monkeypatch.setattr(
        "convo_recall.install.schedulers.systemd.scheduler_unit_dir",
        lambda: tmp_path / "units-empty",
    )

    called = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: called.append(a) or _ok())

    result = SystemdUserScheduler().uninstall_sidecar()

    assert result.ok
    assert "not installed" in result.message
    assert called == []
