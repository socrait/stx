"""Image introspection shared by `new`, `doctor`, and `rebuild`."""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Callable
from pathlib import Path

CompletedProcessRunner = Callable[..., "subprocess.CompletedProcess[str]"]

#: Public — `commands/rebuild.py` stamps this label onto a freshly built
#: image; this module reads it back.
REBUILD_HASH_LABEL = "stx.rebuild_hash"


def image_label(
    image: str, label: str, *, runner: CompletedProcessRunner = subprocess.run
) -> str | None:
    """The value of a label on a built image, or None if unset/the image is
    missing. `runner` is a seam for tests to inject a fake subprocess result."""
    result = runner(
        [
            "docker",
            "image",
            "inspect",
            "--format",
            f'{{{{index .Config.Labels "{label}"}}}}',
            image,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value if value and value != "<no value>" else None


def image_user(
    image: str, *, runner: CompletedProcessRunner = subprocess.run
) -> str | None:
    """The user named by the image's `USER` instruction, without any group
    part, or None if it declares none or the image is missing. A uid-0 `USER`
    comes back as `root`."""
    result = runner(
        ["docker", "image", "inspect", "--format", "{{.Config.User}}", image],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    user = result.stdout.strip().split(":", 1)[0]
    if not user:
        return None
    return "root" if user == "0" else user


def image_has_command(
    image: str,
    binary: str,
    *,
    user: str | None = None,
    runner: CompletedProcessRunner = subprocess.run,
) -> bool:
    """Whether `binary` is on PATH inside a throwaway container of `image`.

    Runs as `user` when given — a user's PATH (e.g. `~/.local/bin`) can carry
    binaries root's own PATH doesn't, which is exactly the case for a
    per-user harness install. `runner` is a seam for tests to inject a fake
    subprocess result.
    """
    args = ["docker", "run", "--rm"]
    if user is not None:
        args += ["-u", user]
    args += [image, "sh", "-c", f"command -v {binary}"]
    result = runner(args, capture_output=True, text=True)
    return result.returncode == 0


def image_has_user(
    image: str, user: str, *, runner: CompletedProcessRunner = subprocess.run
) -> bool:
    """Whether `user` exists (and has a home directory) inside a throwaway
    container of `image`. `runner` is a seam for tests to inject a fake
    subprocess result."""
    result = runner(
        ["docker", "run", "--rm", image, "sh", "-c", f"getent passwd {user}"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def image_exists(
    image: str, *, runner: CompletedProcessRunner = subprocess.run
) -> bool:
    """`runner` is a seam for tests to inject a fake subprocess result."""
    result = runner(
        ["docker", "image", "inspect", image], capture_output=True, text=True
    )
    return result.returncode == 0


def image_id(
    image: str, *, runner: CompletedProcessRunner = subprocess.run
) -> str | None:
    """The local image id `image`'s tag currently resolves to, or None if
    that tag isn't pulled/built locally. `runner` is a seam for tests to
    inject a fake subprocess result."""
    result = runner(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def image_rebuild_hash(
    image: str, *, runner: CompletedProcessRunner = subprocess.run
) -> str | None:
    """The `stx.rebuild_hash` label already baked into a built image, or None."""
    return image_label(image, REBUILD_HASH_LABEL, runner=runner)


def rebuild_hash(root: Path, rebuild_on: tuple[str, ...]) -> str:
    """sha256 over the concatenated contents of every `box.rebuild_on` file, in
    order — the staleness signal `stx new`/`stx rebuild` compare against a
    built image's own `stx.rebuild_hash` label to decide whether to rebuild."""
    digest = hashlib.sha256()
    for rel in rebuild_on:
        digest.update((root / rel).read_bytes())
    return digest.hexdigest()
