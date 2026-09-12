"""Smoke test for slrharness package."""

import slrharness


def test_version():
    assert slrharness.__version__ == "0.1.0"
