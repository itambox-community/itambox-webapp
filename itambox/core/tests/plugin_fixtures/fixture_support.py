"""Shared helpers for the plugin startup fixtures (test-only, never shipped)."""

from packaging.version import parse

from itambox.release import VERSION


def current_range():
    """A manifest range that always contains the running ITAMbox release."""
    release = parse(VERSION).release
    major, minor = release[0], release[1] if len(release) > 1 else 0
    return f">={major}.{minor},<{major}.{minor + 1}"
