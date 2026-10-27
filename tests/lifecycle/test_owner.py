from __future__ import annotations

import os
import shutil

import pytest

from stx.lifecycle import current_owner, owner_alive, process_start_time

# A pid far above any real one, so no live process ever wears it here.
_DEAD_PID = 2_000_000_000


def test_this_process_is_alive():
    pid, started = current_owner()
    assert pid == os.getpid()
    assert owner_alive(pid, started)


def test_no_recorded_pid_is_not_alive():
    assert not owner_alive(None, "")


def test_absent_process_is_not_alive():
    assert not owner_alive(_DEAD_PID, "")


@pytest.mark.skipif(
    shutil.which("ps") is None,
    reason="ps is part of every host stx runs on, not every box",
)
def test_mismatched_start_time_means_the_pid_was_reused():
    # Same live pid, but a start-time token that can't be ours → treated as a
    # different process that reused the number.
    assert process_start_time(os.getpid())
    assert not owner_alive(os.getpid(), "0")


def test_bare_liveness_when_start_time_unknown():
    # With no recorded token, fall back to a plain liveness check.
    assert owner_alive(os.getpid(), "")
