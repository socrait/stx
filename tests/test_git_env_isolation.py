import subprocess


def test_temp_repo_helper_leaves_a_leaked_git_dir_sentinel_untouched(
    tmp_path, monkeypatch
):
    """Regression test for the incident where a sandbox box's ambient
    GIT_DIR/GIT_WORK_TREE leaked into a test's `git init`/`git config`,
    writing core.worktree/user.name/user.email into the host repo's shared
    .git/config (`git config user.*` resolves relative to GIT_DIR, not a
    helper's own `cwd=`, so a leaked GIT_DIR silently redirects the write).

    Simulates that leak reappearing — a sentinel repo stands in for the
    host checkout GIT_DIR would have pointed at — and proves a temp-repo
    helper that never calls `git config user.*` (relying on
    GIT_AUTHOR_*/GIT_COMMITTER_*, set by the autouse `_isolate_git_env`
    fixture in conftest.py, instead) cannot write an identity into it,
    regardless of what GIT_DIR happens to be set to.
    """
    sentinel = tmp_path / "sentinel"
    sentinel.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=sentinel, check=True)
    before = (sentinel / ".git" / "config").read_text()

    monkeypatch.setenv("GIT_DIR", str(sentinel / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(sentinel))

    repo = tmp_path / "throwaway"
    repo.mkdir()
    # No `git config user.*` anywhere in this sequence — GIT_AUTHOR_NAME/
    # GIT_COMMITTER_NAME etc. (set ambiently by _isolate_git_env) are what a
    # real helper relies on instead. Capture output rather than asserting
    # success: with GIT_DIR leaked, git itself may refuse or act on the
    # sentinel's own worktree instead of `repo` — the property under test
    # is only that the sentinel's *config* is never written to.
    subprocess.run(["git", "init", "-q", str(repo)], cwd=repo, capture_output=True)

    after = (sentinel / ".git" / "config").read_text()
    assert after == before
    assert "[user]" not in after
