import argparse
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import stx.commands.prune as _cmd
from stx.commands.prune import SandboxPruneCommand, _ZombieInfo
from stx.core import SandboxRecord, SandboxState
from stx.errors import StxError
from stx.lifecycle import ContainerTeardown, TeardownResult

_MOD = "stx.commands.prune"
# The mechanical teardown now lives in the shared engine; prune only discovers
# what to tear down and wires it in. Patch the engine's seams for those steps.
_TD = "stx.lifecycle.teardown"


@pytest.fixture(autouse=True)
def _stub_sandbox_network(monkeypatch):
    """Network removal (now inside the shared teardown engine) hits real
    `docker network rm` otherwise — irrelevant to the branch/worktree/
    handoff-dir behavior these tests assert."""
    monkeypatch.setattr(f"{_TD}.stx_network.remove_network", lambda *a, **k: None)


def _args(
    *, dry_run: bool = False, force: bool = False, yes: bool = True
) -> argparse.Namespace:
    # yes=True by default: most of this file's tests exercise what gets
    # pruned, not the confirmation gate itself (covered by TestConfirmation
    # below) — pytest's own stdin isn't a tty, so leaving yes=False here
    # would make every one of them hit the non-interactive refusal instead.
    return argparse.Namespace(dry_run=dry_run, force=force, yes=yes)


def _docker_ps_result(rows: list[tuple[str, str, str, str]]) -> MagicMock:
    """Stand in for `docker ps -a --filter label=stx.slug --format ...` output —
    one tab-separated (slug, redis_index, repo_root, databases) row per
    labeled container."""
    result = MagicMock()
    result.returncode = 0
    result.stdout = "\n".join("\t".join(row) for row in rows)
    return result


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, capture_output=True, check=True)
    (path / "f.txt").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True)


def _add_stx_branch_config(
    root: Path, slug: str, *, target: str = "feat/x/foo", marked: bool = True
) -> None:
    subprocess.run(
        ["git", "config", f"branch.stx/{slug}.remote", "origin"], cwd=root, check=True
    )
    subprocess.run(
        ["git", "config", f"branch.stx/{slug}.merge", f"refs/heads/{target}"],
        cwd=root,
        check=True,
    )
    if marked:
        subprocess.run(
            ["git", "config", f"branch.stx/{slug}.stx", "true"],
            cwd=root,
            check=True,
        )


def _sandbox_record(slug: str, *, state: SandboxState) -> SandboxRecord:
    return SandboxRecord(
        slug=slug,
        created="2026-06-26T00:00:00+00:00",
        state=state,
        redis_index=None,
        tailnet_port=None,
        boxes={},
    )


class TestZombieInfoFromRaw:
    def test_reads_redis_index_repo_root_and_databases_from_raw_json(self):
        raw = {
            "slug": "stale",
            "redis_index": 7,
            "repo_root": "/repo/stale",
            "databases": ["stx_stale", "stx_stale_test"],
        }

        info = _cmd._zombie_info_from_raw(raw)

        assert info == _ZombieInfo(
            redis_index=7,
            repo_root=Path("/repo/stale"),
            databases=("stx_stale", "stx_stale_test"),
        )

    def test_missing_databases_key_is_none_not_guessed(self):
        # This is exactly the field a prior schema lacked (the reason the
        # entry failed to parse in the first place) — must stay None so
        # `_prune_one` lists-and-leaves rather than guessing a pair.
        raw = {"slug": "stale", "redis_index": 1, "repo_root": "/repo/stale"}

        assert _cmd._zombie_info_from_raw(raw).databases is None

    def test_empty_databases_list_is_a_legitimate_empty_tuple(self):
        raw = {
            "slug": "stale",
            "redis_index": None,
            "repo_root": "",
            "databases": [],
        }

        info = _cmd._zombie_info_from_raw(raw)

        assert info.databases == ()
        assert info.redis_index is None
        assert info.repo_root is None

    def test_missing_redis_index_and_repo_root_default_to_none(self):
        assert _cmd._zombie_info_from_raw({"slug": "stale"}) == _ZombieInfo(
            redis_index=None, repo_root=None, databases=None
        )


class TestFindStuckProvisioning:
    def test_finds_only_provisioning_entries(self):
        registry = {
            "stuck": _sandbox_record("stuck", state=SandboxState.PROVISIONING),
            "ready": _sandbox_record("ready", state=SandboxState.READY),
        }
        assert _cmd._find_stuck_provisioning(registry) == {"stuck": registry["stuck"]}

    def test_empty_registry_yields_nothing(self):
        assert _cmd._find_stuck_provisioning({}) == {}

    def test_provisioning_entry_with_a_live_owner_is_not_stuck(self):
        from stx.lifecycle import process_start_time

        rec = _sandbox_record("live", state=SandboxState.PROVISIONING)
        rec.owner_pid = os.getpid()
        rec.owner_started = process_start_time(os.getpid())
        # A `new` still running owns this — prune must not treat it as stuck.
        assert _cmd._find_stuck_provisioning({"live": rec}) == {}

    def test_provisioning_entry_with_a_dead_owner_is_stuck(self):
        rec = _sandbox_record("dead", state=SandboxState.PROVISIONING)
        rec.owner_pid = 2_000_000_000  # no such process
        rec.owner_started = "999999"
        assert _cmd._find_stuck_provisioning({"dead": rec}) == {"dead": rec}


class TestFindZombieSlugs:
    def test_finds_slug_not_in_registry(self, monkeypatch):
        monkeypatch.setattr(
            _cmd.subprocess,
            "run",
            lambda *a, **kw: _docker_ps_result([("orphan", "3", "/repo", "")]),
        )
        assert _cmd._find_zombie_slugs({}) == {
            "orphan": _ZombieInfo(redis_index=3, repo_root=Path("/repo"))
        }

    def test_excludes_slugs_already_registered(self, monkeypatch):
        monkeypatch.setattr(
            _cmd.subprocess,
            "run",
            lambda *a, **kw: _docker_ps_result(
                [("known", "1", "/r1", ""), ("orphan", "2", "/r2", "")]
            ),
        )
        assert _cmd._find_zombie_slugs({"known": MagicMock()}) == {
            "orphan": _ZombieInfo(redis_index=2, repo_root=Path("/r2"))
        }

    def test_app_and_dmz_containers_dedupe_to_one_slug(self, monkeypatch):
        # The dmz box carries stx.slug but not
        # stx.redis_index/stx.repo_root/stx.databases — the merge should
        # keep whichever row actually has a value.
        monkeypatch.setattr(
            _cmd.subprocess,
            "run",
            lambda *a, **kw: _docker_ps_result(
                [("foo", "5", "/repo", "stx_foo"), ("foo", "", "", "")]
            ),
        )
        assert _cmd._find_zombie_slugs({}) == {
            "foo": _ZombieInfo(
                redis_index=5, repo_root=Path("/repo"), databases=("stx_foo",)
            )
        }

    def test_ignores_non_sandbox_containers(self, monkeypatch):
        # `--filter label=stx.slug` already excludes unlabeled containers at
        # the docker level — nothing for this command to filter itself.
        monkeypatch.setattr(
            _cmd.subprocess, "run", lambda *a, **kw: _docker_ps_result([])
        )
        assert _cmd._find_zombie_slugs({}) == {}

    def test_returns_empty_dict_on_docker_failure(self, monkeypatch):
        failed = MagicMock(returncode=1, stdout="")
        monkeypatch.setattr(_cmd.subprocess, "run", lambda *a, **kw: failed)
        assert _cmd._find_zombie_slugs({}) == {}

    def test_reads_databases_label(self, monkeypatch):
        monkeypatch.setattr(
            _cmd.subprocess,
            "run",
            lambda *a, **kw: _docker_ps_result(
                [("orphan", "3", "/repo", "stx_orphan,stx_orphan_test")]
            ),
        )
        assert _cmd._find_zombie_slugs({}) == {
            "orphan": _ZombieInfo(
                redis_index=3,
                repo_root=Path("/repo"),
                databases=("stx_orphan", "stx_orphan_test"),
            )
        }

    def test_missing_databases_label_is_none_not_empty_tuple(self, monkeypatch):
        monkeypatch.setattr(
            _cmd.subprocess,
            "run",
            lambda *a, **kw: _docker_ps_result([("orphan", "3", "/repo", "")]),
        )
        assert _cmd._find_zombie_slugs({})["orphan"].databases is None


class TestFindOrphanedSandBranches:
    def test_finds_slug_not_in_registry(self, tmp_path):
        _init_repo(tmp_path)
        _add_stx_branch_config(tmp_path, "orphan")

        assert _cmd._find_orphaned_stx_branches(tmp_path, {}) == {"orphan"}

    def test_excludes_slugs_already_registered(self, tmp_path):
        _init_repo(tmp_path)
        _add_stx_branch_config(tmp_path, "known")
        _add_stx_branch_config(tmp_path, "orphan")

        result = _cmd._find_orphaned_stx_branches(tmp_path, {"known": MagicMock()})

        assert result == {"orphan"}

    def test_returns_empty_set_when_no_stx_branch_config(self, tmp_path):
        _init_repo(tmp_path)

        assert _cmd._find_orphaned_stx_branches(tmp_path, {}) == set()

    def test_unmarked_branch_config_is_left_alone(self, tmp_path):
        # A `stx/<slug>` branch config entry `stx` didn't create itself —
        # e.g. a live box of a different tool sharing this repo. No `.stx`
        # marker means it's invisible to this scan, not just excluded.
        _init_repo(tmp_path)
        _add_stx_branch_config(tmp_path, "sbx-1", marked=False)

        assert _cmd._find_orphaned_stx_branches(tmp_path, {}) == set()

    def test_marked_and_unmarked_entries_coexist(self, tmp_path):
        _init_repo(tmp_path)
        _add_stx_branch_config(tmp_path, "orphan", marked=True)
        _add_stx_branch_config(tmp_path, "sbx-1", marked=False)

        assert _cmd._find_orphaned_stx_branches(tmp_path, {}) == {"orphan"}


class TestPruneOrphanedSandBranch:
    def test_deletes_branch_and_config_when_safe(self, tmp_path, capsys):
        _init_repo(tmp_path)
        subprocess.run(["git", "branch", "stx/orphan"], cwd=tmp_path, check=True)
        _add_stx_branch_config(tmp_path, "orphan")

        _cmd._prune_orphaned_stx_branch(tmp_path, "orphan", force=False)

        assert (
            subprocess.run(
                ["git", "show-ref", "--verify", "--quiet", "refs/heads/stx/orphan"],
                cwd=tmp_path,
            ).returncode
            != 0
        )
        remaining = subprocess.run(
            ["git", "config", "--get-regexp", r"^branch\.stx/orphan\."],
            cwd=tmp_path,
            capture_output=True,
        )
        assert remaining.returncode != 0
        assert "removed orphaned branch config" in capsys.readouterr().out

    def test_removes_config_only_when_branch_already_gone(self, tmp_path):
        _init_repo(tmp_path)
        _add_stx_branch_config(tmp_path, "orphan")

        _cmd._prune_orphaned_stx_branch(tmp_path, "orphan", force=False)

        remaining = subprocess.run(
            ["git", "config", "--get-regexp", r"^branch\.stx/orphan\."],
            cwd=tmp_path,
            capture_output=True,
        )
        assert remaining.returncode != 0

    def test_leaves_unmerged_branch_and_config_without_force(self, tmp_path, capsys):
        _init_repo(tmp_path)
        subprocess.run(
            ["git", "checkout", "-q", "-b", "stx/orphan"], cwd=tmp_path, check=True
        )
        (tmp_path / "g.txt").write_text("more")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
        subprocess.run(
            ["git", "commit", "-m", "unmerged work"], cwd=tmp_path, check=True
        )
        subprocess.run(["git", "checkout", "-q", "-"], cwd=tmp_path, check=True)
        _add_stx_branch_config(tmp_path, "orphan")

        _cmd._prune_orphaned_stx_branch(tmp_path, "orphan", force=False)

        assert (
            subprocess.run(
                ["git", "show-ref", "--verify", "--quiet", "refs/heads/stx/orphan"],
                cwd=tmp_path,
            ).returncode
            == 0
        )
        remaining = subprocess.run(
            ["git", "config", "--get-regexp", r"^branch\.stx/orphan\."],
            cwd=tmp_path,
            capture_output=True,
        )
        assert remaining.returncode == 0
        assert "unmerged commits" in capsys.readouterr().err


class TestFindOrphanedTmpBranchOwners:
    def test_finds_slug_not_in_registry(self, tmp_path):
        _init_repo(tmp_path)
        subprocess.run(["git", "branch", "tmp/orphan/task1"], cwd=tmp_path, check=True)

        assert _cmd._find_orphaned_tmp_branch_owners(tmp_path, {}) == {"orphan"}

    def test_excludes_slugs_already_registered(self, tmp_path):
        _init_repo(tmp_path)
        subprocess.run(["git", "branch", "tmp/known/task1"], cwd=tmp_path, check=True)
        subprocess.run(["git", "branch", "tmp/orphan/task1"], cwd=tmp_path, check=True)

        result = _cmd._find_orphaned_tmp_branch_owners(tmp_path, {"known": MagicMock()})

        assert result == {"orphan"}

    def test_multiple_branches_same_slug_dedupe_to_one_owner(self, tmp_path):
        _init_repo(tmp_path)
        subprocess.run(["git", "branch", "tmp/orphan/task1"], cwd=tmp_path, check=True)
        subprocess.run(["git", "branch", "tmp/orphan/task2"], cwd=tmp_path, check=True)

        assert _cmd._find_orphaned_tmp_branch_owners(tmp_path, {}) == {"orphan"}

    def test_empty_when_no_tmp_branches(self, tmp_path):
        _init_repo(tmp_path)

        assert _cmd._find_orphaned_tmp_branch_owners(tmp_path, {}) == set()


class TestPruneTmpBranches:
    def test_removes_a_fully_pushed_branch(self, tmp_path, capsys):
        remote = tmp_path / "remote.git"
        remote.mkdir()
        subprocess.run(["git", "init", "--bare"], cwd=remote, check=True)

        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        subprocess.run(
            ["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True
        )
        subprocess.run(
            ["git", "checkout", "-b", "tmp/orphan/task1"], cwd=repo, check=True
        )
        subprocess.run(
            ["git", "push", "origin", "HEAD:feat/other/target"],
            cwd=repo,
            capture_output=True,
            check=True,
        )
        subprocess.run(["git", "fetch", "origin"], cwd=repo, capture_output=True)
        subprocess.run(["git", "checkout", "-", "-q"], cwd=repo, check=True)

        _cmd._prune_tmp_branches(repo, "orphan", force=False)

        remaining = subprocess.run(
            ["git", "for-each-ref", "refs/heads/tmp/orphan/"],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        assert remaining.stdout.strip() == ""
        assert "removed cross-branch scratch branch" in capsys.readouterr().out

    def test_leaves_an_unpushed_branch_without_force(self, tmp_path, capsys):
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        subprocess.run(
            ["git", "checkout", "-b", "tmp/orphan/task1"], cwd=repo, check=True
        )
        subprocess.run(["git", "checkout", "-", "-q"], cwd=repo, check=True)

        _cmd._prune_tmp_branches(repo, "orphan", force=False)

        remaining = subprocess.run(
            ["git", "for-each-ref", "refs/heads/tmp/orphan/"],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        assert "tmp/orphan/task1" in remaining.stdout
        assert "not reachable from any remote" in capsys.readouterr().err

    def test_force_removes_an_unpushed_branch(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        subprocess.run(
            ["git", "checkout", "-b", "tmp/orphan/task1"], cwd=repo, check=True
        )
        subprocess.run(["git", "checkout", "-", "-q"], cwd=repo, check=True)

        _cmd._prune_tmp_branches(repo, "orphan", force=True)

        remaining = subprocess.run(
            ["git", "for-each-ref", "refs/heads/tmp/orphan/"],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        assert remaining.stdout.strip() == ""


class TestConfirmation:
    def _run(self, monkeypatch, *, yes: bool, isatty: bool, answer: str | None = None):
        monkeypatch.setattr(_cmd.secrets, "source_for", lambda cfg: MagicMock())
        pruned = []
        monkeypatch.setattr(_cmd.sys.stdin, "isatty", lambda: isatty)
        if answer is not None:
            monkeypatch.setattr("builtins.input", lambda prompt: answer)
        with (
            patch(f"{_MOD}.load_registry_with_unparsed", return_value=({}, {})),
            patch(f"{_MOD}.git_toplevel", return_value=MagicMock()),
            patch(
                f"{_MOD}._find_zombie_slugs",
                return_value={"orphan": _ZombieInfo(redis_index=None, repo_root=None)},
            ),
            patch(f"{_MOD}._find_orphaned_stx_branches", return_value=set()),
            patch(f"{_MOD}._find_orphaned_tmp_branch_owners", return_value=set()),
            patch(f"{_MOD}.PostgresAdminParams.build", return_value=MagicMock()),
            patch(f"{_MOD}.RedisAdminParams.build", return_value=MagicMock()),
            patch(
                f"{_MOD}._prune_one",
                side_effect=lambda slug, *a, **kw: pruned.append(slug),
            ),
        ):
            result = SandboxPruneCommand().execute(_args(yes=yes))

        return result, pruned

    def test_no_tty_and_no_yes_refuses(self, monkeypatch, capsys):
        result, pruned = self._run(monkeypatch, yes=False, isatty=False)

        assert result == 1
        assert pruned == []
        assert "--yes" in capsys.readouterr().err

    def test_yes_skips_the_question(self, monkeypatch):
        result, pruned = self._run(monkeypatch, yes=True, isatty=False)

        assert result is None
        assert pruned == ["orphan"]

    def test_interactive_confirm_prunes(self, monkeypatch):
        result, pruned = self._run(monkeypatch, yes=False, isatty=True, answer="y")

        assert result is None
        assert pruned == ["orphan"]

    def test_interactive_decline_prunes_nothing(self, monkeypatch, capsys):
        result, pruned = self._run(monkeypatch, yes=False, isatty=True, answer="n")

        assert result is None
        assert pruned == []
        assert "aborted" in capsys.readouterr().out


class TestExecute:
    def test_refuses_when_registry_cannot_be_loaded(self, monkeypatch, capsys):
        with patch(
            f"{_MOD}.load_registry_with_unparsed",
            side_effect=StxError("registry at /tmp/registry.json is corrupt"),
        ):
            result = SandboxPruneCommand().execute(_args())

        assert result == 1
        assert "registry" in capsys.readouterr().err

    def test_no_zombies_prints_message_and_returns(self, monkeypatch, capsys):
        monkeypatch.setattr(_cmd.secrets, "source_for", lambda cfg: MagicMock())
        with (
            patch(f"{_MOD}.load_registry_with_unparsed", return_value=({}, {})),
            patch(f"{_MOD}.git_toplevel", return_value=MagicMock()),
            patch(f"{_MOD}._find_zombie_slugs", return_value=set()),
            patch(f"{_MOD}._find_orphaned_stx_branches", return_value=set()),
            patch(f"{_MOD}._find_orphaned_tmp_branch_owners", return_value=set()),
        ):
            result = SandboxPruneCommand().execute(_args())

        assert result is None
        assert "no zombie sandboxes or orphaned branch config found" in (
            capsys.readouterr().out
        )

    def test_dry_run_lists_but_does_not_prune(self, monkeypatch, capsys):
        monkeypatch.setattr(_cmd.secrets, "source_for", lambda cfg: MagicMock())
        prune_calls = []
        with (
            patch(f"{_MOD}.load_registry_with_unparsed", return_value=({}, {})),
            patch(f"{_MOD}.git_toplevel", return_value=MagicMock()),
            patch(
                f"{_MOD}._find_zombie_slugs",
                return_value={"orphan": _ZombieInfo(redis_index=None, repo_root=None)},
            ),
            patch(f"{_MOD}._find_orphaned_stx_branches", return_value=set()),
            patch(f"{_MOD}._find_orphaned_tmp_branch_owners", return_value=set()),
            patch(
                f"{_MOD}._prune_one",
                side_effect=lambda *a, **kw: prune_calls.append(a),
            ),
        ):
            result = SandboxPruneCommand().execute(_args(dry_run=True))

        assert result is None
        assert prune_calls == []
        out = capsys.readouterr().out
        assert "orphan" in out
        assert "dry run" in out

    def test_dry_run_lists_orphaned_branch_config_too(self, monkeypatch, capsys):
        monkeypatch.setattr(_cmd.secrets, "source_for", lambda cfg: MagicMock())
        with (
            patch(f"{_MOD}.load_registry_with_unparsed", return_value=({}, {})),
            patch(f"{_MOD}.git_toplevel", return_value=MagicMock()),
            patch(f"{_MOD}._find_zombie_slugs", return_value=set()),
            patch(f"{_MOD}._find_orphaned_stx_branches", return_value={"stale-slug"}),
            patch(f"{_MOD}._find_orphaned_tmp_branch_owners", return_value=set()),
        ):
            result = SandboxPruneCommand().execute(_args(dry_run=True))

        assert result is None
        out = capsys.readouterr().out
        assert "stale-slug" in out
        assert "orphaned stx/* branch config" in out
        assert "dry run" in out

    def test_dry_run_lists_orphaned_tmp_owners_too(self, monkeypatch, capsys):
        monkeypatch.setattr(_cmd.secrets, "source_for", lambda cfg: MagicMock())
        with (
            patch(f"{_MOD}.load_registry_with_unparsed", return_value=({}, {})),
            patch(f"{_MOD}.git_toplevel", return_value=MagicMock()),
            patch(f"{_MOD}._find_zombie_slugs", return_value=set()),
            patch(f"{_MOD}._find_orphaned_stx_branches", return_value=set()),
            patch(
                f"{_MOD}._find_orphaned_tmp_branch_owners", return_value={"stale-slug"}
            ),
        ):
            result = SandboxPruneCommand().execute(_args(dry_run=True))

        assert result is None
        out = capsys.readouterr().out
        assert "stale-slug" in out
        assert "orphaned tmp/* scratch branches" in out
        assert "dry run" in out

    def test_required_secrets_cover_postgres_and_redis_only_when_zombies_found(
        self, monkeypatch
    ):
        captured = {}

        class _CapturingSource:
            def ensure_loaded(self, required, *, optional=()):
                captured["required"] = list(required)

        monkeypatch.setattr(_cmd.secrets, "source_for", lambda cfg: _CapturingSource())
        with (
            patch(f"{_MOD}.load_registry_with_unparsed", return_value=({}, {})),
            patch(f"{_MOD}.git_toplevel", return_value=MagicMock()),
            patch(
                f"{_MOD}._find_zombie_slugs",
                return_value={"orphan": _ZombieInfo(redis_index=None, repo_root=None)},
            ),
            patch(f"{_MOD}._find_orphaned_stx_branches", return_value=set()),
            patch(f"{_MOD}._find_orphaned_tmp_branch_owners", return_value=set()),
            patch(f"{_MOD}.PostgresAdminParams.build", return_value=MagicMock()),
            patch(f"{_MOD}.RedisAdminParams.build", return_value=MagicMock()),
            patch(f"{_MOD}._prune_one"),
        ):
            SandboxPruneCommand().execute(_args())

        assert captured["required"] == [
            "STX_POSTGRES_PASSWORD",
            "STX_REDIS_PASSWORD",
            "STX_REDIS_APP_PASSWORD",
        ]

    def test_no_zombies_loads_no_secrets_even_with_orphaned_branches(self, monkeypatch):
        source_for_calls = []
        monkeypatch.setattr(
            _cmd.secrets,
            "source_for",
            lambda cfg: source_for_calls.append(cfg) or MagicMock(),
        )
        with (
            patch(f"{_MOD}.load_registry_with_unparsed", return_value=({}, {})),
            patch(f"{_MOD}.git_toplevel", return_value=MagicMock()),
            patch(f"{_MOD}._find_zombie_slugs", return_value=set()),
            patch(f"{_MOD}._find_orphaned_stx_branches", return_value={"stale"}),
            patch(f"{_MOD}._find_orphaned_tmp_branch_owners", return_value=set()),
            patch(f"{_MOD}._prune_orphaned_stx_branch"),
        ):
            SandboxPruneCommand().execute(_args())

        assert source_for_calls == []

    def test_prunes_each_zombie_slug(self, monkeypatch):
        monkeypatch.setattr(_cmd.secrets, "source_for", lambda cfg: MagicMock())
        pruned = []
        with (
            patch(f"{_MOD}.load_registry_with_unparsed", return_value=({}, {})),
            patch(f"{_MOD}.git_toplevel", return_value=MagicMock()),
            patch(
                f"{_MOD}._find_zombie_slugs",
                return_value={
                    "a": _ZombieInfo(redis_index=None, repo_root=None),
                    "b": _ZombieInfo(redis_index=None, repo_root=None),
                },
            ),
            patch(f"{_MOD}._find_orphaned_stx_branches", return_value=set()),
            patch(f"{_MOD}._find_orphaned_tmp_branch_owners", return_value=set()),
            patch(f"{_MOD}.PostgresAdminParams.build", return_value=MagicMock()),
            patch(f"{_MOD}.RedisAdminParams.build", return_value=MagicMock()),
            patch(
                f"{_MOD}._prune_one",
                side_effect=lambda slug, *a, **kw: pruned.append(slug),
            ),
        ):
            result = SandboxPruneCommand().execute(_args())

        assert result is None
        assert pruned == ["a", "b"]

    def test_prunes_each_stuck_provisioning_slug(self, monkeypatch):
        monkeypatch.setattr(_cmd.secrets, "source_for", lambda cfg: MagicMock())
        cleaned = []
        stuck = {
            "a": _sandbox_record("a", state=SandboxState.PROVISIONING),
            "b": _sandbox_record("b", state=SandboxState.PROVISIONING),
        }
        with (
            patch(f"{_MOD}.load_registry_with_unparsed", return_value=({}, {})),
            patch(f"{_MOD}.git_toplevel", return_value=MagicMock()),
            patch(f"{_MOD}._find_zombie_slugs", return_value=set()),
            patch(f"{_MOD}._find_stuck_provisioning", return_value=stuck),
            patch(f"{_MOD}._find_orphaned_stx_branches", return_value=set()),
            patch(f"{_MOD}._find_orphaned_tmp_branch_owners", return_value=set()),
            patch(
                f"{_MOD}._prune_stuck",
                side_effect=lambda record, *a, **kw: cleaned.append(record.slug),
            ),
        ):
            result = SandboxPruneCommand().execute(_args())

        assert result is None
        assert cleaned == ["a", "b"]

    def test_stuck_provisioning_cleanup_failure_warns_but_does_not_abort(
        self, monkeypatch, capsys
    ):
        # teardown_sandbox leaves containers that couldn't be removed; the
        # stuck entry is kept for a retry, with a warning, and prune returns 0.
        monkeypatch.setattr(_cmd.secrets, "source_for", lambda cfg: MagicMock())
        unclean = MagicMock()
        unclean.clean = False
        unclean.failures = []
        with (
            patch(f"{_MOD}.load_registry_with_unparsed", return_value=({}, {})),
            patch(f"{_MOD}.git_toplevel", return_value=MagicMock()),
            patch(f"{_MOD}._find_zombie_slugs", return_value=set()),
            patch(
                f"{_MOD}._find_stuck_provisioning",
                return_value={
                    "a": _sandbox_record("a", state=SandboxState.PROVISIONING)
                },
            ),
            patch(f"{_MOD}._find_orphaned_stx_branches", return_value=set()),
            patch(f"{_MOD}._find_orphaned_tmp_branch_owners", return_value=set()),
            patch(f"{_MOD}.teardown_sandbox", return_value=unclean),
        ):
            result = SandboxPruneCommand().execute(_args())

        assert result is None
        assert "could not fully clean up 'a'" in capsys.readouterr().err

    def test_prunes_each_orphaned_branch_slug(self, monkeypatch):
        monkeypatch.setattr(_cmd.secrets, "source_for", lambda cfg: MagicMock())
        pruned = []
        with (
            patch(f"{_MOD}.load_registry_with_unparsed", return_value=({}, {})),
            patch(f"{_MOD}.git_toplevel", return_value=MagicMock()),
            patch(f"{_MOD}._find_zombie_slugs", return_value=set()),
            patch(f"{_MOD}._find_orphaned_stx_branches", return_value={"a", "b"}),
            patch(f"{_MOD}._find_orphaned_tmp_branch_owners", return_value=set()),
            patch(
                f"{_MOD}._prune_orphaned_stx_branch",
                side_effect=lambda root, slug, **kw: pruned.append(slug),
            ),
        ):
            result = SandboxPruneCommand().execute(_args())

        assert result is None
        assert pruned == ["a", "b"]

    def test_prunes_each_orphaned_tmp_owner(self, monkeypatch):
        monkeypatch.setattr(_cmd.secrets, "source_for", lambda cfg: MagicMock())
        pruned = []
        with (
            patch(f"{_MOD}.load_registry_with_unparsed", return_value=({}, {})),
            patch(f"{_MOD}.git_toplevel", return_value=MagicMock()),
            patch(f"{_MOD}._find_zombie_slugs", return_value=set()),
            patch(f"{_MOD}._find_orphaned_stx_branches", return_value=set()),
            patch(f"{_MOD}._find_orphaned_tmp_branch_owners", return_value={"a", "b"}),
            patch(
                f"{_MOD}._prune_tmp_branches",
                side_effect=lambda root, slug, **kw: pruned.append(slug),
            ),
        ):
            result = SandboxPruneCommand().execute(_args())

        assert result is None
        assert pruned == ["a", "b"]

    def test_missing_db_credentials_skips_db_cleanup_but_still_prunes(
        self, monkeypatch, capsys
    ):
        monkeypatch.setattr(_cmd.secrets, "source_for", lambda cfg: MagicMock())
        pruned = []
        with (
            patch(f"{_MOD}.load_registry_with_unparsed", return_value=({}, {})),
            patch(f"{_MOD}.git_toplevel", return_value=MagicMock()),
            patch(
                f"{_MOD}._find_zombie_slugs",
                return_value={"orphan": _ZombieInfo(redis_index=None, repo_root=None)},
            ),
            patch(f"{_MOD}._find_orphaned_stx_branches", return_value=set()),
            patch(f"{_MOD}._find_orphaned_tmp_branch_owners", return_value=set()),
            patch(
                f"{_MOD}.PostgresAdminParams.build",
                side_effect=_cmd.secrets.SecretsError(["STX_POSTGRES_PASSWORD"]),
            ),
            patch(f"{_MOD}.RedisAdminParams.build", return_value=MagicMock()),
            patch(
                f"{_MOD}._prune_one",
                side_effect=lambda slug, *a, **kw: pruned.append(slug),
            ),
        ):
            result = SandboxPruneCommand().execute(_args())

        assert result is None
        assert pruned == ["orphan"]
        assert "skipping DB/Redis cleanup" in capsys.readouterr().err


class TestExecuteUnparseableEntries:
    def test_dry_run_lists_the_entry_and_its_parse_error(self, monkeypatch, capsys):
        monkeypatch.setattr(_cmd.secrets, "source_for", lambda cfg: MagicMock())
        with (
            patch(
                f"{_MOD}.load_registry_with_unparsed",
                return_value=({}, {"stale": ({"slug": "stale"}, "'databases'")}),
            ),
            patch(f"{_MOD}.git_toplevel", return_value=MagicMock()),
            patch(f"{_MOD}._find_zombie_slugs", return_value={}),
            patch(f"{_MOD}._find_orphaned_stx_branches", return_value=set()),
            patch(f"{_MOD}._find_orphaned_tmp_branch_owners", return_value=set()),
        ):
            result = SandboxPruneCommand().execute(_args(dry_run=True))

        assert result is None
        out = capsys.readouterr().out
        assert "unparseable registry entr" in out
        assert "stale" in out
        assert "'databases'" in out

    def test_no_zombies_message_suppressed_when_an_unparseable_entry_exists(
        self, monkeypatch, capsys
    ):
        monkeypatch.setattr(_cmd.secrets, "source_for", lambda cfg: MagicMock())
        with (
            patch(
                f"{_MOD}.load_registry_with_unparsed",
                return_value=({}, {"stale": ({"slug": "stale"}, "boom")}),
            ),
            patch(f"{_MOD}.git_toplevel", return_value=MagicMock()),
            patch(f"{_MOD}._find_zombie_slugs", return_value={}),
            patch(f"{_MOD}._find_orphaned_stx_branches", return_value=set()),
            patch(f"{_MOD}._find_orphaned_tmp_branch_owners", return_value=set()),
        ):
            result = SandboxPruneCommand().execute(_args(dry_run=True))

        assert result is None
        assert "no zombie sandboxes" not in capsys.readouterr().out

    def test_prunes_and_removes_each_unparseable_entry(self, monkeypatch, capsys):
        monkeypatch.setattr(_cmd.secrets, "source_for", lambda cfg: MagicMock())
        pruned = []
        removed = []

        class _FakeLockedRegistry:
            def remove(self, slug):
                removed.append(slug)

        @contextmanager
        def _fake_locked_registry():
            yield _FakeLockedRegistry()

        raw = {"slug": "stale", "redis_index": 2, "repo_root": "/repo/stale"}
        with (
            patch(
                f"{_MOD}.load_registry_with_unparsed",
                return_value=({}, {"stale": (raw, "'databases'")}),
            ),
            patch(f"{_MOD}.git_toplevel", return_value=MagicMock()),
            patch(f"{_MOD}._find_zombie_slugs", return_value={}),
            patch(f"{_MOD}._find_orphaned_stx_branches", return_value=set()),
            patch(f"{_MOD}._find_orphaned_tmp_branch_owners", return_value=set()),
            patch(f"{_MOD}.PostgresAdminParams.build", return_value=MagicMock()),
            patch(f"{_MOD}.RedisAdminParams.build", return_value=MagicMock()),
            patch(f"{_MOD}.locked_registry", _fake_locked_registry),
            patch(
                f"{_MOD}._prune_one",
                side_effect=lambda slug, *a, **kw: pruned.append(slug),
            ),
        ):
            result = SandboxPruneCommand().execute(_args())

        assert result is None
        assert pruned == ["stale"]
        assert removed == ["stale"]
        assert "removed unparseable registry entry for 'stale'" in (
            capsys.readouterr().out
        )

    def test_prune_one_receives_info_derived_from_the_raw_json(self, monkeypatch):
        monkeypatch.setattr(_cmd.secrets, "source_for", lambda cfg: MagicMock())
        captured = []

        @contextmanager
        def _fake_locked_registry():
            yield MagicMock()

        raw = {
            "slug": "stale",
            "redis_index": 2,
            "repo_root": "/repo/stale",
            "databases": ["stx_stale"],
        }
        with (
            patch(
                f"{_MOD}.load_registry_with_unparsed",
                return_value=({}, {"stale": (raw, "boom")}),
            ),
            patch(f"{_MOD}.git_toplevel", return_value=MagicMock()),
            patch(f"{_MOD}._find_zombie_slugs", return_value={}),
            patch(f"{_MOD}._find_orphaned_stx_branches", return_value=set()),
            patch(f"{_MOD}._find_orphaned_tmp_branch_owners", return_value=set()),
            patch(f"{_MOD}.PostgresAdminParams.build", return_value=MagicMock()),
            patch(f"{_MOD}.RedisAdminParams.build", return_value=MagicMock()),
            patch(f"{_MOD}.locked_registry", _fake_locked_registry),
            patch(
                f"{_MOD}._prune_one",
                side_effect=lambda slug, info, *a, **kw: captured.append(info),
            ),
        ):
            SandboxPruneCommand().execute(_args())

        assert captured == [
            _ZombieInfo(
                redis_index=2, repo_root=Path("/repo/stale"), databases=("stx_stale",)
            )
        ]

    def test_secrets_loaded_when_only_an_unparseable_entry_is_found(self, monkeypatch):
        captured = {}

        class _CapturingSource:
            def ensure_loaded(self, required, *, optional=()):
                captured["required"] = list(required)

        monkeypatch.setattr(_cmd.secrets, "source_for", lambda cfg: _CapturingSource())

        @contextmanager
        def _fake_locked_registry():
            yield MagicMock()

        with (
            patch(
                f"{_MOD}.load_registry_with_unparsed",
                return_value=({}, {"stale": ({"slug": "stale"}, "boom")}),
            ),
            patch(f"{_MOD}.git_toplevel", return_value=MagicMock()),
            patch(f"{_MOD}._find_zombie_slugs", return_value={}),
            patch(f"{_MOD}._find_orphaned_stx_branches", return_value=set()),
            patch(f"{_MOD}._find_orphaned_tmp_branch_owners", return_value=set()),
            patch(f"{_MOD}.PostgresAdminParams.build", return_value=MagicMock()),
            patch(f"{_MOD}.RedisAdminParams.build", return_value=MagicMock()),
            patch(f"{_MOD}.locked_registry", _fake_locked_registry),
            patch(f"{_MOD}._prune_one"),
        ):
            SandboxPruneCommand().execute(_args())

        assert captured["required"] == [
            "STX_POSTGRES_PASSWORD",
            "STX_REDIS_PASSWORD",
            "STX_REDIS_APP_PASSWORD",
        ]

    def test_find_zombie_slugs_receives_unparsed_slugs_as_already_known(
        self, monkeypatch
    ):
        # An unparseable entry is handled by its own dedicated path — it
        # must never also be rediscovered as a plain container zombie.
        monkeypatch.setattr(_cmd.secrets, "source_for", lambda cfg: MagicMock())
        captured_known = {}

        def _fake_find_zombie_slugs(known):
            captured_known.update(known)
            return {}

        @contextmanager
        def _fake_locked_registry():
            yield MagicMock()

        with (
            patch(
                f"{_MOD}.load_registry_with_unparsed",
                return_value=(
                    {"ready": MagicMock()},
                    {"stale": ({"slug": "stale"}, "boom")},
                ),
            ),
            patch(f"{_MOD}.git_toplevel", return_value=MagicMock()),
            patch(f"{_MOD}._find_zombie_slugs", side_effect=_fake_find_zombie_slugs),
            patch(f"{_MOD}._find_orphaned_stx_branches", return_value=set()),
            patch(f"{_MOD}._find_orphaned_tmp_branch_owners", return_value=set()),
            patch(f"{_MOD}.PostgresAdminParams.build", return_value=MagicMock()),
            patch(f"{_MOD}.RedisAdminParams.build", return_value=MagicMock()),
            patch(f"{_MOD}.locked_registry", _fake_locked_registry),
            patch(f"{_MOD}._prune_one"),
        ):
            SandboxPruneCommand().execute(_args())

        assert set(captured_known) == {"ready", "stale"}


def _result(*, clean: bool = True, failures=None):
    container = ContainerTeardown(
        down_ok=clean,
        used_label_fallback=False,
        remaining=[] if clean else ["stx-orphan--app"],
    )
    result = TeardownResult(container=container)
    if failures:
        result.failures = failures
    return result


class TestPruneOne:
    """`_prune_one` wires a zombie's labels into the shared teardown engine
    (mechanics covered in tests/lifecycle/test_teardown_sandbox.py) and
    reports the result. These cover the wiring, not the teardown itself."""

    def _run(self, zombie, *, monkeypatch, tmp_path, force=False, result=None):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        monkeypatch.setattr(_cmd, "worktree_path", lambda slug: tmp_path / "wt")
        captured: dict = {}

        def _td(record, *, policy, root, wt_path, databases, db_params, redis_params):
            captured.update(
                record=record,
                policy=policy,
                root=root,
                wt_path=wt_path,
                databases=databases,
                db_params=db_params,
                redis_params=redis_params,
            )
            return result if result is not None else _result()

        monkeypatch.setattr(_cmd, "teardown_sandbox", _td)
        _cmd._prune_one("orphan", zombie, MagicMock(), MagicMock(), force=force)
        return captured

    def test_passes_the_zombies_redis_index_repo_root_and_databases(
        self, monkeypatch, tmp_path
    ):
        cap = self._run(
            _ZombieInfo(
                redis_index=7, repo_root=tmp_path / "repo", databases=("stx_orphan",)
            ),
            monkeypatch=monkeypatch,
            tmp_path=tmp_path,
        )
        assert cap["record"].redis_index == 7
        assert cap["databases"] == ("stx_orphan",)
        assert cap["root"] == tmp_path / "repo"
        assert cap["wt_path"] == tmp_path / "wt"

    def test_unknown_databases_pass_through_as_none(self, monkeypatch, tmp_path):
        cap = self._run(
            _ZombieInfo(redis_index=None, repo_root=tmp_path, databases=None),
            monkeypatch=monkeypatch,
            tmp_path=tmp_path,
        )
        assert cap["databases"] is None

    def test_uses_a_guarding_best_effort_policy(self, monkeypatch, tmp_path):
        cap = self._run(
            _ZombieInfo(redis_index=None, repo_root=tmp_path, databases=()),
            monkeypatch=monkeypatch,
            tmp_path=tmp_path,
        )
        # Prune keeps the overlay when the down fails and guards unsaved work,
        # unlike rm which vets up front and drops the overlay on --force.
        assert cap["policy"].guard_unsaved
        assert cap["policy"].continue_when_unclean
        assert not cap["policy"].drop_overlay_when_unclean

    def test_force_flows_into_the_policy(self, monkeypatch, tmp_path):
        cap = self._run(
            _ZombieInfo(redis_index=None, repo_root=tmp_path, databases=()),
            monkeypatch=monkeypatch,
            tmp_path=tmp_path,
            force=True,
        )
        assert cap["policy"].force

    def test_falls_back_to_cwd_toplevel_when_no_repo_root(self, monkeypatch, tmp_path):
        monkeypatch.setattr(_cmd, "git_toplevel", lambda p: tmp_path / "top")
        cap = self._run(
            _ZombieInfo(redis_index=None, repo_root=None, databases=()),
            monkeypatch=monkeypatch,
            tmp_path=tmp_path,
        )
        assert cap["root"] == tmp_path / "top"

    def test_prints_pruned(self, monkeypatch, tmp_path, capsys):
        self._run(
            _ZombieInfo(redis_index=None, repo_root=tmp_path, databases=()),
            monkeypatch=monkeypatch,
            tmp_path=tmp_path,
        )
        assert "pruned 'orphan'" in capsys.readouterr().out

    def test_reports_teardown_failures_as_warnings(self, monkeypatch, tmp_path, capsys):
        self._run(
            _ZombieInfo(redis_index=None, repo_root=tmp_path, databases=("x",)),
            monkeypatch=monkeypatch,
            tmp_path=tmp_path,
            result=_result(failures=[("databases", "could not drop database(s) x")]),
        )
        assert "could not drop database(s) x" in capsys.readouterr().err


def _live_record(slug, *, redis_index=None, databases=()):
    return SandboxRecord(
        slug=slug,
        created="2026-06-26T00:00:00+00:00",
        state=SandboxState.READY,
        redis_index=redis_index,
        tailnet_port=None,
        databases=databases,
        boxes={},
    )


class TestPruneOneReclaimGuard:
    """`rm --force` drops a registry entry while its containers survive, freeing
    its Redis index and database names for a brand-new sandbox to claim; the
    survivors then read as a zombie whose labels still name those now-reused
    resources. Prune must not flush/drop what a live entry now holds."""

    def _run(self, zombie, live, *, monkeypatch, tmp_path):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        monkeypatch.setattr(_cmd, "worktree_path", lambda slug: tmp_path / "wt")
        captured: dict = {}

        def _td(record, *, policy, root, wt_path, databases, db_params, redis_params):
            captured.update(record=record, databases=databases)
            return _result()

        monkeypatch.setattr(_cmd, "teardown_sandbox", _td)

        @contextmanager
        def _locked():
            reg = MagicMock()
            reg.all.return_value = live
            yield reg

        monkeypatch.setattr(_cmd, "locked_registry", _locked)
        _cmd._prune_one("orphan", zombie, MagicMock(), MagicMock(), force=False)
        return captured

    def test_withholds_a_redis_index_a_live_entry_now_holds(
        self, monkeypatch, tmp_path, capsys
    ):
        cap = self._run(
            _ZombieInfo(redis_index=7, repo_root=tmp_path, databases=()),
            {"b": _live_record("b", redis_index=7)},
            monkeypatch=monkeypatch,
            tmp_path=tmp_path,
        )
        assert cap["record"].redis_index is None
        out = capsys.readouterr().out
        assert "redis index 7" in out
        assert "live sandbox" in out

    def test_flushes_an_index_no_live_entry_holds(self, monkeypatch, tmp_path, capsys):
        cap = self._run(
            _ZombieInfo(redis_index=7, repo_root=tmp_path, databases=()),
            {"b": _live_record("b", redis_index=4)},
            monkeypatch=monkeypatch,
            tmp_path=tmp_path,
        )
        assert cap["record"].redis_index == 7
        assert "live sandbox" not in capsys.readouterr().out

    def test_withholds_database_names_a_live_entry_now_holds(
        self, monkeypatch, tmp_path, capsys
    ):
        cap = self._run(
            _ZombieInfo(
                redis_index=None,
                repo_root=tmp_path,
                databases=("stx_b", "stx_b_test"),
            ),
            {"b": _live_record("b", databases=("stx_b", "stx_b_test"))},
            monkeypatch=monkeypatch,
            tmp_path=tmp_path,
        )
        assert cap["databases"] == ()
        out = capsys.readouterr().out
        assert "stx_b" in out
        assert "live sandbox" in out

    def test_drops_only_the_databases_no_live_entry_holds(self, monkeypatch, tmp_path):
        cap = self._run(
            _ZombieInfo(
                redis_index=None,
                repo_root=tmp_path,
                databases=("stx_b", "stx_orphan"),
            ),
            {"b": _live_record("b", databases=("stx_b",))},
            monkeypatch=monkeypatch,
            tmp_path=tmp_path,
        )
        assert cap["databases"] == ("stx_orphan",)


class TestPruneStuck:
    def _run(self, record, *, monkeypatch, tmp_path, result, force=False):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        monkeypatch.setattr(_cmd, "worktree_path", lambda slug: tmp_path / "wt")
        removed = []

        @contextmanager
        def _locked():
            reg = MagicMock()
            reg.remove = lambda slug: removed.append(slug)
            yield reg

        monkeypatch.setattr(_cmd, "locked_registry", _locked)
        monkeypatch.setattr(_cmd, "teardown_sandbox", lambda *a, **kw: result)
        _cmd._prune_stuck(record, MagicMock(), MagicMock(), force=force)
        return removed

    def test_removes_the_registry_entry_when_teardown_is_clean(
        self, monkeypatch, tmp_path, capsys
    ):
        removed = self._run(
            _sandbox_record("a", state=SandboxState.PROVISIONING),
            monkeypatch=monkeypatch,
            tmp_path=tmp_path,
            result=_result(),
        )
        assert removed == ["a"]
        assert "cleaned up 'a'" in capsys.readouterr().out

    def test_keeps_the_entry_when_teardown_is_not_clean(
        self, monkeypatch, tmp_path, capsys
    ):
        removed = self._run(
            _sandbox_record("a", state=SandboxState.PROVISIONING),
            monkeypatch=monkeypatch,
            tmp_path=tmp_path,
            result=_result(clean=False),
        )
        assert removed == []
        assert "could not fully clean up 'a'" in capsys.readouterr().err


class TestSecretsUnavailableStillRunsStuckAndBranchSweeps:
    def test_ensure_loaded_failure_does_not_abort_the_rest(self, monkeypatch, capsys):
        # Bug: a SecretsError from ensure_loaded aborted the whole prune after
        # the user confirmed. Now it warns and the stuck-entry and branch
        # sweeps still run.
        monkeypatch.setattr(
            _cmd.secrets,
            "source_for",
            lambda cfg: MagicMock(
                ensure_loaded=MagicMock(
                    side_effect=_cmd.secrets.SecretsError(["STX_POSTGRES_PASSWORD"])
                )
            ),
        )
        stuck_cleaned = []
        branches_swept = []
        with (
            patch(f"{_MOD}.load_registry_with_unparsed", return_value=({}, {})),
            patch(f"{_MOD}.git_toplevel", return_value=MagicMock()),
            patch(
                f"{_MOD}._find_zombie_slugs",
                return_value={"z": _ZombieInfo(redis_index=None, repo_root=None)},
            ),
            patch(
                f"{_MOD}._find_stuck_provisioning",
                return_value={
                    "a": _sandbox_record("a", state=SandboxState.PROVISIONING)
                },
            ),
            patch(f"{_MOD}._find_orphaned_stx_branches", return_value={"b"}),
            patch(f"{_MOD}._find_orphaned_tmp_branch_owners", return_value=set()),
            patch(f"{_MOD}._prune_one"),
            patch(
                f"{_MOD}._prune_stuck",
                side_effect=lambda record, *a, **kw: stuck_cleaned.append(record.slug),
            ),
            patch(
                f"{_MOD}._prune_orphaned_stx_branch",
                side_effect=lambda root, slug, **kw: branches_swept.append(slug),
            ),
        ):
            result = SandboxPruneCommand().execute(_args())

        assert result is None
        assert stuck_cleaned == ["a"]
        assert branches_swept == ["b"]
        err = capsys.readouterr().err
        assert "skipping DB/Redis cleanup" in err
