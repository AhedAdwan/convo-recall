"""CLI argparse-plumbing tests for safety gates.

Unit tests in test_uninstall_walks_all_tiers.py and test_backfill_safety.py
exercise the gate LOGIC at function-call level (mocked TTY, mocked input()).
These tests run the real `recall` binary as a subprocess, so they catch
argparse refactors that forget to wire `--confirm` to the function call —
a class of bug that unit tests on the function alone cannot detect.

Pattern: every safety flag must be discoverable from `--help` and must
default-deny when invoked without explicit confirmation.
"""

import os
import shutil
import subprocess
import tempfile

import pytest

_RECALL = shutil.which("recall")
pytestmark = pytest.mark.skipif(
    _RECALL is None,
    reason="`recall` not on PATH (editable install required for CLI tests)",
)


def _run(*args, env=None, input_text=None, timeout=15):
    """Run `recall <args>` with stdin closed (simulates non-TTY)."""
    e = os.environ.copy()
    if env:
        e.update(env)
    return subprocess.run(
        [_RECALL, *args],
        capture_output=True, text=True, timeout=timeout, env=e,
        input=input_text,
    )


# ── --help discoverability ────────────────────────────────────────────────────

def test_uninstall_help_mentions_purge_data_and_confirm():
    r = _run("uninstall", "--help")
    assert r.returncode == 0
    assert "--purge-data" in r.stdout
    assert "--confirm" in r.stdout
    # The help text must explain the relationship — bare --purge-data is dry-run.
    assert "DRY-RUN" in r.stdout or "dry-run" in r.stdout.lower()


def test_backfill_clean_help_mentions_confirm():
    r = _run("backfill-clean", "--help")
    assert r.returncode == 0
    assert "--confirm" in r.stdout


def test_backfill_redact_help_mentions_confirm():
    r = _run("backfill-redact", "--help")
    assert r.returncode == 0
    assert "--confirm" in r.stdout


def test_chunk_backfill_help_mentions_confirm():
    r = _run("chunk-backfill", "--help")
    assert r.returncode == 0
    assert "--confirm" in r.stdout


def test_top_level_help_lists_destructive_subcommands():
    """Sanity: the destructive commands must be in the top-level help."""
    r = _run("--help")
    assert r.returncode == 0
    for cmd in ("uninstall", "backfill-clean", "backfill-redact",
                "chunk-backfill", "forget"):
        assert cmd in r.stdout, f"{cmd} missing from top-level help"


# ── Default-deny: subprocess invocation with no TTY, no --confirm ─────────────

@pytest.fixture
def isolated_db(tmp_path):
    """Spin up a self-contained DB+config so subprocess invocations don't
    touch the developer's real convo-recall state.

    Yields a dict of env vars + paths. Cleans up after."""
    db_path = tmp_path / "test.db"
    cfg_path = tmp_path / "config.json"
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()

    # Seed a minimal DB by importing ingest in-process.
    import convo_recall.ingest as ingest
    orig_db = ingest.DB_PATH
    ingest.DB_PATH = db_path
    con = ingest.open_db()
    ingest._upsert_session(con, "claude", "proj_a", "s-1", None,
                           "2026-04-01T00:00:00Z", "2026-04-01T00:00:00Z")
    ingest._persist_message(con, "claude", "proj_a", "s-1", "u1",
                            "user", "test message", "2026-04-01T00:00:01Z",
                            do_embed=False)
    con.close()
    ingest.DB_PATH = orig_db

    env = {
        "CONVO_RECALL_DB": str(db_path),
        "CONVO_RECALL_CONFIG": str(cfg_path),
    }
    yield {"env": env, "db": db_path, "config": cfg_path, "runtime": runtime_dir}


def test_uninstall_purge_data_no_confirm_does_not_delete_db(isolated_db):
    """`recall uninstall --purge-data` (no TTY, no --confirm) MUST NOT
    delete the DB. This is the regression the audit closed."""
    r = _run("uninstall", "--purge-data", env=isolated_db["env"], input_text="")
    assert r.returncode == 0, f"uninstall failed: {r.stderr}"
    assert isolated_db["db"].exists(), \
        "DB was deleted in dry-run! Safety gate broken."
    assert "DRY-RUN" in r.stdout or "dry-run" in r.stdout.lower()


def test_backfill_clean_no_confirm_does_not_mutate(isolated_db):
    """Same shape: backfill-clean without --confirm must preview only."""
    # Get initial content
    import sqlite3
    con = sqlite3.connect(f"file:{isolated_db['db']}?mode=ro", uri=True)
    before = con.execute("SELECT content FROM messages").fetchone()[0]
    con.close()

    r = _run("backfill-clean", env=isolated_db["env"], input_text="")
    # Exit code can be 0 (DRY-RUN message) or 0 from "nothing to do" — both fine.
    assert r.returncode == 0, f"backfill-clean failed: {r.stderr}"

    con = sqlite3.connect(f"file:{isolated_db['db']}?mode=ro", uri=True)
    after = con.execute("SELECT content FROM messages").fetchone()[0]
    con.close()
    assert after == before, "row content was mutated in dry-run!"


def test_backfill_redact_no_confirm_does_not_mutate(isolated_db):
    import sqlite3
    con = sqlite3.connect(f"file:{isolated_db['db']}?mode=ro", uri=True)
    before = con.execute("SELECT content FROM messages").fetchone()[0]
    con.close()

    r = _run("backfill-redact", env=isolated_db["env"], input_text="")
    assert r.returncode == 0

    con = sqlite3.connect(f"file:{isolated_db['db']}?mode=ro", uri=True)
    after = con.execute("SELECT content FROM messages").fetchone()[0]
    con.close()
    assert after == before


# ── Argparse boundary: typo'd flag names ──────────────────────────────────────

def test_unknown_flag_on_uninstall_is_rejected():
    """A typo like `--prge-data` (missing letter) should fail loudly, not
    silently fall back to the bare `recall uninstall` behavior."""
    r = _run("uninstall", "--prge-data")
    assert r.returncode == 2  # argparse "unrecognized arguments"
    assert "unrecognized" in r.stderr.lower() or "error" in r.stderr.lower()


def test_unknown_flag_on_backfill_clean_is_rejected():
    r = _run("backfill-clean", "--cnfirm")
    assert r.returncode == 2
