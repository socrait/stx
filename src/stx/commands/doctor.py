from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, override

if TYPE_CHECKING:
    import argparse

from ..command import Command
from ..config import KNOWN_CONFIG_KEYS, config_value, default_dotenv_path, load_config
from ..core import (
    compose_version,
    identity_provenance_summary,
    image_has_command,
    image_has_user,
)
from ..errors import StxError
from ..manifest import Manifest
from ..repo import git_toplevel, resolve_repo_root
from ..secrets import dotenv_mode_ok

_MIN_COMPOSE_VERSION = (2, 20, 0)
_IMAGE_REQUIRED_COMMANDS = (
    "bash",
    "iptables",
    "ip6tables",
    "ss",
    "tinyproxy",
    "ulogd",
    "jq",
    "git",
    "su",
)

# No per-box override, unlike identity/gpgsign/push.default — see docs/reference.md (Doctor).
_HAZARDOUS_UNIVERSAL_KEYS = ("core.worktree", "core.checkstat")


class SandboxDoctorCommand(Command):
    """Report-only diagnosis of the host setup a sandbox box depends on —
    see docs/reference.md (Doctor)."""

    @classmethod
    @override
    def name(cls) -> str:
        return "doctor"

    @classmethod
    @override
    def help(cls) -> str:
        return "Report-only diagnosis of the host setup a sandbox box depends on"

    @override
    def add_parser(self, subparsers) -> argparse.ArgumentParser:
        return subparsers.add_parser(self.name(), help=self.help())

    @override
    def _execute(self, args: argparse.Namespace) -> int | None:
        root = git_toplevel(Path.cwd()) or Path.cwd()
        ok = True

        identity_summary = identity_provenance_summary(root)
        print(identity_summary)
        ok = ok and "warning" not in identity_summary

        print()
        hazards = _find_hazardous_config(root)
        if hazards:
            print("hazardous config found in the shared .git/config:")
            for key, value in hazards:
                print(f"  {key} = {value}")
            print(
                "  these keys have no per-box override — every box (and every "
                "other worktree) sharing this .git/config inherits whatever "
                "value sits here. Almost never legitimate to set locally; unset "
                "with `git config --unset <key>` if it's not intentional."
            )
        else:
            print("no hazardous universal config keys found")
        ok = ok and not hazards

        print()
        _report_user_config()

        print()
        ok = _report_dotenv_mode() and ok

        print()
        ok = _report_compose_version() and ok

        print()
        ok = _report_image_requirements() and ok

        return None if ok else 1


def _find_hazardous_config(root: Path) -> list[tuple[str, str]]:
    found = []
    for key in _HAZARDOUS_UNIVERSAL_KEYS:
        result = subprocess.run(
            ["git", "config", "--get", key],
            cwd=str(root),
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            found.append((key, result.stdout.strip()))
    return found


def _report_user_config() -> None:
    config = load_config()
    print("user config:")
    for key in sorted(KNOWN_CONFIG_KEYS):
        value = config_value(config, key)
        print(f"  {key}: {value if value is not None else '(unset)'}")


def _report_dotenv_mode() -> bool:
    path = default_dotenv_path()
    if not path.exists():
        print(f"dotenv: {path} does not exist (fine if using secrets.source=op)")
        return True
    ok = dotenv_mode_ok(path)
    mode = path.stat().st_mode & 0o777
    icon = "✓" if ok else "✗"
    print(
        f"{icon} dotenv: {path} is mode {oct(mode)}"
        + ("" if ok else " (expected 0600 or stricter)")
    )
    return ok


def _report_compose_version() -> bool:
    version = compose_version()
    if version is None:
        print("✗ compose: could not determine `docker compose` version")
        return False
    ok = version >= _MIN_COMPOSE_VERSION
    icon = "✓" if ok else "✗"
    min_str = ".".join(str(p) for p in _MIN_COMPOSE_VERSION)
    print(f"{icon} compose: {'.'.join(str(p) for p in version)} (need >= {min_str})")
    return ok


def _report_image_requirements() -> bool:
    try:
        root = resolve_repo_root(None, Path.cwd())
    except StxError:
        print("image: not run inside a repo with a manifest — skipped")
        return True

    try:
        manifest = Manifest.load(root)
    except StxError as e:
        print(f"✗ image: manifest failed to load: {e}")
        return False

    image = manifest.image
    print(f"image requirements ({image}):")
    ok = True
    for binary in _IMAGE_REQUIRED_COMMANDS:
        found = image_has_command(image, binary)
        print(f"  {'✓' if found else '✗'} {binary}")
        ok = ok and found

    try:
        user = manifest.resolve_user()
    except StxError as e:
        print(f"  ✗ user: {e}")
        return False

    has_user = image_has_user(image, user)
    print(f"  {'✓' if has_user else '✗'} user '{user}' exists")
    ok = ok and has_user

    has_claude = has_user and image_has_command(image, "claude", user=user)
    print(f"  {'✓' if has_claude else '✗'} claude on {user}'s PATH")
    ok = ok and has_claude

    return ok
