import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from stx.core.hostgit import admin_dir, run, verify_worktree_metadata
from stx.errors import StxError


def _make_worktree(tmp_path: Path) -> tuple[Path, Path, str]:
    """A real repo + linked worktree under tmp_path. Returns (common_dir,
    wt_path, slug)."""
    root = tmp_path / "main"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "init"], cwd=root, check=True
    )
    slug = "foo"
    # git names the admin dir after the worktree's own directory basename —
    # stx's worktree_path(slug) makes that basename the slug itself, so the
    # fixture must match for the admin dir to land at worktrees/<slug>.
    wt_path = tmp_path / slug
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", f"stx/{slug}", str(wt_path)],
        cwd=root,
        check=True,
    )
    common_dir = root / ".git"
    return common_dir, wt_path, slug


class TestVerifyWorktreeMetadata:
    def test_passes_for_untouched_metadata(self, tmp_path):
        common_dir, wt_path, slug = _make_worktree(tmp_path)
        verify_worktree_metadata(slug, wt_path, common_dir)

    def test_raises_when_gitfile_missing(self, tmp_path):
        common_dir, wt_path, slug = _make_worktree(tmp_path)
        (wt_path / ".git").unlink()
        with pytest.raises(StxError, match="worktree gitfile"):
            verify_worktree_metadata(slug, wt_path, common_dir)

    def test_raises_when_gitfile_is_a_directory(self, tmp_path):
        common_dir, wt_path, slug = _make_worktree(tmp_path)
        (wt_path / ".git").unlink()
        (wt_path / ".git").mkdir()
        (wt_path / ".git" / "config").write_text(
            '[core]\n\tfsmonitor = "touch /tmp/pwned"\n'
        )
        with pytest.raises(StxError, match="worktree gitfile"):
            verify_worktree_metadata(slug, wt_path, common_dir)

    def test_raises_when_gitfile_is_a_symlink(self, tmp_path):
        common_dir, wt_path, slug = _make_worktree(tmp_path)
        gitfile = wt_path / ".git"
        content = gitfile.read_text()
        gitfile.unlink()
        target = tmp_path / "elsewhere"
        target.write_text(content)
        gitfile.symlink_to(target)
        with pytest.raises(StxError, match="worktree gitfile"):
            verify_worktree_metadata(slug, wt_path, common_dir)

    def test_raises_when_gitfile_content_altered(self, tmp_path):
        common_dir, wt_path, slug = _make_worktree(tmp_path)
        (wt_path / ".git").write_text("gitdir: /somewhere/else\n")
        with pytest.raises(StxError, match="worktree gitfile"):
            verify_worktree_metadata(slug, wt_path, common_dir)

    def test_raises_when_admin_commondir_altered(self, tmp_path):
        common_dir, wt_path, slug = _make_worktree(tmp_path)
        admin_dir(common_dir, slug).joinpath("commondir").write_text("../../../evil\n")
        with pytest.raises(StxError, match="admin commondir"):
            verify_worktree_metadata(slug, wt_path, common_dir)

    def test_raises_when_admin_gitdir_altered(self, tmp_path):
        common_dir, wt_path, slug = _make_worktree(tmp_path)
        admin_dir(common_dir, slug).joinpath("gitdir").write_text(
            "/somewhere/else/.git\n"
        )
        with pytest.raises(StxError, match="admin gitdir"):
            verify_worktree_metadata(slug, wt_path, common_dir)

    def test_error_names_the_sandbox_and_remediation(self, tmp_path):
        common_dir, wt_path, slug = _make_worktree(tmp_path)
        (wt_path / ".git").write_text("gitdir: /somewhere/else\n")
        with pytest.raises(StxError, match=r"sandbox 'foo'.*stx rm --force"):
            verify_worktree_metadata(slug, wt_path, common_dir)

    def test_raises_when_worktree_config_worktree_present(self, tmp_path):
        common_dir, wt_path, slug = _make_worktree(tmp_path)
        admin_dir(common_dir, slug).joinpath("config.worktree").write_text(
            '[core]\n\tfsmonitor = "touch /tmp/pwned"\n'
        )
        with pytest.raises(StxError, match="worktree config"):
            verify_worktree_metadata(slug, wt_path, common_dir)

    def test_raises_when_top_level_config_worktree_present(self, tmp_path):
        common_dir, wt_path, slug = _make_worktree(tmp_path)
        (common_dir / "config.worktree").write_text("[core]\n\tbare = false\n")
        with pytest.raises(StxError, match="top-level worktree config"):
            verify_worktree_metadata(slug, wt_path, common_dir)

    def test_raises_when_top_level_commondir_present(self, tmp_path):
        common_dir, wt_path, slug = _make_worktree(tmp_path)
        (common_dir / "commondir").write_text("/somewhere/else\n")
        with pytest.raises(StxError, match="top-level commondir"):
            verify_worktree_metadata(slug, wt_path, common_dir)


class TestRun:
    def test_raises_without_invoking_runner_when_verification_fails(self, tmp_path):
        common_dir, wt_path, slug = _make_worktree(tmp_path)
        (wt_path / ".git").write_text("gitdir: /somewhere/else\n")
        runner = MagicMock()
        with pytest.raises(StxError):
            run(slug, wt_path, common_dir, ["status"], runner=runner)
        runner.assert_not_called()

    def test_runs_with_explicit_git_dir_and_hardened_config(self, tmp_path):
        common_dir, wt_path, slug = _make_worktree(tmp_path)
        runner = MagicMock(return_value=MagicMock(returncode=0, stdout="", stderr=""))

        run(slug, wt_path, common_dir, ["status", "--porcelain"], runner=runner)

        (args,), kwargs = runner.call_args
        assert args[0] == "git"
        assert args[-2:] == ["status", "--porcelain"]
        config_settings = [args[i + 1] for i in range(len(args) - 1) if args[i] == "-c"]
        for setting in (
            "core.fsmonitor=false",
            "core.hooksPath=/dev/null",
            "core.sshCommand=false",
            "diff.external=",
            "protocol.allow=never",
        ):
            assert setting in config_settings

        env = kwargs["env"]
        assert env["GIT_DIR"] == str(admin_dir(common_dir, slug))
        assert env["GIT_COMMON_DIR"] == str(common_dir)
        assert env["GIT_WORK_TREE"] == str(wt_path)
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert env["GIT_ATTR_NOSYSTEM"] == "1"
        # Attributes read from the empty tree, so no in-tree `.gitattributes`
        # can steer a command into a filter/textconv driver.
        assert env["GIT_ATTR_SOURCE"] == "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

    def test_a_hung_git_call_fails_clearly_instead_of_blocking_forever(self, tmp_path):
        common_dir, wt_path, slug = _make_worktree(tmp_path)

        def hangs(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))

        result = run(slug, wt_path, common_dir, ["status"], runner=hangs)

        assert result.returncode != 0

    def test_in_tree_gitattributes_filter_does_not_run(self, tmp_path):
        """A box-authored `.gitattributes` labelling a path with a filter the
        operator defined in the (read-only) common config must not run that
        filter when stx checks the worktree — `git status` would otherwise
        invoke the clean filter as the host user. See GIT_ATTR_SOURCE."""
        common_dir, wt_path, slug = _make_worktree(tmp_path)
        sentinel = tmp_path / "filter-ran"
        subprocess.run(
            [
                "git",
                "config",
                "filter.evil.clean",
                f"sh -c 'touch {sentinel}; cat'",
            ],
            cwd=common_dir,
            check=True,
        )
        (wt_path / ".gitattributes").write_text("* filter=evil\n")
        (wt_path / "tracked.txt").write_text("content\n")

        result = run(slug, wt_path, common_dir, ["status", "--porcelain"])

        assert result.returncode == 0
        assert not sentinel.exists()
        # The dirty state is still reported — suppressing the filter didn't
        # blind the check.
        assert "tracked.txt" in result.stdout

    def test_never_passes_dash_c_worktree(self, tmp_path):
        """The whole point: no `-C <worktree>` discovery anywhere in the
        invocation, since that's what lets a tampered gitfile be read."""
        common_dir, wt_path, slug = _make_worktree(tmp_path)
        runner = MagicMock(return_value=MagicMock(returncode=0, stdout="", stderr=""))

        run(slug, wt_path, common_dir, ["status"], runner=runner)

        (args,), _ = runner.call_args
        assert "-C" not in args
