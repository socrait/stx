from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING, override

if TYPE_CHECKING:
    import argparse

from .. import platform as stx_platform
from ..command import Command
from ..core import (
    DEFAULT_SETUP_TIMEOUT_SECONDS,
    SetupState,
    container_name_for_box,
    container_status,
    load_registry,
    logs_dir,
    run_bounded,
    sandbox_has_own_network,
    trusted_box_name,
    wait_for_setup,
)
from ..core.compose import CompletedProcessRunner
from ..errors import StxError

_DOCKER_START_TIMEOUT_SECONDS = 30


def stopped_box_hint(slug: str) -> str:
    """What `shell`/`agent`/`code` should print when a sandbox's container
    isn't running — e.g. after a host reboot, a box's containers don't come
    back on their own."""
    return f"box isn't running — `stx start {slug}` brings it back up"


def _start_container(
    container: str, *, runner: CompletedProcessRunner = subprocess.run
) -> None:
    """Start a sandbox's own container, idempotent when it's already
    running. Raises naming the container when it doesn't exist at all —
    distinct from merely stopped, `start` can't recreate what `rm` (or a
    manual `docker rm`) removed."""
    state = container_status(container, runner=runner)
    if state is None:
        raise StxError(
            f"container '{container}' doesn't exist — `stx start` only "
            "restarts a stopped sandbox, it can't recreate a removed one; "
            "try `stx new` or `stx refresh`"
        )
    if state == "running":
        return
    result = run_bounded(
        ["docker", "start", container],
        runner=runner,
        capture_output=True,
        text=True,
        timeout=_DOCKER_START_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise StxError(f"could not start '{container}': {result.stderr.strip()}")


class SandboxStartCommand(Command):
    """Start a stopped sandbox's containers, re-attach the platform services
    it uses, and wait for its app box's setup to finish.

    See docs/reference.md (`start` row).
    """

    @classmethod
    @override
    def name(cls) -> str:
        return "start"

    @classmethod
    @override
    def help(cls) -> str:
        return "Start a stopped sandbox back up (e.g. after a host reboot)"

    @override
    def add_parser(self, subparsers) -> argparse.ArgumentParser:
        parser = subparsers.add_parser(self.name(), help=self.help())
        parser.add_argument("slug", help="Sandbox slug")
        parser.add_argument(
            "--timeout",
            type=float,
            default=DEFAULT_SETUP_TIMEOUT_SECONDS,
            metavar="SECONDS",
            help=(
                "Give up waiting for the app box's `setup` scripts to finish "
                f"after this many seconds (default: {DEFAULT_SETUP_TIMEOUT_SECONDS:g}, "
                "15m)"
            ),
        )
        return parser

    @override
    def _execute(self, args: argparse.Namespace) -> int | None:
        slug = args.slug
        registry = load_registry()
        if slug not in registry:
            print(f"error: no sandbox named '{slug}'", file=sys.stderr)
            return 1
        record = registry[slug]

        try:
            # Ensure the network first — a box attaches to it (via the
            # overlay's `networks:` entry) when its container starts, so it
            # must already exist. Only a sandbox that has its own network: a
            # dmz-only or stuck entry has none, and creating one here would
            # orphan a `--net` nothing ever removes. A host reboot can lose the
            # platform services' attachments even though the network survives.
            if sandbox_has_own_network(record):
                stx_platform.connect_services_to_sandbox_network(slug, record.services)
            for box in record.boxes:
                _start_container(container_name_for_box(box, slug))
        except StxError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

        app_box = trusted_box_name(record.boxes)
        if app_box is None:
            print(f"✓ sandbox '{slug}' started")
            return None

        container = container_name_for_box(app_box, slug)
        print(f"  waiting for setup in '{app_box}' (timeout {args.timeout:g}s)")
        try:
            status = wait_for_setup(container, app_box, timeout=args.timeout)
        except StxError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        if status.state == SetupState.FAILED:
            log_path = logs_dir(slug, app_box) / "setup.log"
            print(
                f"error: setup script '{status.script}' failed with exit code "
                f"{status.exit_code} in '{app_box}' — see {log_path}",
                file=sys.stderr,
            )
            return 1

        print(f"✓ sandbox '{slug}' started")
        return None
