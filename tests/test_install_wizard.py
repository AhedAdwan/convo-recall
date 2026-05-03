"""C-1 — pexpect-driven `recall install` wizard tests.

Drives the real `recall` binary through the full interactive flow
under `--dry-run --scheduler polling`, so the test runs identically on
macOS and Linux (no launchd / systemd / cron dependency, no real
process spawn). Catches every prompt printed and `[Y/n]` consumed —
the surface unit tests can't reach.

Skipped (not failed) when `recall` isn't on PATH or `pexpect` isn't
installed; CI gets it from `[project.optional-dependencies] dev`.
"""

import shutil

import pytest


pexpect = pytest.importorskip("pexpect")
pytestmark = pytest.mark.skipif(
    shutil.which("recall") is None,
    reason="`recall` not on PATH (editable install required)",
)


_DEFAULT_TIMEOUT = 10


def _spawn(args: list[str], timeout: int = _DEFAULT_TIMEOUT) -> "pexpect.spawn":
    """Launch `recall <args>` under pexpect."""
    return pexpect.spawn(
        shutil.which("recall"),
        args=args,
        timeout=timeout,
        encoding="utf-8",
    )


def test_wizard_full_yes_flow_dry_run():
    """Walk every [Y/n] prompt with `y` under --scheduler polling --dry-run.
    No watcher / sidecar / hook / ingest spawn, no real touch of the host."""
    wizard = _spawn(["install", "--scheduler", "polling", "--dry-run"])
    try:
        wizard.expect("Selected scheduler:")
        wizard.expect("polling \\(Popen fallback\\)")
        # Stream `y` to every [Y/n] until EOF. Since v0.3.5 the watcher
        # question (which used the scheduler's consequence_yes/no text) is
        # suppressed; the first prompt is now Step 1/4 — ingest hooks.
        # Now stream `y` to every [Y/n] until EOF.
        while True:
            idx = wizard.expect([r"\[Y/n\]", pexpect.EOF], timeout=15)
            if idx == 0:
                wizard.sendline("y")
            else:
                break
    finally:
        wizard.close()
    assert wizard.exitstatus == 0, (
        f"wizard exited {wizard.exitstatus}; transcript:\n{wizard.before}"
    )


def test_wizard_decline_hooks_consequence_appears():
    """Reach the hooks question (Step 3) — accepting every prior prompt —
    then decline. Assert the hooks `if NO:` consequence text was printed
    before we answered."""
    wizard = _spawn(["install", "--scheduler", "polling", "--dry-run"])
    try:
        # Walk forward until we see the hooks question, accepting every
        # [Y/n] along the way (watchers + embed-sidecar). Stop at hooks.
        saw_hooks_consequence = False
        while True:
            idx = wizard.expect([
                r"Wire pre-prompt hooks now\?",
                r"\[Y/n\]",
                pexpect.EOF,
            ], timeout=15)
            if idx == 0:
                # Found the hooks question. Now look for its consequence_no
                # text and the [Y/n] prompt that follows.
                wizard.expect(r"Agents won't see convo-recall hints")
                saw_hooks_consequence = True
                wizard.expect(r"\[Y/n\]")
                wizard.sendline("n")
                break
            elif idx == 1:
                wizard.sendline("y")
            else:
                pytest.fail(
                    f"wizard ended before reaching the hooks question; "
                    f"transcript:\n{wizard.before}"
                )
        assert saw_hooks_consequence
        # Drain the rest of the wizard.
        while True:
            idx = wizard.expect([r"\[Y/n\]", pexpect.EOF], timeout=15)
            if idx == 0:
                wizard.sendline("y")
            else:
                break
    finally:
        wizard.close()
    assert wizard.exitstatus == 0


def test_wizard_abort_at_final_confirm():
    """Accept watchers but abort at the final 'Apply these settings now?'.
    Wizard prints 'Aborted. No changes made.' and exits 0.

    Note: --dry-run short-circuits before the final confirm in the current
    wizard, so this case is exercised WITHOUT --dry-run; we still pass
    --scheduler polling + reach the final prompt without ever executing
    install (we say `n` at the apply gate)."""
    wizard = _spawn(["install", "--scheduler", "polling"])
    try:
        # Drive through Q1-Q4 with `y` until we hit "Apply these settings now?"
        while True:
            idx = wizard.expect([
                r"Apply these settings now\?",
                r"\[Y/n\]",
                pexpect.EOF,
            ], timeout=15)
            if idx == 0:
                # Found the final confirm; consume its [Y/n] and decline.
                wizard.expect(r"\[Y/n\]")
                wizard.sendline("n")
                break
            elif idx == 1:
                wizard.sendline("y")
            else:
                pytest.fail(
                    f"wizard ended before reaching final confirm; "
                    f"transcript:\n{wizard.before}"
                )
        wizard.expect_exact("Aborted. No changes made.")
        wizard.expect(pexpect.EOF, timeout=15)
    finally:
        wizard.close()
    assert wizard.exitstatus == 0


# ── H04 — wizard prompts for ingest hooks (Step 1/4 since v0.3.5) ───────────


def test_wizard_prompts_for_ingest_hooks():
    """Step 1/4 surfaces the response-completion ingest hook prompt with
    its consequence_yes/no callouts before the embed sidecar step. Since
    v0.3.5 the watcher-install question is suppressed (TD-004 mitigation),
    so the ingest-hooks step is now the FIRST prompt the user sees."""
    wizard = _spawn(["install", "--scheduler", "polling", "--dry-run"])
    try:
        wizard.expect("Step 1/4: response-completion ingest hooks")
        wizard.expect_exact("Wire response-completion ingest hooks now?")
        # Consequence-yes line mentions "Stop / AfterAgent".
        wizard.expect("Stop / AfterAgent")
        while True:
            idx = wizard.expect([r"\[Y/n\]", pexpect.EOF], timeout=15)
            if idx == 0:
                wizard.sendline("y")
            else:
                break
    finally:
        wizard.close()
    assert wizard.exitstatus == 0


def test_wizard_renumbered_steps_show_4_total():
    """All four step headers appear in order so the v0.3.5 renumber (5→4
    after dropping the watcher question) is visible and old `Step N/5`
    strings don't leak through. Step 2 (embed sidecar) auto-skips without
    a [Y/n] when [embeddings] extra isn't installed (CI default), so we
    expect either [Y/n] OR the next label."""
    wizard = _spawn(["install", "--scheduler", "polling", "--dry-run"])
    try:
        wizard.expect("Step 1/4: response-completion ingest hooks")
        for label in (
            "Step 2/4: hybrid vector",
            "Step 3/4: pre-prompt search hooks",
            "Step 4/4: initial ingest",
        ):
            while True:
                idx = wizard.expect([r"\[Y/n\]", label, pexpect.EOF], timeout=15)
                if idx == 0:
                    wizard.sendline("y")
                elif idx == 1:
                    break
                else:
                    pytest.fail(f"wizard ended before reaching {label!r}")
        while True:
            idx = wizard.expect([r"\[Y/n\]", pexpect.EOF], timeout=15)
            if idx == 0:
                wizard.sendline("y")
            else:
                break
    finally:
        wizard.close()
    assert wizard.exitstatus == 0


def test_wizard_does_not_ask_about_watchers_or_linger():
    """v0.3.5 — the watcher-install question (and its systemd linger
    sub-prompt) are suppressed. The wizard must NOT print 'Install
    polling watchers?' or 'Keep watchers running when logged out?' under
    any scheduler. Mitigates TD-004 by never spawning the second
    detached subprocess that races the backfill child for the WAL lock."""
    wizard = _spawn(["install", "--scheduler", "polling", "--dry-run"])
    transcript_parts: list[str] = []
    try:
        while True:
            idx = wizard.expect([r"\[Y/n\]", pexpect.EOF], timeout=15)
            transcript_parts.append(wizard.before or "")
            if idx == 0:
                transcript_parts.append(wizard.after or "")
                wizard.sendline("y")
            else:
                break
    finally:
        wizard.close()
    transcript = "".join(transcript_parts)
    assert "Install polling" not in transcript, (
        f"watcher question leaked into transcript:\n{transcript}"
    )
    assert "Keep watchers running" not in transcript, (
        f"linger question leaked into transcript:\n{transcript}"
    )
    assert wizard.exitstatus == 0
