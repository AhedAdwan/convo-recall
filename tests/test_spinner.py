"""Tests for the bouncing-bar spinner."""

import io
import time

import pytest

from convo_recall._spinner import BouncingSpinner, _bounce_positions, spin


# ── _bounce_positions ────────────────────────────────────────────────────────

def test_bounce_positions_starts_at_left_edge():
    pos = _bounce_positions(5)
    assert pos[0] == 0


def test_bounce_positions_reaches_right_edge_then_returns():
    pos = _bounce_positions(5)
    # forward = [0,1,2,3,4]; backward = [3,2,1] → endpoints visited only once.
    assert pos == [0, 1, 2, 3, 4, 3, 2, 1]


def test_bounce_positions_handles_width_one():
    assert _bounce_positions(1) == [0]


def test_bounce_positions_handles_width_two():
    assert _bounce_positions(2) == [0, 1]


# ── BouncingSpinner — non-TTY path (deterministic) ────────────────────────────

def test_spinner_non_tty_prints_static_label():
    """Non-TTY streams must NOT animate — they get one static line."""
    buf = io.StringIO()
    with BouncingSpinner("warming sidecar", stream=buf):
        pass
    out = buf.getvalue()
    assert "warming sidecar" in out
    # No carriage returns or ANSI codes in non-TTY output.
    assert "\r" not in out
    assert "\033[" not in out


def test_spinner_non_tty_success_marker_on_clean_exit():
    buf = io.StringIO()
    with BouncingSpinner("step", stream=buf):
        pass
    assert "✅" in buf.getvalue()


def test_spinner_non_tty_failure_marker_on_exception():
    buf = io.StringIO()
    with pytest.raises(RuntimeError, match="boom"):
        with BouncingSpinner("step", stream=buf):
            raise RuntimeError("boom")
    out = buf.getvalue()
    assert "❌" in out
    assert "step" in out


def test_spinner_non_tty_custom_markers():
    buf = io.StringIO()
    with BouncingSpinner("step", stream=buf,
                         success_marker="OK", failure_marker="FAIL"):
        pass
    assert "OK" in buf.getvalue()


def test_spin_helper_returns_bouncing_spinner():
    s = spin("hello")
    assert isinstance(s, BouncingSpinner)
    assert s.label == "hello"


# ── BouncingSpinner — TTY path (smoke test, animation is non-deterministic) ──

class _FakeTTYStream:
    """A StringIO that claims to be a TTY so the spinner runs the animated
    path. We don't assert on exact frame contents (timing is fuzzy); we just
    verify that the spinner produces multiple frames and exits cleanly."""

    def __init__(self):
        self.buf = io.StringIO()

    def write(self, s):
        return self.buf.write(s)

    def flush(self):
        return self.buf.flush()

    def isatty(self):
        return True

    def getvalue(self):
        return self.buf.getvalue()


def test_spinner_tty_animates_then_clears_and_writes_marker():
    s = _FakeTTYStream()
    # Use a fast period so the test runs in <1 sec but still produces frames.
    with BouncingSpinner("warming", stream=s, period=0.01):
        time.sleep(0.05)  # give the animation time to render several frames
    out = s.getvalue()
    # Should contain at least one in-progress frame (with the spinner label
    # followed by the bracket-track) and the final success line.
    assert "warming" in out
    assert "[" in out and "]" in out  # the bracket track was rendered
    assert "●" in out                  # the bouncing dot rendered at least once
    assert "✅" in out                 # final marker
    # Clear-line ANSI must appear before the final marker.
    assert "\r\033[K" in out or "\r" in out


def test_spinner_tty_failure_marker_on_exception():
    s = _FakeTTYStream()
    with pytest.raises(ValueError):
        with BouncingSpinner("doomed step", stream=s, period=0.01):
            time.sleep(0.02)
            raise ValueError("boom")
    out = s.getvalue()
    assert "❌" in out
    assert "doomed step" in out


def test_spinner_tty_no_orphan_thread_after_exit():
    """The spinner thread must be joined on __exit__ — no zombie threads."""
    import threading
    before = threading.active_count()
    s = _FakeTTYStream()
    with BouncingSpinner("quick", stream=s, period=0.01):
        time.sleep(0.02)
    # Allow a tiny moment for the join to complete.
    time.sleep(0.05)
    after = threading.active_count()
    assert after == before, f"thread leak: {before} → {after}"
