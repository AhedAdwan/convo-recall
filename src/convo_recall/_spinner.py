"""Bouncing-bar spinner for indeterminate-time waits in the install wizard.

Used when we don't know the total work (sidecar warmup, hook write, watcher
install). For known-total operations (initial ingest, embed-backfill) the
chain uses tqdm via `recall stats` instead — keep the two paths separate.

Design:
- Threaded context manager: `with BouncingSpinner("label"): do_work()`.
- Bounces a `●` left↔right inside a fixed-width track.
- Auto-detects TTY: in non-TTY (CI, piped, no isatty) prints a single line
  "label…" once and skips the animation entirely. No ANSI noise in CI logs.
- Final marker on `__exit__`: ✅ on clean exit, ❌ if an exception propagated.
- Zero new dependencies — uses only stdlib threading + sys.

Not intended for parallel spinners on the same stream — call them in sequence.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import TextIO


_FRAME_PERIOD_S = 0.08      # ~12 frames/sec — visible motion without flicker
_TRACK_WIDTH = 14           # cells inside the brackets


def _bounce_positions(width: int) -> list[int]:
    """Build a position sequence that bounces left → right → left.

    For width=4: [0,1,2,3,2,1] — repeats forever via modulo. Endpoints are
    visited only once per cycle so the dot doesn't pause at the walls.
    """
    if width <= 1:
        return [0]
    forward = list(range(width))
    backward = list(range(width - 2, 0, -1))
    return forward + backward


class BouncingSpinner:
    """Context-managed spinner. Use as `with BouncingSpinner("text"): work()`."""

    def __init__(self, label: str, *,
                 width: int = _TRACK_WIDTH,
                 period: float = _FRAME_PERIOD_S,
                 stream: TextIO | None = None,
                 success_marker: str = "✅",
                 failure_marker: str = "❌"):
        self.label = label
        self.width = max(2, width)
        self.period = period
        self.stream = stream if stream is not None else sys.stderr
        self.success_marker = success_marker
        self.failure_marker = failure_marker
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._is_tty = bool(getattr(self.stream, "isatty", lambda: False)())

    # ── lifecycle ───────────────────────────────────────────────────────────
    def __enter__(self) -> "BouncingSpinner":
        if not self._is_tty:
            # Non-TTY path: single static line, no animation, no ANSI.
            self.stream.write(f"  ⏳ {self.label}…\n")
            self.stream.flush()
            return self
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if not self._is_tty:
            marker = self.success_marker if exc_type is None else self.failure_marker
            self.stream.write(f"  {marker} {self.label}\n")
            self.stream.flush()
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        # Clear the in-progress line and replace with final status.
        self._clear_line()
        marker = self.success_marker if exc_type is None else self.failure_marker
        self.stream.write(f"  {marker} {self.label}\n")
        self.stream.flush()

    # ── internals ───────────────────────────────────────────────────────────
    def _spin(self) -> None:
        positions = _bounce_positions(self.width)
        i = 0
        while not self._stop.is_set():
            pos = positions[i % len(positions)]
            track = [" "] * self.width
            track[pos] = "●"
            line = f"\r  ⏳ {self.label}  [{''.join(track)}]"
            try:
                self.stream.write(line)
                self.stream.flush()
            except (BrokenPipeError, ValueError):
                # Pipe closed or stream torn down mid-flight — stop quietly.
                return
            i += 1
            self._stop.wait(self.period)

    def _clear_line(self) -> None:
        # ANSI: \r → cursor to col 0, \033[K → clear from cursor to end of line.
        try:
            self.stream.write("\r\033[K")
            self.stream.flush()
        except (BrokenPipeError, ValueError):
            pass


def spin(label: str, **kwargs) -> BouncingSpinner:
    """Convenience constructor: `with spin("Warming sidecar"): ...`."""
    return BouncingSpinner(label, **kwargs)
