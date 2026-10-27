from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, override

if TYPE_CHECKING:
    import argparse

from ..command import Command
from ..core import load_registry, trusted_box_name
from ..manifest import Manifest


class SandboxOpenCommand(Command):
    """Open one of the sandbox's manifest routes in the default browser."""

    @classmethod
    @override
    def name(cls) -> str:
        return "open"

    @classmethod
    @override
    def help(cls) -> str:
        return "Open a sandbox route (http://<route>.<slug>.localhost) in the default browser"

    @override
    def add_parser(self, subparsers) -> argparse.ArgumentParser:
        parser = subparsers.add_parser(self.name(), help=self.help())
        parser.add_argument("slug", help="Sandbox slug")
        parser.add_argument(
            "route",
            nargs="?",
            default=None,
            help=(
                "Route name to open (default: the bare <slug>.localhost, which "
                "serves the manifest's first route)"
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
        if trusted_box_name(record.boxes) is None:
            print(
                f"error: sandbox '{slug}' has no trusted box (boxes: "
                f"{', '.join(sorted(record.boxes)) or 'none'}) — no routes",
                file=sys.stderr,
            )
            return 1
        if not record.repo_root:
            print(
                f"error: sandbox '{slug}' has no repo root on record", file=sys.stderr
            )
            return 1

        manifest = Manifest.load(Path(record.repo_root))
        if not manifest.routes:
            print(f"error: {record.repo} declares no routes", file=sys.stderr)
            return 1

        if args.route is None:
            return _open_url(f"http://{slug}.localhost")

        route = args.route
        if route not in manifest.routes:
            print(
                f"error: unknown route '{route}' (routes: "
                f"{', '.join(sorted(manifest.routes))})",
                file=sys.stderr,
            )
            return 1

        return _open_url(f"http://{route}.{slug}.localhost")


def _open_url(url: str) -> int | None:
    cmd = ["open", url] if sys.platform == "darwin" else ["xdg-open", url]
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"error: could not open {url}", file=sys.stderr)
        return 1
    return None
