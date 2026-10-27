"""The box setup outcome record — see docs/box-contract.md (step 8) and
docs/reference.md (Setup). `init-box.sh` writes it after running (or
skipping) the repo's `setup` scripts; `new`/`refresh` wait on it before
reporting a sandbox ready, and `agent` refuses to dispatch until it exists.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from ..errors import StxError
from .box_env import SYSTEM_PATH
from .compose import container_recent_logs, container_status, run_bounded

CompletedProcessRunner = Callable[..., "subprocess.CompletedProcess[str]"]

SETUP_STATUS_PATH = "/run/stx/setup-status.json"
DEFAULT_SETUP_TIMEOUT_SECONDS = 15 * 60
_POLL_INTERVAL_SECONDS = 1
_HEARTBEAT_INTERVAL_SECONDS = 30


class SetupState(StrEnum):
    DONE = "done"
    FAILED = "failed"


@dataclass
class SetupStatus:
    state: SetupState
    script: str = ""  # set only when state is FAILED
    exit_code: int | None = None  # set only when state is FAILED


def read_setup_status(
    container: str, *, runner: CompletedProcessRunner = subprocess.run
) -> SetupStatus | None:
    """The box runtime's last-written setup record for a box's container.

    None means `init-box.sh` hasn't reached `run_setup_scripts` yet (or the
    container isn't up). The record is root-owned under `/run/stx`, so this
    reads it as root — but with a pinned `PATH`, so the box user can't shadow
    `cat` with one of their own and have this root exec run it. `runner` is a
    seam for tests to inject a fake subprocess result.
    """
    result = run_bounded(
        [
            "docker",
            "exec",
            "--env",
            f"PATH={SYSTEM_PATH}",
            container,
            "cat",
            SETUP_STATUS_PATH,
        ],
        runner=runner,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    try:
        raw = json.loads(result.stdout)
        return SetupStatus(
            state=SetupState(raw["state"]),
            script=raw.get("script", ""),
            exit_code=raw.get("exit_code"),
        )
    except (json.JSONDecodeError, KeyError, ValueError):
        return None


def wait_for_setup(
    container: str,
    box: str,
    *,
    timeout: float = DEFAULT_SETUP_TIMEOUT_SECONDS,
    runner: CompletedProcessRunner = subprocess.run,
) -> SetupStatus:
    """Block until `box`'s setup record appears, printing a heartbeat line
    every 30s. Raises StxError on timeout, or as soon as the container itself
    is no longer running — the entrypoint can exit (firewall setup failed,
    tinyproxy didn't bind) long before the timeout would, and waiting it out
    anyway would just report a dead box as ready. A `failed` record is
    returned, not raised — the setup scripts ran to a known (bad) outcome,
    which the caller reports itself; only "still don't know" is exceptional
    here."""
    start = time.monotonic()
    deadline = start + timeout
    next_heartbeat = start + _HEARTBEAT_INTERVAL_SECONDS
    while True:
        status = read_setup_status(container, runner=runner)
        if status is not None:
            return status
        state = container_status(container, runner=runner)
        if state != "running":
            logs = container_recent_logs(container, runner=runner)
            detail = f"\n{logs}" if logs else ""
            raise StxError(
                f"box '{box}' exited before setup finished "
                f"(container state: {state or 'missing'}){detail}"
            )
        now = time.monotonic()
        if now >= deadline:
            raise StxError(
                f"timed out after {timeout:g}s waiting for setup to finish in '{box}'"
            )
        if now >= next_heartbeat:
            print(
                f"  ... still waiting for setup in '{box}' ({int(now - start)}s)",
                file=sys.stderr,
            )
            next_heartbeat = now + _HEARTBEAT_INTERVAL_SECONDS
        time.sleep(_POLL_INTERVAL_SECONDS)
