"""Renders egress.hosts/ports — see docs/manifest.md."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from .core.compose import atomic_write_text
from .core.paths import allowlist_path

if TYPE_CHECKING:
    from .manifest import Manifest

#: Every trusted box needs these to run the coding-agent harness — a repo
#: should not have to know them.
HARNESS_HOSTS = ("api.anthropic.com", "platform.claude.com")


def render(manifest: Manifest, slug: str, *, forge_host: str | None = None) -> Path:
    """Write the tinyproxy allowlist file for `slug` and return its path.

    `manifest.egress.hosts` plus the harness hosts and (when a forge is
    configured) its host — every trusted box needs these regardless of what
    the manifest declares.
    """
    path = allowlist_path(slug)
    hosts = list(manifest.egress.hosts) + list(HARNESS_HOSTS)
    if forge_host:
        hosts.append(forge_host)
    deduped = list(dict.fromkeys(hosts))
    lines = [_filter_pattern(entry) for entry in deduped]
    atomic_write_text(path, "\n".join(lines) + "\n" if lines else "")
    return path


def remove(slug: str) -> None:
    """Delete the rendered tinyproxy allowlist file for `slug`, if any — `rm`,
    `prune`, and `new`'s rollback all call this."""
    allowlist_path(slug).unlink(missing_ok=True)


def _filter_pattern(entry: str) -> str:
    if entry.startswith("~"):
        return entry[1:]
    return f"^{re.escape(entry)}$"
