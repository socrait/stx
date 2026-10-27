from __future__ import annotations

import sys
from importlib import resources
from typing import TYPE_CHECKING, override

if TYPE_CHECKING:
    import argparse

from ..command import Command
from ..core import load_registry
from ..forge.registry import sandboxes_with_expiring_forge_tokens

_FRONTMATTER_END = "\n---\n"


class SandboxGuideCommand(Command):
    """Print the host skill guide, for a machine with no repo and no Claude Code plugin."""

    @classmethod
    @override
    def name(cls) -> str:
        return "guide"

    @classmethod
    @override
    def help(cls) -> str:
        return "Print the stx host skill guide"

    @override
    def add_parser(self, subparsers) -> argparse.ArgumentParser:
        parser = subparsers.add_parser(self.name(), help=self.help())
        parser.add_argument(
            "--raw",
            action="store_true",
            help="Print the whole skill file verbatim, including its frontmatter",
        )
        return parser

    @override
    def _execute(self, args: argparse.Namespace) -> int | None:
        for slug in sandboxes_with_expiring_forge_tokens(load_registry()):
            print(
                f"warning: '{slug}' forge tokens expire within 24h — run "
                f"`stx refresh {slug}`",
                file=sys.stderr,
            )

        text = _skill_text()
        sys.stdout.write(text if args.raw else _strip_frontmatter(text))
        return None


def _skill_text() -> str:
    return resources.files("stx").joinpath("skill", "SKILL.md").read_text()


def _strip_frontmatter(text: str) -> str:
    if not text.startswith("---\n"):
        return text
    end = text.find(_FRONTMATTER_END, len("---\n"))
    if end == -1:
        return text
    return text[end + len(_FRONTMATTER_END) :]
