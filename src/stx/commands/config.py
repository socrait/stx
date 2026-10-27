from __future__ import annotations

import sys
from typing import TYPE_CHECKING, override

if TYPE_CHECKING:
    import argparse

from ..command import Command
from ..config import (
    KNOWN_CONFIG_KEYS,
    config_value,
    load_config,
    set_config_value,
    unset_config_value,
)


class SandboxConfigCommand(Command):
    """Manage per-human `stx` preferences — see docs/reference.md (User config)."""

    @classmethod
    @override
    def name(cls) -> str:
        return "config"

    @classmethod
    @override
    def help(cls) -> str:
        return "Manage per-human sandbox preferences (e.g. branch.user)"

    @override
    def add_parser(self, subparsers) -> argparse.ArgumentParser:
        parser = subparsers.add_parser(self.name(), help=self.help())
        actions = parser.add_subparsers(dest="config_action", required=True)

        actions.add_parser("list", help="Show all known keys and their current values")

        get_parser = actions.add_parser("get", help="Print one key's value")
        get_parser.add_argument("key")

        set_parser = actions.add_parser(
            "set", help="Validate and write one key's value"
        )
        set_parser.add_argument("key")
        set_parser.add_argument("value")

        unset_parser = actions.add_parser("unset", help="Remove one key's override")
        unset_parser.add_argument("key")

        return parser

    @override
    def _execute(self, args: argparse.Namespace) -> int | None:
        if args.config_action == "list":
            return _list_config()
        if args.config_action == "get":
            return _get_config(args.key)
        if args.config_action == "set":
            return _set_config(args.key, args.value)
        return _unset_config(args.key)


def _list_config() -> int | None:
    config = load_config()
    for key, spec in KNOWN_CONFIG_KEYS.items():
        value = config_value(config, key)
        print(
            f"{key}: {value if value is not None else '(unset)'}  # {spec.description}"
        )
    return None


def _get_config(key: str) -> int | None:
    if key not in KNOWN_CONFIG_KEYS:
        print(f"error: unknown config key '{key}'", file=sys.stderr)
        return 1
    value = config_value(load_config(), key)
    if value is None:
        return 1
    print(value)
    return None


def _set_config(key: str, value: str) -> int | None:
    spec = KNOWN_CONFIG_KEYS.get(key)
    if spec is None:
        print(f"error: unknown config key '{key}'", file=sys.stderr)
        return 1
    if spec.validator:
        try:
            spec.validator(value)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
    set_config_value(key, value)
    print(f"{key} = {value}")
    return None


def _unset_config(key: str) -> int | None:
    if key not in KNOWN_CONFIG_KEYS:
        print(f"error: unknown config key '{key}'", file=sys.stderr)
        return 1
    unset_config_value(key)
    print(f"{key} unset")
    return None
