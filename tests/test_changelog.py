"""C-6 — CHANGELOG has an Unreleased / v0.3.0 entry covering the port."""

from pathlib import Path


_CHANGELOG = Path(__file__).resolve().parents[1] / "CHANGELOG.md"


def _read() -> str:
    return _CHANGELOG.read_text()


def test_changelog_has_unreleased_entry_or_v030():
    text = _read()
    assert "## [Unreleased]" in text or "## [0.3.0]" in text, (
        "CHANGELOG must have a top entry for unreleased changes "
        "(either `## [Unreleased]` or `## [0.3.0]`)"
    )


def test_changelog_mentions_linux_port():
    """The CHANGELOG must surface the cross-platform port —
    one of {Linux, systemd, cross-platform} should appear in the first
    non-empty entry (Unreleased after a fresh release is empty; the
    release-boundary entry like [0.3.0] carries the content)."""
    text = _read()
    keywords = ("Linux", "systemd", "cross-platform")
    for marker in ("## [Unreleased]", "## [0.3.0]"):
        idx = text.find(marker)
        if idx == -1:
            continue
        rest = text[idx + len(marker):]
        next_heading = rest.find("\n## [")
        section = rest[:next_heading] if next_heading != -1 else rest
        if any(k in section for k in keywords):
            return  # passes — first section with the keywords
    raise AssertionError(
        f"no top entry mentions any of {keywords}"
    )


def test_changelog_has_project_id_entry():
    """Post-v4: CHANGELOG announces stable project_id under [0.3.0]."""
    text = _read()
    after = text.split("## [0.3.0]", 1)[1]
    section = after.split("\n## [", 1)[0]
    assert "project_id" in section, "[0.3.0] block must mention project_id"
    assert "display_name" in section, "[0.3.0] block must mention display_name"
    assert "v4" in section.lower() or "_MIGRATION_PROJECT_ID" in section, \
        "[0.3.0] block must reference the v4 migration"


def test_changelog_documents_ingest_hook():
    """Phase 1 hook-driven ingest: [0.3.0] must announce the new hook,
    the Codex caveat, and the opt-out env var."""
    text = _read()
    after = text.split("## [0.3.0]", 1)[1]
    section = after.split("\n## [", 1)[0]
    assert "Response-completion ingest hooks" in section, \
        "[0.3.0] block missing the ingest-hook announcement"
    assert "Codex" in section, "[0.3.0] block should mention the Codex caveat"
    assert "CONVO_RECALL_INGEST_HOOK" in section, \
        "[0.3.0] block should document the opt-out env var"
    assert "--kind" in section, \
        "[0.3.0] block should mention the new --kind CLI flag"
