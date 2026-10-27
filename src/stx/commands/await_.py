from __future__ import annotations

import sys
from typing import TYPE_CHECKING, override

if TYPE_CHECKING:
    import argparse

from ..command import Command
from ..core import (
    box_not_found_error,
    container_name_for_box,
    default_box,
    load_registry,
    wait_for_agent,
)
from ..errors import StxError


class SandboxAwaitCommand(Command):
    """Block until a box's dispatched agent finishes, then print its answer."""

    @classmethod
    @override
    def name(cls) -> str:
        return "await"

    @classmethod
    @override
    def help(cls) -> str:
        return "Block until a dispatched agent finishes, then print its answer"

    @override
    def add_parser(self, subparsers) -> argparse.ArgumentParser:
        parser = subparsers.add_parser(self.name(), help=self.help())
        parser.add_argument("slug", help="Sandbox slug")
        parser.add_argument(
            "-b",
            "--box",
            default=None,
            help="Box to await (default: the trusted box, or dmz if there's none)",
        )
        parser.add_argument(
            "--timeout",
            type=float,
            default=None,
            metavar="SECONDS",
            help="Give up waiting after this many seconds (default: wait forever)",
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
        box = args.box or default_box(record)
        if box not in record.boxes:
            print(f"error: {box_not_found_error(slug, box, record)}", file=sys.stderr)
            return 1

        container = container_name_for_box(box, slug)
        user = record.boxes[box].user

        try:
            exit_code = wait_for_agent(
                slug, box, container, user=user, timeout=args.timeout
            )
        except StxError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        if exit_code is None:
            print(f"error: no agent has been dispatched in '{box}'", file=sys.stderr)
            return 1

        return 0 if exit_code == 0 else 1
