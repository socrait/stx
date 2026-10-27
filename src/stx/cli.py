import argparse
import sys
from importlib.metadata import version
from typing import Dict, Type

from .command import Command
from .commands.agent import SandboxAgentCommand
from .commands.await_ import SandboxAwaitCommand
from .commands.code import SandboxCodeCommand
from .commands.config import SandboxConfigCommand
from .commands.doctor import SandboxDoctorCommand
from .commands.down import SandboxDownCommand
from .commands.guide import SandboxGuideCommand
from .commands.hosts import SandboxHostsCommand
from .commands.init import SandboxInitCommand
from .commands.logs import SandboxLogsCommand
from .commands.ls import SandboxLsCommand
from .commands.new import SandboxNewCommand
from .commands.open import SandboxOpenCommand
from .commands.paths import SandboxPathsCommand
from .commands.prune import SandboxPruneCommand
from .commands.rebuild import SandboxRebuildCommand
from .commands.refresh import SandboxRefreshCommand
from .commands.rm import SandboxRmCommand
from .commands.shell import SandboxShellCommand
from .commands.start import SandboxStartCommand
from .commands.status import SandboxStatusCommand
from .commands.up import SandboxUpCommand
from .errors import StxError


def split_trailing_argv(argv: list[str]) -> tuple[list[str], list[str]]:
    """Split argv on the first literal `--`, if present, so a command can own
    a passthrough tail of its own without fighting argparse."""
    if "--" in argv:
        idx = argv.index("--")
        return argv[:idx], argv[idx + 1 :]
    return argv, []


def _peek_target_command(
    argv: list[str], commands: Dict[str, Command]
) -> Command | None:
    """Find the resolved Command for `<verb> ...` in argv, if any — used only
    to decide whether split_trailing_argv should run, never for dispatch."""
    for arg in argv:
        if not arg.startswith("-"):
            return commands.get(arg)
    return None


def get_commands() -> list[Type[Command]]:
    """Get all available commands."""
    return [
        SandboxUpCommand,
        SandboxDownCommand,
        SandboxStatusCommand,
        SandboxNewCommand,
        SandboxShellCommand,
        SandboxStartCommand,
        SandboxCodeCommand,
        SandboxConfigCommand,
        SandboxPathsCommand,
        SandboxHostsCommand,
        SandboxInitCommand,
        SandboxOpenCommand,
        SandboxLsCommand,
        SandboxLogsCommand,
        SandboxRefreshCommand,
        SandboxRmCommand,
        SandboxPruneCommand,
        SandboxRebuildCommand,
        SandboxDoctorCommand,
        SandboxGuideCommand,
        SandboxAgentCommand,
        SandboxAwaitCommand,
    ]


def main():
    parser = argparse.ArgumentParser(
        description="stx - sandbox dev/agent boxes",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {version('stx')}"
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    commands: Dict[str, Command] = {}
    for cmd_class in get_commands():
        cmd = cmd_class()
        cmd.add_parser(subparsers)
        commands[cmd.name()] = cmd

    raw_argv = sys.argv[1:]

    # Only split on `--` if the target command opts in; others keep argparse's own handling.
    target_cmd = _peek_target_command(raw_argv, commands)
    if target_cmd is not None and target_cmd.accepts_trailing_argv():
        argv, trailing_argv = split_trailing_argv(raw_argv)
    else:
        argv, trailing_argv = raw_argv, []

    args = parser.parse_args(argv)
    args.trailing_argv = trailing_argv

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    cmd = commands[args.command]
    try:
        ret = cmd.execute(args)
    except StxError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    if ret is not None:
        sys.exit(ret)


if __name__ == "__main__":
    main()
