"""Host-side git calls against a sandbox worktree never trust what a box can
write — see docs/security.md. Every such call goes through `run`, which
verifies the worktree's own admin metadata first, then runs with an explicit
GIT_DIR/GIT_COMMON_DIR/GIT_WORK_TREE (never discovery) and a fixed set of
hardened config overrides, so a box-controlled config, hook, pointer file, or
filter driver is never read, let alone run, as the host user."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

from ..errors import StxError
from .compose import run_bounded
from .paths import repo_git_dir

CompletedProcessRunner = Callable[..., "subprocess.CompletedProcess[str]"]

# Belt-and-suspenders beyond the explicit GIT_DIR/GIT_WORK_TREE below: even if
# metadata verification were somehow bypassed, none of these can be
# re-enabled by anything a box can write, since they're set on the command
# line, which always wins over any config file.
_HARDENED_CONFIG = (
    "core.fsmonitor=false",
    "core.hooksPath=/dev/null",
    "core.sshCommand=false",
    "diff.external=",
    "protocol.allow=never",
    "core.untrackedCache=false",
    "core.attributesFile=/dev/null",
)

# The SHA-1 empty tree. Pointed at via GIT_ATTR_SOURCE (below) so attribute
# lookups read *no* .gitattributes at all: `core.attributesFile=/dev/null`
# above only neuters the global/system files, but the worktree's own in-tree
# `.gitattributes` is the box's content and is read regardless — it could name
# a `filter`/`diff` attribute whose driver the operator defined in the
# (read-only) common config, so a plain `git status` would run that driver as
# the host user. Reading attributes from the empty tree selects no driver; git
# treats an unreadable attr source (e.g. a SHA-256 repo, where this id doesn't
# resolve) as "no attributes" without erroring, and an older git that predates
# GIT_ATTR_SOURCE simply ignores it.
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def hardened_config_args() -> list[str]:
    """The `-c` overrides from the belt-and-suspenders list above, as bare
    git CLI args — for a caller that can't go through the full verified
    `run()` because it doesn't know a sandbox's identity yet (e.g. generic
    repo-root discovery that might happen to be pointed at a worktree, see
    repo.py). Still worth applying: these are command-line overrides, so
    they win over anything a box could have written to a discovered
    config."""
    return [arg for setting in _HARDENED_CONFIG for arg in ("-c", setting)]


def common_dir_for(root: Path) -> Path | None:
    """root's common git dir, or None if root isn't a usable git checkout —
    a caller that can't find one has nothing to verify a worktree's
    metadata against, so treats that the same as a failed check (see
    rm/prune's own "a failed check is treated as dirty" guards)."""
    try:
        return repo_git_dir(root)
    except (OSError, subprocess.CalledProcessError):
        return None


def admin_dir(common_dir: Path, slug: str) -> Path:
    """A sandbox's own worktree admin dir under the repo's common git dir —
    what `git worktree add` created it as, and what stx's GIT_DIR overlay
    env var points the box itself at (see overlay.py)."""
    return common_dir / "worktrees" / slug


def verify_worktree_metadata(slug: str, wt_path: Path, common_dir: Path) -> None:
    """Refuse to trust wt_path's git metadata unless it's still exactly what
    `git worktree add` wrote: the worktree's own gitfile, and the admin
    dir's `commondir` and `gitdir` files. A box can replace any of the
    three — the gitfile with a real `.git` directory holding its own
    config, or either admin file with content that redirects an
    administrative lookup elsewhere — see docs/security.md.

    Raises StxError naming the sandbox and which file, with how to remove
    the sandbox without this check (`stx rm --force`).
    """
    admin = admin_dir(common_dir, slug)
    for path, expected, what in (
        (wt_path / ".git", f"gitdir: {admin}\n", "worktree gitfile"),
        (admin / "commondir", "../..\n", "admin commondir"),
        (admin / "gitdir", f"{wt_path}/.git\n", "admin gitdir"),
    ):
        _expect_exact(path, expected, slug=slug, what=what)
    # A per-worktree or whole-repo config file git would read for extra
    # config: `config.worktree` is read when `extensions.worktreeConfig` is
    # on, and a top-level `commondir`/`config.worktree` steers the main repo
    # itself. The overlay mounts the top of /git read-only so a box can't
    # create these, but refuse outright if one is somehow present rather than
    # run git somewhere a box might have pointed it.
    for path, what in (
        (admin / "config.worktree", "worktree config"),
        (common_dir / "config.worktree", "top-level worktree config"),
        (common_dir / "commondir", "top-level commondir"),
    ):
        _refuse_if_present(path, slug=slug, what=what)


def _expect_exact(path: Path, expected: str, *, slug: str, what: str) -> None:
    altered = path.is_symlink() or not path.is_file()
    if not altered:
        try:
            altered = path.read_text() != expected
        except OSError:
            altered = True
    if altered:
        raise StxError(
            f"sandbox '{slug}'s {what} ({path}) was altered from inside its "
            "box — refusing to run git against it. `stx rm --force` removes "
            "the sandbox without this check; otherwise investigate before "
            "trusting its worktree again."
        )


def _refuse_if_present(path: Path, *, slug: str, what: str) -> None:
    """Refuse when `path` exists at all — for a file `git worktree add` never
    creates, so any presence is something a box (or a tampered-with host
    repo) planted."""
    if path.exists() or path.is_symlink():
        raise StxError(
            f"sandbox '{slug}' has a {what} file ({path}) that `git worktree "
            "add` never creates — refusing to run git against it, since git "
            "would read it. `stx rm --force` removes the sandbox without this "
            "check; otherwise investigate before trusting its worktree again."
        )


def run(
    slug: str,
    wt_path: Path,
    common_dir: Path,
    args: list[str],
    *,
    runner: CompletedProcessRunner = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    """Run git against a sandbox worktree — verifying its admin metadata,
    then with an explicit GIT_DIR/GIT_WORK_TREE and the hardened config
    overrides above, never `-C`/discovery. `runner` is a seam for tests to
    inject a fake subprocess result.

    Raises StxError if verification fails.
    """
    verify_worktree_metadata(slug, wt_path, common_dir)
    env = dict(os.environ)
    env["GIT_DIR"] = str(admin_dir(common_dir, slug))
    # Explicit, from the registry-derived common_dir, never discovered — so
    # git never reads the worktree's own `commondir` pointer (which a box can
    # write) to find where the shared refs/objects/config live.
    env["GIT_COMMON_DIR"] = str(common_dir)
    env["GIT_WORK_TREE"] = str(wt_path)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_ATTR_NOSYSTEM"] = "1"
    # Read attributes from the empty tree, not the box's in-tree
    # `.gitattributes` — so a command touching working-tree content (e.g.
    # `status`, which runs the clean filter to detect changes) never selects
    # a filter or textconv driver the box labelled a path with. See _EMPTY_TREE.
    env["GIT_ATTR_SOURCE"] = _EMPTY_TREE
    config_args = hardened_config_args()
    return run_bounded(
        ["git", *config_args, *args],
        runner=runner,
        env=env,
        capture_output=True,
        text=True,
    )
