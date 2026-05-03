"""Docstring/code consistency check for convo_recall.ingest.

The module's docstring lists default paths for CONVO_RECALL_DB, CONVO_RECALL_PROJECTS,
and CONVO_RECALL_SOCK. Those defaults must match the Path objects the module
actually constructs at import time — otherwise readers treat the docstring as
documentation and configure their environment from a stale/dead path.
"""
from __future__ import annotations

import re
from pathlib import Path

from convo_recall import ingest


_DEFAULT_RE = re.compile(
    r"^\s*(CONVO_RECALL_\w+)\s+—.*?default\s+(\S+)\s*\)\s*$",
    flags=re.MULTILINE,
)


def _expand(p: str) -> Path:
    return Path(p).expanduser().resolve()


def test_docstring_defaults_match_module_constants():
    doc = ingest.__doc__ or ""
    pairs = dict(_DEFAULT_RE.findall(doc))
    assert pairs, f"no env-var/default pairs found in ingest.__doc__:\n{doc!r}"

    expected_constant = {
        "CONVO_RECALL_DB": ingest.DB_PATH,
        "CONVO_RECALL_PROJECTS": ingest.PROJECTS_DIR,
        "CONVO_RECALL_SOCK": ingest.EMBED_SOCK,
    }

    mismatches: list[str] = []
    for env, default_str in pairs.items():
        if env not in expected_constant:
            continue
        doc_path = _expand(default_str)
        code_path = _expand(str(expected_constant[env]))
        if doc_path != code_path:
            mismatches.append(
                f"{env}: docstring says {default_str!r} ({doc_path}); "
                f"code uses {expected_constant[env]} ({code_path})"
            )
    assert not mismatches, "\n".join(mismatches)
