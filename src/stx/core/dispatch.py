import json
import subprocess
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from ..errors import StxError
from .box_env import SYSTEM_PATH
from .compose import (
    container_recent_logs,
    container_started_at,
    container_status,
    run_bounded,
)
from .paths import logs_dir, logs_root

CompletedProcessRunner = Callable[..., "subprocess.CompletedProcess[str]"]

AGENT_WRAPPER = "/opt/stx/box/agent-wrapper.sh"
DEFAULT_AGENT_MODEL = "sonnet"
_AGENT_POLL_INTERVAL_SECONDS = 1
_AGENT_STARTUP_GRACE_ATTEMPTS = 15  # ~15s for the wrapper to write its first status


class AgentState(StrEnum):
    """The wrapper-reported lifecycle stage — informational only, see
    docs/reference.md (Dispatch)."""

    WORKING = "working"
    DONE = "done"


@dataclass
class AgentStatus:
    state: AgentState
    pid: int
    started_at: str = ""
    exit_code: int | None = None  # set once state is DONE
    finished_at: str = ""  # set once state is DONE
    index: int = 0  # this box's dispatch index — names its capture file, <index>.jsonl


class ManifestEntryKind(StrEnum):
    """Discriminates the two shapes appended to a box's dispatch manifest."""

    START = "start"
    FINISH = "finish"


@dataclass
class ManifestEntry:
    """One line of a slug's append-only `logs/<slug>/manifest.jsonl`.

    START and FINISH entries share one shape rather than two dataclasses +
    a union: the file is a flat JSONL log, and a single typed row mirrors
    that directly. START entries populate task/model/started_at; FINISH
    entries populate exit_code/finished_at/session_id/total_cost_usd.
    """

    kind: ManifestEntryKind
    index: int
    box: str
    task: str = ""
    model: str = ""
    started_at: str = ""
    exit_code: int | None = None
    finished_at: str = ""
    session_id: str = ""
    total_cost_usd: float | None = None


@dataclass
class DispatchResult:
    """The harness's final answer, extracted from a dispatch's stream-json capture."""

    text: str
    session_id: str = ""
    total_cost_usd: float | None = None


def _box_user_home(
    container: str, user: str, *, runner: CompletedProcessRunner = subprocess.run
) -> str | None:
    """Resolve a box user's home dir via `getent passwd` (never shell `~`
    expansion or `eval echo` — both need a shell to interpret the lookup,
    which this avoids entirely by execing `getent` directly). Run as root
    with a pinned `PATH` so the answer is authoritative and the box user
    can't shadow `getent` with one of their own. `runner` is a seam for
    tests to inject a fake subprocess result."""
    result = run_bounded(
        [
            "docker",
            "exec",
            "--env",
            f"PATH={SYSTEM_PATH}",
            container,
            "getent",
            "passwd",
            user,
        ],
        runner=runner,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    fields = result.stdout.strip().split(":")
    return fields[5] if len(fields) > 5 else None


def read_agent_status(
    container: str, user: str, *, runner: CompletedProcessRunner = subprocess.run
) -> AgentStatus | None:
    """Return the agent wrapper's last-written status for a box's container.

    None means no agent has ever been dispatched in that box (or the
    container isn't running). `user` is the box's exec user — its status
    file lives under that user's own home dir, resolved via `getent passwd`.
    `runner` is a seam for tests to inject a fake subprocess result.
    """
    home = _box_user_home(container, user, runner=runner)
    if home is None:
        return None
    # As the box user: the box drops root's capability to read other users'
    # files, and a home directory is commonly mode 700. Pinned PATH so the
    # exec resolves the real `cat`, not one planted earlier on the box user's
    # own PATH.
    result = run_bounded(
        [
            "docker",
            "exec",
            "-u",
            user,
            "--env",
            f"PATH={SYSTEM_PATH}",
            container,
            "cat",
            f"{home}/.stx-agent/status.json",
        ],
        runner=runner,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    try:
        raw = json.loads(result.stdout)
        return AgentStatus(
            state=AgentState(raw["state"]),
            pid=raw["pid"],
            started_at=raw.get("started_at", ""),
            exit_code=raw.get("exit_code"),
            finished_at=raw.get("finished_at", ""),
            index=raw.get("index", 0),
        )
    except (json.JSONDecodeError, KeyError, ValueError):
        return None


def manifest_path(slug: str) -> Path:
    return logs_root(slug) / "manifest.jsonl"


def read_manifest(slug: str) -> list[ManifestEntry]:
    """Load a slug's dispatch manifest, tolerating individual unparseable lines.

    Mirrors load_registry's per-entry tolerance: one malformed line (a torn
    write, a future schema) must not hide every other dispatch's history.
    """
    path = manifest_path(slug)
    if not path.exists():
        return []
    entries: list[ManifestEntry] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
            entries.append(
                ManifestEntry(**{**raw, "kind": ManifestEntryKind(raw["kind"])})
            )
        except (json.JSONDecodeError, TypeError, KeyError, ValueError):
            continue
    return entries


def append_manifest_entry(slug: str, entry: ManifestEntry) -> None:
    path = manifest_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(asdict(entry)) + "\n")


def allocate_dispatch_index(slug: str, box: str) -> int:
    """Return the next monotonic dispatch index for a box (host-allocated, 1-based)."""
    existing = [
        e.index
        for e in read_manifest(slug)
        if e.box == box and e.kind == ManifestEntryKind.START
    ]
    return max(existing, default=0) + 1


def parse_dispatch_result(capture_path: Path) -> DispatchResult | None:
    """Return the harness's final answer from a dispatch's stream-json capture.

    Scans every enveloped line for `type: result` events rather than stopping
    at the first match, so a capture that legitimately contains more than one
    (unexpected, but not worth hard-failing on) resolves to the last one.
    Returns None if the file is missing, empty, or has no result event yet
    (e.g. a dispatch still running, or one that crashed before completing).
    """
    if not capture_path.exists():
        return None
    result: DispatchResult | None = None
    for line in capture_path.read_text().splitlines():
        try:
            event = json.loads(line)["e"]
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            result = DispatchResult(
                text=event.get("result", ""),
                session_id=event.get("session_id", ""),
                total_cost_usd=event.get("total_cost_usd"),
            )
    return result


def record_dispatch_finish_if_absent(slug: str, box: str, status: AgentStatus) -> None:
    """Append a manifest FINISH entry for a completed dispatch, exactly once —
    idempotent, called by both `await` and `logs`."""
    if status.state != AgentState.DONE:
        return
    already_recorded = any(
        e.kind == ManifestEntryKind.FINISH and e.box == box and e.index == status.index
        for e in read_manifest(slug)
    )
    if already_recorded:
        return
    result = parse_dispatch_result(logs_dir(slug, box) / f"{status.index}.jsonl")
    append_manifest_entry(
        slug,
        ManifestEntry(
            kind=ManifestEntryKind.FINISH,
            index=status.index,
            box=box,
            exit_code=status.exit_code,
            finished_at=status.finished_at,
            session_id=result.session_id if result else "",
            total_cost_usd=result.total_cost_usd if result else None,
        ),
    )


def _is_after(a: str, b: str) -> bool:
    """Whether ISO-8601 timestamp `a` is strictly after `b`. False (not
    True) on an unparseable timestamp — a comparison stx can't make must
    never be mistaken for the restart it's meant to detect."""
    try:
        return datetime.fromisoformat(a) > datetime.fromisoformat(b)
    except ValueError:
        return False


def agent_pid_alive(
    container: str,
    pid: int,
    *,
    user: str,
    started_at: str = "",
    runner: CompletedProcessRunner = subprocess.run,
) -> bool:
    """The ground truth for whether an agent is working — see docs/reference.md
    (Dispatch). Checked as the box's own user: root has no CAP_KILL here.
    The shell's own `kill`: images without procps have no `kill` executable.

    `started_at` is the agent status's own recorded start time. When given,
    a container restarted since then means `pid` can't be the original
    agent even if something now answers to it — pid namespaces restart from
    scratch, so the kernel has simply reused the number for an unrelated
    process. Checked first, before `kill -0` gets a chance to be fooled by
    that coincidence. Omit it (the default) to skip this check, e.g. before
    the caller has a recorded `started_at` to compare against.

    `runner` is a seam for tests to inject a fake subprocess result.
    """
    if started_at:
        restarted_at = container_started_at(container, runner=runner)
        if restarted_at and _is_after(restarted_at, started_at):
            return False
    result = run_bounded(
        [
            "docker",
            "exec",
            "-u",
            user,
            "--env",
            f"PATH={SYSTEM_PATH}",
            container,
            "sh",
            "-c",
            'kill -0 "$1"',
            "sh",
            str(pid),
        ],
        runner=runner,
        capture_output=True,
    )
    return result.returncode == 0


def wait_for_agent(
    slug: str,
    box: str,
    container: str,
    *,
    user: str,
    timeout: float | None = None,
    runner: CompletedProcessRunner = subprocess.run,
) -> int | None:
    """Block until a box's dispatched agent finishes, then print its final
    answer. Returns the harness's exit code, or None if never dispatched.
    Shared by `stx await` and `stx agent --wait`. A short grace period of
    retries covers the startup race before the wrapper writes its first
    status. Raises StxError if `timeout` seconds pass before that, or before
    the agent finishes — or as soon as the box stops running or its agent's
    pid is a different process than the one dispatched (both would otherwise
    look identical to "finished" and silently misreport). `runner` is a
    seam for tests to inject a fake subprocess result."""
    deadline = time.monotonic() + timeout if timeout is not None else None

    status = None
    for _ in range(_AGENT_STARTUP_GRACE_ATTEMPTS):
        status = read_agent_status(container, user, runner=runner)
        if status is not None:
            break
        if deadline is not None and time.monotonic() >= deadline:
            raise StxError(
                f"timed out after {timeout:g}s waiting for an agent to start in '{box}'"
            )
        time.sleep(_AGENT_POLL_INTERVAL_SECONDS)
    if status is None:
        return None

    while agent_pid_alive(
        container, status.pid, user=user, started_at=status.started_at, runner=runner
    ):
        if deadline is not None and time.monotonic() >= deadline:
            raise StxError(
                f"timed out after {timeout:g}s waiting for the agent in "
                f"'{box}' to finish"
            )
        time.sleep(_AGENT_POLL_INTERVAL_SECONDS)

    final_status = read_agent_status(container, user, runner=runner)
    if final_status is None or final_status.state != AgentState.DONE:
        state = container_status(container, runner=runner)
        logs = (
            container_recent_logs(container, runner=runner)
            if state != "running"
            else ""
        )
        detail = f"\n{logs}" if logs else ""
        raise StxError(
            f"box '{box}' never recorded this dispatch finishing "
            f"(container state: {state or 'missing'}){detail}"
        )
    result = parse_dispatch_result(logs_dir(slug, box) / f"{final_status.index}.jsonl")
    print(result.text if result else "(no result captured — check `stx logs`)")

    record_dispatch_finish_if_absent(slug, box, final_status)

    return final_status.exit_code
