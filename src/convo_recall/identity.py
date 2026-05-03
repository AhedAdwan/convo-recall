"""
Project-identity helpers for convo-recall.

Provides:
  - _project_id(cwd) → 12-hex sha1 of realpath(cwd); the canonical project key.
  - _display_name(cwd) → basename of nearest project-root marker ancestor.
  - _ROOT_MARKERS → marker tuple used by display-name resolution.
  - Legacy helpers used only by the v4 migration backfill path:
      _legacy_project_id, _legacy_claude_slug, _legacy_codex_slug,
      _legacy_gemini_slug, _gemini_hash_project_id, and the per-agent
      _scan_*_cwd helpers that recover real cwd from session headers.

Extracted from ingest.py in v0.4.0 (TD-008). Back-compat re-exports keep
`from convo_recall.ingest import _project_id, ...` working for one release.

This module deliberately has no module-level import from `convo_recall.ingest`:
ingest.py imports `from .identity import ...` near its top, and a top-level
`from .ingest import X` here would re-enter ingest.py before its module body
has finished, raising ImportError. The three `_scan_*_cwd` helpers below
import their ingest dependencies at call time. A7 will move those write-path
symbols to their final homes and replace the lazy imports with direct ones.
"""

import hashlib
import json
import os
from pathlib import Path


_ROOT_MARKERS = (
    ".git", "package.json", "Cargo.toml", "pyproject.toml",
    "go.mod", "pom.xml", "build.gradle", "build.gradle.kts",
    "deno.json", ".projectile",
)


def _project_id(cwd) -> str:
    """Stable 12-hex id from realpath(cwd). Same dir → same id forever.

    Built from os.path.realpath so symlinked paths that resolve to the same
    target collapse to one id. Hyphen-vs-slash safe because the input is
    a real path, not the lossy hyphen-encoded directory name Claude uses.
    """
    real = os.path.realpath(str(cwd))
    return hashlib.sha1(real.encode("utf-8")).hexdigest()[:12]


def _display_name(cwd) -> str:
    """basename of nearest ancestor containing a project-root marker.

    Walks up from realpath(cwd) looking for any of _ROOT_MARKERS (.git,
    package.json, Cargo.toml, pyproject.toml, go.mod, …). Returns the
    basename of that ancestor. Falls back to basename of realpath(cwd)
    when no marker is found upstream.
    """
    real = Path(os.path.realpath(str(cwd)))
    for ancestor in (real, *real.parents):
        try:
            if any((ancestor / m).exists() for m in _ROOT_MARKERS):
                return ancestor.name or "/"
        except (OSError, PermissionError):
            continue
    return real.name or "/"


def _legacy_project_id(old_slug: str) -> str:
    """Synthesize project_id for legacy slugs whose real cwd cannot be recovered."""
    return hashlib.sha1(("legacy:" + old_slug).encode("utf-8")).hexdigest()[:12]


def _gemini_hash_project_id(hash_dir: str) -> str:
    """Synthesize project_id for Gemini hash-only sessions."""
    return hashlib.sha1(("gemini-hash:" + hash_dir).encode("utf-8")).hexdigest()[:12]


def _legacy_claude_slug(jsonl_path: Path) -> str:
    """Lossy slug from Claude's flattened storage dir; legacy fallback only.

    Used by (a) the v4 migration to match an existing legacy slug to its
    source dir, and (b) the Claude ingest as a last-resort display_name when
    no cwd field is present in any record. New rows always carry a real
    project_id derived from cwd via _project_id().
    """
    if jsonl_path.parent.name == "subagents":
        project_dir_name = jsonl_path.parent.parent.parent.name
    else:
        project_dir_name = jsonl_path.parent.name
    parts = project_dir_name.lstrip("-").split("-")
    try:
        idx = next(i for i, p in enumerate(parts) if p.lower() == "projects")
        relevant = parts[idx + 1:]
    except StopIteration:
        relevant = parts[-2:] if len(parts) >= 2 else parts
    return "_".join(relevant) if relevant else project_dir_name


def _legacy_gemini_slug(jsonl_path: Path) -> str:
    """Lossy slug from a Gemini session path; legacy fallback only.

    Used by the v4 migration to match an existing Gemini legacy slug to its
    source dir. New rows derive project_id from the session header's cwd.
    """
    return jsonl_path.parent.parent.name.replace("-", "_")


def _legacy_codex_slug(cwd: str) -> str:
    """Lossy slug from a cwd; legacy fallback only.

    Used by the v4 migration to match codex/gemini legacy slugs to their
    source files. New codex rows derive project_id from session_meta.payload.cwd
    via _project_id().
    """
    parts = Path(cwd).parts
    try:
        idx = next(i for i, p in enumerate(parts) if p.lower() == "projects")
        relevant = parts[idx + 1:]
        slug = "_".join(relevant) if relevant else Path(cwd).name
    except StopIteration:
        # No Projects/ in path — use last 2 path components
        relevant = parts[-2:] if len(parts) >= 2 else parts
        slug = "_".join(p for p in relevant if p and p != "/")
    return slug.replace("-", "_")


def _scan_claude_cwd(slug: str) -> str | None:
    """Scan Claude jsonl files for any session matching `slug`, return cwd field.

    Claude stores its session dir as `cwd.replace('/', '-')` — lossy. We can't
    reverse the encoding without scanning record bodies. Read up to ~200 lines
    of each candidate file looking for a `cwd` key.
    """
    from .ingest import PROJECTS_DIR  # lazy: avoids load-time cycle with ingest.py

    if not PROJECTS_DIR.exists():
        return None
    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        # _legacy_claude_slug collapses hyphens to underscores; reverse-test
        # by comparing the slug derivation. Cheap because we're not iterating
        # all files yet — just dirs.
        try:
            test_slug = _legacy_claude_slug(project_dir / "x.jsonl")
        except Exception:
            continue
        if test_slug != slug:
            continue
        for sess in list(project_dir.glob("*.jsonl"))[:5]:
            try:
                with open(sess) as fh:
                    for i, line in enumerate(fh):
                        if i > 200:
                            break
                        try:
                            d = json.loads(line)
                        except (json.JSONDecodeError, ValueError):
                            continue
                        if isinstance(d, dict) and d.get("cwd"):
                            return d["cwd"]
            except OSError:
                continue
    return None


def _scan_codex_cwd(slug: str) -> str | None:
    """Scan Codex rollouts whose session_meta payload.cwd derives `slug`."""
    from .ingest import CODEX_SESSIONS  # lazy: avoids load-time cycle with ingest.py

    if not CODEX_SESSIONS.exists():
        return None
    # Cheap: stop at the first matching cwd. Walk newest first to bias to recent.
    files = sorted(CODEX_SESSIONS.glob("*/*/*/rollout-*.jsonl"), reverse=True)
    for f in files[:200]:  # cap scan budget
        try:
            with open(f) as fh:
                first = json.loads(fh.readline())
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        cwd = (first.get("payload") or {}).get("cwd")
        if not cwd:
            continue
        if _legacy_codex_slug(cwd) == slug:
            return cwd
    return None


def _scan_gemini_cwd(slug: str) -> tuple[str | None, str | None]:
    """For Gemini, attempt to recover real cwd via ~/.gemini/projects.json.

    Returns (cwd, hash_dir_or_None). hash_dir is set when slug looks like a
    SHA-hash dir name and we can't resolve to a real path.
    """
    from .ingest import _load_gemini_aliases  # lazy: A7 moves this to ingest/gemini.py

    aliases = _load_gemini_aliases()
    # aliases is {hash_dir → real_cwd}
    for hash_dir, cwd in aliases.items():
        if _legacy_codex_slug(cwd) == slug:
            return cwd, hash_dir
    # No alias hit — slug might already be a hash_dir name
    return None, slug
