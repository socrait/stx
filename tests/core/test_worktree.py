import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from stx.core.hostgit import admin_dir
from stx.core.worktree import (
    describe_commits,
    has_uncommitted_changes,
    has_unpushed_commits,
    resolve_default_base_branch,
    unpushed_commit_shas,
)
from stx.errors import StxError


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, capture_output=True, check=True)


def _commit(repo: Path, name: str, message: str) -> None:
    (repo / name).write_text(message)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", message], cwd=repo, check=True)


def _push_new_branch(repo: Path, remote: Path, branch: str) -> None:
    subprocess.run(
        ["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True
    )
    subprocess.run(
        ["git", "push", "-u", "origin", f"HEAD:{branch}"],
        cwd=repo,
        capture_output=True,
        check=True,
    )


def _add_worktree(repo: Path, slug: str) -> Path:
    """A real linked worktree — git names its admin dir after the worktree's
    own directory basename, so it must equal slug, same as
    stx.core.paths.worktree_path(slug)."""
    wt_path = repo.parent / slug
    subprocess.run(
        ["git", "worktree", "add", "-b", f"stx/{slug}", str(wt_path)],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    return wt_path


def _run_sequence(results):
    calls = iter(results)

    def _run(*a, **kw):
        return next(calls)

    return _run


def _const_runner(result):
    return _run_sequence([result])


_HAS_ORIGIN = MagicMock(returncode=0, stdout="git@gitlab.com:x/y.git\n", stderr="")
_NO_ORIGIN = MagicMock(returncode=1, stdout="", stderr="fatal: no such remote 'origin'")


class TestResolveDefaultBaseBranch:
    def test_returns_origin_head_symref(self, tmp_path):
        runner = _run_sequence(
            [
                _HAS_ORIGIN,
                MagicMock(returncode=0, stdout="refs/remotes/origin/main\n", stderr=""),
            ]
        )
        assert resolve_default_base_branch(tmp_path, runner=runner) == "origin/main"

    def test_falls_back_to_current_branch_upstream(self, tmp_path):
        runner = _run_sequence(
            [
                _HAS_ORIGIN,
                MagicMock(returncode=1, stdout="", stderr="fatal: no such ref"),
                MagicMock(returncode=0, stdout="origin/feat-x\n", stderr=""),
            ]
        )
        assert resolve_default_base_branch(tmp_path, runner=runner) == "origin/feat-x"

    def test_raises_when_neither_resolves(self, tmp_path):
        runner = _run_sequence(
            [
                _HAS_ORIGIN,
                MagicMock(returncode=1, stdout="", stderr="fatal: no such ref"),
                MagicMock(returncode=128, stdout="", stderr="fatal: no upstream"),
            ]
        )
        with pytest.raises(StxError, match="could not resolve a default base branch"):
            resolve_default_base_branch(tmp_path, runner=runner)

    def test_a_hung_git_call_fails_clearly_instead_of_blocking_forever(self, tmp_path):
        def hangs(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))

        with pytest.raises(StxError, match="could not resolve the current branch"):
            resolve_default_base_branch(tmp_path, runner=hangs)

    def test_no_remote_uses_current_branch(self, tmp_path):
        runner = _run_sequence(
            [
                _NO_ORIGIN,
                MagicMock(returncode=0, stdout="my-local-branch\n", stderr=""),
            ]
        )
        assert resolve_default_base_branch(tmp_path, runner=runner) == "my-local-branch"

    def test_no_remote_and_no_current_branch_raises(self, tmp_path):
        runner = _run_sequence(
            [
                _NO_ORIGIN,
                MagicMock(returncode=128, stdout="", stderr="fatal: not a git repo"),
            ]
        )
        with pytest.raises(StxError, match="could not resolve the current branch"):
            resolve_default_base_branch(tmp_path, runner=runner)


def _fake_worktree_metadata(tmp_path: Path, slug: str) -> tuple[Path, Path]:
    """A worktree gitfile and admin dir holding exactly what `git worktree
    add` would write, without shelling out to git — fast enough for pure
    runner-mocked unit tests below, which fake the actual git invocation."""
    wt_path = tmp_path / slug
    wt_path.mkdir()
    common_dir = tmp_path / "common"
    admin = admin_dir(common_dir, slug)
    admin.mkdir(parents=True)
    (wt_path / ".git").write_text(f"gitdir: {admin}\n")
    (admin / "commondir").write_text("../..\n")
    (admin / "gitdir").write_text(f"{wt_path}/.git\n")
    return wt_path, common_dir


class TestHasUncommittedChanges:
    def test_false_when_clean(self, tmp_path):
        wt_path, common_dir = _fake_worktree_metadata(tmp_path, "my-slug")
        runner = _const_runner(MagicMock(returncode=0, stdout="", stderr=""))
        assert (
            has_uncommitted_changes("my-slug", wt_path, common_dir, runner=runner)
            is False
        )

    def test_true_when_dirty(self, tmp_path):
        wt_path, common_dir = _fake_worktree_metadata(tmp_path, "my-slug")
        runner = _const_runner(MagicMock(returncode=0, stdout=" M foo.py\n", stderr=""))
        assert (
            has_uncommitted_changes("my-slug", wt_path, common_dir, runner=runner)
            is True
        )

    def test_raises_when_git_status_fails(self, tmp_path):
        wt_path, common_dir = _fake_worktree_metadata(tmp_path, "my-slug")
        runner = _const_runner(
            MagicMock(returncode=128, stdout="", stderr="fatal: not a git repository")
        )
        with pytest.raises(StxError, match="could not check git status"):
            has_uncommitted_changes("my-slug", wt_path, common_dir, runner=runner)

    def test_raises_when_worktree_gitfile_was_altered(self, tmp_path):
        wt_path, common_dir = _fake_worktree_metadata(tmp_path, "my-slug")
        (wt_path / ".git").write_text("gitdir: /somewhere/else\n")
        runner = MagicMock()
        with pytest.raises(StxError, match="altered from inside its box"):
            has_uncommitted_changes("my-slug", wt_path, common_dir, runner=runner)
        runner.assert_not_called()


class TestUnpushedCommitShas:
    def test_not_at_risk_when_base_commits_are_on_a_remote_and_no_own_commits(
        self, tmp_path
    ):
        # The be-1 case: a sandbox started with --base <pushed branch> that
        # never pushed its own branch — every commit it has is still
        # reachable from *some* remote-tracking ref.
        remote = tmp_path / "remote.git"
        remote.mkdir()
        subprocess.run(["git", "init", "--bare"], cwd=remote, check=True)

        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        _commit(repo, "file.txt", "init")
        _push_new_branch(repo, remote, "feat/base")

        wt_path = _add_worktree(repo, "orphan")
        common_dir = repo / ".git"

        assert unpushed_commit_shas("orphan", wt_path, common_dir) == []

    def test_at_risk_when_own_commit_is_on_no_remote(self, tmp_path):
        remote = tmp_path / "remote.git"
        remote.mkdir()
        subprocess.run(["git", "init", "--bare"], cwd=remote, check=True)

        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        _commit(repo, "file.txt", "init")
        _push_new_branch(repo, remote, "feat/test")

        wt_path = _add_worktree(repo, "my-slug")
        common_dir = repo / ".git"
        _commit(wt_path, "file2.txt", "second")

        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=wt_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert unpushed_commit_shas("my-slug", wt_path, common_dir) == [sha]

    def test_not_at_risk_when_commit_pushed_to_a_different_remote_branch(
        self, tmp_path
    ):
        remote = tmp_path / "remote.git"
        remote.mkdir()
        subprocess.run(["git", "init", "--bare"], cwd=remote, check=True)

        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        _commit(repo, "file.txt", "init")
        # Pushed to a branch name no local branch tracks — the comparison
        # must still find it via --remotes, not a specific
        # origin/<branch> ref.
        _push_new_branch(repo, remote, "some/other/branch")

        wt_path = _add_worktree(repo, "my-slug")
        common_dir = repo / ".git"

        assert unpushed_commit_shas("my-slug", wt_path, common_dir) == []

    def test_raises_when_rev_list_fails(self, tmp_path):
        wt_path, common_dir = _fake_worktree_metadata(tmp_path, "my-slug")
        runner = _const_runner(
            MagicMock(returncode=128, stdout="", stderr="fatal: boom")
        )
        with pytest.raises(StxError, match="could not determine unpushed commits"):
            unpushed_commit_shas("my-slug", wt_path, common_dir, runner=runner)


class TestHasUnpushedCommits:
    """Thin bool() wrapper around unpushed_commit_shas — see
    TestUnpushedCommitShas above for the comparison's own scenario matrix."""

    def test_false_when_nothing_unpushed(self, tmp_path):
        wt_path, common_dir = _fake_worktree_metadata(tmp_path, "my-slug")
        runner = _const_runner(MagicMock(returncode=0, stdout=""))
        assert (
            has_unpushed_commits("my-slug", wt_path, common_dir, runner=runner) is False
        )

    def test_true_when_something_unpushed(self, tmp_path):
        wt_path, common_dir = _fake_worktree_metadata(tmp_path, "my-slug")
        runner = _const_runner(MagicMock(returncode=0, stdout="abc1234\n"))
        assert (
            has_unpushed_commits("my-slug", wt_path, common_dir, runner=runner) is True
        )


class TestDescribeCommits:
    def test_empty_for_no_shas(self, tmp_path):
        wt_path, common_dir = _fake_worktree_metadata(tmp_path, "my-slug")
        assert describe_commits("my-slug", wt_path, common_dir, []) == []

    def test_returns_sha_and_subject_in_order_given(self, tmp_path):
        remote = tmp_path / "remote.git"
        remote.mkdir()
        subprocess.run(["git", "init", "--bare"], cwd=remote, check=True)

        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        _commit(repo, "file.txt", "init")
        _push_new_branch(repo, remote, "feat/test")

        wt_path = _add_worktree(repo, "my-slug")
        common_dir = repo / ".git"
        _commit(wt_path, "file2.txt", "second")

        shas = subprocess.run(
            ["git", "log", "--format=%h", "HEAD"],
            cwd=wt_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()

        assert describe_commits("my-slug", wt_path, common_dir, shas) == [
            f"{shas[0]} second",
            f"{shas[1]} init",
        ]

    def test_raises_when_log_fails(self, tmp_path):
        wt_path, common_dir = _fake_worktree_metadata(tmp_path, "my-slug")
        runner = _const_runner(
            MagicMock(returncode=128, stdout="", stderr="fatal: bad revision")
        )
        with pytest.raises(StxError, match="could not describe commits"):
            describe_commits("my-slug", wt_path, common_dir, ["deadbee"], runner=runner)
