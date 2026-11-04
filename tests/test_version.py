"""The version has one source: ``logpyt.__version__`` (read by hatch)."""

from __future__ import annotations

from importlib.metadata import version

import logpyt


def test_installed_metadata_matches_the_package_version() -> None:
    """The distribution version is the one written in logpyt/__init__.py."""
    assert version("logpyt") == logpyt.__version__
