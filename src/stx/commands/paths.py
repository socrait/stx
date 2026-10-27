from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Callable, override

if TYPE_CHECKING:
    import argparse

from ..command import Command
from ..config import (
    config_path,
    config_path_origin,
    config_value,
    default_dotenv_path,
    load_config,
)
from ..core import (
    SLUG_RE,
    allowlist_path,
    code_dmz_root,
    compose_overlay_path,
    drop_dir,
    drop_dmz_dir,
    logs_root,
    state_root,
    state_root_origin,
    traefik_routes_dir,
    worktree_path,
)
from ..errors import StxError
from ..platform import platform_dir


def _dotenv() -> tuple[Path, str | None]:
    config = load_config()
    configured = config_value(config, "secrets.dotenv.path")
    path, origin = (
        (Path(configured), "secrets.dotenv.path")
        if configured
        else (default_dotenv_path(), "default")
    )
    if config_value(config, "secrets.source") == "op":
        origin += "; unused, secrets.source=op"
    return path, origin


_GLOBAL_PATHS: dict[str, Callable[[], tuple[Path, str | None]]] = {
    "state": lambda: (state_root(), state_root_origin()),
    "config": lambda: (config_path(), config_path_origin()),
    "dotenv": _dotenv,
    "platform": lambda: (platform_dir(), None),
    "routes": lambda: (traefik_routes_dir(), None),
}

_SLUG_PATHS: dict[str, Callable[[str], Path]] = {
    "worktree": worktree_path,
    "drop": drop_dir,
    "drop-dmz": drop_dmz_dir,
    "code-dmz": code_dmz_root,
    "logs": logs_root,
    "compose": compose_overlay_path,
    "allowlist": allowlist_path,
}


class SandboxPathsCommand(Command):
    """Print where `stx` keeps things — see docs/reference.md (Commands and
    flags, State layout)."""

    @classmethod
    @override
    def name(cls) -> str:
        return "paths"

    @classmethod
    @override
    def help(cls) -> str:
        return "Print where stx keeps its config, secrets and state"

    @override
    def add_parser(self, subparsers) -> argparse.ArgumentParser:
        parser = subparsers.add_parser(self.name(), help=self.help())
        parser.add_argument(
            "path_name",
            nargs="?",
            metavar="NAME",
            help="Print only this path, bare "
            f"({', '.join([*_GLOBAL_PATHS, *_SLUG_PATHS])})",
        )
        parser.add_argument(
            "--slug", help="Show one sandbox's paths instead of the global ones"
        )
        return parser

    @override
    def _execute(self, args: argparse.Namespace) -> int | None:
        slug = args.slug
        if slug is not None and not SLUG_RE.fullmatch(slug):
            raise StxError(f"'{slug}' is not a valid sandbox slug")
        name = args.path_name
        if name is None:
            _print_table(slug)
            return None
        if name in _GLOBAL_PATHS:
            print(_GLOBAL_PATHS[name]()[0])
            return None
        if name in _SLUG_PATHS:
            if slug is None:
                print(f"error: '{name}' needs --slug", file=sys.stderr)
                return 1
            print(_SLUG_PATHS[name](slug))
            return None
        print(
            f"error: unknown path '{name}' "
            f"(known: {', '.join([*_GLOBAL_PATHS, *_SLUG_PATHS])})",
            file=sys.stderr,
        )
        return 1


def _print_table(slug: str | None) -> None:
    if slug is None:
        rows = [(name, *resolve()) for name, resolve in _GLOBAL_PATHS.items()]
    else:
        rows = [(name, resolve(slug), None) for name, resolve in _SLUG_PATHS.items()]
    width = max(len(name) for name, _, _ in rows)
    for name, path, origin in rows:
        suffix = f"  ({origin})" if origin else ""
        print(f"{name:<{width}}  {path}{suffix}")
