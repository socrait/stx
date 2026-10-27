from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime
from typing import TYPE_CHECKING, override

if TYPE_CHECKING:
    import argparse
    from pathlib import Path

from ..command import Command
from ..core import (
    AgentState,
    ManifestEntry,
    ManifestEntryKind,
    box_not_found_error,
    container_name_for_box,
    default_box,
    load_registry,
    logs_dir,
    read_agent_status,
    read_manifest,
    record_dispatch_finish_if_absent,
)

#: Box output is attacker-influenceable (a tool result can echo arbitrary file
#: content), so terminal escape sequences in it must never reach the host
#: terminal, where they could move the cursor, rewrite earlier lines, or set
#: the window title. Strip ANSI escape sequences and every other C0/C1 control
#: character, keeping only newline and tab.
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")

_IDLE_HEARTBEAT_SECONDS = 90
_FOLLOW_POLL_SECONDS = 1
_TASK_SNIPPET_LEN = 60
_TEXT_TRUNCATE_LEN = 100
_TOOL_RESULT_TRUNCATE_LEN = 150


class SandboxLogsCommand(Command):
    """Show a box's dispatched-agent logs: live tail, manifest listing, or raw capture."""

    @classmethod
    @override
    def name(cls) -> str:
        return "logs"

    @classmethod
    @override
    def help(cls) -> str:
        return "Show a box's dispatch logs (latest dispatch by default)"

    @override
    def add_parser(self, subparsers) -> argparse.ArgumentParser:
        parser = subparsers.add_parser(self.name(), help=self.help())
        parser.add_argument("slug", help="Sandbox slug")
        parser.add_argument(
            "-b",
            "--box",
            default=None,
            help="Box to inspect (default: the trusted box, or dmz if there's none)",
        )
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--follow", action="store_true", help="Live-tail the latest dispatch"
        )
        mode.add_argument(
            "--list",
            action="store_true",
            help="List every dispatch recorded for this box",
        )
        mode.add_argument(
            "--raw", action="store_true", help="Emit the raw {t, e} envelope JSONL"
        )
        parser.add_argument(
            "-v",
            "--verbose",
            action="store_true",
            help="Also show successful tool results, not just failures",
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
        box = args.box or default_box(record)
        if box not in record.boxes:
            print(f"error: {box_not_found_error(slug, box, record)}", file=sys.stderr)
            return 1

        container = container_name_for_box(box, slug)
        user = record.boxes[box].user
        verbose = args.verbose

        # Record a since-finished dispatch lazily, so --list sees it unawaited.
        status = read_agent_status(container, user)
        if status is not None:
            record_dispatch_finish_if_absent(slug, box, status)

        if args.list:
            _print_list(slug, box)
            return None

        entries = [e for e in read_manifest(slug) if e.box == box]
        starts = [e for e in entries if e.kind == ManifestEntryKind.START]
        if not starts:
            print(f"no dispatches recorded for '{box}'", file=sys.stderr)
            return 1
        latest_index = max(e.index for e in starts)
        capture_path = logs_dir(slug, box) / f"{latest_index}.jsonl"

        if args.raw:
            if capture_path.exists():
                print(capture_path.read_text(), end="")
            return None

        latest_start = next(e for e in starts if e.index == latest_index)
        latest_finish = next(
            (
                e
                for e in entries
                if e.kind == ManifestEntryKind.FINISH and e.index == latest_index
            ),
            None,
        )

        if args.follow:
            _follow(
                slug, box, container, user, capture_path, latest_start, verbose=verbose
            )
            return None

        _print_dispatch(capture_path, latest_start, latest_finish, verbose=verbose)
        return None


def _strip_control_chars(text: str) -> str:
    """`text` with ANSI escape sequences and C0/C1 control characters removed,
    keeping newline and tab — safe to print box output to the host terminal."""
    return _CONTROL_CHARS_RE.sub("", _ANSI_ESCAPE_RE.sub("", text))


def _task_snippet(task: str) -> str:
    return _truncate(task.replace("\n", " "), _TASK_SNIPPET_LEN)


def _truncate(text: str, length: int) -> str:
    text = text.strip()
    return text if len(text) <= length else text[: length - 1] + "…"


def _format_duration(started_at: str, finished_at: str) -> str:
    try:
        start = datetime.fromisoformat(started_at)
        finish = datetime.fromisoformat(finished_at)
    except ValueError:
        return "-"
    seconds = (finish - start).total_seconds()
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, sec = divmod(int(seconds), 60)
    return f"{minutes}m{sec:02d}s"


def _print_list(slug: str, box: str) -> None:
    entries = [e for e in read_manifest(slug) if e.box == box]
    starts = {e.index: e for e in entries if e.kind == ManifestEntryKind.START}
    finishes = {e.index: e for e in entries if e.kind == ManifestEntryKind.FINISH}

    if not starts:
        print(f"no dispatches recorded for '{box}'")
        return

    total_cost = 0.0
    # SESSION printed in full — must be copy-pasteable into `claude --resume`.
    print(
        f"{'#':<4} {'STATE':<10} {'STARTED':<20} {'DUR':<8} {'COST':<8} "
        f"{'SESSION':<36} TASK"
    )
    for index in sorted(starts):
        start = starts[index]
        finish = finishes.get(index)
        if finish is not None:
            state = "done" if finish.exit_code == 0 else f"exit {finish.exit_code}"
            duration = _format_duration(start.started_at, finish.finished_at)
            cost = (
                f"${finish.total_cost_usd:.2f}"
                if finish.total_cost_usd is not None
                else "-"
            )
            session = finish.session_id or "-"
            if finish.total_cost_usd is not None:
                total_cost += finish.total_cost_usd
        else:
            state, duration, cost, session = "working", "-", "-", "-"
        print(
            f"{index:<4} {state:<10} {start.started_at:<20} {duration:<8} {cost:<8} "
            f"{session:<36} {_task_snippet(start.task)}"
        )
    print(f"\ntotal cost: ${total_cost:.2f}")


def _session_id_from_capture(capture_path: Path) -> str:
    if not capture_path.exists():
        return ""
    for line in capture_path.read_text().splitlines():
        try:
            event = json.loads(line)["e"]
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
        if (
            isinstance(event, dict)
            and event.get("type") == "system"
            and event.get("subtype") == "init"
        ):
            return event.get("session_id", "")
    return ""


def _render_tool_call(block: dict) -> str:
    name = block.get("name", "?")
    tool_input = block.get("input", {})
    if name == "Bash":
        return (
            f"Bash: {_truncate(str(tool_input.get('command', '')), _TEXT_TRUNCATE_LEN)}"
        )
    if name in ("Read", "Write", "Edit"):
        return f"{name}: {tool_input.get('file_path', '?')}"
    if name == "Task":
        return f"Task: {_truncate(str(tool_input.get('description', '')), 80)}"
    return f"{name}: {_truncate(json.dumps(tool_input), 80)}"


def _tool_result_text(block: dict) -> str:
    content = block.get("content", "")
    if isinstance(content, list):
        content = " ".join(
            c.get("text", "")
            for c in content
            if isinstance(c, dict) and c.get("type") == "text"
        )
    return str(content)


def _render_event(envelope: dict, *, verbose: bool) -> str | None:
    """Compact one-line rendering of a captured event, or None to skip it — see
    docs/reference.md (`logs` table, brief)."""
    t = envelope.get("t", "")
    event = envelope.get("e", {})
    if not isinstance(event, dict):
        return None
    etype = event.get("type")

    if etype == "assistant":
        message = event.get("message", {})
        lines = []
        for block in message.get("content", []):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = _truncate(block.get("text", ""), _TEXT_TRUNCATE_LEN)
                if text:
                    lines.append(f"{t}  ✎ {text}")
            elif block.get("type") == "tool_use":
                lines.append(f"{t}  → {_render_tool_call(block)}")
        return "\n".join(lines) if lines else None

    if etype == "user":
        message = event.get("message", {})
        for block in message.get("content", []):
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            is_error = bool(block.get("is_error", False))
            if not is_error and not verbose:
                return None
            marker = "✗" if is_error else "✓"
            text = _truncate(_tool_result_text(block), _TOOL_RESULT_TRUNCATE_LEN)
            return f"{t}  {marker} {text}"
        return None

    return None


def _print_dispatch(
    capture_path: Path,
    start: ManifestEntry,
    finish: ManifestEntry | None,
    *,
    verbose: bool,
) -> None:
    session_id = _session_id_from_capture(capture_path)
    print(
        f"dispatch #{start.index}  box={start.box}  model={start.model}  "
        f"session={session_id or '?'}  started={start.started_at}"
    )
    print(f"task: {_task_snippet(start.task)}")
    print("-" * 60)

    if capture_path.exists():
        for line in capture_path.read_text().splitlines():
            try:
                envelope = json.loads(line)
            except json.JSONDecodeError:
                continue
            rendered = _render_event(envelope, verbose=verbose)
            if rendered:
                print(_strip_control_chars(rendered))

    print("-" * 60)
    print(_footer_line(start, finish))


def _footer_line(start: ManifestEntry, finish: ManifestEntry | None) -> str:
    if finish is None:
        return "state=working"
    state = "done" if finish.exit_code == 0 else f"exit {finish.exit_code}"
    duration = _format_duration(start.started_at, finish.finished_at)
    cost = f"${finish.total_cost_usd:.2f}" if finish.total_cost_usd is not None else "-"
    return f"state={state}  duration={duration}  cost={cost}"


def _find_finish(slug: str, box: str, index: int) -> ManifestEntry | None:
    return next(
        (
            e
            for e in read_manifest(slug)
            if e.kind == ManifestEntryKind.FINISH and e.box == box and e.index == index
        ),
        None,
    )


def _follow(
    slug: str,
    box: str,
    container: str,
    user: str,
    capture_path: Path,
    start: ManifestEntry,
    *,
    verbose: bool,
) -> None:
    print(
        f"dispatch #{start.index}  box={start.box}  model={start.model}  "
        f"started={start.started_at}  (following — Ctrl-C to stop)"
    )
    print(f"task: {_task_snippet(start.task)}")
    print("-" * 60)

    position = 0
    last_event_time = time.monotonic()
    try:
        while True:
            if capture_path.exists():
                with capture_path.open() as f:
                    f.seek(position)
                    new_lines = f.readlines()
                    position = f.tell()
                for raw_line in new_lines:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        envelope = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    rendered = _render_event(envelope, verbose=verbose)
                    if rendered:
                        print(_strip_control_chars(rendered))
                        last_event_time = time.monotonic()

            finish = _find_finish(slug, box, start.index)
            if finish is None:
                status = read_agent_status(container, user)
                if (
                    status is not None
                    and status.state == AgentState.DONE
                    and status.index == start.index
                ):
                    record_dispatch_finish_if_absent(slug, box, status)
                    finish = _find_finish(slug, box, start.index)
            if finish is not None:
                print("-" * 60)
                print(_footer_line(start, finish))
                return

            idle = time.monotonic() - last_event_time
            if idle >= _IDLE_HEARTBEAT_SECONDS:
                print(f"  … no events for {int(idle)}s")
                last_event_time = time.monotonic()
            time.sleep(_FOLLOW_POLL_SECONDS)
    except KeyboardInterrupt:
        pass
