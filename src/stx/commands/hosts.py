from __future__ import annotations

import sys
from typing import TYPE_CHECKING, override

if TYPE_CHECKING:
    import argparse

from ..command import Command
from ..core import (
    add_hosts_entry,
    hosts_entry_exists,
    hosts_hostnames_for,
    load_registry,
    remove_hosts_entry,
    trusted_box_name,
)


class SandboxHostsCommand(Command):
    """Add/remove the hosts-file entry for non-browser access to a sandbox —
    see docs/reference.md (Hosts entries)."""

    @classmethod
    @override
    def name(cls) -> str:
        return "hosts"

    @classmethod
    @override
    def help(cls) -> str:
        return "Add/remove the hosts-file entry for non-browser access to a sandbox"

    @override
    def add_parser(self, subparsers) -> argparse.ArgumentParser:
        parser = subparsers.add_parser(self.name(), help=self.help())
        actions = parser.add_subparsers(dest="hosts_action", required=True)

        add_parser = actions.add_parser(
            "add", help="Add the hosts entry (prompts for sudo)"
        )
        add_parser.add_argument("slug")

        remove_parser = actions.add_parser(
            "remove", help="Remove the hosts entry (prompts for sudo)"
        )
        remove_parser.add_argument("slug")

        return parser

    @override
    def _execute(self, args: argparse.Namespace) -> int | None:
        slug = args.slug
        registry = load_registry()
        if slug not in registry:
            print(f"error: no sandbox named '{slug}'", file=sys.stderr)
            return 1

        try:
            if args.hosts_action == "add":
                # The registry's own BoxRecord.routes, not the manifest — this
                # works even if the manifest has changed on the branch since.
                trusted = trusted_box_name(registry[slug].boxes)
                routes = registry[slug].boxes[trusted].routes if trusted else {}
                hostnames = hosts_hostnames_for(slug, routes)
                if not hostnames:
                    print(f"sandbox '{slug}' declares no routes, nothing to add")
                    return None
                if hosts_entry_exists(slug, routes):
                    print(f"  already present: {', '.join(hostnames)} -> 127.0.0.1")
                else:
                    add_hosts_entry(slug, routes)
                    print(f"  added: {', '.join(hostnames)} -> 127.0.0.1")
            else:
                removed = remove_hosts_entry(slug)
                if removed:
                    print(f"  removed: {', '.join(removed)}")
                else:
                    print("  already absent")
        except OSError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        return None
