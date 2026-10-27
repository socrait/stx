"""Identifying the process that owns a sandbox mid-provisioning.

`new` stamps the provisioning registry entry with its own pid and a start-time
token; `prune` reads them back to tell a crashed `new` (safe to clean up) from
one still running (must be left alone). The start-time token guards against pid
reuse — a bare "is this pid alive" check would mistake an unrelated process that
happened to inherit the number for the original owner.
"""

from __future__ import annotations

import os
import subprocess


def process_start_time(pid: int) -> str:
    """A token distinguishing `pid`'s current process from a future one that
    reuses the number: its start time as `ps` reports it, which works on both
    macOS and Linux hosts. Empty when it can't be read (the process is already
    gone); callers then fall back to a bare liveness check."""
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=10,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def current_owner() -> tuple[int, str]:
    """This process's `(pid, start-time token)` — what `new` records on the
    provisioning entry it owns."""
    pid = os.getpid()
    return pid, process_start_time(pid)


def owner_alive(pid: int | None, started: str) -> bool:
    """Whether the process that reserved a provisioning entry is still running.

    A recorded start-time token that no longer matches the pid's current one
    means the number was reused by an unrelated process — the original owner
    is gone. No recorded pid (an older entry, or a crash before the stamp
    landed) counts as not alive, so such an entry stays eligible for pruning.
    """
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # The pid exists but belongs to another user — alive either way.
        return True
    except OSError:
        return False
    if started:
        current = process_start_time(pid)
        if current and current != started:
            return False
    return True
