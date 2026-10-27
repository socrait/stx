import argparse
import signal
import subprocess
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

import stx.commands.new as _cmd
import stx.core as _common
from stx import network as _net
from stx.commands.new import SandboxNewCommand
from stx.core import routing, setup_status
from stx.forge import ForgeCredential, ForgeCredentialKind
from stx.lifecycle import ContainerTeardown


def _clean_teardown(*_a, **_k) -> ContainerTeardown:
    return ContainerTeardown(down_ok=True, used_label_fallback=False, remaining=[])


from stx.manifest import (
    ForgeManifest,
    Manifest,
    PackageRegistryManifest,
    PlatformManifest,
    PostgresManifest,
    ProvisionManifest,
)

_MOD = "stx.commands.new"


def _test_manifest(**overrides) -> Manifest:
    """A manifest exercising the full surface: an app box with routes (so
    redis/tailnet/traefik/hosts all fire), postgres databases, and a forge
    with a package registry (so both token mints fire)."""
    defaults = dict(
        schema=1,
        name="app",
        compose="compose.yaml",
        image="widgetshop-devbox",
        user="devuser",
        branch="feat/{user}/{slug}",
        routes={"api": 8080},
        publish="api",
        platform=PlatformManifest(
            postgres=PostgresManifest(databases=("{db}", "{db}_test")), redis=True
        ),
        forge=ForgeManifest(
            kind="gitlab",
            project="widgetshop/widgetshop",
            package_registry=PackageRegistryManifest(
                group="widgetshop",
                env={
                    "username": "UV_INDEX_GITLAB_INTERNAL_USERNAME",
                    "password": "UV_INDEX_GITLAB_INTERNAL_PASSWORD",
                },
            ),
        ),
    )
    defaults.update(overrides)
    return Manifest(**defaults)


def _fake_overlay_render(tmp_path):
    """Stand in for overlay.render(): writes a real (minimal) overlay file so
    the real `json.loads(overlay_path.read_text())` call downstream (feeding
    overlay.check_merged, itself mocked in these tests) doesn't hit a missing
    file — these tests exercise `_execute()`'s own orchestration/rollback,
    not the box-contract checks themselves (covered separately by overlay's
    own tests)."""

    def _render(*args, **kwargs):
        path = tmp_path / "overlay.json"
        path.write_text('{"services": {"app": {}}}')
        return path

    return _render


def _push_credential(
    *,
    scope_id=42,
    token_id=1,
    scope="write_repository",
    secret="fake-token",
    expires_at="2026-07-09",
) -> ForgeCredential:
    return ForgeCredential(
        kind=ForgeCredentialKind.PUSH,
        scope_id=scope_id,
        token_id=token_id,
        scope=scope,
        secret=secret,
        expires_at=expires_at,
    )


def _package_credential(
    *,
    scope_id=43,
    token_id=2,
    secret="pkg-token",
    username="pkg-user",
    expires_at="2026-07-09",
) -> ForgeCredential:
    return ForgeCredential(
        kind=ForgeCredentialKind.PACKAGE,
        scope_id=scope_id,
        token_id=token_id,
        scope="read_package_registry",
        secret=secret,
        expires_at=expires_at,
        username=username,
    )


class _FakeForge:
    """A `Forge` double driving `new`'s mint/revoke flow without touching
    GitLab — mints scope_id=42/token_id=1 (push) and scope_id=43/token_id=2
    (package)."""

    host = "gitlab.example.com"
    git_user = "oauth2"

    def __init__(
        self,
        *,
        push: ForgeCredential | None = None,
        package: ForgeCredential | None = None,
        mint_push_error: Exception | None = None,
        mint_package_error: Exception | None = None,
    ) -> None:
        self._push = push or _push_credential()
        self._package = package if package is not None else _package_credential()
        self._mint_push_error = mint_push_error
        self._mint_package_error = mint_package_error
        self.mint_push_calls: list[tuple[str, bool]] = []
        self.revoked: list[tuple[ForgeCredentialKind, int, int]] = []

    def mint_push(self, slug: str, *, read_only: bool) -> ForgeCredential:
        self.mint_push_calls.append((slug, read_only))
        if self._mint_push_error is not None:
            raise self._mint_push_error
        return self._push

    def mint_package(self, slug: str) -> ForgeCredential | None:
        if self._mint_package_error is not None:
            raise self._mint_package_error
        return self._package

    def revoke(self, kind: ForgeCredentialKind, scope_id: int, token_id: int) -> bool:
        self.revoked.append((kind, scope_id, token_id))
        return True

    def username(self) -> str | None:
        return None


@pytest.fixture(autouse=True)
def _stub_sandbox_network(monkeypatch):
    """Rollback's network teardown shells out to `docker network ...`, which
    raises outright where no docker binary exists — irrelevant to the
    orchestration/rollback behavior these tests assert, and (now that a
    rollback step raising keeps the entry for retry) a source of spurious
    'rollback failed' noise. A test that cares re-patches these."""
    monkeypatch.setattr(_net, "disconnect", lambda *a, **k: None)
    monkeypatch.setattr(_net, "remove_network", lambda *a, **k: None)
    monkeypatch.setattr(_net, "connect", lambda *a, **k: None)
    monkeypatch.setattr(_net, "ensure_network", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _default_oauth_token(monkeypatch):
    # Oauth is the default auth mode — set the token ambiently so tests
    # unrelated to auth mode don't all need to configure it individually.
    monkeypatch.setenv("STX_CLAUDE_OAUTH_TOKEN", "oauth-fake")


@pytest.fixture(autouse=True)
def _setup_finishes_immediately(monkeypatch):
    """These tests exercise `_execute()`'s own orchestration, not the box
    runtime's setup-status record — a test that cares about that waits
    itself re-patches this to something slower/failing."""
    monkeypatch.setattr(
        setup_status,
        "wait_for_setup",
        lambda container, box, *, timeout: _common.SetupStatus(
            state=_common.SetupState.DONE
        ),
    )


@pytest.fixture(autouse=True)
def _traefik_routes_match(monkeypatch):
    """These tests exercise `_execute()`'s own orchestration, not the live
    `stx-traefik` container's mount — a test that cares about that
    re-patches this to a mismatch warning."""
    monkeypatch.setattr(routing, "traefik_routes_warning", lambda container: None)


@pytest.fixture(autouse=True)
def _default_route_bind_addr(monkeypatch):
    # Route-port bind-address resolution shells out to `docker network
    # inspect` — orthogonal to what most of these tests exercise, so set it
    # ambiently; a test that cares about the actual value re-patches this.
    monkeypatch.setattr(_net, "route_bind_address", lambda: "127.0.0.1")


def _args(**kwargs) -> argparse.Namespace:
    defaults = {
        "slug": "test",
        "repo": "widgetshop",
        "with_dmz": False,
        "branch": None,
        "base": None,
        "force": False,
        "ro": False,
        "oauth": False,
        "api_key": False,
        "shell": False,
        "code": False,
        "replace": False,
        "no_hosts": False,
        "timeout": _common.DEFAULT_SETUP_TIMEOUT_SECONDS,
        "link": None,
        "yes": False,
        "verbose": False,
        "dry_run": False,
        "debug": False,
    }
    return argparse.Namespace(**{**defaults, **kwargs})


def test_refuses_when_in_sandbox_box(monkeypatch, capsys):
    monkeypatch.setenv("STX_ZONE", "trusted")
    monkeypatch.setattr(
        _cmd.secrets,
        "source_for",
        MagicMock(side_effect=AssertionError("should not run")),
    )
    result = SandboxNewCommand().execute(_args())
    assert result == 1
    assert "host-only" in capsys.readouterr().err


class TestValidation:
    def test_repo_none_without_dmz_returns_1(self, capsys):
        result = SandboxNewCommand().execute(_args(repo="none"))
        assert result == 1
        assert "--with-dmz" in capsys.readouterr().err

    def test_ro_without_app_box_returns_1(self, capsys):
        result = SandboxNewCommand().execute(_args(repo="none", with_dmz=True, ro=True))
        assert result == 1
        assert "--ro" in capsys.readouterr().err

    def test_dmz_without_harness_key_returns_1(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        monkeypatch.setenv("STX_CONFIG_PATH", str(tmp_path / "config.toml"))
        monkeypatch.delenv("STX_DMZ_ANTHROPIC_API_KEY", raising=False)
        result = SandboxNewCommand().execute(_args(repo="none", with_dmz=True))
        assert result == 1
        assert "STX_DMZ_ANTHROPIC_API_KEY" in capsys.readouterr().err

    def test_oauth_without_app_box_returns_1(self, capsys):
        result = SandboxNewCommand().execute(
            _args(repo="none", with_dmz=True, oauth=True)
        )
        assert result == 1
        assert "--oauth/--api-key" in capsys.readouterr().err

    def test_api_key_without_app_box_returns_1(self, capsys):
        result = SandboxNewCommand().execute(
            _args(repo="none", with_dmz=True, api_key=True)
        )
        assert result == 1
        assert "--oauth/--api-key" in capsys.readouterr().err

    def test_branch_without_app_box_returns_1(self, capsys):
        result = SandboxNewCommand().execute(
            _args(repo="none", with_dmz=True, branch="feat/someone/existing")
        )
        assert result == 1
        assert "--branch" in capsys.readouterr().err

    def test_default_oauth_without_token_returns_1(self, monkeypatch, tmp_path, capsys):
        # No auth flag passed at all — oauth is the default, so a missing
        # token must still fail fast rather than silently falling back.
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        monkeypatch.setenv("STX_CONFIG_PATH", str(tmp_path / "config.toml"))
        monkeypatch.delenv("STX_CLAUDE_OAUTH_TOKEN", raising=False)
        with (
            patch(f"{_MOD}.resolve_repo_root", return_value=tmp_path),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
        ):
            result = SandboxNewCommand().execute(_args())
        assert result == 1
        assert "STX_CLAUDE_OAUTH_TOKEN" in capsys.readouterr().err


class _CapturingSource:
    """A `SecretSource` double that records the `required` list it was asked
    to load instead of actually loading anything, so a test can assert on
    `new`'s own `required` computation independent of whatever a real
    dotenv/op source would do."""

    def __init__(self):
        self.required: list[str] | None = None

    def ensure_loaded(self, required, *, optional=()):
        self.required = list(required)
        return {}


class TestRequiredSecrets:
    def test_full_manifest_requires_platform_provision_auth_dmz_and_minter_secrets(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        manifest = _test_manifest(
            provision=ProvisionManifest(env={"DATABASE_USER": "${PGUSER}"})
        )
        source = _CapturingSource()

        with (
            patch(f"{_MOD}.secrets.source_for", return_value=source),
            patch(f"{_MOD}.resolve_repo_root", return_value=tmp_path),
            patch(f"{_MOD}.Manifest.load", return_value=manifest),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.core.compose.compose_variables", return_value=[]),
        ):
            # forge_for raises (no STX_GITLAB_MINTER_TOKEN actually set) right
            # after required secrets are captured — exactly where this test
            # wants execution to stop.
            result = SandboxNewCommand().execute(
                _args(repo="widgetshop", with_dmz=True)
            )

        assert result == 1
        assert source.required == [
            "STX_POSTGRES_PASSWORD",
            "STX_REDIS_PASSWORD",
            "STX_REDIS_APP_PASSWORD",
            "STX_CLAUDE_OAUTH_TOKEN",
            "STX_DMZ_ANTHROPIC_API_KEY",
            "STX_GITLAB_MINTER_TOKEN",
        ]

    def test_bare_manifest_needs_no_platform_secrets_and_starts_no_services(
        self, monkeypatch, tmp_path
    ):
        bare = _test_manifest(
            routes={}, publish=None, platform=PlatformManifest(), forge=None
        )
        source = _CapturingSource()
        platform_up_calls = []

        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        with (
            patch(f"{_MOD}.secrets.source_for", return_value=source),
            patch(f"{_MOD}.resolve_repo_root", return_value=tmp_path),
            patch(f"{_MOD}.Manifest.load", return_value=bare),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.core.compose.compose_variables", return_value=[]),
            patch(
                f"{_MOD}.stx_platform.run_platform_up",
                side_effect=lambda services: platform_up_calls.append(services) or 1,
            ),
        ):
            result = SandboxNewCommand().execute(_args(repo="widgetshop"))

        assert result == 1  # run_platform_up stubbed to fail, stopping right here
        # Only the harness auth var — no STX_POSTGRES_*/STX_REDIS_*/forge secret.
        assert source.required == ["STX_CLAUDE_OAUTH_TOKEN"]
        assert platform_up_calls == [[]]

    def test_optional_compose_variable_not_in_required(self, monkeypatch, tmp_path):
        from stx.core.compose import ComposeVariable

        manifest = _test_manifest(
            routes={}, publish=None, platform=PlatformManifest(), forge=None
        )
        source = _CapturingSource()

        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        with (
            patch(f"{_MOD}.secrets.source_for", return_value=source),
            patch(f"{_MOD}.resolve_repo_root", return_value=tmp_path),
            patch(f"{_MOD}.Manifest.load", return_value=manifest),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch(
                "stx.core.compose.compose_variables",
                return_value=[
                    ComposeVariable(name="REQUIRED_VAR", required=True),
                    ComposeVariable(name="OPTIONAL_VAR", required=False),
                ],
            ),
            patch(f"{_MOD}.stx_platform.run_platform_up", return_value=1),
        ):
            result = SandboxNewCommand().execute(_args(repo="widgetshop"))

        assert result == 1
        assert source.required == ["REQUIRED_VAR", "STX_CLAUDE_OAUTH_TOKEN"]

    def test_stx_credential_in_compose_file_refused_before_anything_changes(
        self, monkeypatch, tmp_path
    ):
        manifest = _test_manifest()
        platform_up_calls = []
        leaking_config = {
            "services": {"app": {"environment": {"X": "${STX_GITLAB_MINTER_TOKEN}"}}}
        }

        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        with (
            patch(f"{_MOD}.resolve_repo_root", return_value=tmp_path),
            patch(f"{_MOD}.Manifest.load", return_value=manifest),
            patch("stx.core.compose.compose_config", return_value=leaking_config),
            patch("stx.overlay.check_repo_compose"),
            patch(
                f"{_MOD}.stx_platform.run_platform_up",
                side_effect=lambda services: platform_up_calls.append(services),
            ),
        ):
            result = SandboxNewCommand().execute(_args(repo="widgetshop"))

        assert result == 1
        assert platform_up_calls == []

    def test_allow_list_violation_refused_before_anything_changes(
        self, monkeypatch, tmp_path
    ):
        """The compose check runs before `new` changes
        anything (platform up, worktree, branch)."""
        from stx.errors import StxError

        manifest = _test_manifest()
        platform_up_calls = []
        worktree_add_calls = []

        def fake_run(cmd, **kwargs):
            if cmd[:3] == ["git", "worktree", "add"]:
                worktree_add_calls.append(cmd)
            return MagicMock(returncode=0, stdout="", stderr="")

        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        monkeypatch.setattr(subprocess, "run", fake_run)
        with (
            patch(f"{_MOD}.resolve_repo_root", return_value=tmp_path),
            patch(f"{_MOD}.Manifest.load", return_value=manifest),
            patch("stx.core.compose.compose_config", return_value={}),
            patch(
                "stx.overlay.check_repo_compose",
                side_effect=StxError("compose.yaml: service 'app': key 'privileged'"),
            ),
            patch(
                f"{_MOD}.stx_platform.run_platform_up",
                side_effect=lambda services: platform_up_calls.append(services),
            ),
        ):
            result = SandboxNewCommand().execute(_args(repo="widgetshop"))

        assert result == 1
        assert platform_up_calls == []
        assert worktree_add_calls == []
        assert "test" not in _cmd.load_registry()


class TestExecuteComposition:
    def _ok(self, tmp_path):
        result = MagicMock()
        result.returncode = 0
        result.stdout = str(tmp_path / ".git")
        result.stderr = ""
        return result

    def _patch_subprocess(self, monkeypatch, tmp_path):
        ok = self._ok(tmp_path)
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: ok)
        # State (drop/drop-dmz/code-dmz dirs) is machine-global, not repo-relative —
        # isolate it to tmp_path or these tests would write into the real
        # ~/.local/state/stx/.
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        # Same reasoning for the hosts file the default hosts-entry step would touch.
        monkeypatch.setenv("STX_HOSTS_PATH", str(tmp_path / "hosts"))

    def test_repo_none_with_dmz_registers_dmz_only(self, monkeypatch, tmp_path):
        self._patch_subprocess(monkeypatch, tmp_path)
        monkeypatch.setenv("STX_DMZ_ANTHROPIC_API_KEY", "fake-dmz-key")

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.stx_platform.run_platform_up", return_value=None),
            patch("stx.overlay.render", side_effect=_fake_overlay_render(tmp_path)),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.overlay.check_merged"),
            patch("stx.commands.rebuild.ensure_image_fresh", return_value=None),
            patch("stx.dmz.ensure_built", return_value="stx-dmz:0.0.1"),
            patch("stx.core.images.image_user", return_value="devuser"),
        ):
            result = SandboxNewCommand().execute(_args(repo="none", with_dmz=True))

        assert result is None
        record = _cmd.load_registry()["test"]
        assert set(record.boxes) == {"dmz"}
        assert record.redis_index is None

    def test_dmz_snapshot_failure_rolls_back_app_box(self, monkeypatch, tmp_path):
        self._patch_subprocess(monkeypatch, tmp_path)
        monkeypatch.setenv("STX_DMZ_ANTHROPIC_API_KEY", "fake-dmz-key")
        compose_down_calls = []
        dropped_databases = []
        fake_forge = _FakeForge()

        def _run(cmd, **kwargs):
            if "down" in cmd:
                compose_down_calls.append(cmd)
            return self._ok(tmp_path)

        monkeypatch.setattr(subprocess, "run", _run)

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.stx_platform.run_platform_up", return_value=None),
            patch("stx.overlay.render", side_effect=_fake_overlay_render(tmp_path)),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.overlay.check_merged"),
            patch("stx.commands.rebuild.ensure_image_fresh", return_value=None),
            patch(f"{_MOD}.resolve_repo_root", return_value=tmp_path),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.platform.PostgresAdminParams.build", return_value=MagicMock()),
            patch("stx.core.resources.existing_databases", return_value=[]),
            patch(f"{_MOD}.forge_for", return_value=fake_forge),
            patch("stx.dmz.ensure_built", return_value="stx-dmz:0.0.1"),
            patch("stx.core.images.image_user", return_value="dmzuser"),
            patch("stx.lifecycle.provision._create_dmz_snapshot", return_value=1),
            patch(
                "stx.core.resources.drop_databases",
                side_effect=lambda dbs, params: dropped_databases.extend(dbs),
            ),
        ):
            result = SandboxNewCommand().execute(
                _args(repo="widgetshop", with_dmz=True)
            )

        assert result == 1
        # The dmz snapshot fails before the overlay is ever rendered, so
        # there's no compose project on disk yet for rollback to tear down —
        # unlike the token revokes and worktree/db cleanup below, which do
        # need to undo work that already happened by this point.
        assert compose_down_calls == []
        assert dropped_databases == ["stx_test", "stx_test_test"]
        assert fake_forge.revoked == [
            (ForgeCredentialKind.PUSH, 42, 1),
            (ForgeCredentialKind.PACKAGE, 43, 2),
        ]
        assert _cmd.load_registry() == {}

    def test_existing_databases_connection_failure_never_drops_on_rollback(
        self, monkeypatch, tmp_path
    ):
        from stx.errors import StxError

        self._patch_subprocess(monkeypatch, tmp_path)
        drop_databases_calls = []

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.stx_platform.run_platform_up", return_value=None),
            patch(f"{_MOD}.resolve_repo_root", return_value=tmp_path),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.platform.PostgresAdminParams.build", return_value=MagicMock()),
            patch(
                "stx.core.resources.existing_databases",
                side_effect=StxError("could not connect to postgres: refused"),
            ),
            patch(
                "stx.core.resources.drop_databases",
                side_effect=lambda *a, **kw: drop_databases_calls.append(a),
            ),
        ):
            result = SandboxNewCommand().execute(
                _args(repo="widgetshop", with_dmz=False)
            )

        assert result == 1
        assert drop_databases_calls == []
        assert _cmd.load_registry() == {}

    def test_package_registry_mint_failure_revokes_git_token(
        self, monkeypatch, tmp_path
    ):
        self._patch_subprocess(monkeypatch, tmp_path)
        dropped_databases = []
        fake_forge = _FakeForge(
            mint_package_error=_cmd.ForgeError("insufficient permissions")
        )

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.stx_platform.run_platform_up", return_value=None),
            patch("stx.overlay.render", side_effect=_fake_overlay_render(tmp_path)),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.overlay.check_merged"),
            patch("stx.commands.rebuild.ensure_image_fresh", return_value=None),
            patch(f"{_MOD}.resolve_repo_root", return_value=tmp_path),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.platform.PostgresAdminParams.build", return_value=MagicMock()),
            patch("stx.core.resources.existing_databases", return_value=[]),
            patch(f"{_MOD}.forge_for", return_value=fake_forge),
            patch(
                "stx.core.resources.drop_databases",
                side_effect=lambda dbs, params: dropped_databases.extend(dbs),
            ),
        ):
            result = SandboxNewCommand().execute(_args(repo="widgetshop"))

        assert result == 1
        # The databases didn't pre-exist (existing_databases == []), so
        # rollback drops what this attempt itself created.
        assert dropped_databases == ["stx_test", "stx_test_test"]
        assert fake_forge.revoked == [(ForgeCredentialKind.PUSH, 42, 1)]

    def test_keyboard_interrupt_unwinds_then_ends_with_the_rm_hint(
        self, monkeypatch, tmp_path, capsys
    ):
        # Ctrl+C mid-`new` must end with the `stx rm` hint, not a bare
        # KeyboardInterrupt traceback — it still unwinds first.
        self._patch_subprocess(monkeypatch, tmp_path)
        fake_forge = _FakeForge(mint_push_error=KeyboardInterrupt())

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.stx_platform.run_platform_up", return_value=None),
            patch(f"{_MOD}.resolve_repo_root", return_value=tmp_path),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.platform.PostgresAdminParams.build", return_value=MagicMock()),
            patch("stx.core.resources.drop_databases"),
            patch("stx.core.resources.existing_databases", return_value=[]),
            patch(f"{_MOD}.forge_for", return_value=fake_forge),
        ):
            result = SandboxNewCommand().execute(_args(repo="widgetshop"))

        assert result == 130
        assert "stx rm test" in capsys.readouterr().err
        # A clean unwind (every undo succeeded) drops the reservation.
        assert _cmd.load_registry() == {}

    def test_oserror_mid_provisioning_is_reported_as_a_clean_error(
        self, monkeypatch, tmp_path, capsys
    ):
        # An OSError escaping a provisioning step must surface as a reported
        # error (exit 1), never a raw traceback the CLI can't render.
        self._patch_subprocess(monkeypatch, tmp_path)

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.stx_platform.run_platform_up", return_value=None),
            patch("stx.overlay.render", side_effect=_fake_overlay_render(tmp_path)),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.overlay.check_merged"),
            patch(
                "stx.commands.rebuild.ensure_image_fresh",
                side_effect=OSError("disk went away"),
            ),
            patch(f"{_MOD}.resolve_repo_root", return_value=tmp_path),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.platform.PostgresAdminParams.build", return_value=MagicMock()),
            patch("stx.core.resources.drop_databases"),
            patch("stx.core.resources.existing_databases", return_value=[]),
            patch(f"{_MOD}.forge_for", return_value=_FakeForge()),
        ):
            result = SandboxNewCommand().execute(_args(repo="widgetshop"))

        assert result == 1
        assert "disk went away" in capsys.readouterr().err

    def test_compose_up_failure_tears_down_containers_that_already_started(
        self, monkeypatch, tmp_path
    ):
        self._patch_subprocess(monkeypatch, tmp_path)
        down_calls = []

        def _run(cmd, **kwargs):
            if "up" in cmd:
                result = MagicMock()
                result.returncode = 1
                result.stdout = ""
                result.stderr = ""
                return result
            return self._ok(tmp_path)

        monkeypatch.setattr(subprocess, "run", _run)

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.stx_platform.run_platform_up", return_value=None),
            patch("stx.overlay.render", side_effect=_fake_overlay_render(tmp_path)),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.overlay.check_merged"),
            patch("stx.commands.rebuild.ensure_image_fresh", return_value=None),
            patch(f"{_MOD}.resolve_repo_root", return_value=tmp_path),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.platform.PostgresAdminParams.build", return_value=MagicMock()),
            patch("stx.platform.RedisAdminParams.build", return_value=MagicMock()),
            patch("stx.core.resources.drop_databases"),
            patch("stx.core.resources.flush_redis"),
            patch("stx.core.resources.existing_databases", return_value=[]),
            # Rollback brings containers down through the shared path.
            patch(
                "stx.lifecycle.provision.bring_down_containers",
                side_effect=lambda slug: down_calls.append(slug) or _clean_teardown(),
            ),
            patch(f"{_MOD}.forge_for", return_value=_FakeForge()),
        ):
            result = SandboxNewCommand().execute(_args(repo="widgetshop"))

        assert result == 1
        assert down_calls  # rollback tore the containers down
        assert _cmd.load_registry() == {}

    def test_failed_provision_step_still_flushes_its_redis_index(
        self, monkeypatch, tmp_path
    ):
        # Bug: the Redis flush undo was registered only alongside `compose up`,
        # after the provision steps' own `compose run`. A step that wrote Redis
        # then failed left the index unflushed. The flush undo is now on the
        # stack before the first `compose run`, so rollback flushes it.
        self._patch_subprocess(monkeypatch, tmp_path)
        flushed: list[int] = []

        def _run(cmd, **kwargs):
            if "run" in cmd:  # a `compose run` provision step — fail it
                result = MagicMock()
                result.returncode = 1
                result.stdout = result.stderr = ""
                return result
            return self._ok(tmp_path)

        monkeypatch.setattr(subprocess, "run", _run)
        manifest = _test_manifest(provision=ProvisionManifest(steps=(("migrate",),)))

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.stx_platform.run_platform_up", return_value=None),
            patch("stx.overlay.render", side_effect=_fake_overlay_render(tmp_path)),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.overlay.check_merged"),
            patch("stx.commands.rebuild.ensure_image_fresh", return_value=None),
            patch(f"{_MOD}.resolve_repo_root", return_value=tmp_path),
            patch(f"{_MOD}.Manifest.load", return_value=manifest),
            patch("stx.platform.PostgresAdminParams.build", return_value=MagicMock()),
            patch("stx.platform.RedisAdminParams.build", return_value=MagicMock()),
            patch(
                "stx.core.resources.flush_redis",
                side_effect=lambda idx, params: flushed.append(idx),
            ),
            patch(
                "stx.lifecycle.provision.bring_down_containers",
                side_effect=_clean_teardown,
            ),
            patch("stx.core.resources.drop_databases"),
            patch("stx.core.resources.existing_databases", return_value=[]),
            patch(f"{_MOD}.forge_for", return_value=_FakeForge()),
        ):
            result = SandboxNewCommand().execute(_args(repo="widgetshop"))

        assert result == 1
        assert flushed == [15]

    def test_failed_rollback_keeps_the_entry_in_provisioning_for_retry(
        self, monkeypatch, tmp_path
    ):
        self._patch_subprocess(monkeypatch, tmp_path)

        def _run(cmd, **kwargs):
            if "up" in cmd:
                result = MagicMock()
                result.returncode = 1
                result.stdout = result.stderr = ""
                return result
            return self._ok(tmp_path)

        monkeypatch.setattr(subprocess, "run", _run)

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.stx_platform.run_platform_up", return_value=None),
            patch("stx.overlay.render", side_effect=_fake_overlay_render(tmp_path)),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.overlay.check_merged"),
            patch("stx.commands.rebuild.ensure_image_fresh", return_value=None),
            patch(f"{_MOD}.resolve_repo_root", return_value=tmp_path),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.platform.PostgresAdminParams.build", return_value=MagicMock()),
            patch("stx.platform.RedisAdminParams.build", return_value=MagicMock()),
            patch("stx.core.resources.flush_redis"),
            # A rollback step that cannot finish — the database drop fails.
            patch(
                "stx.core.resources.drop_databases",
                side_effect=_cmd.StxError("postgres unreachable"),
            ),
            patch("stx.core.resources.existing_databases", return_value=[]),
            patch(f"{_MOD}.forge_for", return_value=_FakeForge()),
        ):
            result = SandboxNewCommand().execute(_args(repo="widgetshop"))

        assert result == 1
        # The entry survives, still provisioning, carrying what rollback could
        # not reclaim (its databases) so `stx rm` can retry.
        record = _cmd.load_registry().get("test")
        assert record is not None
        assert record.state == _common.SandboxState.PROVISIONING
        assert record.databases == ("stx_test", "stx_test_test")

    def test_sigterm_mid_provisioning_triggers_rollback(self, monkeypatch, tmp_path):
        self._patch_subprocess(monkeypatch, tmp_path)
        fake_forge = _FakeForge()

        def _sigterm(root, manifest):
            # A SIGTERM arriving mid-`new` must unwind, not kill the process
            # outright. raise_signal lands the handler synchronously here.
            signal.raise_signal(signal.SIGTERM)
            return None

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.stx_platform.run_platform_up", return_value=None),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch(f"{_MOD}.resolve_repo_root", return_value=tmp_path),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.platform.PostgresAdminParams.build", return_value=MagicMock()),
            patch("stx.core.resources.drop_databases"),
            patch("stx.core.resources.existing_databases", return_value=[]),
            patch(f"{_MOD}.forge_for", return_value=fake_forge),
            patch("stx.commands.rebuild.ensure_image_fresh", side_effect=_sigterm),
        ):
            result = SandboxNewCommand().execute(_args(repo="widgetshop"))

        # The SIGTERM (raised as KeyboardInterrupt) unwinds then ends with the
        # hint and a non-zero code, not a bare traceback.
        assert result == 130
        # Rollback ran: the minted credentials were revoked, the reservation
        # dropped — none of it left behind by the terminating signal.
        assert fake_forge.revoked
        assert _cmd.load_registry() == {}

    def test_rollback_flushes_the_redis_index_before_freeing_it(
        self, monkeypatch, tmp_path
    ):
        self._patch_subprocess(monkeypatch, tmp_path)
        flushed: list[int] = []
        order: list[str] = []

        def _run(cmd, **kwargs):
            if "up" in cmd:
                result = MagicMock()
                result.returncode = 1
                result.stdout = ""
                result.stderr = ""
                return result
            return self._ok(tmp_path)

        monkeypatch.setattr(subprocess, "run", _run)

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.stx_platform.run_platform_up", return_value=None),
            patch("stx.overlay.render", side_effect=_fake_overlay_render(tmp_path)),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.overlay.check_merged"),
            patch("stx.commands.rebuild.ensure_image_fresh", return_value=None),
            patch(f"{_MOD}.resolve_repo_root", return_value=tmp_path),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.platform.PostgresAdminParams.build", return_value=MagicMock()),
            patch("stx.platform.RedisAdminParams.build", return_value=MagicMock()),
            patch(
                "stx.core.resources.flush_redis",
                side_effect=lambda idx, params: (
                    order.append("flush"),
                    flushed.append(idx),
                ),
            ),
            patch(
                "stx.lifecycle.provision.bring_down_containers",
                side_effect=lambda slug: order.append("down") or _clean_teardown(),
            ),
            patch("stx.core.resources.drop_databases"),
            patch("stx.core.resources.existing_databases", return_value=[]),
            patch(f"{_MOD}.forge_for", return_value=_FakeForge()),
        ):
            result = SandboxNewCommand().execute(_args(repo="widgetshop"))

        assert result == 1
        # The redis=True manifest allocates the top index from an empty
        # registry; rollback must flush it, and only *after* the box is down.
        assert flushed == [15]
        assert order.index("down") < order.index("flush")
        assert _cmd.load_registry() == {}

    def test_forge_token_ids_recorded_the_moment_they_are_minted(
        self, monkeypatch, tmp_path
    ):
        self._patch_subprocess(monkeypatch, tmp_path)
        captured: dict[str, object] = {}

        def _capture_then_fail(root, manifest):
            # Runs right after the mint + pending-forge-record steps; snapshot
            # the registry as it stands mid-provisioning, then fail the step.
            captured["record"] = _cmd.load_registry().get("test")
            return 1

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.stx_platform.run_platform_up", return_value=None),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch(f"{_MOD}.resolve_repo_root", return_value=tmp_path),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.platform.PostgresAdminParams.build", return_value=MagicMock()),
            patch("stx.core.resources.existing_databases", return_value=[]),
            patch(f"{_MOD}.forge_for", return_value=_FakeForge()),
            patch(
                "stx.commands.rebuild.ensure_image_fresh",
                side_effect=_capture_then_fail,
            ),
        ):
            result = SandboxNewCommand().execute(_args(repo="widgetshop"))

        assert result == 1
        record = captured["record"]
        assert record is not None
        assert record.state == _common.SandboxState.PROVISIONING
        # The push token id was on the entry before the box was ever built.
        assert record.pending_forge is not None
        assert record.pending_forge.push.token_id == 1
        assert record.pending_forge_source.host == "gitlab.example.com"
        # And the owning `new` process stamped itself, so prune won't treat a
        # still-running provision as stuck.
        import os as _os

        assert record.owner_pid == _os.getpid()

    def test_pending_forge_cleared_once_provisioning_finishes(
        self, monkeypatch, tmp_path
    ):
        self._patch_subprocess(monkeypatch, tmp_path)

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.stx_platform.run_platform_up", return_value=None),
            patch("stx.overlay.render", side_effect=_fake_overlay_render(tmp_path)),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.overlay.check_merged"),
            patch("stx.commands.rebuild.ensure_image_fresh", return_value=None),
            patch(f"{_MOD}.resolve_repo_root", return_value=tmp_path),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.platform.PostgresAdminParams.build", return_value=MagicMock()),
            patch("stx.core.resources.existing_databases", return_value=[]),
            patch(f"{_MOD}.forge_for", return_value=_FakeForge()),
        ):
            result = SandboxNewCommand().execute(_args(repo="widgetshop"))

        assert result is None
        record = _cmd.load_registry()["test"]
        assert record.pending_forge is None
        assert record.boxes["app"].forge.push.token_id == 1

    def test_branch_uses_resolve_branch_username_chain(self, monkeypatch, tmp_path):
        self._patch_subprocess(monkeypatch, tmp_path)

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.stx_platform.run_platform_up", return_value=None),
            patch("stx.overlay.render", side_effect=_fake_overlay_render(tmp_path)),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.overlay.check_merged"),
            patch("stx.commands.rebuild.ensure_image_fresh", return_value=None),
            patch(f"{_MOD}.resolve_repo_root", return_value=tmp_path),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.platform.PostgresAdminParams.build", return_value=MagicMock()),
            patch("stx.core.resources.existing_databases", return_value=[]),
            patch(f"{_MOD}.forge_for", return_value=_FakeForge()),
            patch(f"{_MOD}.resolve_branch_username", return_value="alex"),
        ):
            result = SandboxNewCommand().execute(_args(repo="widgetshop"))

        assert result is None
        record = _cmd.load_registry()["test"]
        assert record.boxes["app"].branch == "feat/alex/test"
        assert record.boxes["app"].claude_auth_mode == _common.ClaudeAuthMode.OAUTH

    def test_explicit_branch_overrides_derived_name(self, monkeypatch, tmp_path):
        self._patch_subprocess(monkeypatch, tmp_path)

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.stx_platform.run_platform_up", return_value=None),
            patch("stx.overlay.render", side_effect=_fake_overlay_render(tmp_path)),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.overlay.check_merged"),
            patch("stx.commands.rebuild.ensure_image_fresh", return_value=None),
            patch(f"{_MOD}.resolve_repo_root", return_value=tmp_path),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.platform.PostgresAdminParams.build", return_value=MagicMock()),
            patch("stx.core.resources.existing_databases", return_value=[]),
            patch(f"{_MOD}.forge_for", return_value=_FakeForge()),
            # An explicit --branch never needs the slug-derived username chain.
            patch(
                f"{_MOD}.resolve_branch_username",
                side_effect=AssertionError("should not derive a branch name"),
            ),
        ):
            result = SandboxNewCommand().execute(
                _args(repo="widgetshop", branch="feat/navneet/existing-work")
            )

        assert result is None
        record = _cmd.load_registry()["test"]
        assert record.boxes["app"].branch == "feat/navneet/existing-work"

    def test_app_and_dmz_registered_together(self, monkeypatch, tmp_path):
        self._patch_subprocess(monkeypatch, tmp_path)
        monkeypatch.setenv("STX_DMZ_ANTHROPIC_API_KEY", "fake-dmz-key")

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.stx_platform.run_platform_up", return_value=None),
            patch("stx.overlay.render", side_effect=_fake_overlay_render(tmp_path)),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.overlay.check_merged"),
            patch("stx.commands.rebuild.ensure_image_fresh", return_value=None),
            patch(f"{_MOD}.resolve_repo_root", return_value=tmp_path),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.platform.PostgresAdminParams.build", return_value=MagicMock()),
            patch("stx.core.resources.existing_databases", return_value=[]),
            patch(f"{_MOD}.forge_for", return_value=_FakeForge()),
            patch("stx.core.routing.get_tailscale_hostname", return_value=None),
            patch("stx.dmz.ensure_built", return_value="stx-dmz:0.0.1"),
            patch("stx.core.images.image_user", return_value="dmzuser"),
        ):
            result = SandboxNewCommand().execute(
                _args(repo="widgetshop", with_dmz=True)
            )

        assert result is None
        record = _cmd.load_registry()["test"]
        assert set(record.boxes) == {"app", "dmz"}
        assert record.redis_index == 15
        assert record.tailnet_port == 5099
        assert record.boxes["app"].zone == "trusted"
        assert record.boxes["dmz"].zone == "dmz"
        assert record.boxes["app"].forge.push.scope == "write_repository"
        assert record.boxes["app"].forge.package.scope_id == 43
        assert record.boxes["app"].forge.package.token_id == 2

    def test_explicit_api_key_threads_into_box_record(self, monkeypatch, tmp_path):
        self._patch_subprocess(monkeypatch, tmp_path)

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.stx_platform.run_platform_up", return_value=None),
            patch("stx.overlay.render", side_effect=_fake_overlay_render(tmp_path)),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.overlay.check_merged"),
            patch("stx.commands.rebuild.ensure_image_fresh", return_value=None),
            patch(f"{_MOD}.resolve_repo_root", return_value=tmp_path),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.platform.PostgresAdminParams.build", return_value=MagicMock()),
            patch("stx.core.resources.existing_databases", return_value=[]),
            patch(f"{_MOD}.forge_for", return_value=_FakeForge()),
            patch("stx.core.routing.get_tailscale_hostname", return_value=None),
        ):
            result = SandboxNewCommand().execute(_args(repo="widgetshop", api_key=True))

        assert result is None
        record = _cmd.load_registry()["test"]
        assert record.boxes["app"].claude_auth_mode == _common.ClaudeAuthMode.API_KEY

    def test_records_rendered_databases_on_the_registry_entry(
        self, monkeypatch, tmp_path
    ):
        """Recorded on the SandboxRecord (not only derivable from the
        manifest later) so `rm`/`prune` never need the checkout's manifest to
        know what to drop — see docs/reference.md (Teardown guards)."""
        self._patch_subprocess(monkeypatch, tmp_path)

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.stx_platform.run_platform_up", return_value=None),
            patch("stx.overlay.render", side_effect=_fake_overlay_render(tmp_path)),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.overlay.check_merged"),
            patch("stx.commands.rebuild.ensure_image_fresh", return_value=None),
            patch(f"{_MOD}.resolve_repo_root", return_value=tmp_path),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.platform.PostgresAdminParams.build", return_value=MagicMock()),
            patch("stx.core.resources.existing_databases", return_value=[]),
            patch(f"{_MOD}.forge_for", return_value=_FakeForge()),
            patch("stx.core.routing.get_tailscale_hostname", return_value=None),
        ):
            result = SandboxNewCommand().execute(_args(repo="widgetshop", api_key=True))

        assert result is None
        record = _cmd.load_registry()["test"]
        assert record.databases == ("stx_test", "stx_test_test")

    def test_records_forge_source_on_the_box_record(self, monkeypatch, tmp_path):
        """Recorded so `rm` can rebuild a revoke-capable `Forge` client from
        the registry alone, never the checkout's manifest — see
        docs/reference.md (Teardown guards)."""
        self._patch_subprocess(monkeypatch, tmp_path)

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.stx_platform.run_platform_up", return_value=None),
            patch("stx.overlay.render", side_effect=_fake_overlay_render(tmp_path)),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.overlay.check_merged"),
            patch("stx.commands.rebuild.ensure_image_fresh", return_value=None),
            patch(f"{_MOD}.resolve_repo_root", return_value=tmp_path),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.platform.PostgresAdminParams.build", return_value=MagicMock()),
            patch("stx.core.resources.existing_databases", return_value=[]),
            patch(f"{_MOD}.forge_for", return_value=_FakeForge()),
            patch("stx.core.routing.get_tailscale_hostname", return_value=None),
        ):
            result = SandboxNewCommand().execute(_args(repo="widgetshop", api_key=True))

        assert result is None
        record = _cmd.load_registry()["test"]
        assert record.boxes["app"].forge_source == _common.ForgeSourceRecord(
            kind="gitlab",
            host="gitlab.example.com",
            project="widgetshop/widgetshop",
        )


class TestLeftoverDatabasesRefusal:
    """Pre-existing databases are refused outright, never silently adopted
    — see docs/reference.md (`new` row) and the incident it fixes: a prior
    sandbox's data carrying over into a fresh `new` with no warning."""

    def _patch_subprocess(self, monkeypatch, tmp_path):
        ok = MagicMock(returncode=0, stdout=str(tmp_path / ".git"), stderr="")
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: ok)
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        monkeypatch.setenv("STX_HOSTS_PATH", str(tmp_path / "hosts"))

    def test_refuses_and_names_the_registered_owner(
        self, monkeypatch, tmp_path, capsys
    ):
        self._patch_subprocess(monkeypatch, tmp_path)
        with _common.locked_registry() as reg:
            reg.reserve(
                "be-1",
                created="2026-10-21T00:00:00+00:00",
                redis_index=None,
                tailnet_port=None,
                route_ports={},
                services=[],
                repo="widgetshop",
                repo_root="/tmp/be-1",
                databases=("stx_test", "stx_test_test"),
            )

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.stx_platform.run_platform_up", return_value=None),
            patch(f"{_MOD}.resolve_repo_root", return_value=tmp_path),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.platform.PostgresAdminParams.build", return_value=MagicMock()),
            patch(f"{_MOD}.forge_for", return_value=_FakeForge()),
            patch(
                "stx.core.resources.existing_databases",
                return_value=["stx_test", "stx_test_test"],
            ),
        ):
            result = SandboxNewCommand().execute(_args(repo="widgetshop"))

        assert result == 1
        err = capsys.readouterr().err
        assert "stx_test" in err
        assert "be-1" in err
        assert "stx rm be-1" in err
        assert "test" not in _cmd.load_registry()

    def test_refuses_with_manual_drop_instructions_when_unregistered(
        self, monkeypatch, tmp_path, capsys
    ):
        """Mirrors the actual incident: the owning sandbox was already
        `rm`'d (successfully or not), so no registry entry claims these
        databases — the fix can't be `stx rm <slug>`."""
        self._patch_subprocess(monkeypatch, tmp_path)

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.stx_platform.run_platform_up", return_value=None),
            patch(f"{_MOD}.resolve_repo_root", return_value=tmp_path),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.platform.PostgresAdminParams.build", return_value=MagicMock()),
            patch(f"{_MOD}.forge_for", return_value=_FakeForge()),
            patch(
                "stx.core.resources.existing_databases",
                return_value=["stx_test", "stx_test_test"],
            ),
        ):
            result = SandboxNewCommand().execute(_args(repo="widgetshop"))

        assert result == 1
        err = capsys.readouterr().err
        assert "stx_test, stx_test_test" in err
        assert "no longer registered" in err
        assert "DROP DATABASE" in err
        assert "test" not in _cmd.load_registry()

    def test_no_flag_to_adopt_leftover_databases(self, monkeypatch, tmp_path):
        """There is no override flag — refusal is unconditional."""
        self._patch_subprocess(monkeypatch, tmp_path)

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.stx_platform.run_platform_up", return_value=None),
            patch(f"{_MOD}.resolve_repo_root", return_value=tmp_path),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.platform.PostgresAdminParams.build", return_value=MagicMock()),
            patch(f"{_MOD}.forge_for", return_value=_FakeForge()),
            patch(
                "stx.core.resources.existing_databases",
                return_value=["stx_test", "stx_test_test"],
            ),
        ):
            result = SandboxNewCommand().execute(_args(repo="widgetshop", replace=True))

        assert result == 1


class TestReplace:
    def _patch_subprocess(self, monkeypatch, tmp_path):
        ok = MagicMock(returncode=0, stdout=str(tmp_path / ".git"), stderr="")
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: ok)
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        monkeypatch.setenv("STX_HOSTS_PATH", str(tmp_path / "hosts"))

    def _existing(self) -> object:
        return _common.SandboxRecord(
            slug="test",
            created="2026-06-26T00:00:00+00:00",
            state=_common.SandboxState.READY,
            redis_index=None,
            tailnet_port=None,
        )

    def test_existing_slug_without_replace_points_at_replace(
        self, monkeypatch, tmp_path, capsys
    ):
        self._patch_subprocess(monkeypatch, tmp_path)

        with (
            patch(f"{_MOD}.load_registry", return_value={"test": self._existing()}),
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.resolve_repo_root", return_value=tmp_path),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch(f"{_MOD}.forge_for", return_value=_FakeForge()),
        ):
            result = SandboxNewCommand().execute(_args(repo="widgetshop"))

        assert result == 1
        assert "--replace" in capsys.readouterr().err

    def test_replace_tears_down_via_rm_with_normal_guards(self, monkeypatch, tmp_path):
        """--replace runs `stx rm` without --force, so the old sandbox's guards
        (dirty worktree, unpushed commits, handoff files) still apply — a
        blocked teardown aborts the replace rather than destroying work."""
        self._patch_subprocess(monkeypatch, tmp_path)
        captured: dict[str, object] = {}

        def _fake_rm(_self, args):
            captured["args"] = args
            return 1  # a guard blocked the teardown

        with (
            patch(f"{_MOD}.load_registry", return_value={"test": self._existing()}),
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.resolve_repo_root", return_value=tmp_path),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch(f"{_MOD}.forge_for", return_value=_FakeForge()),
            patch.object(_cmd.SandboxRmCommand, "_execute", _fake_rm),
        ):
            result = SandboxNewCommand().execute(_args(repo="widgetshop", replace=True))

        assert result == 1
        assert captured["args"].slug == "test"
        assert captured["args"].force is False


class TestSandboxNetwork:
    def _patch_subprocess(self, monkeypatch, tmp_path):
        ok = MagicMock(returncode=0, stdout=str(tmp_path / ".git"), stderr="")
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: ok)
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        monkeypatch.setenv("STX_HOSTS_PATH", str(tmp_path / "hosts"))

    def test_creates_and_connects_its_own_network(self, monkeypatch, tmp_path):
        self._patch_subprocess(monkeypatch, tmp_path)
        manifest = _test_manifest(platform=PlatformManifest(redis=True))
        connects = []
        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.stx_platform.run_platform_up", return_value=None),
            patch("stx.overlay.render", side_effect=_fake_overlay_render(tmp_path)),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.overlay.check_merged"),
            patch("stx.commands.rebuild.ensure_image_fresh", return_value=None),
            patch(f"{_MOD}.resolve_repo_root", return_value=tmp_path),
            patch(f"{_MOD}.Manifest.load", return_value=manifest),
            patch("stx.platform.PostgresAdminParams.build", return_value=MagicMock()),
            patch("stx.core.resources.existing_databases", return_value=[]),
            patch(f"{_MOD}.forge_for", return_value=_FakeForge()),
            patch("stx.core.routing.get_tailscale_hostname", return_value=None),
            patch("stx.network.ensure_network") as ensure_network,
            patch(
                "stx.network.connect",
                side_effect=lambda net, container: connects.append((net, container)),
            ),
        ):
            result = SandboxNewCommand().execute(_args(repo="widgetshop"))

        assert result is None
        ensure_network.assert_called_once_with("stx-test--net")
        assert ("stx-test--net", "stx-redis") in connects
        # Traefik always reaches the box through the host, never by joining
        # the sandbox's own network — see docs/security.md (Isolation).
        assert all(container != "stx-traefik" for _net, container in connects)

    def test_rollback_disconnects_and_removes_the_network(self, monkeypatch, tmp_path):
        self._patch_subprocess(monkeypatch, tmp_path)
        manifest = _test_manifest(platform=PlatformManifest(redis=True))
        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.stx_platform.run_platform_up", return_value=None),
            patch("stx.overlay.render", side_effect=_fake_overlay_render(tmp_path)),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.overlay.check_merged"),
            patch("stx.commands.rebuild.ensure_image_fresh", return_value=None),
            patch(f"{_MOD}.resolve_repo_root", return_value=tmp_path),
            patch(f"{_MOD}.Manifest.load", return_value=manifest),
            patch("stx.platform.PostgresAdminParams.build", return_value=MagicMock()),
            patch("stx.core.resources.existing_databases", return_value=[]),
            patch(f"{_MOD}.forge_for", return_value=_FakeForge()),
            patch("stx.core.routing.get_tailscale_hostname", return_value=None),
            patch("stx.network.ensure_network"),
            patch("stx.network.connect"),
            patch(
                "stx.lifecycle.provision._compose_up_new",
                side_effect=_cmd.StxError("boom"),
            ),
            patch("stx.network.disconnect") as disconnect,
            patch("stx.network.remove_network") as remove_network,
        ):
            result = SandboxNewCommand().execute(_args(repo="widgetshop"))

        assert result == 1
        disconnect.assert_any_call("stx-test--net", "stx-redis")
        remove_network.assert_called_once_with("stx-test--net")


class TestLinkFlow:
    def _patch_subprocess(self, monkeypatch, tmp_path):
        ok = MagicMock(returncode=0, stdout=str(tmp_path / ".git"), stderr="")
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: ok)
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        monkeypatch.setenv("STX_HOSTS_PATH", str(tmp_path / "hosts"))

    def _register_target(self, *, routes) -> None:
        with _common.locked_registry() as reg:
            reg.reserve(
                "be-1",
                created="2026-10-21T00:00:00+00:00",
                redis_index=None,
                tailnet_port=None,
                route_ports={},
                services=[],
                repo="widgetshop",
                repo_root="/tmp/be-1",
            )
            reg.finish(
                "be-1",
                {
                    "app": _common.BoxRecord(
                        zone=_common.Zone.TRUSTED, user="devuser", routes=routes
                    )
                },
            )

    def test_valid_link_persists_on_the_box_record(self, monkeypatch, tmp_path):
        self._patch_subprocess(monkeypatch, tmp_path)
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        self._register_target(routes={"api": 8080})
        from stx.manifest import LinkManifest

        manifest = _test_manifest(links={"api": LinkManifest(route="api")})

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.stx_platform.run_platform_up", return_value=None),
            patch("stx.overlay.render", side_effect=_fake_overlay_render(tmp_path)),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.overlay.check_merged"),
            patch("stx.commands.rebuild.ensure_image_fresh", return_value=None),
            patch(f"{_MOD}.resolve_repo_root", return_value=tmp_path),
            patch(f"{_MOD}.Manifest.load", return_value=manifest),
            patch("stx.platform.PostgresAdminParams.build", return_value=MagicMock()),
            patch("stx.core.resources.existing_databases", return_value=[]),
            patch(f"{_MOD}.forge_for", return_value=_FakeForge()),
            patch("stx.core.routing.get_tailscale_hostname", return_value=None),
            patch("stx.network.ensure_network"),
            patch("stx.network.connect"),
        ):
            result = SandboxNewCommand().execute(
                _args(repo="widgetshop", link=["api=be-1"])
            )

        assert result is None
        record = _cmd.load_registry()["test"]
        link = record.boxes["app"].links["api"]
        assert link.target == "be-1"
        assert link.port == 8080
        assert link.container == "stx-be-1--app"
        assert record.boxes["app"].routes == {"api": 8080}

    def test_invalid_link_refuses_before_any_change(self, monkeypatch, tmp_path):
        self._patch_subprocess(monkeypatch, tmp_path)
        manifest = _test_manifest()  # declares no `links`

        with (
            patch(f"{_MOD}.resolve_repo_root", return_value=tmp_path),
            patch(f"{_MOD}.Manifest.load", return_value=manifest),
        ):
            result = SandboxNewCommand().execute(
                _args(repo="widgetshop", link=["api=be-1"])
            )

        assert result == 1
        assert "test" not in _cmd.load_registry()


class TestExecuteSandBranchIndirection:
    def _run(self, monkeypatch, tmp_path, *, extra_args=None):
        captured_cmds: list[list[str]] = []

        def mock_run(cmd, **kwargs):
            captured_cmds.append(cmd)
            result = MagicMock()
            # Nothing pre-exists: fresh stx/<slug>, fresh target.
            result.returncode = 1 if cmd[:2] == ["git", "rev-parse"] else 0
            result.stdout = str(tmp_path / ".git")
            result.stderr = ""
            return result

        monkeypatch.setattr(subprocess, "run", mock_run)
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        monkeypatch.setenv("STX_HOSTS_PATH", str(tmp_path / "hosts"))

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.stx_platform.run_platform_up", return_value=None),
            patch("stx.overlay.render", side_effect=_fake_overlay_render(tmp_path)),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.overlay.check_merged"),
            patch("stx.commands.rebuild.ensure_image_fresh", return_value=None),
            patch(f"{_MOD}.resolve_repo_root", return_value=tmp_path),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.platform.PostgresAdminParams.build", return_value=MagicMock()),
            patch("stx.core.resources.existing_databases", return_value=[]),
            patch(f"{_MOD}.forge_for", return_value=_FakeForge()),
            patch(f"{_MOD}.resolve_branch_username", return_value="alex"),
            patch(
                "stx.core.worktree.resolve_default_base_branch",
                return_value="origin/dev",
            ),
        ):
            result = SandboxNewCommand().execute(
                _args(repo="widgetshop", **(extra_args or {}))
            )

        saved = {"registry": _cmd.load_registry()}
        return result, captured_cmds, saved

    def test_worktree_add_targets_stx_slug_not_the_feature_branch(
        self, monkeypatch, tmp_path
    ):
        _, captured_cmds, _ = self._run(monkeypatch, tmp_path)

        worktree_add_cmd = next(
            cmd for cmd in captured_cmds if cmd[:3] == ["git", "worktree", "add"]
        )
        assert worktree_add_cmd[-3:] == ["-b", "stx/test", "origin/dev"]

    def test_worktree_add_creates_it_locked(self, monkeypatch, tmp_path):
        _, captured_cmds, _ = self._run(monkeypatch, tmp_path)

        worktree_add_cmd = next(
            cmd for cmd in captured_cmds if cmd[:3] == ["git", "worktree", "add"]
        )
        assert "--lock" in worktree_add_cmd

    def test_tracking_config_synced_to_the_target_after_worktree_add(
        self, monkeypatch, tmp_path
    ):
        _, captured_cmds, _ = self._run(monkeypatch, tmp_path)

        assert ["git", "config", "branch.stx/test.remote", "origin"] in captured_cmds
        assert [
            "git",
            "config",
            "branch.stx/test.merge",
            "refs/heads/feat/alex/test",
        ] in captured_cmds

    def test_branch_config_is_marked_as_stxs_own(self, monkeypatch, tmp_path):
        """`stx prune`'s orphan sweep only touches `stx/<slug>` branch config
        it marked itself — see docs/reference.md (Branches)."""
        _, captured_cmds, _ = self._run(monkeypatch, tmp_path)

        assert [
            "git",
            "config",
            "branch.stx/test.stx",
            "true",
        ] in captured_cmds

    def test_boxrecord_branch_stays_the_target_not_the_local_ref(
        self, monkeypatch, tmp_path
    ):
        _, _, saved = self._run(monkeypatch, tmp_path)

        assert saved["registry"]["test"].boxes["app"].branch == "feat/alex/test"

    def test_explicit_branch_flag_becomes_the_tracked_target(
        self, monkeypatch, tmp_path
    ):
        _, captured_cmds, saved = self._run(
            monkeypatch, tmp_path, extra_args={"branch": "feat/navneet/existing-work"}
        )

        assert saved["registry"]["test"].boxes["app"].branch == (
            "feat/navneet/existing-work"
        )
        worktree_add_cmd = next(
            cmd for cmd in captured_cmds if cmd[:3] == ["git", "worktree", "add"]
        )
        # Local ref is still the slug-derived name, never the explicit target.
        assert worktree_add_cmd[-3:] == ["-b", "stx/test", "origin/dev"]
        assert [
            "git",
            "config",
            "branch.stx/test.merge",
            "refs/heads/feat/navneet/existing-work",
        ] in captured_cmds

    def test_warns_when_tracking_config_write_fails(
        self, monkeypatch, tmp_path, capsys
    ):
        def mock_run(cmd, **kwargs):
            result = MagicMock()
            if cmd[:2] == ["git", "config"]:
                result.returncode = 1
                result.stdout = ""
                result.stderr = "error: could not lock config file"
                return result
            result.returncode = 1 if cmd[:2] == ["git", "rev-parse"] else 0
            result.stdout = str(tmp_path / ".git")
            result.stderr = ""
            return result

        monkeypatch.setattr(subprocess, "run", mock_run)
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        monkeypatch.setenv("STX_HOSTS_PATH", str(tmp_path / "hosts"))

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.stx_platform.run_platform_up", return_value=None),
            patch("stx.overlay.render", side_effect=_fake_overlay_render(tmp_path)),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.overlay.check_merged"),
            patch("stx.commands.rebuild.ensure_image_fresh", return_value=None),
            patch(f"{_MOD}.resolve_repo_root", return_value=tmp_path),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.platform.PostgresAdminParams.build", return_value=MagicMock()),
            patch("stx.core.resources.existing_databases", return_value=[]),
            patch(f"{_MOD}.forge_for", return_value=_FakeForge()),
            patch(f"{_MOD}.resolve_branch_username", return_value="alex"),
            patch(
                "stx.core.worktree.resolve_default_base_branch",
                return_value="origin/dev",
            ),
        ):
            result = SandboxNewCommand().execute(_args(repo="widgetshop"))

        # Non-fatal: provisioning still completes despite the tracking write failing.
        assert result is None
        err = capsys.readouterr().err
        assert "warning: could not set stx/test's tracking" in err
        assert "could not lock config file" in err

    def test_explicit_base_wins_even_when_target_already_exists_on_origin(
        self, monkeypatch, tmp_path
    ):
        captured_cmds: list[list[str]] = []

        def mock_run(cmd, **kwargs):
            captured_cmds.append(cmd)
            result = MagicMock()
            if cmd[:3] == ["git", "rev-parse", "--verify"]:
                # local stx/test: absent. target (locally or on origin): present.
                result.returncode = 1 if cmd[3].startswith("refs/heads/") else 0
            else:
                result.returncode = 0
            result.stdout = str(tmp_path / ".git")
            result.stderr = ""
            return result

        monkeypatch.setattr(subprocess, "run", mock_run)
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        monkeypatch.setenv("STX_HOSTS_PATH", str(tmp_path / "hosts"))

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.stx_platform.run_platform_up", return_value=None),
            patch("stx.overlay.render", side_effect=_fake_overlay_render(tmp_path)),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.overlay.check_merged"),
            patch("stx.commands.rebuild.ensure_image_fresh", return_value=None),
            patch(f"{_MOD}.resolve_repo_root", return_value=tmp_path),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.platform.PostgresAdminParams.build", return_value=MagicMock()),
            patch("stx.core.resources.existing_databases", return_value=[]),
            patch(f"{_MOD}.forge_for", return_value=_FakeForge()),
            patch(f"{_MOD}.resolve_branch_username", return_value="alex"),
        ):
            SandboxNewCommand().execute(
                _args(repo="widgetshop", base="some/explicit-base")
            )

        worktree_add_cmd = next(
            cmd for cmd in captured_cmds if cmd[:3] == ["git", "worktree", "add"]
        )
        assert worktree_add_cmd[-3:] == ["-b", "stx/test", "some/explicit-base"]


class TestRollbackDeletesCreatedBranch:
    """A failed `new` deletes the `stx/<slug>` branch it
    created — but never one it merely reused."""

    def _patch_subprocess(self, monkeypatch, tmp_path):
        ok = MagicMock(returncode=0, stdout=str(tmp_path / ".git"), stderr="")
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: ok)
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        monkeypatch.setenv("STX_HOSTS_PATH", str(tmp_path / "hosts"))

    def test_deletes_freshly_created_branch_on_failure(self, monkeypatch, tmp_path):
        from stx.errors import StxError

        captured_cmds: list[list[str]] = []

        def mock_run(cmd, **kwargs):
            captured_cmds.append(cmd)
            result = MagicMock()
            result.returncode = 1 if cmd[:2] == ["git", "rev-parse"] else 0
            result.stdout = str(tmp_path / ".git")
            result.stderr = ""
            return result

        monkeypatch.setattr(subprocess, "run", mock_run)
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        monkeypatch.setenv("STX_HOSTS_PATH", str(tmp_path / "hosts"))

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.stx_platform.run_platform_up", return_value=None),
            patch(f"{_MOD}.resolve_repo_root", return_value=tmp_path),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.platform.PostgresAdminParams.build", return_value=MagicMock()),
            patch("stx.core.resources.existing_databases", return_value=[]),
            patch(f"{_MOD}.forge_for", return_value=_FakeForge()),
            patch(
                "stx.core.worktree.resolve_default_base_branch",
                return_value="origin/dev",
            ),
            # Fails right after the worktree/branch are created.
            patch(
                "stx.lifecycle.provision._prepare_handoff_dirs",
                side_effect=StxError("boom"),
            ),
        ):
            result = SandboxNewCommand().execute(_args(repo="widgetshop"))

        assert result == 1
        branch_delete_cmds = [
            c for c in captured_cmds if c[:3] == ["git", "branch", "-D"]
        ]
        assert branch_delete_cmds == [["git", "branch", "-D", "stx/test"]]

    def test_does_not_delete_a_reused_branch_on_failure(self, monkeypatch, tmp_path):
        from stx.errors import StxError

        captured_cmds: list[list[str]] = []

        def mock_run(cmd, **kwargs):
            captured_cmds.append(cmd)
            result = MagicMock()
            # The local stx/test branch already exists (reused).
            result.returncode = 0
            result.stdout = str(tmp_path / ".git")
            result.stderr = ""
            return result

        monkeypatch.setattr(subprocess, "run", mock_run)
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        monkeypatch.setenv("STX_HOSTS_PATH", str(tmp_path / "hosts"))

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.stx_platform.run_platform_up", return_value=None),
            patch(f"{_MOD}.resolve_repo_root", return_value=tmp_path),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.platform.PostgresAdminParams.build", return_value=MagicMock()),
            patch("stx.core.resources.existing_databases", return_value=[]),
            patch(f"{_MOD}.forge_for", return_value=_FakeForge()),
            patch(
                "stx.lifecycle.provision._prepare_handoff_dirs",
                side_effect=StxError("boom"),
            ),
        ):
            result = SandboxNewCommand().execute(_args(repo="widgetshop"))

        assert result == 1
        branch_delete_cmds = [
            c for c in captured_cmds if c[:3] == ["git", "branch", "-D"]
        ]
        assert branch_delete_cmds == []

    def test_deletes_fresh_branch_when_worktree_add_fails(self, monkeypatch, tmp_path):
        captured_cmds: list[list[str]] = []

        def mock_run(cmd, **kwargs):
            captured_cmds.append(cmd)
            result = MagicMock()
            result.returncode = 0
            result.stdout = str(tmp_path / ".git")
            result.stderr = ""
            if cmd[:2] == ["git", "rev-parse"]:
                result.returncode = 1
            if cmd[:3] == ["git", "worktree", "add"]:
                # `-b` has already created the branch by the time the path
                # check fails.
                result.returncode = 128
                result.stderr = "fatal: '/state/worktrees/test' already exists\n"
            return result

        monkeypatch.setattr(subprocess, "run", mock_run)
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        monkeypatch.setenv("STX_HOSTS_PATH", str(tmp_path / "hosts"))

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.stx_platform.run_platform_up", return_value=None),
            patch(f"{_MOD}.resolve_repo_root", return_value=tmp_path),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.platform.PostgresAdminParams.build", return_value=MagicMock()),
            patch("stx.core.resources.existing_databases", return_value=[]),
            patch(f"{_MOD}.forge_for", return_value=_FakeForge()),
            patch(
                "stx.core.worktree.resolve_default_base_branch",
                return_value="origin/dev",
            ),
        ):
            result = SandboxNewCommand().execute(_args(repo="widgetshop"))

        assert result == 1
        branch_delete_cmds = [
            c for c in captured_cmds if c[:3] == ["git", "branch", "-D"]
        ]
        assert branch_delete_cmds == [["git", "branch", "-D", "stx/test"]]


class TestTailnetPortAllocation:
    def _patch_subprocess(self, monkeypatch, tmp_path):
        ok = MagicMock(returncode=0, stdout=str(tmp_path / ".git"), stderr="")
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: ok)
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        monkeypatch.setenv("STX_HOSTS_PATH", str(tmp_path / "hosts"))

    def test_routes_without_publish_allocates_no_tailnet_port(
        self, monkeypatch, tmp_path
    ):
        self._patch_subprocess(monkeypatch, tmp_path)
        unpublished = _test_manifest(publish=None)

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.stx_platform.run_platform_up", return_value=None),
            patch("stx.overlay.render", side_effect=_fake_overlay_render(tmp_path)),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.overlay.check_merged"),
            patch("stx.commands.rebuild.ensure_image_fresh", return_value=None),
            patch(f"{_MOD}.resolve_repo_root", return_value=tmp_path),
            patch(f"{_MOD}.Manifest.load", return_value=unpublished),
            patch("stx.platform.PostgresAdminParams.build", return_value=MagicMock()),
            patch("stx.core.resources.existing_databases", return_value=[]),
            patch(f"{_MOD}.forge_for", return_value=_FakeForge()),
        ):
            result = SandboxNewCommand().execute(_args(repo="widgetshop"))

        assert result is None
        assert _cmd.load_registry()["test"].tailnet_port is None

    def test_publish_allocates_a_tailnet_port(self, monkeypatch, tmp_path):
        self._patch_subprocess(monkeypatch, tmp_path)

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.stx_platform.run_platform_up", return_value=None),
            patch("stx.overlay.render", side_effect=_fake_overlay_render(tmp_path)),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.overlay.check_merged"),
            patch("stx.commands.rebuild.ensure_image_fresh", return_value=None),
            patch(f"{_MOD}.resolve_repo_root", return_value=tmp_path),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.platform.PostgresAdminParams.build", return_value=MagicMock()),
            patch("stx.core.resources.existing_databases", return_value=[]),
            patch(f"{_MOD}.forge_for", return_value=_FakeForge()),
        ):
            result = SandboxNewCommand().execute(_args(repo="widgetshop"))

        assert result is None
        assert _cmd.load_registry()["test"].tailnet_port == 5099


class TestComposeUpOutput:
    """A box without Redis prints no "redis db None"."""

    def _patch_subprocess(self, monkeypatch, tmp_path):
        ok = MagicMock(returncode=0, stdout=str(tmp_path / ".git"), stderr="")
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: ok)
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        monkeypatch.setenv("STX_HOSTS_PATH", str(tmp_path / "hosts"))

    def test_no_redis_omits_the_redis_db_line(self, monkeypatch, tmp_path, capsys):
        self._patch_subprocess(monkeypatch, tmp_path)
        bare = _test_manifest(
            routes={}, publish=None, platform=PlatformManifest(), forge=None
        )

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.stx_platform.run_platform_up", return_value=None),
            patch("stx.overlay.render", side_effect=_fake_overlay_render(tmp_path)),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.overlay.check_merged"),
            patch("stx.commands.rebuild.ensure_image_fresh", return_value=None),
            patch(f"{_MOD}.resolve_repo_root", return_value=tmp_path),
            patch(f"{_MOD}.Manifest.load", return_value=bare),
            patch("stx.platform.PostgresAdminParams.build", return_value=MagicMock()),
            patch("stx.core.resources.existing_databases", return_value=[]),
        ):
            result = SandboxNewCommand().execute(_args(repo="widgetshop"))

        assert result is None
        out = capsys.readouterr().out
        assert "starting 'app' box" in out
        assert "redis db None" not in out
        assert "redis db" not in out

    def test_uses_the_manifest_name_not_a_hardcoded_app(
        self, monkeypatch, tmp_path, capsys
    ):
        self._patch_subprocess(monkeypatch, tmp_path)
        web = _test_manifest(
            name="web", routes={}, publish=None, platform=PlatformManifest(), forge=None
        )

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.stx_platform.run_platform_up", return_value=None),
            patch("stx.overlay.render", side_effect=_fake_overlay_render(tmp_path)),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.overlay.check_merged"),
            patch("stx.commands.rebuild.ensure_image_fresh", return_value=None),
            patch(f"{_MOD}.resolve_repo_root", return_value=tmp_path),
            patch(f"{_MOD}.Manifest.load", return_value=web),
            patch("stx.platform.PostgresAdminParams.build", return_value=MagicMock()),
            patch("stx.core.resources.existing_databases", return_value=[]),
        ):
            result = SandboxNewCommand().execute(_args(repo="widgetshop"))

        assert result is None
        out = capsys.readouterr().out
        assert "starting 'web' box" in out
        assert "starting 'app' box" not in out

    def test_with_redis_shows_the_redis_db_line(self, monkeypatch, tmp_path, capsys):
        self._patch_subprocess(monkeypatch, tmp_path)

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.stx_platform.run_platform_up", return_value=None),
            patch("stx.overlay.render", side_effect=_fake_overlay_render(tmp_path)),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.overlay.check_merged"),
            patch("stx.commands.rebuild.ensure_image_fresh", return_value=None),
            patch(f"{_MOD}.resolve_repo_root", return_value=tmp_path),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.platform.PostgresAdminParams.build", return_value=MagicMock()),
            patch("stx.core.resources.existing_databases", return_value=[]),
            patch(f"{_MOD}.forge_for", return_value=_FakeForge()),
        ):
            result = SandboxNewCommand().execute(_args(repo="widgetshop"))

        assert result is None
        assert "redis db 15" in capsys.readouterr().out


class TestWaitForSetup:
    """A setup failure/timeout must not unwind what `new` already provisioned
    — see docs/box-contract.md (step 8) and _step_wait_for_setup's docstring."""

    def _patch_subprocess(self, monkeypatch, tmp_path):
        ok = MagicMock(returncode=0, stdout=str(tmp_path / ".git"), stderr="")
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: ok)
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        monkeypatch.setenv("STX_HOSTS_PATH", str(tmp_path / "hosts"))

    def _run(self, monkeypatch, tmp_path):
        self._patch_subprocess(monkeypatch, tmp_path)
        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.stx_platform.run_platform_up", return_value=None),
            patch("stx.overlay.render", side_effect=_fake_overlay_render(tmp_path)),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.overlay.check_merged"),
            patch("stx.commands.rebuild.ensure_image_fresh", return_value=None),
            patch(f"{_MOD}.resolve_repo_root", return_value=tmp_path),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.platform.PostgresAdminParams.build", return_value=MagicMock()),
            patch("stx.core.resources.existing_databases", return_value=[]),
            patch(f"{_MOD}.forge_for", return_value=_FakeForge()),
        ):
            return SandboxNewCommand().execute(_args(repo="widgetshop"))

    def test_success_prints_setup_done(self, monkeypatch, tmp_path, capsys):
        result = self._run(monkeypatch, tmp_path)

        assert result is None
        out = capsys.readouterr().out
        assert "setup done" in out
        assert "sandbox 'test' ready" in out

    def test_failed_setup_fails_the_command_but_keeps_the_sandbox(
        self, monkeypatch, tmp_path, capsys
    ):
        monkeypatch.setattr(
            setup_status,
            "wait_for_setup",
            lambda container, box, *, timeout: _common.SetupStatus(
                state=_common.SetupState.FAILED,
                script="scripts/box-init.sh",
                exit_code=7,
            ),
        )

        result = self._run(monkeypatch, tmp_path)

        assert result == 1
        captured = capsys.readouterr()
        assert "scripts/box-init.sh" in captured.err
        assert "exit code 7" in captured.err
        # Entry instructions still print — the sandbox is left running to debug.
        assert "sandbox 'test' ready" in captured.out

    def test_timeout_fails_the_command_but_keeps_the_sandbox(
        self, monkeypatch, tmp_path, capsys
    ):
        def _raise_timeout(container, box, *, timeout):
            raise _cmd.StxError(f"timed out after {timeout:g}s waiting for setup")

        monkeypatch.setattr(setup_status, "wait_for_setup", _raise_timeout)

        result = self._run(monkeypatch, tmp_path)

        assert result == 1
        assert "timed out" in capsys.readouterr().err


class TestTraefikRoutesWarningSurfaced:
    def test_mount_mismatch_warning_is_printed(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(
            routing,
            "traefik_routes_warning",
            lambda container: f"warning: '{container}' mount mismatch",
        )

        result = TestWaitForSetup()._run(monkeypatch, tmp_path)

        assert result is None
        assert "mount mismatch" in capsys.readouterr().err


class TestShellConvenience:
    def _patch_common(self, monkeypatch, tmp_path):
        ok = MagicMock(returncode=0, stdout=str(tmp_path / ".git"), stderr="")
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: ok)
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        monkeypatch.setenv("STX_HOSTS_PATH", str(tmp_path / "hosts"))

    def test_shell_flag_execs_into_app_box_after_provisioning(
        self, monkeypatch, tmp_path
    ):
        self._patch_common(monkeypatch, tmp_path)
        exec_calls = []
        monkeypatch.setattr(_cmd.os, "execvp", lambda *a: exec_calls.append(a))

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.stx_platform.run_platform_up", return_value=None),
            patch("stx.overlay.render", side_effect=_fake_overlay_render(tmp_path)),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.overlay.check_merged"),
            patch("stx.commands.rebuild.ensure_image_fresh", return_value=None),
            patch(f"{_MOD}.resolve_repo_root", return_value=tmp_path),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.platform.PostgresAdminParams.build", return_value=MagicMock()),
            patch("stx.core.resources.existing_databases", return_value=[]),
            patch(f"{_MOD}.forge_for", return_value=_FakeForge()),
        ):
            result = SandboxNewCommand().execute(_args(repo="widgetshop", shell=True))

        # execvp is mocked to return rather than replace the process, so execute()
        # falls through to its post-exec `return 1` (never reached on the real path).
        assert result == 1
        assert len(exec_calls) == 1
        docker_cmd = exec_calls[0][1]
        assert docker_cmd[:3] == ["docker", "exec", "-it"]
        assert docker_cmd[-1] == "bash"

    def test_without_shell_flag_does_not_exec(self, monkeypatch, tmp_path):
        self._patch_common(monkeypatch, tmp_path)
        exec_calls = []
        monkeypatch.setattr(_cmd.os, "execvp", lambda *a: exec_calls.append(a))

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.stx_platform.run_platform_up", return_value=None),
            patch("stx.overlay.render", side_effect=_fake_overlay_render(tmp_path)),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.overlay.check_merged"),
            patch("stx.commands.rebuild.ensure_image_fresh", return_value=None),
            patch(f"{_MOD}.resolve_repo_root", return_value=tmp_path),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.platform.PostgresAdminParams.build", return_value=MagicMock()),
            patch("stx.core.resources.existing_databases", return_value=[]),
            patch(f"{_MOD}.forge_for", return_value=_FakeForge()),
        ):
            result = SandboxNewCommand().execute(_args(repo="widgetshop"))

        assert result is None
        assert exec_calls == []


class TestCodeConvenience:
    def _patch_common(self, monkeypatch, tmp_path):
        ok = MagicMock(returncode=0, stdout=str(tmp_path / ".git"), stderr="")
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: ok)
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        monkeypatch.setenv("STX_HOSTS_PATH", str(tmp_path / "hosts"))

    def _patch_provisioning(self, stack, tmp_path):
        stack.enter_context(patch(f"{_MOD}.secrets.source_for"))
        stack.enter_context(
            patch(f"{_MOD}.stx_platform.run_platform_up", return_value=None)
        )
        stack.enter_context(
            patch("stx.overlay.render", side_effect=_fake_overlay_render(tmp_path))
        )
        stack.enter_context(patch("stx.core.compose.compose_config", return_value={}))
        stack.enter_context(patch("stx.overlay.check_repo_compose"))
        stack.enter_context(patch("stx.overlay.check_merged"))
        stack.enter_context(
            patch("stx.commands.rebuild.ensure_image_fresh", return_value=None)
        )
        stack.enter_context(patch(f"{_MOD}.resolve_repo_root", return_value=tmp_path))
        stack.enter_context(
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest())
        )
        stack.enter_context(
            patch("stx.platform.PostgresAdminParams.build", return_value=MagicMock())
        )
        stack.enter_context(
            patch("stx.core.resources.existing_databases", return_value=[])
        )
        stack.enter_context(patch(f"{_MOD}.forge_for", return_value=_FakeForge()))

    def test_code_flag_opens_editor_after_provisioning(self, monkeypatch, tmp_path):
        self._patch_common(monkeypatch, tmp_path)
        code_calls = []
        monkeypatch.setattr(
            _cmd.SandboxCodeCommand,
            "execute",
            lambda self, args: code_calls.append(args),
        )

        with ExitStack() as stack:
            self._patch_provisioning(stack, tmp_path)
            result = SandboxNewCommand().execute(_args(repo="widgetshop", code=True))

        assert result is None
        assert len(code_calls) == 1
        assert code_calls[0].slug == "test"

    def test_without_code_flag_does_not_open_editor(self, monkeypatch, tmp_path):
        self._patch_common(monkeypatch, tmp_path)
        code_calls = []
        monkeypatch.setattr(
            _cmd.SandboxCodeCommand,
            "execute",
            lambda self, args: code_calls.append(args),
        )

        with ExitStack() as stack:
            self._patch_provisioning(stack, tmp_path)
            result = SandboxNewCommand().execute(_args(repo="widgetshop"))

        assert result is None
        assert code_calls == []

    def test_code_and_shell_together_opens_editor_then_execs_shell(
        self, monkeypatch, tmp_path
    ):
        self._patch_common(monkeypatch, tmp_path)
        call_order = []
        monkeypatch.setattr(
            _cmd.SandboxCodeCommand,
            "execute",
            lambda self, args: call_order.append("code"),
        )
        monkeypatch.setattr(_cmd.os, "execvp", lambda *a: call_order.append("shell"))

        with ExitStack() as stack:
            self._patch_provisioning(stack, tmp_path)
            result = SandboxNewCommand().execute(
                _args(repo="widgetshop", code=True, shell=True)
            )

        # execvp is mocked to return rather than replace the process, so execute()
        # falls through to its post-exec `return 1` (never reached on the real path).
        assert result == 1
        assert call_order == ["code", "shell"]


class TestHostsProvisioning:
    def _patch_common(self, monkeypatch, tmp_path):
        ok = MagicMock(returncode=0, stdout=str(tmp_path / ".git"), stderr="")
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: ok)
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        monkeypatch.setenv("STX_HOSTS_PATH", str(tmp_path / "hosts"))

    def _patch_provisioning(self, stack, tmp_path):
        stack.enter_context(patch(f"{_MOD}.secrets.source_for"))
        stack.enter_context(
            patch(f"{_MOD}.stx_platform.run_platform_up", return_value=None)
        )
        stack.enter_context(
            patch("stx.overlay.render", side_effect=_fake_overlay_render(tmp_path))
        )
        stack.enter_context(patch("stx.core.compose.compose_config", return_value={}))
        stack.enter_context(patch("stx.overlay.check_repo_compose"))
        stack.enter_context(patch("stx.overlay.check_merged"))
        stack.enter_context(
            patch("stx.commands.rebuild.ensure_image_fresh", return_value=None)
        )
        stack.enter_context(patch(f"{_MOD}.resolve_repo_root", return_value=tmp_path))
        stack.enter_context(
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest())
        )
        stack.enter_context(
            patch("stx.platform.PostgresAdminParams.build", return_value=MagicMock())
        )
        stack.enter_context(
            patch("stx.core.resources.existing_databases", return_value=[])
        )
        stack.enter_context(patch(f"{_MOD}.forge_for", return_value=_FakeForge()))

    def test_added_by_default(self, monkeypatch, tmp_path, capsys):
        self._patch_common(monkeypatch, tmp_path)
        calls = []
        monkeypatch.setattr(
            _cmd, "add_hosts_entry", lambda slug, routes: calls.append((slug, routes))
        )

        with ExitStack() as stack:
            self._patch_provisioning(stack, tmp_path)
            result = SandboxNewCommand().execute(_args(repo="widgetshop"))

        assert result is None
        assert calls == [("test", {"api": 8080})]
        assert "curl:   http://test.localhost" in capsys.readouterr().out

    def test_no_hosts_flag_skips(self, monkeypatch, tmp_path):
        self._patch_common(monkeypatch, tmp_path)
        calls = []
        monkeypatch.setattr(
            _cmd, "add_hosts_entry", lambda slug, routes: calls.append((slug, routes))
        )

        with ExitStack() as stack:
            self._patch_provisioning(stack, tmp_path)
            result = SandboxNewCommand().execute(
                _args(repo="widgetshop", no_hosts=True)
            )

        assert result is None
        assert calls == []

    def test_failure_is_a_warning_not_fatal(self, monkeypatch, tmp_path, capsys):
        self._patch_common(monkeypatch, tmp_path)

        def _raise(slug, routes):
            raise OSError("sudo failed while updating the hosts file")

        monkeypatch.setattr(_cmd, "add_hosts_entry", _raise)

        with ExitStack() as stack:
            self._patch_provisioning(stack, tmp_path)
            result = SandboxNewCommand().execute(_args(repo="widgetshop"))

        assert result is None  # non-fatal: sandbox creation still succeeds
        assert "warning: failed to add hosts entry" in capsys.readouterr().err
