"""C-4 — pyproject.toml classifiers + dev deps reflect cross-platform support.

`Operating System :: POSIX :: Linux` was added; `Operating System :: MacOS :: MacOS X`
was dropped (replaced by the broader `Operating System :: MacOS`).

`pexpect` is in the dev extras so CI can run the wizard tests.
"""

import sys
import tomllib
from pathlib import Path


_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _load() -> dict:
    return tomllib.loads(_PYPROJECT.read_text())


def test_classifiers_include_linux():
    data = _load()
    classifiers = data["project"]["classifiers"]
    assert "Operating System :: POSIX :: Linux" in classifiers, (
        f"missing Linux classifier; got: {classifiers}"
    )


def test_classifiers_drop_macos_x_exclusivity():
    data = _load()
    classifiers = data["project"]["classifiers"]
    assert "Operating System :: MacOS :: MacOS X" not in classifiers, (
        f"old MacOS X-only classifier should be dropped: {classifiers}"
    )


def test_classifiers_keep_macos_support():
    data = _load()
    classifiers = data["project"]["classifiers"]
    assert "Operating System :: MacOS" in classifiers, (
        f"macOS support classifier missing: {classifiers}"
    )


def test_dev_deps_include_pexpect():
    data = _load()
    dev = data["project"]["optional-dependencies"]["dev"]
    assert any(spec.startswith("pexpect") for spec in dev), (
        f"pexpect missing from [project.optional-dependencies].dev: {dev}"
    )
