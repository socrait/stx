import subprocess
from collections.abc import Callable
from pathlib import Path

from ..errors import StxError
from .compose import run_bounded
from .hostgit import run as host_git_run

CompletedProcessRunner = Callable[..., "subprocess.CompletedProcess[str]"]


def resolve_default_base_branch(
    cwd: Path, *, runner: CompletedProcessRunner = subprocess.run
) -> str:
    """The base ref for a target with no history anywhere yet.

    With no `origin` remote at all, the base is the current branch — there
    is nothing else to compare against. With an origin, prefers its own
    default branch (`origin/HEAD`, resolved via `symbolic-ref`), falling
    back to the current branch's upstream when origin has no HEAD symref.
    `runner` is a seam for tests to inject a fake subprocess result.

    Raises StxError if an origin exists but neither resolves — there is no
    sandbox-wide convention to fall back to silently.
    """
    has_origin = run_bounded(
        ["git", "-C", str(cwd), "remote", "get-url", "origin"],
        runner=runner,
        capture_output=True,
        text=True,
    )
    if has_origin.returncode != 0:
        current = run_bounded(
            ["git", "-C", str(cwd), "rev-parse", "--abbrev-ref", "HEAD"],
            runner=runner,
            capture_output=True,
            text=True,
        )
        if current.returncode == 0:
            return current.stdout.strip()
        raise StxError(f"could not resolve the current branch in {cwd}")

    symref = run_bounded(
        ["git", "-C", str(cwd), "symbolic-ref", "refs/remotes/origin/HEAD"],
        runner=runner,
        capture_output=True,
        text=True,
    )
    if symref.returncode == 0:
        return symref.stdout.strip().removeprefix("refs/remotes/")
    upstream = run_bounded(
        [
            "git",
            "-C",
            str(cwd),
            "rev-parse",
            "--abbrev-ref",
            "--symbolic-full-name",
            "@{upstream}",
        ],
        runner=runner,
        capture_output=True,
        text=True,
    )
    if upstream.returncode == 0:
        return upstream.stdout.strip()
    raise StxError(
        f"could not resolve a default base branch in {cwd}: no origin/HEAD "
        "and no upstream for the current branch"
    )


def has_uncommitted_changes(
    slug: str,
    wt_path: Path,
    common_dir: Path,
    *,
    runner: CompletedProcessRunner = subprocess.run,
) -> bool:
    """Whether a worktree has staged or unstaged changes worth not losing.
    Runs through core.hostgit, so altered worktree metadata surfaces as the
    same StxError a failed git call would. `runner` is a seam for tests to
    inject a fake subprocess result.

    Raises StxError if the check itself fails (e.g. the worktree is gone or
    git can't run there) — a failed check must never be read as "clean",
    since that's exactly the state a data-loss guard exists to catch.
    """
    result = host_git_run(
        slug,
        wt_path,
        common_dir,
        ["status", "--porcelain", "--ignore-submodules"],
        runner=runner,
    )
    if result.returncode != 0:
        raise StxError(
            f"could not check git status in {wt_path}: {result.stderr.strip()}"
        )
    return bool(result.stdout.strip())


def unpushed_commit_shas(
    slug: str,
    wt_path: Path,
    common_dir: Path,
    *,
    runner: CompletedProcessRunner = subprocess.run,
) -> list[str]:
    """Short SHAs of commits on HEAD that no remote-tracking ref reaches,
    newest first. Runs through core.hostgit, so altered worktree metadata
    surfaces as the same StxError a failed git call would. `runner` is a
    seam for tests to inject a fake subprocess result.

    Raises StxError if the check itself fails to run — a failed check must
    never be read as "nothing at risk", since that's exactly the state this
    guards against losing silently.
    """
    result = host_git_run(
        slug,
        wt_path,
        common_dir,
        ["rev-list", "--abbrev-commit", "HEAD", "--not", "--remotes"],
        runner=runner,
    )
    if result.returncode != 0:
        raise StxError(
            f"could not determine unpushed commits in {wt_path}: "
            f"{result.stderr.strip()}"
        )
    return result.stdout.split()


def has_unpushed_commits(
    slug: str,
    wt_path: Path,
    common_dir: Path,
    *,
    runner: CompletedProcessRunner = subprocess.run,
) -> bool:
    """Whether a worktree holds commits that only exist locally.

    See unpushed_commit_shas for the comparison and the `runner` seam.
    """
    return bool(unpushed_commit_shas(slug, wt_path, common_dir, runner=runner))


def describe_commits(
    slug: str,
    wt_path: Path,
    common_dir: Path,
    shas: list[str],
    *,
    runner: CompletedProcessRunner = subprocess.run,
) -> list[str]:
    """Short SHA and subject for each of shas, in the order given — for
    naming at-risk commits in a user-facing message. Runs through
    core.hostgit, so altered worktree metadata surfaces as the same StxError
    a failed git call would. `runner` is a seam for tests to inject a fake
    subprocess result.

    Raises StxError if the lookup fails.
    """
    if not shas:
        return []
    result = host_git_run(
        slug,
        wt_path,
        common_dir,
        ["log", "--no-walk", "--format=%h %s", *shas],
        runner=runner,
    )
    if result.returncode != 0:
        raise StxError(
            f"could not describe commits in {wt_path}: {result.stderr.strip()}"
        )
    return result.stdout.splitlines()


def remove_locked_worktree(
    wt_path: Path, *, cwd: Path, runner: CompletedProcessRunner = subprocess.run
) -> bool:
    """Remove a sandbox worktree, returning whether it succeeded — see
    docs/reference.md (Branches). `runner` is a seam for tests to inject a
    fake subprocess result."""
    result = run_bounded(
        ["git", "worktree", "remove", "--force", "--force", str(wt_path)],
        runner=runner,
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def find_tmp_branches(
    root: Path, slug: str, *, runner: CompletedProcessRunner = subprocess.run
) -> list[str]:
    """Local tmp/<slug>/* scratch branches a box created for cross-branch
    work — see docs/reference.md (Branches). `runner` is a seam for tests to
    inject a fake subprocess result."""
    result = run_bounded(
        ["git", "for-each-ref", "--format=%(refname:short)", f"refs/heads/tmp/{slug}/"],
        runner=runner,
        cwd=str(root),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line]


def branch_is_fully_pushed(
    root: Path, branch: str, *, runner: CompletedProcessRunner = subprocess.run
) -> bool:
    """Whether every commit on branch is reachable from some remote-tracking
    ref — catches the pushed-to-a-different-branch case `git branch -d`
    misses. `runner` is a seam for tests to inject a fake subprocess result."""
    result = run_bounded(
        ["git", "rev-list", branch, "--not", "--remotes"],
        runner=runner,
        cwd=str(root),
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and not result.stdout.strip()
