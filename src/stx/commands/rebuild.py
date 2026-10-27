from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, override

if TYPE_CHECKING:
    import argparse

from .. import dmz
from ..command import Command
from ..core import (
    REBUILD_HASH_LABEL,
    base_env,
    compose_config,
    image_exists,
    image_rebuild_hash,
    rebuild_hash,
    resolve_dockerfile_path,
)
from ..errors import StxError
from ..manifest import Manifest
from ..repo import resolve_repo_root


class SandboxRebuildCommand(Command):
    """Build the cwd repo's shared box image, stamped with a staleness label."""

    @classmethod
    @override
    def name(cls) -> str:
        return "rebuild"

    @classmethod
    @override
    def help(cls) -> str:
        return "Build the cwd repo's box image, stamped with a staleness label"

    @override
    def add_parser(self, subparsers) -> argparse.ArgumentParser:
        parser = subparsers.add_parser(self.name(), help=self.help())
        parser.add_argument(
            "--repo",
            default=None,
            metavar="PATH",
            help="Repo checkout to build (default: the cwd's checkout)",
        )
        parser.add_argument(
            "--dmz",
            action="store_true",
            help="Force-rebuild stx's own dmz image instead of the repo's box image",
        )
        return parser

    @override
    def _execute(self, args: argparse.Namespace) -> int | None:
        if args.dmz:
            ret = dmz.build()
            if ret:
                return ret
            print(f"✓ built {dmz.image_tag()}")
            return None
        try:
            root = resolve_repo_root(args.repo, Path.cwd())
            manifest = Manifest.load(root)
        except StxError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        ret = build_image(
            root, manifest, label_hash=rebuild_hash(root, manifest.rebuild_on)
        )
        if ret:
            return ret
        print(f"✓ built {manifest.image}")
        return None


def _repo_build_spec(root: Path, manifest: Manifest) -> dict:
    """The repo's own build context/dockerfile/args/target, read directly from
    its compose file — no overlay needed."""
    config = compose_config(
        [root / manifest.compose], cwd=root, no_interpolate=True, env=base_env()
    )
    return config["services"][manifest.name].get("build") or {}


def build_image(root: Path, manifest: Manifest, *, label_hash: str) -> int | None:
    build = _repo_build_spec(root, manifest)
    context = root / build.get("context", ".")
    docker_args = [
        "docker",
        "build",
        "-t",
        manifest.image,
        "--label",
        f"{REBUILD_HASH_LABEL}={label_hash}",
    ]
    if "dockerfile" in build:
        docker_args += ["-f", str(resolve_dockerfile_path(context, build))]
    for key, value in (build.get("args") or {}).items():
        docker_args += ["--build-arg", f"{key}={value}"]
    if "target" in build:
        docker_args += ["--target", build["target"]]
    docker_args.append(str(context))

    print(f"  building {manifest.image}")
    result = subprocess.run(docker_args, cwd=str(root))
    if result.returncode != 0:
        print("error: devbox image build failed", file=sys.stderr)
        return 1
    return None


def ensure_image_fresh(root: Path, manifest: Manifest) -> int | None:
    image = manifest.image
    fresh_hash = rebuild_hash(root, manifest.rebuild_on)
    if image_exists(image) and image_rebuild_hash(image) == fresh_hash:
        return None
    return build_image(root, manifest, label_hash=fresh_hash)
