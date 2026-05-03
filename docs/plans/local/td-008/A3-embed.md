## A3: Extract `embed.py` — UDS client + vec helpers

**Status:** not started
**Dependencies:** A2

### Scope

Move the embedding-sidecar client and all `_vec_*` helpers out of `ingest.py` into `src/convo_recall/embed.py`. Note: this module is named to NOT collide with the existing `embed_service.py` (the sidecar daemon itself); `embed.py` is the *client* that talks to it over the Unix-domain socket. Pure file-move; zero behavior change.

### Key Components

- `embed.EMBED_SOCK` — `Path(os.environ.get("CONVO_RECALL_SOCK", "~/.local/share/convo-recall/embed.sock"))`.
- `embed._UnixHTTPConn(http.client.HTTPConnection)` — UDS subclass.
- `embed.embed(text, mode="document") -> list[float] | None` — POST to sidecar; returns `None` if unreachable.
- `embed._vec_bytes(v) -> bytes` — float32 little-endian packing for sqlite-vec.
- `embed._wait_for_embed_socket(timeout_s=5.0, …)` — used by the install chain to wait for the sidecar.
- `embed._vec_insert(con, rowid, vec)`, `embed._vec_search(con, qvec, k)`, `embed._vec_count(con)`, `embed._vec_ok(con)`.

### Rough File Inventory

- New: 1 file (`src/convo_recall/embed.py`) — ~180 LOC moved.
- Modified: 1 file (`src/convo_recall/ingest.py`) — call sites and re-exports.
- Modified: 0 test files (`tests/test_ingest.py` uses `monkeypatch.setattr(ingest, 'EMBED_SOCK', …)` — keeps working through the shim).

### Risks & Blockers

- **Import-time defaults** — `EMBED_SOCK` is a `Path` literal computed at import time from `os.environ`. If A2 also moved `EMBED_SOCK` (it shouldn't — `EMBED_SOCK` is sidecar-client config, not DB config), undo that. `embed.py` is its sole owner.
- **`_vec_ok(con)`** — depends on `db._VEC_ENABLED[con]` (per-connection vec state placed in `db.py` per A2's decision). Cross-module coupling acceptable; the alternative (move `_VEC_ENABLED` to `embed.py` and have `db.open_db` write to it) creates a circular `db → embed → db` import. Keep state owner = `db.py`, reader = `embed.py`.
- **`tests/test_ingest_docstring_truth.py`** — parses `ingest.py` module docstring asserting `CONVO_RECALL_*` defaults. The docstring stays in `ingest.py` for the shim release; update it AFTER A8 lands. For A3, do NOT remove the docstring lines about `CONVO_RECALL_SOCK` from `ingest.py`.

### Done Criteria

- [ ] `src/convo_recall/embed.py` exists with all symbols listed above.
- [ ] `from convo_recall.ingest import embed, _vec_search, EMBED_SOCK` still works (re-export).
- [ ] `pytest tests/test_ingest.py` → green (vec-search-disabled branch and others).
- [ ] `embed("hi")` against a running sidecar returns a `list[float]` of length 1024.
- [ ] `recall search "test"` on a populated DB returns results that include vec hits (`_vec_search` not silently bypassed).

### Verification Artifacts

| File | Must Contain | Why |
|------|-------------|-----|
| `src/convo_recall/embed.py` | `def embed(` and `class _UnixHTTPConn` | Client API present |
| `src/convo_recall/embed.py` | `def _vec_search(` and `def _vec_insert(` | Vec helpers moved |
| `src/convo_recall/embed.py` | `from .db import` | Dep direction (embed → db) enforced |
| `src/convo_recall/ingest.py` | `from .embed import` | Re-export block in place |
