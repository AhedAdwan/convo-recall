"""B4 Item 5 — `recall install --scheduler X` CLI flag."""

import shutil
import subprocess
import sys

import pytest


_RECALL = shutil.which("recall")
pytestmark = pytest.mark.skipif(
    _RECALL is None,
    reason="`recall` not on PATH (editable install required for CLI tests)",
)


def _run(*args, timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_RECALL, *args],
        capture_output=True, text=True, timeout=timeout,
    )


def test_install_argparse_accepts_scheduler_flag():
    r = _run("install", "--scheduler", "polling", "--dry-run")
    assert r.returncode == 0, (
        f"--scheduler polling should succeed in dry-run; "
        f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    )
    assert "polling (Popen fallback)" in r.stdout


def test_install_argparse_rejects_unknown_scheduler():
    r = _run("install", "--scheduler", "bogus", "--dry-run")
    assert r.returncode != 0
    err = r.stderr.lower()
    # argparse's "invalid choice" error lists every allowed value.
    for name in ("auto", "launchd", "systemd", "cron", "polling"):
        assert name in err, f"missing {name} in argparse error: {r.stderr}"


def test_install_help_lists_scheduler_choices():
    r = _run("install", "--help")
    assert r.returncode == 0
    assert "--scheduler {auto,launchd,systemd,cron,polling}" in r.stdout
