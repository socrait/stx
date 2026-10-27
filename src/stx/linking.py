"""Validates and resolves `stx new --link NAME=TARGET` against a repo's
declared `links` and the target's registry record — see docs/manifest.md
(links).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .core.names import container_name_for_box
from .core.registry import LinkRecord, SandboxRecord, trusted_box_name
from .errors import StxError

if TYPE_CHECKING:
    from .manifest import Manifest

_LINK_ARG_RE = re.compile(r"([a-z][a-z0-9_]*)=([a-z0-9-]+)")


def parse_link_args(raw: list[str] | None) -> dict[str, str]:
    """`--link NAME=TARGET` args into a name->target mapping — raises naming
    the first malformed or repeated entry."""
    links: dict[str, str] = {}
    for item in raw or []:
        match = _LINK_ARG_RE.fullmatch(item)
        if not match:
            raise StxError(
                f"--link '{item}' must be NAME=TARGET "
                "(NAME: [a-z][a-z0-9_]*, TARGET: a sandbox slug)"
            )
        name, target = match.groups()
        if name in links:
            raise StxError(f"--link '{name}' given more than once")
        links[name] = target
    return links


def resolve_links(
    manifest: Manifest | None,
    link_args: list[str] | None,
    registry: dict[str, SandboxRecord],
) -> dict[str, LinkRecord]:
    """Validate and resolve every `--link` against this repo's declared
    `links` and the target's registry record, before `new` changes anything.
    Raises StxError naming the problem: an undeclared link name, a target
    that doesn't exist or has no trusted box, or a route the target doesn't
    declare."""
    requested = parse_link_args(link_args)
    resolved: dict[str, LinkRecord] = {}
    for name, target in requested.items():
        declared = manifest.links.get(name) if manifest is not None else None
        if declared is None:
            raise StxError(f"--link '{name}' is not declared in this repo's `links`")
        target_record = registry.get(target)
        if target_record is None:
            raise StxError(f"--link '{name}': target '{target}' does not exist")
        box_name = trusted_box_name(target_record.boxes)
        if box_name is None:
            raise StxError(f"--link '{name}': target '{target}' has no trusted box")
        port = target_record.boxes[box_name].routes.get(declared.route)
        if port is None:
            raise StxError(
                f"--link '{name}': target '{target}' has no route '{declared.route}'"
            )
        resolved[name] = LinkRecord(
            target=target,
            route=declared.route,
            port=port,
            container=container_name_for_box(box_name, target),
        )
    return resolved
