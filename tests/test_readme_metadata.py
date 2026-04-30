"""C-5 — README documents Linux support and the four-tier scheduler ladder."""

import re
from pathlib import Path


_README = Path(__file__).resolve().parents[1] / "README.md"


def _read() -> str:
    return _README.read_text()


def test_readme_mentions_linux():
    text = _read()
    assert "Linux" in text, "README must mention Linux now that the port is shipped"


def test_readme_drops_macos_only_claim():
    text = _read().lower()
    assert "macos only" not in text, (
        "README still claims macOS only — drop the qualifier"
    )
    assert "requires macos" not in text, (
        "README still says `requires macOS` — replace with `macOS or Linux`"
    )


def test_readme_has_schedulers_section():
    text = _read()
    has_section = bool(re.search(r"^##+ Schedulers\b", text, re.MULTILINE))
    assert has_section, "README is missing a `## Schedulers` (or `### Schedulers`) heading"


def test_readme_documents_each_scheduler_name():
    text = _read()
    for name in ("launchd", "systemd", "cron", "polling"):
        assert name in text, f"README does not mention {name!r}"


def test_readme_has_ci_badge():
    text = _read()
    assert "actions/workflows/test.yml/badge.svg" in text, (
        "README is missing the CI status badge"
    )
