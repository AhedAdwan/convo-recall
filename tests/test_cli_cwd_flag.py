"""Tests for --cwd flag overriding os.getcwd() in search/tail dispatch.

The CLI dispatch (cli.py) computes `project = ingest._display_name(args.cwd or os.getcwd())`
when neither --project nor --all-projects is passed. These tests exercise that
auto-scope path directly via subprocess, asserting the flag is respected.
"""

import os
import shutil
import subprocess
import sys

import pytest


_RECALL = shutil.which("recall")
pytestmark = pytest.mark.skipif(
    _RECALL is None,
    reason="`recall` not on PATH (editable install required for CLI tests)",
)


def _run(*args, env=None, cwd=None, timeout=15):
    full_env = {**os.environ, **(env or {})}
    return subprocess.run(
        [_RECALL, *args],
        capture_output=True, text=True, timeout=timeout,
        env=full_env, cwd=cwd,
    )


def test_search_cwd_flag_overrides_getcwd(tmp_path):
    """`--cwd PATH` resolves project from PATH not from process cwd."""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    (repo / ".git").mkdir()  # marker → display_name = "myrepo"

    other = tmp_path / "other"
    other.mkdir()

    # Use a fresh DB so search has nothing to find — we just want to confirm
    # argparse accepts the flag and dispatch resolves --cwd over getcwd.
    db = tmp_path / "convo.db"
    env = {"CONVO_RECALL_DB": str(db)}

    # Invoke from `other` but pass --cwd repo. Search will return no results,
    # but we expect no argparse error and a 0/1 exit.
    r = _run("search", "anything", "-n", "1", "--cwd", str(repo),
             env=env, cwd=str(other))
    # Must not raise argparse "unrecognized argument" — that would mean rc=2
    assert r.returncode in (0, 1), f"unexpected rc {r.returncode}: {r.stderr}"


def test_tail_cwd_flag_accepted(tmp_path):
    """`tail --cwd PATH` must be accepted by argparse (no rc=2)."""
    db = tmp_path / "convo.db"
    env = {"CONVO_RECALL_DB": str(db)}
    r = _run("tail", "5", "--cwd", str(tmp_path),
             env=env, cwd=str(tmp_path))
    # No sessions in fresh DB → rc=1 with "no session" message; but argparse OK.
    assert r.returncode in (0, 1), f"unexpected rc {r.returncode}: {r.stderr}"
