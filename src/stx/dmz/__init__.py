"""stx's own dmz image — every dmz box uses it, with or without a repo. See
docs/box-contract.md (Image requirements)."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from importlib import metadata, resources
from pathlib import Path

from ..core.images import image_exists
from ..errors import StxError

CompletedProcessRunner = Callable[..., "subprocess.CompletedProcess[str]"]

_PACKAGE = "stx.dmz"


def dockerfile_dir() -> Path:
    """Host path to the dmz image's build context, mounted from the `stx`
    package's own data — see docs/box-contract.md."""
    return resources.files(_PACKAGE)


def image_tag() -> str:
    """`stx-dmz:<stx version>` — see docs/box-contract.md (Image requirements)."""
    return f"stx-dmz:{metadata.version('stx')}"


def build(*, runner: CompletedProcessRunner = subprocess.run) -> int | None:
    """Build the dmz image from package data. `runner` is a seam for tests
    to inject a fake subprocess result."""
    image = image_tag()
    print(f"  building {image}")
    result = runner(["docker", "build", "-t", image, str(dockerfile_dir())])
    if result.returncode != 0:
        print("error: dmz image build failed", file=sys.stderr)
        return 1
    return None


def ensure_built(*, runner: CompletedProcessRunner = subprocess.run) -> str:
    """Build the dmz image if it isn't already present; returns its tag
    either way. `runner` is a seam for tests to inject a fake subprocess
    result."""
    image = image_tag()
    if not image_exists(image, runner=runner):
        if build(runner=runner):
            raise StxError(f"could not build dmz image {image}")
    return image
