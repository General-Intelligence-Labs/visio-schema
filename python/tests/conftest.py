"""Shared pytest configuration.

Skip ``pty``-marked tests on macOS. Those tests simulate a serial device with a
pseudo-terminal, but macOS does not signal pty readability through
select/poll/kqueue the way it does for a real serial device (``/dev/cu.*``) — a
reader waiting on the pty for data (or EOF) blocks forever there. This is a
test-harness limitation, NOT a product bug: real macOS serial signals readability
normally (it's how pyserial works), so ``read_serial`` / the launcher work on real
hardware; only the pty stand-in doesn't. These tests run on Linux (the fleet), and
macOS still exercises the codec, golden vectors, and native-decode paths.
"""
import sys

import pytest


def pytest_collection_modifyitems(config, items):
    if sys.platform != "darwin":
        return
    skip_pty = pytest.mark.skip(
        reason="pty readability isn't signaled on macOS (test-harness limitation, not a "
        "product bug); covered on Linux + by the golden-vector / codec tests on macOS"
    )
    for item in items:
        if "pty" in item.keywords:
            item.add_marker(skip_pty)
