# Contributing to convo-recall

Thanks for considering a contribution. This file is the short version of how the project is run; the [README](README.md), [CHANGELOG](CHANGELOG.md), and [docs/TECH_DEBT.md](docs/TECH_DEBT.md) carry the rest of the context.

## Before you open a PR

1. **Sign the CLA** (`CLA.md`) — every PR contributor needs the lightweight DCO-style sign-off plus the explicit grant. The CLA exists so the maintainer can keep offering commercial licenses while the project remains FSL-1.1-Apache-2.0 source-available; it isn't optional, but it's one paragraph.
2. **Skim the [Acceptable Use Policy](ACCEPTABLE_USE.md)** — convo-recall has perpetual ethical-use restrictions (no government / military / mass-surveillance use) that apply across all licenses. Not a contribution barrier, but worth knowing what the project is and isn't OK with.
3. **Check if your change matches an open issue or `docs/TECH_DEBT.md` row.** TD-NNN entries are the maintainer's running list of paydown work; touching one is the easiest path to "this PR matches a need we already wanted addressed."

## Local development

The project is a single-package Python tool with embedded shell hooks. macOS and Linux are first-class; Windows is unsupported (hooks rely on POSIX UDS sockets).

```bash
git clone https://github.com/AhedAdwan/convo-recall.git
cd convo-recall
pipx install -e '.[embeddings]'      # editable install with the embedding sidecar deps
recall install                       # interactive wizard wires hooks + sidecar
```

Editable install means edits to `src/convo_recall/` take effect on the next `recall …` invocation without reinstalling. Wizard prompts can be answered with the defaults if you're not sure — they all default to the safe option.

## Running the test suite

```bash
python -m pytest tests/ -q                       # full suite (~2 min on the maintainer's M-series Mac)
python -m pytest tests/test_<area>.py -v         # focused
python tests/integrity_sweep.py                  # 65-probe health check against the live install
```

CI runs the full suite on Linux + macOS × Python 3.11–3.14. PRs that don't pass CI won't be merged.

The `tests/integrity_sweep.py` is the secondary battery — it exercises the **installed** convo-recall against your **live DB** and reports PASS / SKIP / FAIL across 10 sections (install sanity, DB integrity, embedding subsystem, search, tail, hooks, per-agent ingest, tool_error extraction, data quality, source-code review). Useful as a smoke check after upgrading.

## Conventions

- **Commit messages** follow the existing `type(scope): subject` shape — see `git log --oneline` for examples (`refactor(ingest): …`, `fix(test): …`, `docs(readme): …`).
- **Comments**: only when the *why* is non-obvious. Self-explanatory code doesn't need narration.
- **Tests for fixes**: every bug fix lands with a regression test that fails on the old code and passes on the new.
- **TD-NNN trail**: when you discover a debt item that's out of scope for the current PR, log it in `docs/TECH_DEBT.md` rather than expanding the PR.

## Reporting issues

Use the [GitHub Issues](https://github.com/AhedAdwan/convo-recall/issues) tab. The issue templates (bug report / feature request) prompt for the bits the maintainer needs to act on the report — please fill them out rather than free-typing.

For security-sensitive issues, follow [SECURITY.md](SECURITY.md) — don't open a public issue.

## Questions

[GitHub Discussions](https://github.com/AhedAdwan/convo-recall/discussions) is the right place for usage questions, design questions, or anything that's not a defect or an explicit feature request.
