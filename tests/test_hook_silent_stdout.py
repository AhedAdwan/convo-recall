"""Hook silent-stdout regression test (TD-005).

Locks the empty-stdout-exit-0 contract on
`src/convo_recall/hooks/conversation-ingest.sh`. If the hook ever writes
to stdout, Claude Code's hook validator rejects the response and after
N validation failures in a single session silently disables the Stop
hook for the rest of that session — ingest stops working mid-conversation.

The fix landed in `10969f0` and `d2c5028` (Phase F → H of
`docs/feedback/2026-05-02-sandbox-phase1.md`). This test guards it.

Approach: spawn the hook via subprocess with isolated XDG_RUNTIME_DIR
and a stub `recall` on PATH, then assert stdout == b"" AND returncode
== 0 for every plausible payload — happy paths, unknown event types,
malformed JSON, garbage, unicode, large input, opt-out, unwritable
hook-log, and empty PATH (no recall discoverable).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / "src" / "convo_recall" / "hooks" / "conversation-ingest.sh"

# System path entries the hook needs to find bash builtins' helpers
# (mkdir, stat, date, touch, cat) and that subprocess needs to find bash.
# Order matters only for `command -v recall`, which finds the stub first.
_SYSTEM_PATH = "/bin:/usr/bin:/sbin:/usr/sbin"


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def recall_stub_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A directory containing a `recall` stub that exits 0 immediately.

    Mirrors the pattern in tests/test_hook_auto_ingest.sh:25-33 — keeps
    the hook's spawned `recall ingest` from touching the user's real DB.
    """
    stub_dir = tmp_path_factory.mktemp("recall-stub")
    stub = stub_dir / "recall"
    stub.write_text("#!/usr/bin/env bash\nexit 0\n")
    stub.chmod(0o755)
    return stub_dir


@pytest.fixture
def isolated_runtime(tmp_path: Path) -> Path:
    """Per-test XDG_RUNTIME_DIR — guarantees a fresh lock state."""
    runtime = tmp_path / "xdg-runtime"
    runtime.mkdir()
    return runtime


@pytest.fixture
def fake_home(tmp_path: Path) -> Path:
    """A HOME with no `.local/bin/recall` — disables the $HOME fallback path."""
    home = tmp_path / "fake-home"
    home.mkdir()
    return home


def _build_env(
    *,
    recall_stub_dir: Path,
    runtime: Path,
    home: Path,
    extras: dict[str, str] | None = None,
) -> dict[str, str]:
    env = {
        "PATH": f"{recall_stub_dir}:{_SYSTEM_PATH}",
        "HOME": str(home),
        "XDG_RUNTIME_DIR": str(runtime),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    if extras:
        env.update(extras)
    return env


def _run_hook(
    payload: bytes | None,
    *,
    env: dict[str, str],
    stdin_mode: str = "pipe",
) -> subprocess.CompletedProcess[bytes]:
    """Run the hook with `payload` on stdin under the given env.

    `stdin_mode`:
      - "pipe":   feed `payload` (bytes) on stdin via PIPE.
      - "devnull": stdin is /dev/null (no input attached).
    """
    kwargs: dict[str, object] = {
        "env": env,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "timeout": 30,
    }
    if stdin_mode == "pipe":
        kwargs["input"] = payload if payload is not None else b""
    elif stdin_mode == "devnull":
        kwargs["stdin"] = subprocess.DEVNULL
    else:
        raise ValueError(f"unknown stdin_mode: {stdin_mode}")

    return subprocess.run(["bash", str(HOOK)], **kwargs)  # type: ignore[arg-type]


# ── The contract ────────────────────────────────────────────────────────────


def _assert_contract(proc: subprocess.CompletedProcess[bytes], label: str) -> None:
    """Empty stdout + exit 0. The validator-disable failure mode is binary."""
    assert proc.returncode == 0, (
        f"{label}: returncode must be 0, got {proc.returncode}; "
        f"stderr={proc.stderr!r}"
    )
    assert proc.stdout == b"", (
        f"{label}: stdout MUST be empty (Stop/AfterAgent/SessionEnd schema "
        f"rejects any output and Claude Code disables the hook after N "
        f"validation failures); got: {proc.stdout!r}"
    )


# ── Parametrized payload cases ──────────────────────────────────────────────


_LARGE_PAYLOAD = (
    b'{"hook_event_name":"Stop","pad":"' + (b"x" * (1024 * 1024)) + b'"}'
)

_PAYLOAD_CASES: list[tuple[str, bytes]] = [
    ("empty_stdin", b""),
    ("claude_stop", b'{"hook_event_name":"Stop","stop_hook_active":true}'),
    ("codex_stop", b'{"hook_event_name":"Stop"}'),
    (
        "gemini_after_agent",
        b'{"hook_event_name":"AfterAgent","agent_name":"gemini-2.0"}',
    ),
    # Unknown event types — future Claude Code releases may add new events
    # (e.g. SubagentStop) with different payload shapes. The hook MUST NOT
    # try to parse-and-respond; it must keep emitting empty stdout + exit 0.
    ("unknown_event_subagent_stop", b'{"hook_event_name":"SubagentStop"}'),
    (
        "unknown_event_pretooluse",
        b'{"hook_event_name":"PreToolUse","tool_name":"Read"}',
    ),
    ("malformed_json", b"{not really json"),
    ("garbage_non_json", b"hello world\n"),
    (
        "unicode",
        json.dumps(
            {"hook_event_name": "Stop", "note": "日本語 🎌"},
            ensure_ascii=False,
        ).encode("utf-8"),
    ),
    ("large_1mb_payload", _LARGE_PAYLOAD),
]


@pytest.mark.parametrize("label,payload", _PAYLOAD_CASES, ids=[c[0] for c in _PAYLOAD_CASES])
def test_hook_emits_empty_stdout_for_payload(
    label: str,
    payload: bytes,
    recall_stub_dir: Path,
    isolated_runtime: Path,
    fake_home: Path,
) -> None:
    env = _build_env(
        recall_stub_dir=recall_stub_dir, runtime=isolated_runtime, home=fake_home
    )
    proc = _run_hook(payload, env=env)
    _assert_contract(proc, label)


# ── Special cases: stdin shapes, opt-out, broken hook log, empty PATH ──────


def test_hook_silent_when_stdin_is_devnull(
    recall_stub_dir: Path, isolated_runtime: Path, fake_home: Path
) -> None:
    """No stdin attached at all (closed FD) — emulates a detached invocation."""
    env = _build_env(
        recall_stub_dir=recall_stub_dir, runtime=isolated_runtime, home=fake_home
    )
    proc = _run_hook(None, env=env, stdin_mode="devnull")
    _assert_contract(proc, "stdin_devnull")


def test_hook_silent_when_opt_out(
    recall_stub_dir: Path, isolated_runtime: Path, fake_home: Path
) -> None:
    """CONVO_RECALL_INGEST_HOOK=off must skip work AND still exit silently."""
    env = _build_env(
        recall_stub_dir=recall_stub_dir,
        runtime=isolated_runtime,
        home=fake_home,
        extras={"CONVO_RECALL_INGEST_HOOK": "off"},
    )
    proc = _run_hook(
        b'{"hook_event_name":"Stop","stop_hook_active":true}', env=env
    )
    _assert_contract(proc, "opt_out")


def test_hook_silent_when_hook_log_path_unwritable(
    recall_stub_dir: Path,
    isolated_runtime: Path,
    fake_home: Path,
    tmp_path: Path,
) -> None:
    """If CONVO_RECALL_HOOK_LOG points at an unwritable path, the redirect
    inside the hook fails — but stdout must still be empty (the redirect
    error goes to stderr, not stdout)."""
    # Path under a non-existent intermediate directory — `>>` cannot create it.
    bad_log = tmp_path / "no-such-dir" / "log.txt"
    env = _build_env(
        recall_stub_dir=recall_stub_dir,
        runtime=isolated_runtime,
        home=fake_home,
        extras={"CONVO_RECALL_HOOK_LOG": str(bad_log)},
    )
    proc = _run_hook(
        b'{"hook_event_name":"Stop","stop_hook_active":true}', env=env
    )
    _assert_contract(proc, "unwritable_hook_log")


def _hardcoded_fallback_recall_resolves() -> bool:
    """True iff one of the hook's hard-coded fallback `recall` paths is
    a callable executable visible to this process. Wrapped in try/except
    because some CI runners can SEE the `/root/.local/bin/recall` path
    via stat but raise PermissionError when reading it (the runner user
    isn't root). A path we can't access is one we won't fall through to,
    so it counts as 'no fallback resolves here'."""
    for p in (
        "/root/.local/bin/recall",
        "/usr/local/bin/recall",
        "/opt/homebrew/bin/recall",
    ):
        try:
            if Path(p).is_file() and os.access(p, os.X_OK):
                return True
        except (OSError, PermissionError):
            continue
    return False


@pytest.mark.skipif(
    _hardcoded_fallback_recall_resolves(),
    reason=(
        "A hard-coded fallback path resolves on this host; the 'no recall "
        "discoverable' branch cannot be exercised deterministically. CI "
        "runners (clean macOS / ubuntu-latest) hit the branch."
    ),
)
def test_hook_silent_when_no_recall_on_path_or_fallback(
    isolated_runtime: Path, fake_home: Path, tmp_path: Path
) -> None:
    """Empty PATH + no fallback recall on disk: the hook must NOT error
    when `recall_bin` ends up empty — it must exit silently. Guards
    against future edits like `echo "recall not found"` slipping in."""
    empty_dir = tmp_path / "empty-bin"
    empty_dir.mkdir()
    env = {
        # System PATH only — no `recall` discoverable. fake_home blocks
        # the $HOME fallback; the skipif guards the four hard-coded paths.
        "PATH": f"{empty_dir}:{_SYSTEM_PATH}",
        "HOME": str(fake_home),
        "XDG_RUNTIME_DIR": str(isolated_runtime),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    proc = _run_hook(
        b'{"hook_event_name":"Stop","stop_hook_active":true}', env=env
    )
    _assert_contract(proc, "no_recall_anywhere")
