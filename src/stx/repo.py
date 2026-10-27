"""Repo root resolution — see docs/reference.md (Repo root resolution)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .core.hostgit import hardened_config_args
from .core.paths import state_root
from .core.registry import load_registry
from .errors import StxError


def git_toplevel(start: Path) -> Path | None:
    """The top of the git checkout containing `start`, or None if not inside
    one. `start` may be a sandbox worktree (see resolve_repo_root) whose
    metadata a box can write — the hardened config overrides apply even
    though there's no sandbox identity yet to verify against (see
    core.hostgit)."""
    result = subprocess.run(
        [
            "git",
            *hardened_config_args(),
            "-C",
            str(start),
            "rev-parse",
            "--show-toplevel",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    top = result.stdout.strip()
    return Path(top).resolve() if top else None


def sandbox_worktree_repo_root(top: Path) -> Path | None:
    """If `top` is a sandbox's own worktree (under the state root), the repo it
    was cut from — looked up by slug in the registry, never via `git worktree
    list`, whose output is parsed from the worktree's own `commondir`/`gitdir`
    admin files, which a box can write (see docs/security.md). None if `top`
    isn't a sandbox worktree, or its slug has no registered repo root."""
    worktrees_root = state_root() / "worktrees"
    if not top.is_relative_to(worktrees_root):
        return None
    slug = top.relative_to(worktrees_root).parts[0]
    record = load_registry().get(slug)
    if record is None or not record.repo_root:
        return None
    return Path(record.repo_root).resolve()


def resolve_repo_root(repos_arg: str | None, cwd: Path) -> Path | None:
    """Resolve the one repo root for a command — see docs/reference.md (Repo
    root resolution). Raises StxError if the resolved top has no sandbox.yaml."""
    if repos_arg == "none":
        return None
    start = Path(repos_arg).resolve() if repos_arg else cwd
    top = git_toplevel(start)
    if top is not None:
        top = sandbox_worktree_repo_root(top) or top
    if top is None or not (top / "sandbox.yaml").exists():
        raise StxError(
            f"{start} is not in a checkout with a sandbox.yaml (pass --repo PATH)"
        )
    return top
