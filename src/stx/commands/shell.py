from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, override

if TYPE_CHECKING:
    import argparse

from ..command import Command
from ..core import (
    box_not_found_error,
    container_name_for_box,
    container_status,
    default_box,
    load_registry,
    logs_dir,
)
from .start import stopped_box_hint

_LOG_NAME_RE = re.compile(r"[^a-zA-Z0-9._-]+")


class SandboxShellCommand(Command):
    """Open a shell inside a sandbox box, or run one command.

    See docs/reference.md (`shell` row).
    """

    @classmethod
    @override
    def name(cls) -> str:
        return "shell"

    @classmethod
    @override
    def help(cls) -> str:
        return "Open a shell inside a sandbox box (default: app, or dmz if there's no app box)"

    @classmethod
    @override
    def accepts_trailing_argv(cls) -> bool:
        return True

    @override
    def add_parser(self, subparsers) -> argparse.ArgumentParser:
        parser = subparsers.add_parser(self.name(), help=self.help())
        parser.add_argument("slug", help="Sandbox slug")
        parser.add_argument(
            "-b",
            "--box",
            default=None,
            help="Box to enter (default: the trusted box, or dmz if there's none)",
        )
        parser.add_argument(
            "-c",
            "--command",
            dest="shell_command",
            metavar="CMD",
            help=(
                "Run CMD (a shell string, via `bash -c`) instead of opening an "
                "interactive shell — needed for pipes/redirects/`&&`. For a plain "
                "command, prefer `-- CMD [ARGS...]` instead (e.g. `-- ./dev.sh "
                "up`) — no quoting, passed straight to `docker exec`. Mutually "
                "exclusive with a trailing `--` command."
            ),
        )
        parser.add_argument(
            "--detach",
            action="store_true",
            help=(
                "Run the command in the background inside the box and return "
                "immediately, redirecting its stdout/stderr to a log file under "
                "/logs instead of streaming them back — for a command that "
                "doesn't return on its own (e.g. `./dev.sh up`). Requires "
                "-c or a trailing `--` command; follow the log afterward with "
                "`stx shell <slug> -- tail -f /logs/<name>.log` or by "
                "reading it directly from the host"
            ),
        )
        return parser

    @override
    def _execute(self, args: argparse.Namespace) -> int | None:
        slug = args.slug
        # Populated by cli.py's top-level `--` split, never by argparse itself here.
        exec_argv: list[str] = getattr(args, "trailing_argv", [])
        if exec_argv and args.shell_command:
            print(
                "error: pass either -c or a command after --, not both",
                file=sys.stderr,
            )
            return 1

        if args.detach and not exec_argv and not args.shell_command:
            print(
                "error: --detach needs a command — pass -c or a trailing -- command",
                file=sys.stderr,
            )
            return 1

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

        if args.detach:
            log_name = _detached_log_name(args.shell_command, exec_argv)
            result = subprocess.run(
                _detached_exec_args(
                    container, user, args.shell_command, exec_argv, log_name
                )
            )
            if result.returncode != 0:
                print("error: failed to start detached command", file=sys.stderr)
                return result.returncode
            host_log_path = logs_dir(slug, box) / log_name
            print(f"started detached; log: /logs/{log_name} inside the box")
            print(f"  host:   {host_log_path}")
            print(f"  follow: stx shell {slug} --box {box} -- tail -f /logs/{log_name}")
            return None

        tty = sys.stdin.isatty()
        # Replace this process with docker exec so stdio is inherited directly.
        os.execvp(
            "docker",
            _docker_exec_args(container, user, args.shell_command, exec_argv, tty=tty),
        )
        # execvp never returns on success; reaching here means exec failed
        return 1


def _docker_exec_args(
    container: str,
    user: str,
    command: str | None,
    exec_argv: list[str],
    *,
    tty: bool = False,
) -> list[str]:
    """`docker exec` argv for an interactive shell, a `bash -c` string, or a
    direct passthrough command. `tty` requests a real pty+stdin — needed for
    Ctrl+C to reach the remote process — only when actually attached to one."""
    tty_flags = ["-it"] if tty else []
    if exec_argv:
        return ["docker", "exec", *tty_flags, "-u", user, container, *exec_argv]
    if command:
        return [
            "docker",
            "exec",
            *tty_flags,
            "-u",
            user,
            container,
            "bash",
            "-c",
            command,
        ]
    return ["docker", "exec", "-it", "-u", user, container, "bash"]


def _detached_log_name(command: str | None, exec_argv: list[str]) -> str:
    """Stable per command, not per invocation — overwrites, doesn't accumulate."""
    tokens = (command or "").split()
    first_token = exec_argv[0] if exec_argv else (tokens[0] if tokens else "detached")
    name = PurePosixPath(first_token).name or "detached"
    # A first token made entirely of shell-syntax characters (e.g. a `-c`
    # string starting with `[[`) sanitizes down to nothing — fall back rather
    # than write to the confusing, easy-to-miss bare `.log`.
    sanitized = _LOG_NAME_RE.sub("-", name).strip("-") or "detached"
    return sanitized + ".log"


def _detached_exec_args(
    container: str, user: str, command: str | None, exec_argv: list[str], log_name: str
) -> list[str]:
    """`docker exec -d` argv that backgrounds the command, redirecting output
    to `/logs/<log_name>`. Always routed through `bash -c` — even the no-shell
    passthrough form — since the redirect itself needs a shell."""
    inner = shlex.join(exec_argv) if exec_argv else (command or "")
    redirected = f"{{ {inner} ; }} > /logs/{log_name} 2>&1"
    return [
        "docker",
        "exec",
        "-d",
        "-u",
        user,
        container,
        "bash",
        "-c",
        redirected,
    ]
