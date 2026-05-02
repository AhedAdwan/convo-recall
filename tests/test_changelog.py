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
    """The newest CHANGELOG entry must surface the cross-platform port —
    one of {Linux, systemd, cross-platform} should appear before the
    next versioned heading."""
    text = _read()
    # Find the top entry (Unreleased or 0.3.0) and slice up to the next `## [`.
    head_markers = ("## [Unreleased]", "## [0.3.0]")
    start = -1
    for marker in head_markers:
        idx = text.find(marker)
        if idx != -1:
            start = idx + len(marker)
            break
    assert start != -1, "could not locate top changelog entry"

    # Slice until next `## [` heading.
    rest = text[start:]
    next_heading = rest.find("\n## [")
    if next_heading != -1:
        section = rest[:next_heading]
    else:
        section = rest

    keywords = ("Linux", "systemd", "cross-platform")
    assert any(k in section for k in keywords), (
        f"top changelog entry must mention one of {keywords}; "
        f"section was:\n{section}"
    )


def test_changelog_has_project_id_entry():
    """Post-v4: CHANGELOG announces stable project_id under Unreleased."""
    text = _read()
    after = text.split("## [Unreleased]", 1)[1]
    unreleased = after.split("\n## [", 1)[0]
    assert "project_id" in unreleased, "Unreleased block must mention project_id"
    assert "display_name" in unreleased, "Unreleased block must mention display_name"
    assert "v4" in unreleased.lower() or "_MIGRATION_PROJECT_ID" in unreleased, \
        "Unreleased block must reference the v4 migration"
