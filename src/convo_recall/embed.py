"""
Embedding-sidecar client + sqlite-vec helpers for convo-recall.

Note: this module is named to NOT collide with `embed_service.py` (the
sidecar daemon itself, which runs as a launchd/systemd unit). `embed.py`
is the *client* that talks to the sidecar over a Unix-domain socket.

Provides:
  - embed(text, mode) → list[float] | None — POST to UDS, get vector.
  - _UnixHTTPConn — http.client subclass that connects to a UDS path.
  - _wait_for_embed_socket(timeout_s) — poll the socket path during install.
  - _vec_bytes(v) — float32 little-endian packing for sqlite-vec.
  - _vec_insert(con, rowid, vec) / _vec_search(con, qvec, k) /
    _vec_count(con) — sqlite-vec ops, all guarded by `_vec_ok(con)`.

Extracted from ingest.py in v0.4.0 (TD-008). Back-compat re-exports keep
`from convo_recall.ingest import embed, _vec_search, ...` working through
one release.

Test-monkeypatch contract: `tests/test_*.py` historically `monkeypatch.
setattr`'s `ingest.{EMBED_SOCK,_UnixHTTPConn,_vec_ok}`. Functions below
read those names through the ingest module at call time so the patches
flow into production code. EMBED_SOCK additionally MUST stay defined in
ingest.py for v0.4.0 because `test_ingest_docstring_truth.py` reloads
ingest with env vars cleared and reads `ingest.EMBED_SOCK`; if EMBED_SOCK
lived here, the reload wouldn't refresh it. A8 finalizes the move.
"""

import http.client
import json
import socket
import struct
import sys

from .db import EMBED_DIM


_EMBED_TIMEOUT_S = 30.0


class _UnixHTTPConn(http.client.HTTPConnection):
    def __init__(self, sock_path: str, timeout: float = _EMBED_TIMEOUT_S):
        super().__init__("localhost", timeout=timeout)
        self._sock_path = sock_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._sock_path)


def embed(text: str, mode: str = "document") -> "list[float] | None":
    """POST text to the UDS embed service. Returns None if unreachable
    or the sidecar returns a non-200 / malformed response.

    Long texts are chunked and mean-pooled by the sidecar — no client-side
    truncation. Caller falls back to FTS-only when this returns None.
    """
    # Read EMBED_SOCK and _UnixHTTPConn through ingest so test monkeypatches
    # on ingest reach this codepath. See module docstring.
    from . import ingest as _ing
    sock_path = _ing.EMBED_SOCK
    UnixConn = _ing._UnixHTTPConn

    body = json.dumps({"text": text, "mode": mode}).encode()
    conn = UnixConn(str(sock_path))
    try:
        conn.request("POST", "/embed", body=body,
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        if resp.status != 200:
            # Drain so the connection can close cleanly. Don't spam stderr —
            # a sidecar that's rate-limiting (429) or briefly unhealthy is a
            # transient condition, and the caller already degrades to FTS.
            try: resp.read()
            except Exception: pass
            return None
        return json.loads(resp.read()).get("vector")
    except (ConnectionRefusedError, FileNotFoundError, OSError, socket.timeout):
        return None  # service down or hung — expected when sidecar isn't healthy
    except Exception as e:
        print(f"[warn] embed: {type(e).__name__}: {e}", file=sys.stderr)
        return None
    finally:
        conn.close()


def _vec_bytes(v: "list[float]") -> bytes:
    return struct.pack(f"{len(v)}f", *v)


def _wait_for_embed_socket(timeout_s: float = 5.0,
                           poll_interval_s: float = 0.2,
                           verbose: bool = False) -> bool:
    """Poll for `EMBED_SOCK.exists()` up to `timeout_s` seconds.

    Returns True if the socket exists by the deadline (immediately if it
    already does), False on timeout. Used by ingest + embed-backfill to
    close the race where:

      1. The wizard starts the embed sidecar systemd unit.
      2. The wizard immediately spawns the detached _backfill-chain.
      3. The chain calls `open_db()` and ingest before the sidecar has
         finished loading the model + binding the socket (~5s on Linux).
      4. `embed_live = EMBED_SOCK.exists()` was set to False at the start
         of ingest → self-heal pass + embed-backfill silently no-op.

    Pre-fix the race only manifested on truly cold installs (the bug
    pattern the user hit on a fresh Linux sandbox). On warm systems
    (e.g. macOS rerunning install with the launchd sidecar already up)
    the socket exists at step 4 → no race → coincidentally "just works".
    """
    from . import ingest as _ing
    sock = _ing.EMBED_SOCK

    if sock.exists():
        return True
    if verbose:
        print(f"[ingest] waiting up to {timeout_s:.0f}s for embed socket "
              f"at {sock} …", file=sys.stderr)
    import time as _time
    deadline = _time.time() + timeout_s
    while _time.time() < deadline:
        if _ing.EMBED_SOCK.exists():  # re-read each poll for monkeypatch awareness
            if verbose:
                elapsed = timeout_s - (deadline - _time.time())
                print(f"[ingest] embed socket appeared after {elapsed:.1f}s",
                      file=sys.stderr)
            return True
        _time.sleep(poll_interval_s)
    if verbose:
        print(f"[ingest] embed socket did not appear within {timeout_s:.0f}s — "
              f"running in FTS-only mode", file=sys.stderr)
    return False


def _vec_insert(con, rowid: int, vec: "list[float]") -> None:
    from . import ingest as _ing
    if not _ing._vec_ok(con):
        return
    try:
        con.execute(
            "INSERT OR REPLACE INTO message_vecs(rowid, embedding) VALUES (?, ?)",
            (rowid, _vec_bytes(vec)),
        )
    except Exception:
        pass


def _vec_search(con, qvec: "list[float]", k: int = 100,
                restrict_rowids: "set[int] | None" = None) -> "list[int]":
    """Vector KNN search. When `restrict_rowids` is set, results are limited
    to that subset. For small subsets (<500) we compute cosine in Python
    against the filtered embeddings — exact recall, sub-millisecond at this
    size. For larger sets we ask sqlite-vec for a generous top-k and let the
    caller intersect.
    """
    from . import ingest as _ing
    if not _ing._vec_ok(con):
        return []
    try:
        if restrict_rowids is not None and len(restrict_rowids) < 500:
            placeholders = ",".join("?" * len(restrict_rowids))
            rows = con.execute(
                f"SELECT rowid, embedding FROM message_vecs "
                f"WHERE rowid IN ({placeholders})",
                tuple(restrict_rowids),
            ).fetchall()
            qbytes = _vec_bytes(qvec)
            scored: "list[tuple[float, int]]" = []
            qf = struct.unpack(f"{EMBED_DIM}f", qbytes)
            for r in rows:
                emb = struct.unpack(f"{EMBED_DIM}f", r[1])
                # Vectors from BAAI/bge-large-en-v1.5 are L2-normalized at the
                # sidecar, so dot product == cosine similarity. No norm needed.
                dot = sum(a * b for a, b in zip(qf, emb))
                scored.append((dot, r[0]))
            scored.sort(reverse=True)
            return [rid for _, rid in scored[:k]]

        rows = con.execute(
            "SELECT rowid FROM message_vecs WHERE embedding MATCH ? AND k = ?",
            (_vec_bytes(qvec), k),
        ).fetchall()
        return [r[0] for r in rows]
    except Exception:
        return []


def _vec_count(con) -> int:
    from . import ingest as _ing
    if not _ing._vec_ok(con):
        return 0
    try:
        return con.execute("SELECT COUNT(*) FROM message_vecs").fetchone()[0]
    except Exception:
        return 0
