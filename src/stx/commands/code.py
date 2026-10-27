from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, override

if TYPE_CHECKING:
    import argparse

from ..command import Command
from ..core import (
    SandboxRecord,
    box_not_found_error,
    code_dmz_dir,
    common_dir_for,
    container_name_for_box,
    container_status,
    default_box,
    load_registry,
    trusted_box_name,
    verify_worktree_metadata,
)
from ..errors import StxError
from .start import stopped_box_hint

# (CLI binary, macOS app bundle name) per IDE choice — see docs/reference.md (`code` row).
_IDE_SPECS: dict[str, tuple[str, str]] = {
    "cursor": ("cursor", "Cursor"),
    "vscode": ("code", "Visual Studio Code"),
}


class SandboxCodeCommand(Command):
    """Open the sandbox worktree in Cursor or VS Code."""

    @classmethod
    @override
    def name(cls) -> str:
        return "code"

    @classmethod
    @override
    def help(cls) -> str:
        return "Open the sandbox worktree in Cursor or VS Code"

    @override
    def add_parser(self, subparsers) -> argparse.ArgumentParser:
        parser = subparsers.add_parser(self.name(), help=self.help())
        parser.add_argument("slug", help="Sandbox slug")
        parser.add_argument(
            "-b",
            "--box",
            default=None,
            help="Box to open (default: the trusted box, or dmz if there's none)",
        )
        parser.add_argument(
            "--attach",
            action="store_true",
            help="Attach to the running container via Dev Containers (respects --ide)",
        )
        parser.add_argument(
            "--ide",
            choices=["auto", "cursor", "vscode"],
            default="auto",
            help="IDE to open (default: auto — tries Cursor then VS Code)",
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

        if args.attach:
            container = container_name_for_box(box, slug)
            status = container_status(container)
            if status is None:
                print(
                    f"error: container '{container}' doesn't exist — `stx "
                    "start` only restarts a stopped sandbox, it can't "
                    "recreate a removed one; try `stx new` or `stx refresh`",
                    file=sys.stderr,
                )
                return 1
            if status != "running":
                print(f"error: {stopped_box_hint(slug)}", file=sys.stderr)
                return 1
            return _open_attached(slug, box=box, ide=args.ide)
        trusted = trusted_box_name(record.boxes)
        if box == trusted:
            error = _refuse_if_worktree_metadata_altered(slug, record)
            if error:
                print(f"error: {error}", file=sys.stderr)
                return 1
            folder = record.boxes[trusted].worktree
        else:
            folder = str(code_dmz_dir(slug, record.repo or "repo"))
        # Opened on the host, outside the box's confinement — its contents were
        # written by the box, so the editor must not auto-run anything from it.
        print(
            "warning: this folder is box-written — open it as untrusted; your "
            "editor should not auto-run tasks, extensions, or scripts from it",
            file=sys.stderr,
        )
        return _open_host_folder(folder, ide=args.ide)


def _refuse_if_worktree_metadata_altered(
    slug: str, record: SandboxRecord
) -> str | None:
    """None if the trusted box's worktree metadata is still what `stx new`
    wrote; otherwise the reason to refuse opening it. A host IDE opened on
    the worktree runs its own git against it — see docs/security.md — so
    this is the host's one chance to catch tampering before that happens,
    same check `rm`/`prune` run before trusting the worktree themselves."""
    if not record.repo_root:
        return (
            f"could not verify sandbox '{slug}'s worktree metadata (no repo "
            "checkout to verify it against)"
        )
    common_dir = common_dir_for(Path(record.repo_root))
    if common_dir is None:
        return (
            f"could not verify sandbox '{slug}'s worktree metadata (no repo "
            "checkout to verify it against)"
        )
    trusted = trusted_box_name(record.boxes)
    wt_path = Path(record.boxes[trusted].worktree)
    try:
        verify_worktree_metadata(slug, wt_path, common_dir)
    except StxError as e:
        return str(e)
    return None


def _open_host_folder(worktree: str, *, ide: str = "auto") -> int | None:
    specs = list(_IDE_SPECS.values()) if ide == "auto" else [_IDE_SPECS[ide]]
    for binary, app_name in specs:
        if _launch_editor(binary, app_name, worktree):
            return None
    if ide == "auto":
        print(
            "error: neither Cursor nor VS Code found — install Cursor or VS Code",
            file=sys.stderr,
        )
    else:
        print(f"error: '{ide}' not found", file=sys.stderr)
    return 1


def _launch_editor(binary: str, app_name: str, worktree: str) -> bool:
    if sys.platform == "darwin":
        result = subprocess.run(["open", "-a", app_name, worktree], capture_output=True)
        if result.returncode == 0:
            return True
    if shutil.which(binary):
        result = subprocess.run([binary, worktree])
        return result.returncode == 0
    return False


def _app_bundle_binary(app_bundle_name: str, path_binary: str) -> str | None:
    # macOS: checks the app bundle directly, since 'code' on PATH is ambiguous.
    if sys.platform == "darwin":
        for app_root in (
            Path(f"/Applications/{app_bundle_name}.app"),
            Path.home() / f"Applications/{app_bundle_name}.app",
        ):
            cli = app_root / "Contents/Resources/app/bin/code"
            if cli.exists():
                return str(cli)
    return shutil.which(path_binary)


def _cursor_binary() -> str | None:
    return _app_bundle_binary("Cursor", "cursor")


def _vscode_binary() -> str | None:
    return _app_bundle_binary("Visual Studio Code", "code")


# Display names only — resolvers are looked up by name below so tests can monkeypatch them.
_ATTACH_IDE_DISPLAY_NAMES: dict[str, str] = {
    "cursor": "Cursor",
    "vscode": "Visual Studio Code",
}


def _open_attached(slug: str, *, box: str, ide: str = "auto") -> int | None:
    # Cursor first for ide="auto" — see docs/reference.md (`code --attach` row).
    names = list(_ATTACH_IDE_DISPLAY_NAMES) if ide == "auto" else [ide]
    binary = None
    for name in names:
        binary = _cursor_binary() if name == "cursor" else _vscode_binary()
        if binary:
            break
    if not binary:
        tried = "Cursor or VS Code" if ide == "auto" else _ATTACH_IDE_DISPLAY_NAMES[ide]
        print(
            f"error: {tried} not found — install it for Dev Containers attach",
            file=sys.stderr,
        )
        return 1
    container = container_name_for_box(box, slug)
    hex_name = container.encode().hex()  # Dev Containers URI authority is hex-encoded
    # /workspace is the fixed mount point defined in compose.yaml (working_dir: /workspace).
    subprocess.run(
        [
            binary,
            "--folder-uri",
            f"vscode-remote://attached-container+{hex_name}/workspace",
        ]
    )
    return None
