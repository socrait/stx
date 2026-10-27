import subprocess
from pathlib import Path

import pytest

from stx.core.registry import SandboxRecord, SandboxState, save_registry
from stx.errors import StxError
from stx.repo import git_toplevel, resolve_repo_root


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, capture_output=True, check=True)
    (path / "sandbox.yaml").write_text("schema: 1\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True
    )


class TestGitToplevel:
    def test_returns_repo_root(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        nested = repo / "a" / "b"
        nested.mkdir(parents=True)
        assert git_toplevel(nested) == repo.resolve()

    def test_returns_none_outside_any_checkout(self, tmp_path):
        outside = tmp_path / "not-a-repo"
        outside.mkdir()
        assert git_toplevel(outside) is None


def _register(slug: str, repo_root: Path) -> None:
    """Minimal registry entry mapping a sandbox slug to its repo root — the
    seam resolve_repo_root uses to map a worktree back to its repo."""
    save_registry(
        {
            slug: SandboxRecord(
                slug=slug,
                created="2026-01-01T00:00:00+00:00",
                state=SandboxState.READY,
                redis_index=None,
                tailnet_port=None,
                repo_root=str(repo_root),
            )
        }
    )


class TestResolveRepoRoot:
    def test_repos_none_returns_none_with_no_git_calls(self, tmp_path):
        assert resolve_repo_root("none", tmp_path) is None

    def test_resolves_from_cwd_by_default(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        assert resolve_repo_root(None, repo) == repo.resolve()

    def test_resolves_from_cwd_subdirectory(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        nested = repo / "a" / "b"
        nested.mkdir(parents=True)
        assert resolve_repo_root(None, nested) == repo.resolve()

    def test_repos_path_overrides_cwd(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        other_cwd = tmp_path / "elsewhere"
        other_cwd.mkdir()
        assert resolve_repo_root(str(repo), other_cwd) == repo.resolve()

    def test_linked_worktree_of_repo_is_itself_a_valid_root(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        linked = tmp_path / "linked"
        subprocess.run(
            ["git", "worktree", "add", "-b", "other", str(linked)],
            cwd=repo,
            check=True,
            capture_output=True,
        )
        # A linked worktree is a fresh checkout of the branch's tree, which
        # already carries sandbox.yaml from the commit in _init_repo.
        assert resolve_repo_root(None, linked) == linked.resolve()

    def test_sandboxs_own_worktree_redirects_to_main_checkout(
        self, monkeypatch, tmp_path
    ):
        # The redirect comes from the registry (slug -> repo_root), not from
        # `git worktree list` — so a box that rewrote its own worktree admin
        # metadata can't steer a host command at a repo of its choosing.
        state = tmp_path / "state"
        monkeypatch.setenv("STX_STATE_ROOT", str(state))
        repo = tmp_path / "repo"
        _init_repo(repo)
        sandbox_wt = state / "worktrees" / "my-slug"
        sandbox_wt.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "worktree", "add", "-b", "stx/my-slug", str(sandbox_wt)],
            cwd=repo,
            check=True,
            capture_output=True,
        )
        _register("my-slug", repo.resolve())
        assert resolve_repo_root(None, sandbox_wt) == repo.resolve()

    def test_sandbox_worktree_without_registry_entry_does_not_redirect(
        self, monkeypatch, tmp_path
    ):
        # No registry entry for the slug: the worktree is itself a checkout
        # carrying sandbox.yaml, so it resolves to itself rather than reaching
        # into box-written admin state to find a main checkout.
        state = tmp_path / "state"
        monkeypatch.setenv("STX_STATE_ROOT", str(state))
        repo = tmp_path / "repo"
        _init_repo(repo)
        sandbox_wt = state / "worktrees" / "unregistered"
        sandbox_wt.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "worktree", "add", "-b", "stx/unregistered", str(sandbox_wt)],
            cwd=repo,
            check=True,
            capture_output=True,
        )
        assert resolve_repo_root(None, sandbox_wt) == sandbox_wt.resolve()

    def test_missing_manifest_raises(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)

        with pytest.raises(StxError, match="sandbox.yaml"):
            resolve_repo_root(None, repo)

    def test_outside_any_checkout_raises(self, tmp_path):
        outside = tmp_path / "not-a-repo"
        outside.mkdir()

        with pytest.raises(StxError, match="sandbox.yaml"):
            resolve_repo_root(None, outside)
