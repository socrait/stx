from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, override

if TYPE_CHECKING:
    import argparse

from ..command import Command
from ..core import (
    AGENT_WRAPPER,
    DEFAULT_AGENT_MODEL,
    SETUP_STATUS_PATH,
    ManifestEntry,
    ManifestEntryKind,
    agent_pid_alive,
    allocate_dispatch_index,
    append_manifest_entry,
    box_not_found_error,
    container_name_for_box,
    container_status,
    default_box,
    load_registry,
    read_agent_status,
    read_setup_status,
    wait_for_agent,
)
from .start import stopped_box_hint


class SandboxAgentCommand(Command):
    """Dispatch a harness headlessly into a sandbox box to run one task."""

    @classmethod
    @override
    def name(cls) -> str:
        return "agent"

    @classmethod
    @override
    def help(cls) -> str:
        return "Dispatch a harness headlessly into a box to run one task"

    @override
    def add_parser(self, subparsers) -> argparse.ArgumentParser:
        parser = subparsers.add_parser(self.name(), help=self.help())
        parser.add_argument("slug", help="Sandbox slug")
        parser.add_argument(
            "task",
            nargs="?",
            default=None,
            help="Task prompt for the harness (mutually exclusive with --task-file)",
        )
        parser.add_argument(
            "-b",
            "--box",
            default=None,
            help="Box to dispatch into (default: the trusted box, or dmz if there's none)",
        )
        parser.add_argument(
            "--model",
            default=DEFAULT_AGENT_MODEL,
            help=f"Model alias or id for the harness (default: {DEFAULT_AGENT_MODEL})",
        )
        parser.add_argument(
            "--task-file",
            default=None,
            help="Host-side path whose contents are the task; mutually exclusive "
            "with the positional task",
        )
        parser.add_argument(
            "--wait",
            action="store_true",
            help="Block until the agent finishes and print its answer (same as a "
            "separate `stx await` call)",
        )
        return parser

    @override
    def _execute(self, args: argparse.Namespace) -> int | None:
        task = _resolve_task(args.task, args.task_file)
        if task is None:
            return 1

        slug = args.slug
        registry = load_registry()

        if slug not in registry:
            print(f"error: no sandbox named '{slug}'", file=sys.stderr)
            return 1

        record = registry[slug]
        box = args.box or default_box(record)
        if box not in record.boxes:
            print(f"error: {box_not_found_error(slug, box, record)}", file=sys.stderr)
            return 1

        container = container_name_for_box(box, slug)
        user = record.boxes[box].user

        status = container_status(container)
        if status is None:
            print(
                f"error: container '{container}' doesn't exist — `stx start` "
                "only restarts a stopped sandbox, it can't recreate a "
                "removed one; try `stx new` or `stx refresh`",
                file=sys.stderr,
            )
            return 1
        if status != "running":
            print(f"error: {stopped_box_hint(slug)}", file=sys.stderr)
            return 1

        if read_setup_status(container) is None:
            print(
                f"error: setup hasn't finished in '{box}' yet — wait for `stx new`/"
                f"`stx refresh` to finish, or check `docker exec {container} cat "
                f"{SETUP_STATUS_PATH}`",
                file=sys.stderr,
            )
            return 1

        status = read_agent_status(container, user)
        if status is not None and agent_pid_alive(container, status.pid, user=user):
            print(
                f"error: an agent is already running in '{box}' (pid {status.pid}). "
                f"Run `stx await {slug} --box {box}` first.",
                file=sys.stderr,
            )
            return 1

        index = allocate_dispatch_index(slug, box)

        result = subprocess.run(
            [
                "docker",
                "exec",
                "-d",
                "-u",
                user,
                container,
                "/bin/bash",
                AGENT_WRAPPER,
                task,
                args.model,
                str(index),
            ]
        )
        if result.returncode != 0:
            print("error: failed to dispatch agent", file=sys.stderr)
            return 1

        append_manifest_entry(
            slug,
            ManifestEntry(
                kind=ManifestEntryKind.START,
                index=index,
                box=box,
                task=task,
                model=args.model,
                started_at=datetime.now(timezone.utc).isoformat(),
            ),
        )

        if args.wait:
            exit_code = wait_for_agent(slug, box, container, user=user)
            if exit_code is None:
                print(
                    f"error: dispatched agent in '{box}' but its status never "
                    "appeared — check `stx shell` inside the box",
                    file=sys.stderr,
                )
                return 1
            return 0 if exit_code == 0 else 1

        print(
            f"✓ agent dispatched in '{box}' — await with `stx await {slug} --box {box}`"
        )
        return None


def _resolve_task(task: str | None, task_file: str | None) -> str | None:
    """The task text from whichever of the positional/`--task-file` was
    given, or None (having already printed why) when neither or both were."""
    if task_file is None:
        if task is None:
            print(
                "error: a task is required (positional or --task-file)", file=sys.stderr
            )
            return None
        return task

    if task is not None:
        print("error: task and --task-file are mutually exclusive", file=sys.stderr)
        return None

    path = Path(task_file)
    try:
        content = path.read_text()
    except OSError as e:
        print(f"error: cannot read task file '{path}': {e}", file=sys.stderr)
        return None
    if not content.strip():
        print(f"error: task file '{path}' is empty", file=sys.stderr)
        return None
    return content
