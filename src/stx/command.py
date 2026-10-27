"""
Command interface for the CLI.
"""

import argparse
import sys
from abc import ABC, abstractmethod
from typing import Optional, Protocol

from .core import host_only_error, in_sandbox_box


class SubParsersProtocol(Protocol):
    """Protocol defining the public interface for subparsers."""

    def add_parser(self, name: str, **kwargs) -> argparse.ArgumentParser: ...


class Command(ABC):
    """Base class for all commands."""

    @classmethod
    @abstractmethod
    def name(cls) -> str:
        """The name of the command."""
        pass

    @classmethod
    @abstractmethod
    def help(cls) -> str:
        """Help text for the command."""
        pass

    @abstractmethod
    def add_parser(self, subparsers: SubParsersProtocol) -> argparse.ArgumentParser:
        """Add this command's parser to the given subparsers object."""
        pass

    @classmethod
    def host_only(cls) -> bool:
        """Whether this command refuses to run inside a sandbox box — see
        docs/reference.md (Commands and flags). True unless overridden."""
        return True

    @abstractmethod
    def _execute(self, args: argparse.Namespace) -> Optional[int]:
        """Execute the command's own behavior, after the host-only guard has passed."""
        pass

    def execute(self, args: argparse.Namespace) -> Optional[int]:
        """Run the host-only guard, then dispatch to this command's own behavior."""
        if self.host_only() and in_sandbox_box():
            print(host_only_error(self.name()), file=sys.stderr)
            return 1
        return self._execute(args)

    @classmethod
    def accepts_trailing_argv(cls) -> bool:
        """Whether this command consumes a raw `--` passthrough tail, split
        out by cli.py before argparse runs. Opt-in only; defaults to False."""
        return False
