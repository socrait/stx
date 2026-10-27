from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, override

if TYPE_CHECKING:
    import argparse

    from ..core.registry import BoxRecord, SandboxRecord

from ..command import Command
from ..core import (
    DMZ_BOX,
    AgentState,
    ClaudeAuthMode,
    container_name_for_box,
    db_name,
    load_registry,
    logs_dir,
    read_agent_status,
    trusted_box_name,
)
from ..forge.registry import (
    earliest_box_expiry,
    sandboxes_with_expired_forge_tokens,
    sandboxes_with_expiring_forge_tokens,
)

# BRANCH is last in both views — it's the widest column.
_DEFAULT_COLUMNS = ("slug", "status", "age", "expires", "dispatch", "branch")
_ALL_COLUMNS = (
    "slug",
    "boxes",
    "scope",
    "db",
    "redis",
    "state",
    "status",
    "age",
    "expires",
    "repo",
    "port",
    "auth",
    "dispatch",
    "links",
    "branch",
)
_HEADERS = {
    "slug": "SLUG",
    "boxes": "BOXES",
    "branch": "BRANCH",
    "scope": "SCOPE",
    "db": "DB",
    "redis": "REDIS",
    "state": "STATE",
    "status": "STATUS",
    "repo": "REPO",
    "port": "PORT",
    "auth": "AUTH",
    "dispatch": "DISPATCH",
    "age": "AGE",
    "expires": "EXPIRES",
    "links": "LINKS",
}


class SandboxLsCommand(Command):
    """List active sandboxes and their status."""

    @classmethod
    @override
    def name(cls) -> str:
        return "ls"

    @classmethod
    @override
    def help(cls) -> str:
        return "List active sandboxes"

    @override
    def add_parser(self, subparsers) -> argparse.ArgumentParser:
        parser = subparsers.add_parser(self.name(), help=self.help())
        parser.add_argument(
            "-w", "--wide", action="store_true", help="Show every column"
        )
        return parser

    @override
    def _execute(self, args: argparse.Namespace) -> int | None:
        registry = load_registry()

        if not registry:
            print("no sandboxes")
            return None

        columns = list(_ALL_COLUMNS) if args.wide else list(_DEFAULT_COLUMNS)
        rows = [_row_values(slug, record) for slug, record in sorted(registry.items())]

        widths = {
            col: max(len(_HEADERS[col]), *(len(row[col]) for row in rows), 1)
            for col in columns
        }

        header = "  ".join(f"{_HEADERS[col]:<{widths[col]}}" for col in columns)
        print(header)
        print("-" * len(header))
        for row in rows:
            print("  ".join(f"{row[col]:<{widths[col]}}" for col in columns))

        expired = sandboxes_with_expired_forge_tokens(registry)
        for slug in sandboxes_with_expiring_forge_tokens(registry):
            if slug in expired:
                continue
            print(
                f"warning: '{slug}' forge tokens expire within 24h — run "
                f"`stx refresh {slug}`",
                file=sys.stderr,
            )
        for slug in expired:
            print(
                f"'{slug}' forge tokens have expired — run `stx refresh {slug}`",
                file=sys.stderr,
            )

        return None


def _row_values(slug: str, record: SandboxRecord) -> dict[str, str]:
    trusted = trusted_box_name(record.boxes)
    app = record.boxes.get(trusted) if trusted else None

    # Trusted box's status/dispatch slot preferred over dmz's.
    if trusted:
        status = _container_status(container_name_for_box(trusted, slug))
        dispatch = _dispatch_label(slug, trusted, app.user)
    elif DMZ_BOX in record.boxes:
        status = _container_status(container_name_for_box(DMZ_BOX, slug))
        dispatch = _dispatch_label(slug, DMZ_BOX, record.boxes[DMZ_BOX].user)
    else:
        status = "down"
        dispatch = "-"

    return {
        "slug": slug,
        "boxes": ",".join(sorted(record.boxes)),
        "branch": app.branch if app else "-",
        "scope": (
            _scope_label(app.forge.push.scope)
            if app and app.forge and app.forge.push
            else "-"
        ),
        "db": db_name(slug) if app and "postgres" in record.services else "-",
        "redis": str(record.redis_index) if record.redis_index is not None else "-",
        "state": record.state.value,
        "status": status,
        "repo": record.repo or "-",
        "port": str(record.tailnet_port) if record.tailnet_port else "-",
        "auth": _auth_label(app.claude_auth_mode) if app else "-",
        "dispatch": dispatch,
        "age": _age_label(record.created),
        "expires": _expires_label(app),
        "links": _links_label(app),
    }


def _links_label(app: BoxRecord | None) -> str:
    if not app or not app.links:
        return "-"
    return ",".join(f"{name}→{link.target}" for name, link in sorted(app.links.items()))


def _scope_label(gitlab_token_scope: str) -> str:
    if gitlab_token_scope == "write_repository":
        return "rw"
    if gitlab_token_scope == "read_repository":
        return "ro"
    return "-"


def _auth_label(claude_auth_mode: ClaudeAuthMode) -> str:
    return "oauth" if claude_auth_mode == ClaudeAuthMode.OAUTH else "api"


def _age_label(created: str) -> str:
    """Largest whole unit of elapsed time since `created`, rounded down."""
    try:
        created_dt = datetime.fromisoformat(created)
    except ValueError:
        return "-"
    if created_dt.tzinfo is None:
        created_dt = created_dt.replace(tzinfo=timezone.utc)
    elapsed = max(0.0, (datetime.now(timezone.utc) - created_dt).total_seconds())
    minutes = int(elapsed // 60)
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"
    return f"{hours // 24}d"


def _expires_label(box: BoxRecord | None) -> str:
    expiry = earliest_box_expiry(box)
    if expiry is None:
        return "-"
    today = datetime.now(timezone.utc).date()
    if expiry < today:
        return "expired"
    if expiry == today:
        return "today"
    return f"{expiry.month:02d}-{expiry.day:02d}"


def _dispatch_label(slug: str, box: str, user: str) -> str:
    """A box's dispatch-slot state, not a claim about the container itself."""
    status = read_agent_status(container_name_for_box(box, slug), user)
    if status is None:
        return "-"
    if status.state == AgentState.WORKING:
        idle = _seconds_since_mtime(logs_dir(slug, box) / f"{status.index}.jsonl")
        idle_label = f"{idle}s" if idle is not None else "?"
        return f"working #{status.index} · {idle_label}"
    exit_code = status.exit_code if status.exit_code is not None else "?"
    return f"done #{status.index} · exit {exit_code}"


def _seconds_since_mtime(path: Path) -> int | None:
    try:
        return int(time.time() - path.stat().st_mtime)
    except OSError:
        return None


def _container_status(container: str) -> str:
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Status}}", container],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return "down"
    return result.stdout.strip() or "down"
