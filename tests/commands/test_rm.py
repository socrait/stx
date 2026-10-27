import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from stx.commands.rm import SandboxRmCommand
from stx.core import (
    BoxRecord,
    LinkRecord,
    SandboxRecord,
    SandboxState,
    compose_overlay_path,
    db_name,
    drop_dir,
    drop_dmz_dir,
    logs_root,
)
from stx.core.registry import (
    ForgeBoxRecord,
    ForgePackageRecord,
    ForgePushRecord,
    ForgeSourceRecord,
)
from stx.forge import ForgeCredentialKind
from stx.lifecycle import ContainerTeardown
from stx.manifest import (
    ForgeManifest,
    Manifest,
    PlatformManifest,
    PostgresManifest,
)

_MOD = "stx.commands.rm"
# The mechanical teardown (containers, DB, Redis, hosts, worktree, dirs) now
# lives in the shared engine; rm only resolves secrets/forge and runs its
# guards. Patch the engine's own seams for those steps.
_TD = "stx.lifecycle.teardown"


@pytest.fixture(autouse=True)
def _stub_sandbox_network(monkeypatch):
    """Network teardown hits real `docker network ...` otherwise —
    irrelevant to the DB/Redis/hosts/branch behavior these tests assert."""
    monkeypatch.setattr(f"{_MOD}.stx_network.disconnect", lambda *a, **k: None)
    monkeypatch.setattr(f"{_MOD}.stx_network.remove_network", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _stub_bring_down_containers(monkeypatch):
    """Container teardown shells out to `docker` (absent here). Stub it to a
    clean result by default — the seam `rm` tears down through — so tests
    assert the DB/Redis/forge/worktree behavior around it without a docker
    daemon. A test that cares about an unclean teardown re-patches this."""
    monkeypatch.setattr(
        f"{_TD}.bring_down_containers",
        lambda slug, **k: ContainerTeardown(
            down_ok=True, used_label_fallback=False, remaining=[]
        ),
    )


def _test_manifest(*, forge: bool = False) -> Manifest:
    return Manifest(
        schema=1,
        name="app",
        compose="compose.yaml",
        image="widgetshop-devbox",
        user="devuser",
        platform=PlatformManifest(
            postgres=PostgresManifest(databases=("{db}", "{db}_test")), redis=True
        ),
        forge=(
            ForgeManifest(kind="gitlab", project="widgetshop/widgetshop")
            if forge
            else None
        ),
    )


def _make_record(
    slug: str,
    root: Path,
    *,
    redis_index: int | None = 1,
    databases: tuple[str, ...] | None = None,
) -> SandboxRecord:
    # worktree path that doesn't exist on disk — skips all git/compose subprocess calls
    if databases is None:
        databases = (db_name(slug), f"{db_name(slug)}_test")
    return SandboxRecord(
        slug=slug,
        created="2026-06-26T00:00:00+00:00",
        state=SandboxState.READY,
        redis_index=redis_index,
        tailnet_port=5099 if redis_index else None,
        repo="widgetshop",
        repo_root=str(root),
        databases=databases,
        boxes={
            "app": BoxRecord(
                zone="trusted",
                user="devuser",
                branch=f"feat/alex/{slug}",
                worktree="/nonexistent/wt/my-slug",
            )
        },
    )


def _make_dmz_only_record(slug: str) -> SandboxRecord:
    return SandboxRecord(
        slug=slug,
        created="2026-06-26T00:00:00+00:00",
        state=SandboxState.READY,
        redis_index=None,
        tailnet_port=None,
        boxes={"dmz": BoxRecord(zone="dmz", user="devuser")},
    )


def _make_args(
    slug: str, *, keep_db: bool = False, rm_branch: bool = False, force: bool = False
):
    args = MagicMock()
    args.slug = slug
    args.keep_db = keep_db
    args.rm_branch = rm_branch
    args.force = force
    return args


def test_refuses_when_in_sandbox_box(monkeypatch, capsys):
    monkeypatch.setenv("STX_ZONE", "trusted")
    with patch(
        f"{_MOD}.secrets.source_for",
        side_effect=AssertionError("should not run"),
    ):
        result = SandboxRmCommand().execute(_make_args("foo"))

    assert result == 1
    assert "host-only" in capsys.readouterr().err


class TestUnparseableEntryRefusal:
    def test_refuses_and_points_at_prune(self, monkeypatch, capsys):
        with patch(
            f"{_MOD}.load_registry_with_unparsed",
            return_value=({}, {"stale": ({"slug": "stale"}, "'databases'")}),
        ):
            result = SandboxRmCommand().execute(_make_args("stale"))

        assert result == 1
        err = capsys.readouterr().err
        assert "stx prune" in err
        assert "'databases'" in err

    def test_missing_slug_not_in_unparsed_gets_the_plain_refusal(
        self, monkeypatch, capsys
    ):
        with patch(
            f"{_MOD}.load_registry_with_unparsed",
            return_value=({}, {}),
        ):
            result = SandboxRmCommand().execute(_make_args("missing"))

        assert result == 1
        assert "no sandbox named" in capsys.readouterr().err


class _CapturingSource:
    def __init__(self):
        self.required: list[str] | None = None

    def ensure_loaded(self, required, *, optional=()):
        self.required = list(required)


class TestRequiredSecrets:
    def test_matches_services_and_forge_presence(self, monkeypatch, tmp_path):
        slug = "my-slug"
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        record = _make_record(slug, tmp_path)
        record.services = ["postgres", "redis"]
        record.boxes["app"].forge = ForgeBoxRecord(
            kind="gitlab",
            push=ForgePushRecord(
                scope_id=1,
                token_id=2,
                scope="write_repository",
                expires_at="2026-07-09",
            ),
        )
        source = _CapturingSource()

        with (
            patch(f"{_MOD}.secrets.source_for", return_value=source),
            patch(
                f"{_MOD}.load_registry_with_unparsed", return_value=({slug: record}, {})
            ),
            patch(f"{_MOD}.PostgresAdminParams.build", return_value=MagicMock()),
            patch(f"{_MOD}.RedisAdminParams.build", return_value=MagicMock()),
            patch(f"{_TD}.drop_databases"),
            patch(f"{_TD}.flush_redis"),
            patch(f"{_TD}.remove_hosts_entry"),
        ):
            SandboxRmCommand().execute(_make_args(slug))

        assert source.required == [
            "STX_POSTGRES_PASSWORD",
            "STX_REDIS_PASSWORD",
            "STX_REDIS_APP_PASSWORD",
            "STX_GITLAB_MINTER_TOKEN",
        ]

    def test_bare_services_and_no_forge_needs_nothing(self, monkeypatch, tmp_path):
        slug = "my-slug"
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        record = _make_record(slug, tmp_path)
        assert record.services == []
        assert record.boxes["app"].forge is None
        source = _CapturingSource()

        with (
            patch(f"{_MOD}.secrets.source_for", return_value=source),
            patch(
                f"{_MOD}.load_registry_with_unparsed", return_value=({slug: record}, {})
            ),
            patch(f"{_MOD}.PostgresAdminParams.build", return_value=MagicMock()),
            patch(f"{_MOD}.RedisAdminParams.build", return_value=MagicMock()),
            patch(f"{_TD}.drop_databases"),
            patch(f"{_TD}.flush_redis"),
            patch(f"{_TD}.remove_hosts_entry"),
        ):
            SandboxRmCommand().execute(_make_args(slug))

        # No services and no forge means nothing to load — secrets.source_for
        # is still constructed (cheap) but ensure_loaded is never called.
        assert source.required is None


class TestKeepDb:
    def _run(self, slug: str, monkeypatch, tmp_path, *, keep_db: bool = False):
        """Run SandboxRmCommand.execute and return (dropped_slugs, flushed_indexes)."""
        dropped = []
        flushed = []

        # STX_STATE_ROOT isolates drop/drop-dmz/code-dmz/route paths so this
        # test doesn't touch the real ~/.local/state/stx/.
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        (tmp_path / "sandbox.yaml").touch()

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(
                f"{_MOD}.load_registry_with_unparsed",
                return_value=({slug: _make_record(slug, tmp_path)}, {}),
            ),
            patch(f"{_MOD}.PostgresAdminParams.build", return_value=MagicMock()),
            patch(f"{_MOD}.RedisAdminParams.build", return_value=MagicMock()),
            patch(
                f"{_TD}.drop_databases",
                side_effect=lambda dbs, p: dropped.extend(dbs),
            ),
            patch(f"{_TD}.flush_redis", side_effect=lambda idx, p: flushed.append(idx)),
            patch(f"{_TD}.remove_hosts_entry"),
        ):
            SandboxRmCommand().execute(_make_args(slug, keep_db=keep_db))

        return dropped, flushed

    def test_drops_databases_by_default(self, monkeypatch, tmp_path):
        dropped, _ = self._run("my-slug", monkeypatch, tmp_path)
        assert dropped == ["stx_my_slug", "stx_my_slug_test"]

    def test_keep_db_skips_drop(self, monkeypatch, tmp_path):
        dropped, _ = self._run("my-slug", monkeypatch, tmp_path, keep_db=True)
        assert dropped == []

    def test_keep_db_still_flushes_redis(self, monkeypatch, tmp_path):
        _, flushed = self._run("my-slug", monkeypatch, tmp_path, keep_db=True)
        assert flushed == [1]


class TestDbRedisCleanupFailureKeepsRegistryEntry:
    def _run(
        self,
        slug: str,
        monkeypatch,
        tmp_path,
        *,
        drop_fails: bool = False,
        flush_fails: bool = False,
    ):
        from stx.errors import StxError

        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        (tmp_path / "sandbox.yaml").touch()

        drop_patch = (
            patch(
                f"{_TD}.drop_databases",
                side_effect=StxError("could not drop database(s): connection refused"),
            )
            if drop_fails
            else patch(f"{_TD}.drop_databases")
        )
        flush_patch = (
            patch(
                f"{_TD}.flush_redis",
                side_effect=StxError("could not flush redis: connection refused"),
            )
            if flush_fails
            else patch(f"{_TD}.flush_redis")
        )

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(
                f"{_MOD}.load_registry_with_unparsed",
                return_value=({slug: _make_record(slug, tmp_path)}, {}),
            ),
            patch(f"{_MOD}.PostgresAdminParams.build", return_value=MagicMock()),
            patch(f"{_MOD}.RedisAdminParams.build", return_value=MagicMock()),
            drop_patch,
            flush_patch,
            patch(f"{_TD}.remove_hosts_entry"),
        ):
            result = SandboxRmCommand().execute(_make_args(slug))

        return result

    def test_drop_failure_returns_1_and_keeps_registry_entry(
        self, monkeypatch, tmp_path, capsys
    ):
        result = self._run("my-slug", monkeypatch, tmp_path, drop_fails=True)

        assert result == 1
        err = capsys.readouterr().err
        assert "kept in the registry" in err
        assert "retry" in err

    def test_flush_failure_returns_1_and_keeps_registry_entry(
        self, monkeypatch, tmp_path, capsys
    ):
        result = self._run("my-slug", monkeypatch, tmp_path, flush_fails=True)

        assert result == 1
        err = capsys.readouterr().err
        assert "kept in the registry" in err

    def test_registry_entry_actually_survives_on_disk(self, monkeypatch, tmp_path):
        from stx.core import SandboxRecord, save_registry
        from stx.core.registry import load_registry
        from stx.errors import StxError

        slug = "my-slug"
        state_root = tmp_path / "state"
        monkeypatch.setenv("STX_STATE_ROOT", str(state_root))
        (tmp_path / "sandbox.yaml").touch()
        record = _make_record(slug, tmp_path)
        save_registry({slug: record})

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(
                f"{_MOD}.load_registry_with_unparsed", return_value=({slug: record}, {})
            ),
            patch(f"{_MOD}.PostgresAdminParams.build", return_value=MagicMock()),
            patch(f"{_MOD}.RedisAdminParams.build", return_value=MagicMock()),
            patch(
                f"{_TD}.drop_databases",
                side_effect=StxError("connection refused"),
            ),
            patch(f"{_TD}.flush_redis"),
            patch(f"{_TD}.remove_hosts_entry"),
        ):
            result = SandboxRmCommand().execute(_make_args(slug))

        assert result == 1
        assert slug in load_registry()
        assert isinstance(load_registry()[slug], SandboxRecord)


class TestDbSecretsUnavailableSkip:
    """Postgres admin secrets missing means the drop is never attempted at
    all (unlike TestDbRedisCleanupFailureKeepsRegistryEntry, where it's
    attempted and fails) — a skip, not a failure, so --force is what lets
    it through rather than being unconditional."""

    def _run(self, slug: str, monkeypatch, tmp_path, *, force: bool = False):
        from stx.secrets import SecretsError

        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        (tmp_path / "sandbox.yaml").touch()

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(
                f"{_MOD}.load_registry_with_unparsed",
                return_value=(
                    {slug: _make_record(slug, tmp_path, redis_index=None)},
                    {},
                ),
            ),
            patch(
                f"{_MOD}.PostgresAdminParams.build",
                side_effect=SecretsError(["STX_POSTGRES_PASSWORD"]),
            ),
            patch(f"{_TD}.drop_databases"),
            patch(f"{_TD}.remove_hosts_entry"),
        ):
            result = SandboxRmCommand().execute(_make_args(slug, force=force))

        return result

    def test_refuses_without_force(self, monkeypatch, tmp_path, capsys):
        result = self._run("my-slug", monkeypatch, tmp_path)

        assert result == 1
        err = capsys.readouterr().err
        assert "left behind: database(s) stx_my_slug, stx_my_slug_test" in err

    def test_force_proceeds_anyway(self, monkeypatch, tmp_path, capsys):
        result = self._run("my-slug", monkeypatch, tmp_path, force=True)

        assert result is None
        assert "despite the skipped cleanup" in capsys.readouterr().err

    def test_preflight_aborts_before_touching_anything(self, monkeypatch, tmp_path):
        """When cleanup can't finish and --force wasn't given, rm must abort in
        preflight — before bringing containers down or removing the overlay —
        never half-remove the sandbox."""
        from stx.secrets import SecretsError

        slug = "my-slug"
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        overlay_path = compose_overlay_path(slug)
        overlay_path.parent.mkdir(parents=True, exist_ok=True)
        overlay_path.write_text("{}")
        down_calls = []

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(
                f"{_MOD}.load_registry_with_unparsed",
                return_value=(
                    {slug: _make_record(slug, tmp_path, redis_index=None)},
                    {},
                ),
            ),
            patch(
                f"{_MOD}.PostgresAdminParams.build",
                side_effect=SecretsError(["STX_POSTGRES_PASSWORD"]),
            ),
            patch(
                f"{_TD}.bring_down_containers",
                side_effect=lambda s, **k: down_calls.append(s),
            ),
            patch(f"{_TD}.drop_databases"),
            patch(f"{_TD}.remove_hosts_entry"),
        ):
            result = SandboxRmCommand().execute(_make_args(slug))

        assert result == 1
        assert down_calls == []  # containers never brought down
        assert overlay_path.exists()  # overlay never removed


def test_removes_logs_dir(monkeypatch, tmp_path):
    slug = "my-slug"
    state_root = tmp_path / "state"
    monkeypatch.setenv("STX_STATE_ROOT", str(state_root))

    logs_path = logs_root(slug)
    (logs_path / "app").mkdir(parents=True)
    (logs_path / "app" / "1.jsonl").write_text("{}\n")

    with (
        patch(f"{_MOD}.secrets.source_for"),
        patch(
            f"{_MOD}.load_registry_with_unparsed",
            return_value=({slug: _make_record(slug, tmp_path)}, {}),
        ),
        patch(f"{_MOD}.PostgresAdminParams.build", return_value=MagicMock()),
        patch(f"{_MOD}.RedisAdminParams.build", return_value=MagicMock()),
        patch(f"{_TD}.drop_databases"),
        patch(f"{_TD}.flush_redis"),
        patch(f"{_TD}.remove_hosts_entry"),
    ):
        SandboxRmCommand().execute(_make_args(slug))

    assert not logs_path.exists()


class TestHandoffDirGuard:
    def _run(self, slug: str, monkeypatch, tmp_path, *, force: bool = False):
        """Run SandboxRmCommand.execute and return (result, down_calls) — where
        down_calls records whether container teardown ran at all (the handoff
        guard, when it blocks, does so before teardown is ever reached)."""
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))

        down_calls = []

        def _down(s, **k):
            down_calls.append(s)
            return ContainerTeardown(
                down_ok=True, used_label_fallback=False, remaining=[]
            )

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(
                f"{_MOD}.load_registry_with_unparsed",
                return_value=({slug: _make_record(slug, tmp_path)}, {}),
            ),
            patch(f"{_TD}.bring_down_containers", side_effect=_down),
            patch(f"{_MOD}.PostgresAdminParams.build", return_value=MagicMock()),
            patch(f"{_MOD}.RedisAdminParams.build", return_value=MagicMock()),
            patch(f"{_TD}.drop_databases"),
            patch(f"{_TD}.flush_redis"),
            patch(f"{_TD}.remove_hosts_entry"),
        ):
            result = SandboxRmCommand().execute(_make_args(slug, force=force))

        return result, down_calls

    def test_blocks_without_force_when_drop_dir_has_files(
        self, monkeypatch, tmp_path, capsys
    ):
        slug = "my-slug"
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        d = drop_dir(slug)
        d.mkdir(parents=True)
        (d / "plan.md").write_text("# plan\n")

        result, down_calls = self._run(slug, monkeypatch, tmp_path)

        assert result == 1
        assert down_calls == []
        assert d.exists()
        assert "error" in capsys.readouterr().err

    def test_blocks_without_force_when_drop_dmz_dir_has_files(
        self, monkeypatch, tmp_path, capsys
    ):
        slug = "my-slug"
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        d = drop_dmz_dir(slug)
        d.mkdir(parents=True)
        (d / "findings.md").write_text("# findings\n")

        result, down_calls = self._run(slug, monkeypatch, tmp_path)

        assert result == 1
        assert down_calls == []
        assert d.exists()

    def test_does_not_block_with_force(self, monkeypatch, tmp_path):
        slug = "my-slug"
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        d = drop_dir(slug)
        d.mkdir(parents=True)
        (d / "plan.md").write_text("# plan\n")

        result, down_calls = self._run(slug, monkeypatch, tmp_path, force=True)

        assert result is None
        assert down_calls
        assert not d.exists()

    def test_empty_drop_dirs_do_not_block(self, monkeypatch, tmp_path):
        slug = "my-slug"
        result, down_calls = self._run(slug, monkeypatch, tmp_path)

        assert result is None
        assert down_calls


def test_dmz_only_skips_db_and_redis_cleanup(monkeypatch, tmp_path):
    slug = "triage"
    monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))

    db_calls = []
    with (
        patch(f"{_MOD}.secrets.source_for"),
        patch(
            f"{_MOD}.load_registry_with_unparsed",
            return_value=({slug: _make_dmz_only_record(slug)}, {}),
        ),
        patch(
            f"{_TD}.drop_databases",
            side_effect=lambda dbs, p: db_calls.extend(dbs),
        ),
    ):
        result = SandboxRmCommand().execute(_make_args(slug))

    assert result is None
    # A dmz-only sandbox has no databases or redis index to clean up.
    assert db_calls == []


def test_box_without_databases_or_redis_needs_no_admin_secrets(
    monkeypatch, tmp_path, capsys
):
    slug = "bare"
    monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.delenv("STX_POSTGRES_PASSWORD", raising=False)
    monkeypatch.delenv("STX_REDIS_PASSWORD", raising=False)

    with (
        patch(f"{_MOD}.secrets.source_for"),
        patch(
            f"{_MOD}.load_registry_with_unparsed",
            return_value=(
                {slug: _make_record(slug, tmp_path, redis_index=None, databases=())},
                {},
            ),
        ),
        patch(f"{_TD}.remove_hosts_entry"),
    ):
        result = SandboxRmCommand().execute(_make_args(slug))

    assert result is None
    assert "skipping DB/Redis cleanup" not in capsys.readouterr().err


class TestMissingRepoRoot:
    def test_removes_worktree_directory_without_git(self, monkeypatch, tmp_path):
        slug = "orphan"
        wt_path = tmp_path / "wt"
        wt_path.mkdir()
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))

        record = SandboxRecord(
            slug=slug,
            created="2026-06-26T00:00:00+00:00",
            state=SandboxState.READY,
            redis_index=None,
            tailnet_port=None,
            boxes={
                "app": BoxRecord(
                    zone="trusted",
                    user="devuser",
                    branch=f"feat/alex/{slug}",
                    worktree=str(wt_path),
                )
            },
        )

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(
                f"{_MOD}.load_registry_with_unparsed", return_value=({slug: record}, {})
            ),
            patch(f"{_MOD}.PostgresAdminParams.build", return_value=MagicMock()),
            patch(f"{_MOD}.RedisAdminParams.build", return_value=MagicMock()),
            patch(f"{_TD}.drop_databases"),
            patch(f"{_TD}.remove_hosts_entry"),
        ):
            result = SandboxRmCommand().execute(_make_args(slug, force=True))

        assert result is None
        assert not wt_path.exists()

    def test_warns_instead_of_crashing(self, monkeypatch, tmp_path, capsys):
        slug = "orphan"
        wt_path = tmp_path / "wt"
        wt_path.mkdir()
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))

        record = SandboxRecord(
            slug=slug,
            created="2026-06-26T00:00:00+00:00",
            state=SandboxState.READY,
            redis_index=None,
            tailnet_port=None,
            boxes={
                "app": BoxRecord(
                    zone="trusted",
                    user="devuser",
                    branch=f"feat/alex/{slug}",
                    worktree=str(wt_path),
                )
            },
        )

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(
                f"{_MOD}.load_registry_with_unparsed", return_value=({slug: record}, {})
            ),
            patch(f"{_MOD}.PostgresAdminParams.build", return_value=MagicMock()),
            patch(f"{_MOD}.RedisAdminParams.build", return_value=MagicMock()),
            patch(f"{_TD}.drop_databases"),
            patch(f"{_TD}.remove_hosts_entry"),
        ):
            result = SandboxRmCommand().execute(_make_args(slug, force=True))

        assert result is None
        assert "no repo checkout" in capsys.readouterr().err


class TestOverlayAndAllowlistCleanup:
    def test_removes_overlay_and_allowlist_files(self, monkeypatch, tmp_path):
        from stx.core import allowlist_path

        slug = "my-slug"
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        overlay_path = compose_overlay_path(slug)
        overlay_path.parent.mkdir(parents=True, exist_ok=True)
        overlay_path.write_text('{"services": {}}')
        allow_path = allowlist_path(slug)
        allow_path.parent.mkdir(parents=True, exist_ok=True)
        allow_path.write_text("example.com\n")

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(
                f"{_MOD}.load_registry_with_unparsed",
                return_value=({slug: _make_record(slug, tmp_path)}, {}),
            ),
            patch(f"{_MOD}.PostgresAdminParams.build", return_value=MagicMock()),
            patch(f"{_MOD}.RedisAdminParams.build", return_value=MagicMock()),
            patch(f"{_TD}.drop_databases"),
            patch(f"{_TD}.flush_redis"),
            patch(f"{_TD}.remove_hosts_entry"),
        ):
            result = SandboxRmCommand().execute(_make_args(slug))

        assert result is None
        assert not overlay_path.exists()
        assert not allow_path.exists()

    def test_failed_container_teardown_keeps_entry_and_overlay(
        self, monkeypatch, tmp_path
    ):
        """A container that can't be removed must abort `rm` with the entry and
        overlay intact — never delete the overlay and strand a zombie no later
        command would find. --force is the only override."""
        from stx.core import allowlist_path, save_registry

        slug = "my-slug"
        state_root = tmp_path / "state"
        monkeypatch.setenv("STX_STATE_ROOT", str(state_root))
        overlay_path = compose_overlay_path(slug)
        overlay_path.parent.mkdir(parents=True, exist_ok=True)
        overlay_path.write_text('{"services": {}}')
        allow_path = allowlist_path(slug)
        allow_path.parent.mkdir(parents=True, exist_ok=True)
        allow_path.write_text("example.com\n")
        record = _make_record(slug, tmp_path)
        save_registry({slug: record})

        unclean = ContainerTeardown(
            down_ok=False, used_label_fallback=True, remaining=["stx-my-slug--app"]
        )
        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(
                f"{_MOD}.load_registry_with_unparsed", return_value=({slug: record}, {})
            ),
            patch(f"{_TD}.bring_down_containers", return_value=unclean),
            patch(f"{_MOD}.PostgresAdminParams.build", return_value=MagicMock()),
            patch(f"{_MOD}.RedisAdminParams.build", return_value=MagicMock()),
            patch(f"{_TD}.drop_databases"),
            patch(f"{_TD}.flush_redis"),
            patch(f"{_TD}.remove_hosts_entry"),
        ):
            result = SandboxRmCommand().execute(_make_args(slug))

        from stx.core.registry import load_registry

        assert result == 1
        assert overlay_path.exists()  # overlay kept — the stack still exists
        assert slug in load_registry()  # entry kept so a retry can find it

    def test_force_proceeds_past_an_unclean_container_teardown(
        self, monkeypatch, tmp_path
    ):
        from stx.core import save_registry

        slug = "my-slug"
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        overlay_path = compose_overlay_path(slug)
        overlay_path.parent.mkdir(parents=True, exist_ok=True)
        overlay_path.write_text('{"services": {}}')
        record = _make_record(slug, tmp_path)
        save_registry({slug: record})

        unclean = ContainerTeardown(
            down_ok=False, used_label_fallback=True, remaining=["stx-my-slug--app"]
        )
        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(
                f"{_MOD}.load_registry_with_unparsed", return_value=({slug: record}, {})
            ),
            patch(f"{_TD}.bring_down_containers", return_value=unclean),
            patch(f"{_MOD}.PostgresAdminParams.build", return_value=MagicMock()),
            patch(f"{_MOD}.RedisAdminParams.build", return_value=MagicMock()),
            patch(f"{_TD}.drop_databases"),
            patch(f"{_TD}.flush_redis"),
            patch(f"{_TD}.remove_hosts_entry"),
        ):
            result = SandboxRmCommand().execute(_make_args(slug, force=True))

        from stx.core.registry import load_registry

        assert result is None
        assert not overlay_path.exists()
        assert slug not in load_registry()

    def test_teardown_needs_no_manifest_so_a_broken_sandbox_yaml_cannot_block_it(
        self, monkeypatch, tmp_path
    ):
        """Teardown addresses the stack by compose project name, never by
        loading the checkout's sandbox.yaml — a broken manifest can no longer
        block `rm`."""
        slug = "my-slug"
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        # A sandbox.yaml that would raise if anyone tried to load it.
        (tmp_path / "sandbox.yaml").write_text("}{ not valid yaml")

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(
                f"{_MOD}.load_registry_with_unparsed",
                return_value=({slug: _make_record(slug, tmp_path)}, {}),
            ),
            # Loading the manifest would raise — rm must never reach for it.
            patch(
                "stx.manifest.Manifest.load",
                side_effect=AssertionError("rm must not load the manifest"),
            ),
            patch(f"{_MOD}.PostgresAdminParams.build", return_value=MagicMock()),
            patch(f"{_MOD}.RedisAdminParams.build", return_value=MagicMock()),
            patch(f"{_TD}.drop_databases"),
            patch(f"{_TD}.flush_redis"),
            patch(f"{_TD}.remove_hosts_entry"),
        ):
            result = SandboxRmCommand().execute(_make_args(slug))

        assert result is None


class TestLiveOwnerGuard:
    def _provisioning_record(self, slug: str, tmp_path: Path) -> SandboxRecord:
        rec = _make_record(slug, tmp_path, redis_index=None, databases=())
        rec.state = SandboxState.PROVISIONING
        return rec

    def test_refuses_a_provisioning_entry_whose_owner_is_alive(
        self, monkeypatch, tmp_path, capsys
    ):
        slug = "in-flight"
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        record = self._provisioning_record(slug, tmp_path)

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(
                f"{_MOD}.load_registry_with_unparsed", return_value=({slug: record}, {})
            ),
            patch(f"{_MOD}.owner_alive", return_value=True),
        ):
            result = SandboxRmCommand().execute(_make_args(slug))

        assert result == 1
        assert "still being provisioned" in capsys.readouterr().err

    def test_force_tears_down_a_live_provisioning_entry(self, monkeypatch, tmp_path):
        slug = "in-flight"
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        record = self._provisioning_record(slug, tmp_path)

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(
                f"{_MOD}.load_registry_with_unparsed", return_value=({slug: record}, {})
            ),
            patch(f"{_MOD}.owner_alive", return_value=True),
            patch(f"{_TD}.remove_hosts_entry"),
        ):
            result = SandboxRmCommand().execute(_make_args(slug, force=True))

        assert result is None

    def test_a_dead_owner_is_not_blocked(self, monkeypatch, tmp_path):
        slug = "crashed"
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        record = self._provisioning_record(slug, tmp_path)

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(
                f"{_MOD}.load_registry_with_unparsed", return_value=({slug: record}, {})
            ),
            patch(f"{_MOD}.owner_alive", return_value=False),
            patch(f"{_TD}.remove_hosts_entry"),
        ):
            result = SandboxRmCommand().execute(_make_args(slug))

        assert result is None


class TestStuckProvisioning:
    def _stuck_record(
        self, slug: str, root: Path, *, redis_index=7, databases=()
    ) -> SandboxRecord:
        return SandboxRecord(
            slug=slug,
            created="2026-06-26T00:00:00+00:00",
            state=SandboxState.PROVISIONING,
            redis_index=redis_index,
            tailnet_port=None,
            services=["postgres", "redis"],
            repo="widgetshop",
            repo_root=str(root),
            databases=databases,
            boxes={},
        )

    def test_drops_the_databases_recorded_at_reserve_time(self, monkeypatch, tmp_path):
        """A crash between reserve() and finish() leaves no BoxRecord and
        possibly no loadable manifest — but reserve() now records the
        rendered database names on the registry entry itself, so `rm` drops
        the right ones regardless of the checkout's current state."""
        slug = "my-slug"
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        dropped = []
        flushed = []

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(
                f"{_MOD}.load_registry_with_unparsed",
                return_value=(
                    {
                        slug: self._stuck_record(
                            slug,
                            tmp_path,
                            databases=("stx_my_slug_main", "stx_my_slug_queue"),
                        )
                    },
                    {},
                ),
            ),
            patch(f"{_MOD}.PostgresAdminParams.build", return_value=MagicMock()),
            patch(f"{_MOD}.RedisAdminParams.build", return_value=MagicMock()),
            patch(
                f"{_TD}.drop_databases",
                side_effect=lambda dbs, p: dropped.extend(dbs),
            ),
            patch(f"{_TD}.flush_redis", side_effect=lambda idx, p: flushed.append(idx)),
            patch(f"{_TD}.remove_hosts_entry"),
        ):
            result = SandboxRmCommand().execute(_make_args(slug))

        assert result is None
        assert dropped == ["stx_my_slug_main", "stx_my_slug_queue"]
        assert flushed == [7]

    def test_no_databases_recorded_drops_nothing_and_never_guesses(
        self, monkeypatch, tmp_path, capsys
    ):
        """No `platform.postgres` (or --repos none) means no databases were
        ever reserved — `rm` must drop nothing, and never fall back to
        guessing the conventional {db}/{db}_test pair."""
        slug = "my-slug"
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        dropped = []
        flushed = []

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(
                f"{_MOD}.load_registry_with_unparsed",
                return_value=({slug: self._stuck_record(slug, tmp_path)}, {}),
            ),
            patch(f"{_MOD}.RedisAdminParams.build", return_value=MagicMock()),
            patch(
                f"{_TD}.drop_databases",
                side_effect=lambda dbs, p: dropped.extend(dbs),
            ),
            patch(f"{_TD}.flush_redis", side_effect=lambda idx, p: flushed.append(idx)),
            patch(f"{_TD}.remove_hosts_entry"),
        ):
            result = SandboxRmCommand().execute(_make_args(slug))

        assert result is None
        assert dropped == []
        assert flushed == [7]
        assert "unknown" not in capsys.readouterr().err

    def test_refuses_an_existing_worktree_without_force(self, monkeypatch, tmp_path):
        from stx.core import worktree_path

        slug = "my-slug"
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        (tmp_path / "sandbox.yaml").touch()
        worktree_path(slug).mkdir(parents=True)

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(
                f"{_MOD}.load_registry_with_unparsed",
                return_value=({slug: self._stuck_record(slug, tmp_path)}, {}),
            ),
        ):
            result = SandboxRmCommand().execute(_make_args(slug))

        assert result == 1

    def test_revokes_the_pending_forge_tokens_new_recorded_mid_provisioning(
        self, monkeypatch, tmp_path
    ):
        """`new` records the tokens it mints on `pending_forge` the moment it
        mints them, so even a sandbox that crashed before `finish` (no
        BoxRecord) can have those tokens revoked rather than left to expire."""
        slug = "my-slug"
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        record = self._stuck_record(slug, tmp_path)
        record.pending_forge = ForgeBoxRecord(
            kind="gitlab",
            push=ForgePushRecord(
                scope_id=42,
                token_id=1,
                scope="write_repository",
                expires_at="2026-07-09",
            ),
        )
        record.pending_forge_source = ForgeSourceRecord(
            kind="gitlab",
            host="gitlab.example.com",
            project="widgetshop/widgetshop",
        )
        revoke_calls = []

        class _FakeForge:
            def revoke(self, kind, scope_id, token_id):
                revoke_calls.append((kind, scope_id, token_id))
                return True

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(
                f"{_MOD}.load_registry_with_unparsed",
                return_value=({slug: record}, {}),
            ),
            patch(f"{_MOD}.forge_for_source", return_value=_FakeForge()),
            patch(f"{_MOD}.PostgresAdminParams.build", return_value=MagicMock()),
            patch(f"{_MOD}.RedisAdminParams.build", return_value=MagicMock()),
            patch(f"{_TD}.drop_databases"),
            patch(f"{_TD}.flush_redis"),
            patch(f"{_TD}.remove_hosts_entry"),
        ):
            result = SandboxRmCommand().execute(_make_args(slug))

        assert result is None
        assert (ForgeCredentialKind.PUSH, 42, 1) in revoke_calls


def test_redis_flush_gate_keeps_registry_entry_when_secrets_unavailable(
    monkeypatch, tmp_path
):
    from stx.core import SandboxRecord, save_registry
    from stx.core.registry import load_registry as real_load_registry
    from stx.secrets import SecretsError

    slug = "my-slug"
    monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
    (tmp_path / "sandbox.yaml").touch()
    record = _make_record(slug, tmp_path)
    save_registry({slug: record})

    with (
        patch(f"{_MOD}.secrets.source_for"),
        patch(f"{_MOD}.load_registry_with_unparsed", return_value=({slug: record}, {})),
        patch(
            f"{_MOD}.PostgresAdminParams.build",
            side_effect=SecretsError(["STX_POSTGRES_PASSWORD"]),
        ),
        patch(f"{_TD}.drop_databases"),
        patch(f"{_TD}.flush_redis"),
        patch(f"{_TD}.remove_hosts_entry"),
    ):
        result = SandboxRmCommand().execute(_make_args(slug))

    assert result == 1
    assert slug in real_load_registry()
    assert isinstance(real_load_registry()[slug], SandboxRecord)


class TestRevokesTokens:
    def _record_with_tokens(self, slug: str, root: Path) -> SandboxRecord:
        record = _make_record(slug, root)
        record.boxes["app"].forge = ForgeBoxRecord(
            kind="gitlab",
            push=ForgePushRecord(
                scope_id=66319962,
                token_id=111,
                scope="write_repository",
                expires_at="2026-07-09",
            ),
            package=ForgePackageRecord(
                scope_id=70226642, token_id=222, expires_at="2026-07-09"
            ),
        )
        record.boxes["app"].forge_source = ForgeSourceRecord(
            kind="gitlab", host="gitlab.com", project="widgetshop/widgetshop"
        )
        return record

    def test_revokes_both_git_and_package_registry_tokens(self, monkeypatch, tmp_path):
        slug = "my-slug"
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        (tmp_path / "sandbox.yaml").touch()
        revoke_calls = []

        class _FakeForge:
            def revoke(self, kind, scope_id, token_id):
                revoke_calls.append((kind, scope_id, token_id))
                return True

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(
                f"{_MOD}.load_registry_with_unparsed",
                return_value=({slug: self._record_with_tokens(slug, tmp_path)}, {}),
            ),
            patch(f"{_MOD}.forge_for_source", return_value=_FakeForge()),
            patch(f"{_MOD}.PostgresAdminParams.build", return_value=MagicMock()),
            patch(f"{_MOD}.RedisAdminParams.build", return_value=MagicMock()),
            patch(f"{_TD}.drop_databases"),
            patch(f"{_TD}.flush_redis"),
            patch(f"{_TD}.remove_hosts_entry"),
        ):
            SandboxRmCommand().execute(_make_args(slug))

        assert revoke_calls == [
            (ForgeCredentialKind.PUSH, 66319962, 111),
            (ForgeCredentialKind.PACKAGE, 70226642, 222),
        ]

    def test_revoke_failure_keeps_the_entry_unless_force(
        self, monkeypatch, tmp_path, capsys
    ):
        """A revoke that fails leaves the token live on the forge — rm must
        keep the entry (so a retry can revoke it) rather than drop it and lose
        the id. --force accepts leaving it to expire."""
        from stx.core import save_registry
        from stx.core.registry import load_registry

        slug = "my-slug"
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))

        class _FailingForge:
            def revoke(self, kind, scope_id, token_id):
                return False  # the forge call failed

        def _run(*, force):
            record = self._record_with_tokens(slug, tmp_path)
            save_registry({slug: record})
            with (
                patch(f"{_MOD}.secrets.source_for"),
                patch(
                    f"{_MOD}.load_registry_with_unparsed",
                    return_value=({slug: record}, {}),
                ),
                patch(f"{_MOD}.forge_for_source", return_value=_FailingForge()),
                patch(f"{_MOD}.PostgresAdminParams.build", return_value=MagicMock()),
                patch(f"{_MOD}.RedisAdminParams.build", return_value=MagicMock()),
                patch(f"{_TD}.drop_databases"),
                patch(f"{_TD}.flush_redis"),
                patch(f"{_TD}.remove_hosts_entry"),
            ):
                return SandboxRmCommand().execute(_make_args(slug, force=force))

        assert _run(force=False) == 1
        assert slug in load_registry()  # kept for a retry

        assert _run(force=True) is None
        assert slug not in load_registry()  # removed, tokens left to expire

    def test_revokes_tokens_even_when_the_manifest_cannot_load(
        self, monkeypatch, tmp_path
    ):
        """No `sandbox.yaml` on this checkout at all (branch switched, or the
        repo gone) — revocation must not depend on it; `forge_source` on the
        registry's own BoxRecord is enough."""
        slug = "my-slug"
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        revoke_calls = []

        class _FakeForge:
            def revoke(self, kind, scope_id, token_id):
                revoke_calls.append((kind, scope_id, token_id))
                return True

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(
                f"{_MOD}.load_registry_with_unparsed",
                return_value=({slug: self._record_with_tokens(slug, tmp_path)}, {}),
            ),
            patch(f"{_MOD}.forge_for_source", return_value=_FakeForge()),
            patch(f"{_MOD}.PostgresAdminParams.build", return_value=MagicMock()),
            patch(f"{_MOD}.RedisAdminParams.build", return_value=MagicMock()),
            patch(f"{_TD}.drop_databases"),
            patch(f"{_TD}.flush_redis"),
            patch(f"{_TD}.remove_hosts_entry"),
        ):
            SandboxRmCommand().execute(_make_args(slug))

        assert revoke_calls == [
            (ForgeCredentialKind.PUSH, 66319962, 111),
            (ForgeCredentialKind.PACKAGE, 70226642, 222),
        ]

    def test_no_forge_source_recorded_refuses_without_force(
        self, monkeypatch, tmp_path, capsys
    ):
        """An entry from before `forge_source` was recorded (or any other
        gap) can't rebuild a `Forge` client to revoke with — warn, keep the
        registry entry, and exit non-zero, same as any other skipped
        cleanup step; --force is what lets it through (see the paired test
        below)."""
        slug = "my-slug"
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        record = self._record_with_tokens(slug, tmp_path)
        record.boxes["app"].forge_source = None

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(
                f"{_MOD}.load_registry_with_unparsed", return_value=({slug: record}, {})
            ),
            patch(f"{_MOD}.PostgresAdminParams.build", return_value=MagicMock()),
            patch(f"{_MOD}.RedisAdminParams.build", return_value=MagicMock()),
            patch(f"{_TD}.drop_databases"),
            patch(f"{_TD}.flush_redis"),
            patch(f"{_TD}.remove_hosts_entry"),
        ):
            result = SandboxRmCommand().execute(_make_args(slug))

        assert result == 1
        err = capsys.readouterr().err
        assert "not revoked" in err
        # Preflight catches this before any teardown — entry kept intact.
        assert "kept in the registry" in err

    def test_no_forge_source_recorded_force_proceeds_anyway(
        self, monkeypatch, tmp_path, capsys
    ):
        slug = "my-slug"
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        record = self._record_with_tokens(slug, tmp_path)
        record.boxes["app"].forge_source = None

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(
                f"{_MOD}.load_registry_with_unparsed", return_value=({slug: record}, {})
            ),
            patch(f"{_MOD}.PostgresAdminParams.build", return_value=MagicMock()),
            patch(f"{_MOD}.RedisAdminParams.build", return_value=MagicMock()),
            patch(f"{_TD}.drop_databases"),
            patch(f"{_TD}.flush_redis"),
            patch(f"{_TD}.remove_hosts_entry"),
        ):
            result = SandboxRmCommand().execute(_make_args(slug, force=True))

        assert result is None
        err = capsys.readouterr().err
        assert "not revoked" in err
        assert "despite the skipped cleanup" in err


class TestUnpushedCommitsGuard:
    def _run(
        self,
        slug: str,
        monkeypatch,
        tmp_path,
        *,
        force: bool = False,
        unpushed: list[str] | None = None,
    ):
        wt_path = tmp_path / "wt"
        wt_path.mkdir()
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))

        record = _make_record(slug, tmp_path)
        record.boxes["app"].worktree = str(wt_path)

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(
                f"{_MOD}.load_registry_with_unparsed", return_value=({slug: record}, {})
            ),
            patch(f"{_MOD}.PostgresAdminParams.build", return_value=MagicMock()),
            patch(f"{_MOD}.RedisAdminParams.build", return_value=MagicMock()),
            patch(f"{_TD}.drop_databases"),
            patch(f"{_TD}.flush_redis"),
            patch(f"{_TD}.remove_hosts_entry"),
            patch(f"{_MOD}.has_uncommitted_changes", return_value=False),
            patch(f"{_MOD}.unpushed_commit_shas", return_value=unpushed or []),
            patch(
                f"{_MOD}.subprocess.run",
                return_value=MagicMock(returncode=0, stdout="", stderr=""),
            ),
        ):
            result = SandboxRmCommand().execute(_make_args(slug, force=force))

        return result

    def test_blocks_without_force_when_unpushed_commits_exist(
        self, monkeypatch, tmp_path, capsys
    ):
        result = self._run("my-slug", monkeypatch, tmp_path, unpushed=["abc1234"])
        assert result == 1
        assert "unpushed commits" in capsys.readouterr().err

    def test_force_warns_instead_of_blocking(self, monkeypatch, tmp_path, capsys):
        result = self._run(
            "my-slug",
            monkeypatch,
            tmp_path,
            force=True,
            unpushed=["abc1234", "def5678"],
        )
        assert result is None
        err = capsys.readouterr().err
        assert "warning" in err
        assert "2 unpushed commit(s)" in err

    def test_force_no_warning_when_nothing_unpushed(
        self, monkeypatch, tmp_path, capsys
    ):
        result = self._run("my-slug", monkeypatch, tmp_path, force=True, unpushed=[])
        assert result is None
        assert "unpushed" not in capsys.readouterr().err

    def test_no_unpushed_commits_does_not_block(self, monkeypatch, tmp_path):
        result = self._run("my-slug", monkeypatch, tmp_path, unpushed=[])
        assert result is None


class TestAtRiskCommitsListing:
    def _run(
        self,
        slug: str,
        monkeypatch,
        tmp_path,
        *,
        force: bool = False,
        unpushed: list[str],
        describe_result=None,
        describe_error: Exception | None = None,
    ):
        wt_path = tmp_path / "wt"
        wt_path.mkdir()
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))

        record = _make_record(slug, tmp_path)
        record.boxes["app"].worktree = str(wt_path)

        describe_patch = (
            patch(f"{_MOD}.describe_commits", side_effect=describe_error)
            if describe_error
            else patch(f"{_MOD}.describe_commits", return_value=describe_result or [])
        )

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(
                f"{_MOD}.load_registry_with_unparsed", return_value=({slug: record}, {})
            ),
            patch(f"{_MOD}.PostgresAdminParams.build", return_value=MagicMock()),
            patch(f"{_MOD}.RedisAdminParams.build", return_value=MagicMock()),
            patch(f"{_TD}.drop_databases"),
            patch(f"{_TD}.flush_redis"),
            patch(f"{_TD}.remove_hosts_entry"),
            patch(f"{_MOD}.has_uncommitted_changes", return_value=False),
            patch(f"{_MOD}.unpushed_commit_shas", return_value=unpushed),
            describe_patch,
            patch(
                f"{_MOD}.subprocess.run",
                return_value=MagicMock(returncode=0, stdout="", stderr=""),
            ),
        ):
            result = SandboxRmCommand().execute(_make_args(slug, force=force))

        return result

    def test_lists_sha_and_subject_up_to_five_then_a_count(
        self, monkeypatch, tmp_path, capsys
    ):
        shas = [f"sha{i}" for i in range(7)]
        described = [f"sha{i} commit number {i}" for i in range(5)]

        result = self._run(
            "my-slug",
            monkeypatch,
            tmp_path,
            unpushed=shas,
            describe_result=described,
        )

        assert result == 1
        err = capsys.readouterr().err
        for line in described:
            assert line in err
        assert "...and 2 more" in err
        assert "sha5" not in err
        assert "sha6" not in err

    def test_falls_back_to_bare_shas_when_describe_fails(
        self, monkeypatch, tmp_path, capsys
    ):
        from stx.errors import StxError

        result = self._run(
            "my-slug",
            monkeypatch,
            tmp_path,
            unpushed=["abc1234"],
            describe_error=StxError("could not describe commits: boom"),
        )

        assert result == 1
        assert "abc1234" in capsys.readouterr().err


class TestUnknownWorktreeStateTreatedAsDirty:
    def _run(
        self,
        slug: str,
        monkeypatch,
        tmp_path,
        *,
        force: bool = False,
        fail_uncommitted_check: bool = False,
        fail_unpushed_check: bool = False,
    ):
        from stx.errors import StxError

        wt_path = tmp_path / "wt"
        wt_path.mkdir()
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))

        record = _make_record(slug, tmp_path)
        record.boxes["app"].worktree = str(wt_path)

        uncommitted_patch = (
            patch(
                f"{_MOD}.has_uncommitted_changes",
                side_effect=StxError("could not check git status: boom"),
            )
            if fail_uncommitted_check
            else patch(f"{_MOD}.has_uncommitted_changes", return_value=False)
        )
        unpushed_patch = (
            patch(
                f"{_MOD}.unpushed_commit_shas",
                side_effect=StxError("could not determine unpushed commits: boom"),
            )
            if fail_unpushed_check
            else patch(f"{_MOD}.unpushed_commit_shas", return_value=[])
        )

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(
                f"{_MOD}.load_registry_with_unparsed", return_value=({slug: record}, {})
            ),
            patch(f"{_MOD}.PostgresAdminParams.build", return_value=MagicMock()),
            patch(f"{_MOD}.RedisAdminParams.build", return_value=MagicMock()),
            patch(f"{_TD}.drop_databases"),
            patch(f"{_TD}.flush_redis"),
            patch(f"{_TD}.remove_hosts_entry"),
            uncommitted_patch,
            unpushed_patch,
            patch(
                f"{_MOD}.subprocess.run",
                return_value=MagicMock(returncode=0, stdout="", stderr=""),
            ),
        ):
            result = SandboxRmCommand().execute(_make_args(slug, force=force))

        return result

    def test_blocks_without_force_when_uncommitted_check_fails(
        self, monkeypatch, tmp_path, capsys
    ):
        result = self._run(
            "my-slug", monkeypatch, tmp_path, fail_uncommitted_check=True
        )
        assert result == 1
        err = capsys.readouterr().err
        assert "could not check" in err
        assert "--force" in err

    def test_force_proceeds_despite_uncommitted_check_failure(
        self, monkeypatch, tmp_path
    ):
        result = self._run(
            "my-slug",
            monkeypatch,
            tmp_path,
            force=True,
            fail_uncommitted_check=True,
        )
        assert result is None

    def test_blocks_without_force_when_unpushed_check_fails(
        self, monkeypatch, tmp_path, capsys
    ):
        result = self._run("my-slug", monkeypatch, tmp_path, fail_unpushed_check=True)
        assert result == 1
        err = capsys.readouterr().err
        assert "could not determine" in err
        assert "--force" in err

    def test_force_warns_and_proceeds_despite_unpushed_check_failure(
        self, monkeypatch, tmp_path, capsys
    ):
        result = self._run(
            "my-slug",
            monkeypatch,
            tmp_path,
            force=True,
            fail_unpushed_check=True,
        )
        assert result is None
        err = capsys.readouterr().err
        assert "warning" in err
        assert "could not determine" in err


class TestRmBranchDeletion:
    def _run(self, slug: str, monkeypatch, tmp_path, *, branch_delete_returncode: int):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(
                f"{_MOD}.load_registry_with_unparsed",
                return_value=({slug: _make_record(slug, tmp_path)}, {}),
            ),
            patch(f"{_MOD}.PostgresAdminParams.build", return_value=MagicMock()),
            patch(f"{_MOD}.RedisAdminParams.build", return_value=MagicMock()),
            patch(f"{_TD}.drop_databases"),
            patch(f"{_TD}.flush_redis"),
            patch(f"{_TD}.remove_hosts_entry"),
            patch(
                f"{_MOD}.subprocess.run",
                return_value=MagicMock(
                    returncode=branch_delete_returncode,
                    stdout="",
                    stderr="error: branch 'feat/alex/my-slug' is not fully merged",
                ),
            ),
        ):
            result = SandboxRmCommand().execute(_make_args(slug, rm_branch=True))

        return result

    def test_warns_when_branch_delete_refuses(self, monkeypatch, tmp_path, capsys):
        result = self._run("my-slug", monkeypatch, tmp_path, branch_delete_returncode=1)

        assert result is None  # non-fatal: teardown still completes
        err = capsys.readouterr().err
        assert "warning: could not delete branch" in err
        assert "not fully merged" in err

    def test_deletes_the_local_stx_branch_not_the_target(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        calls = []

        def _record(cmd, **kwargs):
            calls.append(cmd)
            return MagicMock(returncode=0, stdout="", stderr="")

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(
                f"{_MOD}.load_registry_with_unparsed",
                return_value=({"my-slug": _make_record("my-slug", tmp_path)}, {}),
            ),
            patch(f"{_MOD}.PostgresAdminParams.build", return_value=MagicMock()),
            patch(f"{_MOD}.RedisAdminParams.build", return_value=MagicMock()),
            patch(f"{_TD}.drop_databases"),
            patch(f"{_TD}.flush_redis"),
            patch(f"{_TD}.remove_hosts_entry"),
            patch(f"{_MOD}.subprocess.run", side_effect=_record),
        ):
            SandboxRmCommand().execute(_make_args("my-slug", rm_branch=True))

        branch_delete_calls = [c for c in calls if c[:3] == ["git", "branch", "-d"]]
        assert branch_delete_calls == [["git", "branch", "-d", "stx/my-slug"]]

    def test_no_warning_when_branch_delete_succeeds(
        self, monkeypatch, tmp_path, capsys
    ):
        result = self._run("my-slug", monkeypatch, tmp_path, branch_delete_returncode=0)

        assert result is None
        assert "could not delete branch" not in capsys.readouterr().err


class TestTmpBranchSweep:
    def _init_repo_with_commit(self, root):
        subprocess.run(["git", "init"], cwd=root, capture_output=True, check=True)
        (root / "f.txt").write_text("hello")
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True)

    def _run(self, slug: str, monkeypatch, root, *, force: bool = False):
        monkeypatch.setenv("STX_STATE_ROOT", str(root / "state"))

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(
                f"{_MOD}.load_registry_with_unparsed",
                return_value=({slug: _make_record(slug, root)}, {}),
            ),
            patch(f"{_MOD}.PostgresAdminParams.build", return_value=MagicMock()),
            patch(f"{_MOD}.RedisAdminParams.build", return_value=MagicMock()),
            patch(f"{_TD}.drop_databases"),
            patch(f"{_TD}.flush_redis"),
            patch(f"{_TD}.remove_hosts_entry"),
        ):
            return SandboxRmCommand().execute(_make_args(slug, force=force))

    def test_removes_fully_pushed_tmp_branches(self, monkeypatch, tmp_path, capsys):
        remote = tmp_path / "remote.git"
        remote.mkdir()
        subprocess.run(["git", "init", "--bare"], cwd=remote, check=True)

        root = tmp_path / "repo"
        root.mkdir()
        self._init_repo_with_commit(root)
        subprocess.run(
            ["git", "remote", "add", "origin", str(remote)], cwd=root, check=True
        )
        subprocess.run(
            ["git", "checkout", "-b", "tmp/my-slug/task1"], cwd=root, check=True
        )
        subprocess.run(
            ["git", "push", "origin", "HEAD:feat/other/target"],
            cwd=root,
            capture_output=True,
            check=True,
        )
        subprocess.run(["git", "fetch", "origin"], cwd=root, capture_output=True)
        subprocess.run(["git", "checkout", "-", "-q"], cwd=root, check=True)

        result = self._run("my-slug", monkeypatch, root)

        assert result is None
        remaining = subprocess.run(
            ["git", "for-each-ref", "refs/heads/tmp/my-slug/"],
            cwd=root,
            capture_output=True,
            text=True,
        )
        assert remaining.stdout.strip() == ""

    def test_leaves_unpushed_tmp_branch_without_force(self, monkeypatch, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()
        self._init_repo_with_commit(root)
        subprocess.run(
            ["git", "checkout", "-b", "tmp/my-slug/task1"], cwd=root, check=True
        )
        subprocess.run(["git", "checkout", "-", "-q"], cwd=root, check=True)

        self._run("my-slug", monkeypatch, root)

        remaining = subprocess.run(
            ["git", "for-each-ref", "refs/heads/tmp/my-slug/"],
            cwd=root,
            capture_output=True,
            text=True,
        )
        assert "tmp/my-slug/task1" in remaining.stdout


class TestHostsCleanup:
    def test_removes_hosts_entry(self, monkeypatch, tmp_path):
        slug = "my-slug"
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        calls = []

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(
                f"{_MOD}.load_registry_with_unparsed",
                return_value=({slug: _make_record(slug, tmp_path)}, {}),
            ),
            patch(f"{_MOD}.PostgresAdminParams.build", return_value=MagicMock()),
            patch(f"{_MOD}.RedisAdminParams.build", return_value=MagicMock()),
            patch(f"{_TD}.drop_databases"),
            patch(f"{_TD}.flush_redis"),
            patch(f"{_TD}.remove_hosts_entry", side_effect=lambda s: calls.append(s)),
        ):
            SandboxRmCommand().execute(_make_args(slug))

        assert calls == [slug]

    def test_hosts_removal_failure_is_a_warning_not_fatal(self, monkeypatch, tmp_path):
        slug = "my-slug"
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))

        def _raise(s):
            raise OSError("sudo failed while updating the hosts file")

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(
                f"{_MOD}.load_registry_with_unparsed",
                return_value=({slug: _make_record(slug, tmp_path)}, {}),
            ),
            patch(f"{_MOD}.PostgresAdminParams.build", return_value=MagicMock()),
            patch(f"{_MOD}.RedisAdminParams.build", return_value=MagicMock()),
            patch(f"{_TD}.drop_databases"),
            patch(f"{_TD}.flush_redis"),
            patch(f"{_TD}.remove_hosts_entry", side_effect=_raise),
        ):
            result = SandboxRmCommand().execute(_make_args(slug))

        assert result is None  # non-fatal: teardown still completes


def _linking_record(slug: str, *, target: str) -> SandboxRecord:
    return SandboxRecord(
        slug=slug,
        created="2026-06-26T00:00:00+00:00",
        state=SandboxState.READY,
        redis_index=None,
        tailnet_port=None,
        boxes={
            "app": BoxRecord(
                zone="trusted",
                user="devuser",
                worktree="/nonexistent/wt/" + slug,
                links={
                    "api": LinkRecord(
                        target=target,
                        route="api",
                        port=8080,
                        container=f"stx-{target}--app",
                    )
                },
            )
        },
    )


class TestLinkedToGuard:
    def test_refuses_without_force_when_linked_to(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        registry = {
            "be-1": _make_record("be-1", tmp_path),
            "web-1": _linking_record("web-1", target="be-1"),
        }

        with patch(f"{_MOD}.load_registry_with_unparsed", return_value=(registry, {})):
            result = SandboxRmCommand().execute(_make_args("be-1"))

        assert result == 1
        assert "web-1" in capsys.readouterr().err

    def test_force_proceeds_and_disconnects_the_linker(
        self, monkeypatch, tmp_path, capsys
    ):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        registry = {
            "be-1": _make_record("be-1", tmp_path),
            "web-1": _linking_record("web-1", target="be-1"),
        }
        disconnects = []

        with (
            patch(f"{_MOD}.load_registry_with_unparsed", return_value=(registry, {})),
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.PostgresAdminParams.build", return_value=MagicMock()),
            patch(f"{_MOD}.RedisAdminParams.build", return_value=MagicMock()),
            patch(f"{_TD}.drop_databases"),
            patch(f"{_TD}.flush_redis"),
            patch(f"{_TD}.remove_hosts_entry"),
            patch(
                f"{_MOD}.stx_network.disconnect",
                side_effect=lambda net, container: disconnects.append((net, container)),
            ),
        ):
            result = SandboxRmCommand().execute(_make_args("be-1", force=True))

        assert result is None
        assert ("stx-be-1--net", "stx-web-1--app") in disconnects

    def test_no_linkers_proceeds_without_force(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        registry = {"be-1": _make_record("be-1", tmp_path)}

        with (
            patch(f"{_MOD}.load_registry_with_unparsed", return_value=(registry, {})),
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.PostgresAdminParams.build", return_value=MagicMock()),
            patch(f"{_MOD}.RedisAdminParams.build", return_value=MagicMock()),
            patch(f"{_TD}.drop_databases"),
            patch(f"{_TD}.flush_redis"),
            patch(f"{_TD}.remove_hosts_entry"),
        ):
            result = SandboxRmCommand().execute(_make_args("be-1"))

        assert result is None


class TestNetworkTeardown:
    def test_trusted_sandbox_removes_its_network(self, monkeypatch, tmp_path):
        slug = "my-slug"
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        removed = []

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(
                f"{_MOD}.load_registry_with_unparsed",
                return_value=({slug: _make_record(slug, tmp_path)}, {}),
            ),
            patch(f"{_MOD}.PostgresAdminParams.build", return_value=MagicMock()),
            patch(f"{_MOD}.RedisAdminParams.build", return_value=MagicMock()),
            patch(f"{_TD}.drop_databases"),
            patch(f"{_TD}.flush_redis"),
            patch(f"{_TD}.remove_hosts_entry"),
            patch(
                f"{_MOD}.stx_network.remove_network",
                side_effect=lambda net: removed.append(net),
            ),
        ):
            result = SandboxRmCommand().execute(_make_args(slug))

        assert result is None
        assert removed == ["stx-my-slug--net"]

    def test_dmz_only_sandbox_never_touches_a_network(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        removed = []
        with (
            patch(
                f"{_MOD}.load_registry_with_unparsed",
                return_value=({"triage": _make_dmz_only_record("triage")}, {}),
            ),
            patch(
                f"{_MOD}.stx_network.remove_network",
                side_effect=lambda net: removed.append(net),
            ),
        ):
            SandboxRmCommand().execute(_make_args("triage"))

        assert removed == []
