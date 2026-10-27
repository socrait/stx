import subprocess

from stx.core.paths import repo_cache_id


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_cache_id_is_the_main_checkouts_name_even_from_a_linked_worktree(tmp_path):
    main = tmp_path / "widgetshop"
    main.mkdir()
    _git("init", "-q", cwd=main)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=main)
    linked = tmp_path / "elsewhere" / "feature-x"
    _git("worktree", "add", "-q", "-b", "feature-x", str(linked), cwd=main)

    assert repo_cache_id(main) == "widgetshop"
    assert repo_cache_id(linked) == "widgetshop"
