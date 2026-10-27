from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING, override

if TYPE_CHECKING:
    import argparse

from .. import platform as stx_platform
from ..command import Command
from ..core import compose_command, load_registry, trusted_box_name


class SandboxDownCommand(Command):
    """Stop the singleton platform (traefik, postgres, redis, otel)."""

    @classmethod
    @override
    def name(cls) -> str:
        return "down"

    @classmethod
    @override
    def help(cls) -> str:
        return "Stop the platform (all four shared services); use at end of day"

    @override
    def add_parser(self, subparsers) -> argparse.ArgumentParser:
        parser = subparsers.add_parser(self.name(), help=self.help())
        parser.add_argument(
            "-f",
            "--force",
            action="store_true",
            help="Stop the platform even though active sandboxes depend on it",
        )
        return parser

    @override
    def _execute(self, args: argparse.Namespace) -> int | None:
        registry = load_registry()
        active = [s for s, r in registry.items() if trusted_box_name(r.boxes)]
        if active and not args.force:
            slugs = ", ".join(sorted(active))
            print(
                f"error: active sandboxes will lose DB and Redis connectivity: {slugs}\n"
                "Run `stx rm` for each, or use --force to stop anyway.",
                file=sys.stderr,
            )
            return 1

        result = subprocess.run(
            compose_command([stx_platform.compose_file()], "down"),
            env=stx_platform.compose_env(list(stx_platform.ALL_SERVICES)),
        )
        if result.returncode != 0:
            print("error: platform failed to stop", file=sys.stderr)
            return 1
        print("✓ platform down")
        return None
