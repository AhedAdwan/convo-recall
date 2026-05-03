"""Docstring/code consistency check for convo_recall.ingest.

The module's docstring lists default paths for CONVO_RECALL_DB, CONVO_RECALL_PROJECTS,
and CONVO_RECALL_SOCK. Those defaults must match the Path objects the module
actually constructs at import time — otherwise readers treat the docstring as
documentation and configure their environment from a stale/dead path.

Other tests (e.g. test_tail.py) set CONVO_RECALL_DB=:memory: at import time to
keep their test DB hermetic. We reload the module with those env vars cleared
so we always validate the *defaults*, not whatever was injected by another
test fixture.
"""
from __future__ import annotations

import importlib
import os
import re
from pathlib import Path

from convo_recall import ingest


_DEFAULT_RE = re.compile(
    r"^\s*(CONVO_RECALL_\w+)\s+—.*?default\s+(\S+)\s*\)\s*$",
    flags=re.MULTILINE,
)
_OVERRIDE_VARS = (
    "CONVO_RECALL_DB",
    "CONVO_RECALL_PROJECTS",
    "CONVO_RECALL_SOCK",
    "CONVO_RECALL_GEMINI_TMP",
    "CONVO_RECALL_CODEX_SESSIONS",
    "CONVO_RECALL_CONFIG",
)


def _expand(p: str) -> Path:
    return Path(p).expanduser().resolve()


def test_docstring_defaults_match_module_constants():
    # Reload with CONVO_RECALL_* env vars cleared so we validate the *defaults*,
    # not whatever a test fixture (e.g. test_tail.py setting :memory:) injected
    # before us. Snapshot the constants while still inside the cleared-env block
    # — `importlib.reload` returns the *same* module object that `ingest` already
    # binds, so a finally-block reload to restore state would mutate the values
    # we care about right out from under us.
    saved = {k: os.environ.pop(k, None) for k in _OVERRIDE_VARS}
    try:
        fresh = importlib.reload(ingest)
        doc = fresh.__doc__ or ""
        defaults = {
            "CONVO_RECALL_DB": fresh.DB_PATH,
            "CONVO_RECALL_PROJECTS": fresh.PROJECTS_DIR,
            "CONVO_RECALL_SOCK": fresh.EMBED_SOCK,
        }
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
        importlib.reload(ingest)

    pairs = dict(_DEFAULT_RE.findall(doc))
    assert pairs, f"no env-var/default pairs found in ingest.__doc__:\n{doc!r}"

    mismatches: list[str] = []
    for env, default_str in pairs.items():
        if env not in defaults:
            continue
        doc_path = _expand(default_str)
        code_path = _expand(str(defaults[env]))
        if doc_path != code_path:
            mismatches.append(
                f"{env}: docstring says {default_str!r} ({doc_path}); "
                f"code uses {defaults[env]} ({code_path})"
            )
    assert not mismatches, "\n".join(mismatches)
