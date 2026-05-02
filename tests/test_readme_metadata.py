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


def test_readme_has_project_identity_section():
    """Post-v4: README documents project_id + display_name + cross-machine limitation."""
    text = _read()
    assert "Project identity" in text, \
        "README missing a `Project identity` subsection"
    assert "project_id" in text, "README should mention project_id"
    assert "display_name" in text, "README should mention display_name"
    assert "cross-machine" in text.lower() or "Cross-machine" in text, \
        "README should document the cross-machine identity limitation"


def test_readme_documents_continuous_ingest():
    """Phase 1 hook-driven ingest: README documents the response-completion hook,
    its three event names, the Codex feature flag, and the opt-out env var."""
    text = _read()
    assert "Continuous ingest" in text, \
        "README missing a 'Continuous ingest' subsection"
    # All three response-end event names appear.
    for event in ("Stop", "AfterAgent"):
        assert event in text, f"README should mention the `{event}` event"
    # Codex feature flag.
    assert "codex_hooks" in text, "README should mention the codex_hooks feature flag"
    # Opt-out env var.
    assert "CONVO_RECALL_INGEST_HOOK" in text, \
        "README should document the CONVO_RECALL_INGEST_HOOK opt-out"
    # Doctor is the verification path.
    assert "recall doctor" in text, "README should point users at `recall doctor`"
