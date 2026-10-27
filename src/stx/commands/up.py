from __future__ import annotations

from typing import TYPE_CHECKING, override

if TYPE_CHECKING:
    import argparse

from .. import platform as stx_platform
from ..command import Command


class SandboxUpCommand(Command):
    """Start the shared platform services, or recreate drifted ones."""

    @classmethod
    @override
    def name(cls) -> str:
        return "up"

    @classmethod
    @override
    def help(cls) -> str:
        return (
            "Start platform services (default: all four); auto-called by `new` "
            "for just the services a manifest needs. Never recreates a running "
            "one — pass --recreate for that"
        )

    @override
    def add_parser(self, subparsers) -> argparse.ArgumentParser:
        parser = subparsers.add_parser(self.name(), help=self.help())
        parser.add_argument(
            "services",
            nargs="*",
            choices=list(stx_platform.ALL_SERVICES),
            metavar="SERVICE",
            help=(
                "Which platform service(s) to start — one or more of "
                f"{', '.join(stx_platform.ALL_SERVICES)} (default: all four)"
            ),
        )
        parser.add_argument(
            "--recreate",
            action="store_true",
            help=(
                "Recreate services that have drifted from their compose "
                "definition. With a sandbox using one mid-dispatch, refuses "
                "unless -y/--yes is also passed"
            ),
        )
        parser.add_argument(
            "-y",
            "--yes",
            action="store_true",
            help=(
                "With --recreate, recreate anyway even if a sandbox has an "
                "agent dispatch in progress on a differing service"
            ),
        )
        return parser

    @override
    def _execute(self, args: argparse.Namespace) -> int | None:
        services = (
            list(args.services) if args.services else list(stx_platform.ALL_SERVICES)
        )
        if args.recreate:
            return stx_platform.run_platform_recreate(services, yes=args.yes)
        return stx_platform.run_platform_up(services)
