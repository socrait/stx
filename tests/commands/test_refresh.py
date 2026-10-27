import argparse
from unittest.mock import MagicMock, patch

import pytest

from stx import network as _net
from stx.commands.refresh import SandboxRefreshCommand
from stx.core import (
    DEFAULT_SETUP_TIMEOUT_SECONDS,
    BoxRecord,
    ClaudeAuthMode,
    SandboxRecord,
    SandboxState,
    SetupState,
    SetupStatus,
    load_registry,
    save_registry,
    setup_status,
)
from stx.core.gitlab import GitLabTokenScope
from stx.core.registry import (
    ForgeBoxRecord,
    ForgePackageRecord,
    ForgePushRecord,
    LinkRecord,
)
from stx.errors import StxError
from stx.forge import ForgeCredential, ForgeCredentialKind, ForgeError
from stx.manifest import (
    ForgeManifest,
    Manifest,
    PackageRegistryManifest,
)
from stx.secrets import SecretsError

_MOD = "stx.commands.refresh"


@pytest.fixture(autouse=True)
def _setup_finishes_immediately(monkeypatch):
    """These tests exercise `_execute()`'s own orchestration, not the box
    runtime's setup-status record — a test that cares about that re-patches
    this to something slower/failing."""
    monkeypatch.setattr(
        setup_status,
        "wait_for_setup",
        lambda container, box, *, timeout: SetupStatus(state=SetupState.DONE),
    )


def _test_manifest(*, with_package_registry: bool = True) -> Manifest:
    return Manifest(
        schema=1,
        name="app",
        compose="compose.yaml",
        image="widgetshop-devbox",
        user="devuser",
        forge=ForgeManifest(
            kind="gitlab",
            project="widgetshop/widgetshop",
            package_registry=(
                PackageRegistryManifest(
                    group="widgetshop",
                    env={
                        "username": "UV_INDEX_GITLAB_INTERNAL_USERNAME",
                        "password": "UV_INDEX_GITLAB_INTERNAL_PASSWORD",
                    },
                )
                if with_package_registry
                else None
            ),
        ),
    )


def _make_record(
    slug: str,
    root,
    *,
    read_only: bool = False,
    redis_index: int | None = 1,
    tailnet_port: int | None = 5050,
    route_ports: dict[str, int] | None = None,
    with_package_registry_token: bool = True,
    claude_auth_mode: ClaudeAuthMode = ClaudeAuthMode.API_KEY,
    links: dict | None = None,
) -> SandboxRecord:
    return SandboxRecord(
        slug=slug,
        created="2026-06-26T00:00:00+00:00",
        state=SandboxState.READY,
        redis_index=redis_index,
        tailnet_port=tailnet_port,
        route_ports=route_ports if route_ports is not None else {},
        repo="widgetshop",
        repo_root=str(root),
        boxes={
            "app": BoxRecord(
                zone="trusted",
                user="devuser",
                branch=f"feat/alex/{slug}",
                worktree="/nonexistent/wt/my-slug",
                claude_auth_mode=claude_auth_mode,
                links=links or {},
                forge=ForgeBoxRecord(
                    kind="gitlab",
                    push=ForgePushRecord(
                        scope_id=42,
                        token_id=1,
                        scope=(
                            GitLabTokenScope.READ_REPOSITORY.value
                            if read_only
                            else GitLabTokenScope.WRITE_REPOSITORY.value
                        ),
                        expires_at="2026-07-09",
                    ),
                    package=(
                        ForgePackageRecord(
                            scope_id=43, token_id=2, expires_at="2026-07-09"
                        )
                        if with_package_registry_token
                        else None
                    ),
                ),
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


def _args(
    slug: str, *, force: bool = False, oauth: bool = False, api_key: bool = False
) -> argparse.Namespace:
    return argparse.Namespace(
        slug=slug,
        oauth=oauth,
        api_key=api_key,
        timeout=DEFAULT_SETUP_TIMEOUT_SECONDS,
        yes=False,
        force=force,
        verbose=False,
        dry_run=False,
        debug=False,
    )


def _push_credential(
    *,
    scope_id=42,
    token_id=99,
    scope="write_repository",
    secret="new-git-secret",
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
    token_id=88,
    secret="new-pkg-secret",
    username="pkg-user-2",
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
    """A `Forge` double driving refresh's mint/revoke flow without touching
    GitLab — exercises refresh.py's own logic, not GitLabForge's."""

    host = "gitlab.example.com"
    git_user = "oauth2"

    def __init__(
        self,
        *,
        push: ForgeCredential | None = None,
        package: ForgeCredential | None = None,
        mint_package_error: Exception | None = None,
    ) -> None:
        self._push = push or _push_credential()
        self._package = package
        self._mint_package_error = mint_package_error
        self.mint_push_calls: list[tuple[str, bool]] = []
        self.revoked: list[tuple[ForgeCredentialKind, int, int]] = []

    def mint_push(self, slug: str, *, read_only: bool) -> ForgeCredential:
        self.mint_push_calls.append((slug, read_only))
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


def _fake_render(tmp_path):
    """Stand in for overlay.render(): writes a real (minimal) overlay file so
    the real `json.loads(overlay_path.read_text())` call downstream (used to
    feed overlay.check_merged, itself mocked out here) doesn't hit a missing
    file."""

    def _render(*args, **kwargs):
        path = tmp_path / "overlay.json"
        path.write_text('{"services": {"app": {}}}')
        return path

    return _render


class TestValidation:
    def test_missing_sandbox_returns_1(self, capsys):
        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.load_registry", return_value={}),
        ):
            result = SandboxRefreshCommand().execute(_args("nope"))

        assert result == 1
        assert "no sandbox named" in capsys.readouterr().err

    def test_dmz_only_sandbox_returns_1(self, capsys):
        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(
                f"{_MOD}.load_registry",
                return_value={"my-slug": _make_dmz_only_record("my-slug")},
            ),
        ):
            result = SandboxRefreshCommand().execute(_args("my-slug"))

        assert result == 1
        assert "no app box" in capsys.readouterr().err

    def test_no_forge_token_is_recreatable_and_skips_the_minter_secret(self, tmp_path):
        """A box with no forge token is still recreatable (bug: refresh used
        to refuse it). Recreation requires no minter token — only re-minting
        does."""
        record = _make_record("my-slug", tmp_path)
        record.boxes["app"].forge = None
        source = _CapturingSource()
        with (
            patch(f"{_MOD}.secrets.source_for", return_value=source),
            patch(f"{_MOD}.load_registry", return_value={"my-slug": record}),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.core.compose.compose_config", return_value={}),
        ):
            SandboxRefreshCommand().execute(_args("my-slug"))

        assert source.required is not None
        assert "STX_GITLAB_MINTER_TOKEN" not in source.required

    def test_missing_repo_root_returns_1(self, capsys):
        record = _make_record("my-slug", "")
        record.repo_root = ""
        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.load_registry", return_value={"my-slug": record}),
        ):
            result = SandboxRefreshCommand().execute(_args("my-slug"))

        assert result == 1
        assert "no repo root on record" in capsys.readouterr().err


class _CapturingSource:
    def __init__(self):
        self.required: list[str] | None = None

    def ensure_loaded(self, required, *, optional=()):
        self.required = list(required)
        # Stop refresh right after capture — forge_for would otherwise try a
        # real GitLab call with no actual minter token in the environment.
        raise SecretsError(["stop-here"])


class TestRequiredSecrets:
    def test_oauth_mode_requires_oauth_token_and_minter_token(
        self, monkeypatch, tmp_path
    ):
        record = _make_record(
            "my-slug", tmp_path, claude_auth_mode=ClaudeAuthMode.OAUTH
        )
        source = _CapturingSource()

        with (
            patch(f"{_MOD}.secrets.source_for", return_value=source),
            patch(f"{_MOD}.load_registry", return_value={"my-slug": record}),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.core.compose.compose_config", return_value={}),
        ):
            result = SandboxRefreshCommand().execute(_args("my-slug"))

        assert result == 1
        assert source.required == ["STX_CLAUDE_OAUTH_TOKEN", "STX_GITLAB_MINTER_TOKEN"]

    def test_api_key_mode_requires_api_key_and_minter_token(
        self, monkeypatch, tmp_path
    ):
        record = _make_record(
            "my-slug", tmp_path, claude_auth_mode=ClaudeAuthMode.API_KEY
        )
        source = _CapturingSource()

        with (
            patch(f"{_MOD}.secrets.source_for", return_value=source),
            patch(f"{_MOD}.load_registry", return_value={"my-slug": record}),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.core.compose.compose_config", return_value={}),
        ):
            result = SandboxRefreshCommand().execute(_args("my-slug"))

        assert result == 1
        assert source.required == ["ANTHROPIC_API_KEY", "STX_GITLAB_MINTER_TOKEN"]

    def test_dmz_sandbox_also_requires_dmz_key(self, monkeypatch, tmp_path):
        record = _make_record(
            "my-slug", tmp_path, claude_auth_mode=ClaudeAuthMode.API_KEY
        )
        record.boxes["dmz"] = BoxRecord(
            zone="dmz", user="devuser", image="widgetshop-devbox"
        )
        source = _CapturingSource()

        with (
            patch(f"{_MOD}.secrets.source_for", return_value=source),
            patch(f"{_MOD}.load_registry", return_value={"my-slug": record}),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.core.compose.compose_config", return_value={}),
        ):
            result = SandboxRefreshCommand().execute(_args("my-slug"))

        assert result == 1
        assert source.required == [
            "ANTHROPIC_API_KEY",
            "STX_GITLAB_MINTER_TOKEN",
            "STX_DMZ_ANTHROPIC_API_KEY",
        ]


class TestExecuteSuccess:
    def _run(self, slug: str, record: SandboxRecord, tmp_path, *, forge=None):
        compose_run_calls = []
        fake_forge = forge or _FakeForge(package=_package_credential())

        def mock_run(cmd, **kwargs):
            compose_run_calls.append((cmd, kwargs))
            return MagicMock(returncode=0)

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.load_registry", return_value={slug: record}),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.core.routing.get_tailscale_ipv4", return_value=None),
            patch(f"{_MOD}.sandbox_live_boxes", return_value=[]),
            patch("stx.overlay.render", side_effect=_fake_render(tmp_path)),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.overlay.check_merged"),
            patch("stx.commands.rebuild.ensure_image_fresh", return_value=None),
            patch(
                f"{_MOD}.identity_provenance_summary", return_value="git identity: fake"
            ),
            patch(
                "stx.core.compose.compose_command",
                return_value=["docker", "compose", "up"],
            ),
            patch("subprocess.run", side_effect=mock_run),
            patch(f"{_MOD}.forge_for", return_value=fake_forge),
        ):
            result = SandboxRefreshCommand().execute(_args(slug))

        return result, fake_forge, compose_run_calls

    def test_recreates_container_then_revokes_old_tokens(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        record = _make_record("my-slug", tmp_path)
        save_registry({"my-slug": record})
        result, fake_forge, compose_calls = self._run("my-slug", record, tmp_path)

        assert result is None
        assert len(compose_calls) == 1
        # Old tokens revoked — never the freshly minted ones.
        assert fake_forge.revoked == [
            (ForgeCredentialKind.PUSH, 42, 1),
            (ForgeCredentialKind.PACKAGE, 43, 2),
        ]

    def test_updates_registry_with_new_token_ids(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        record = _make_record("my-slug", tmp_path)
        save_registry({"my-slug": record})
        result, *_ = self._run("my-slug", record, tmp_path)

        assert result is None
        app = load_registry()["my-slug"].boxes["app"]
        assert app.forge.push.token_id == 99
        assert app.forge.package.token_id == 88
        assert app.forge.package.scope_id == 43

    def test_updates_forge_source_from_the_current_manifest(
        self, monkeypatch, tmp_path
    ):
        """forge_source must stay in sync with whatever was actually
        rebuilt — a later `rm` revokes against this, not the one from the
        sandbox's original `new`."""
        from stx.core.registry import ForgeSourceRecord

        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        record = _make_record("my-slug", tmp_path)
        record.boxes["app"].forge_source = ForgeSourceRecord(
            kind="gitlab", host="stale.example.com", project="stale/stale"
        )
        save_registry({"my-slug": record})
        result, *_ = self._run("my-slug", record, tmp_path)

        assert result is None
        app = load_registry()["my-slug"].boxes["app"]
        assert app.forge_source == ForgeSourceRecord(
            kind="gitlab", host="gitlab.example.com", project="widgetshop/widgetshop"
        )

    def test_preserves_read_only_scope_on_remint(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        record = _make_record("my-slug", tmp_path, read_only=True)
        save_registry({"my-slug": record})
        _, fake_forge, _ = self._run("my-slug", record, tmp_path)

        assert fake_forge.mint_push_calls == [("my-slug", True)]

    def test_legacy_record_without_package_registry_token_skips_deploy_revoke(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        record = _make_record("my-slug", tmp_path, with_package_registry_token=False)
        save_registry({"my-slug": record})
        result, fake_forge, _ = self._run("my-slug", record, tmp_path)

        assert result is None
        assert fake_forge.revoked == [(ForgeCredentialKind.PUSH, 42, 1)]

    def test_recreate_passes_the_boxs_own_links_to_overlay_render(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        link = LinkRecord(
            target="be-1", route="api", port=8080, container="stx-be-1--app"
        )
        record = _make_record("my-slug", tmp_path, links={"api": link})
        save_registry({"my-slug": record})
        render_mock = MagicMock(side_effect=_fake_render(tmp_path))
        fake_forge = _FakeForge(package=_package_credential())

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.load_registry", return_value={"my-slug": record}),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.core.routing.get_tailscale_ipv4", return_value=None),
            patch(f"{_MOD}.sandbox_live_boxes", return_value=[]),
            patch("stx.overlay.render", render_mock),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.overlay.check_merged"),
            patch("stx.commands.rebuild.ensure_image_fresh", return_value=None),
            patch(
                f"{_MOD}.identity_provenance_summary", return_value="git identity: fake"
            ),
            patch(
                "stx.core.compose.compose_command",
                return_value=["docker", "compose", "up"],
            ),
            patch("subprocess.run", return_value=MagicMock(returncode=0)),
            patch(f"{_MOD}.forge_for", return_value=fake_forge),
        ):
            result = SandboxRefreshCommand().execute(_args("my-slug"))

        assert result is None
        assert render_mock.call_args.kwargs["links"] == {"api": link}

    def test_recreate_passes_the_records_own_route_ports_to_overlay_render(
        self, monkeypatch, tmp_path
    ):
        import dataclasses

        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        record = _make_record("my-slug", tmp_path, route_ports={"api": 5101})
        save_registry({"my-slug": record})
        # The manifest still declares `api`, so its already-allocated host port
        # is kept (not re-probed) and flows through to the overlay.
        manifest = dataclasses.replace(_test_manifest(), routes={"api": 8080})
        render_mock = MagicMock(side_effect=_fake_render(tmp_path))
        fake_forge = _FakeForge(package=_package_credential())

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.load_registry", return_value={"my-slug": record}),
            patch(f"{_MOD}.Manifest.load", return_value=manifest),
            patch("stx.core.routing.get_tailscale_ipv4", return_value=None),
            patch("stx.network.route_bind_address", return_value="127.0.0.1"),
            patch("stx.core.routing.write_traefik_route"),
            patch(f"{_MOD}.add_hosts_entry"),
            patch(f"{_MOD}.sandbox_live_boxes", return_value=[]),
            patch("stx.overlay.render", render_mock),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.overlay.check_build_context"),
            patch("stx.overlay.check_merged"),
            patch("stx.commands.rebuild.ensure_image_fresh", return_value=None),
            patch(
                f"{_MOD}.identity_provenance_summary", return_value="git identity: fake"
            ),
            patch(
                "stx.core.compose.compose_command",
                return_value=["docker", "compose", "up"],
            ),
            patch("subprocess.run", return_value=MagicMock(returncode=0)),
            patch(f"{_MOD}.forge_for", return_value=fake_forge),
        ):
            result = SandboxRefreshCommand().execute(_args("my-slug"))

        assert result is None
        assert render_mock.call_args.kwargs["route_ports"] == {"api": 5101}
        assert load_registry()["my-slug"].route_ports == {"api": 5101}

    def test_route_added_since_new_is_allocated_a_host_port(
        self, monkeypatch, tmp_path
    ):
        """A route added to the manifest after `new` had no host port — refresh
        must allocate one, publish it, and persist it on the entry."""
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        record = _make_record("my-slug", tmp_path, route_ports={})
        save_registry({"my-slug": record})
        import dataclasses

        manifest = dataclasses.replace(_test_manifest(), routes={"web": 3000})
        render_mock = MagicMock(side_effect=_fake_render(tmp_path))
        wrote_routes = []

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.load_registry", return_value={"my-slug": record}),
            patch(f"{_MOD}.Manifest.load", return_value=manifest),
            patch("stx.core.routing.get_tailscale_ipv4", return_value=None),
            patch(f"{_MOD}.sandbox_live_boxes", return_value=[]),
            patch(f"{_MOD}.port_bindable", return_value=True),
            patch.object(_net, "route_bind_address", return_value="127.0.0.1"),
            patch(
                "stx.core.routing.write_traefik_route",
                side_effect=lambda slug, ports: wrote_routes.append(ports),
            ),
            patch(f"{_MOD}.add_hosts_entry"),
            patch("stx.overlay.render", render_mock),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.overlay.check_build_context"),
            patch("stx.overlay.check_merged"),
            patch("stx.commands.rebuild.ensure_image_fresh", return_value=None),
            patch(
                f"{_MOD}.identity_provenance_summary", return_value="git identity: fake"
            ),
            patch(
                "stx.core.compose.compose_command",
                return_value=["docker", "compose", "up"],
            ),
            patch("subprocess.run", return_value=MagicMock(returncode=0)),
            patch(f"{_MOD}.forge_for", return_value=_FakeForge(package=None)),
        ):
            result = SandboxRefreshCommand().execute(_args("my-slug"))

        assert result is None
        allocated = render_mock.call_args.kwargs["route_ports"]
        assert "web" in allocated and 5100 <= allocated["web"] <= 5599
        # Persisted on the entry and served via Traefik.
        assert load_registry()["my-slug"].route_ports == allocated
        assert wrote_routes == [allocated]

    def test_recreate_passes_the_records_own_databases_to_overlay_render(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        record = _make_record("my-slug", tmp_path)
        record.databases = ("stx_my_slug", "stx_my_slug_test")
        save_registry({"my-slug": record})
        render_mock = MagicMock(side_effect=_fake_render(tmp_path))
        fake_forge = _FakeForge(package=_package_credential())

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.load_registry", return_value={"my-slug": record}),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.core.routing.get_tailscale_ipv4", return_value=None),
            patch(f"{_MOD}.sandbox_live_boxes", return_value=[]),
            patch("stx.overlay.render", render_mock),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.overlay.check_merged"),
            patch("stx.commands.rebuild.ensure_image_fresh", return_value=None),
            patch(
                f"{_MOD}.identity_provenance_summary", return_value="git identity: fake"
            ),
            patch(
                "stx.core.compose.compose_command",
                return_value=["docker", "compose", "up"],
            ),
            patch("subprocess.run", return_value=MagicMock(returncode=0)),
            patch(f"{_MOD}.forge_for", return_value=fake_forge),
        ):
            result = SandboxRefreshCommand().execute(_args("my-slug"))

        assert result is None
        assert render_mock.call_args.kwargs["databases"] == (
            "stx_my_slug",
            "stx_my_slug_test",
        )

    def test_recreate_resolves_the_bind_address_when_the_manifest_has_routes(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        record = _make_record("my-slug", tmp_path, route_ports={"api": 5101})
        save_registry({"my-slug": record})
        render_mock = MagicMock(side_effect=_fake_render(tmp_path))
        fake_forge = _FakeForge(package=_package_credential())
        manifest = _test_manifest()
        object.__setattr__(manifest, "routes", {"api": 8080})

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.load_registry", return_value={"my-slug": record}),
            patch(f"{_MOD}.Manifest.load", return_value=manifest),
            patch("stx.core.routing.get_tailscale_ipv4", return_value=None),
            patch(f"{_MOD}.sandbox_live_boxes", return_value=[]),
            patch("stx.overlay.render", render_mock),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.overlay.check_merged"),
            patch("stx.commands.rebuild.ensure_image_fresh", return_value=None),
            patch(
                f"{_MOD}.identity_provenance_summary", return_value="git identity: fake"
            ),
            patch(
                "stx.core.compose.compose_command",
                return_value=["docker", "compose", "up"],
            ),
            patch("subprocess.run", return_value=MagicMock(returncode=0)),
            patch(f"{_MOD}.forge_for", return_value=fake_forge),
            patch("stx.network.route_bind_address", return_value="172.20.0.1"),
        ):
            result = SandboxRefreshCommand().execute(_args("my-slug"))

        assert result is None
        assert render_mock.call_args.kwargs["route_bind_addr"] == "172.20.0.1"


class TestWaitForSetup:
    """A setup failure/timeout must not block the credential swap that
    already succeeded — it's reported after, same spirit as `new`."""

    def test_failed_setup_fails_the_command_but_tokens_still_swap(
        self, monkeypatch, tmp_path, capsys
    ):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        monkeypatch.setattr(
            setup_status,
            "wait_for_setup",
            lambda container, box, *, timeout: SetupStatus(
                state=SetupState.FAILED, script="scripts/box-init.sh", exit_code=3
            ),
        )
        record = _make_record("my-slug", tmp_path)
        save_registry({"my-slug": record})

        result, fake_forge, compose_calls = TestExecuteSuccess()._run(
            "my-slug", record, tmp_path
        )

        assert result == 1
        assert len(compose_calls) == 1
        # Old tokens still revoked — the setup outcome doesn't block it.
        assert fake_forge.revoked == [
            (ForgeCredentialKind.PUSH, 42, 1),
            (ForgeCredentialKind.PACKAGE, 43, 2),
        ]
        captured = capsys.readouterr()
        assert "scripts/box-init.sh" in captured.err
        assert "exit code 3" in captured.err
        assert "refreshed" in captured.out

    def test_timeout_fails_the_command_but_tokens_still_swap(
        self, monkeypatch, tmp_path, capsys
    ):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))

        def _raise_timeout(container, box, *, timeout):
            raise StxError(f"timed out after {timeout:g}s waiting for setup")

        monkeypatch.setattr(setup_status, "wait_for_setup", _raise_timeout)
        record = _make_record("my-slug", tmp_path)
        save_registry({"my-slug": record})

        result, fake_forge, _ = TestExecuteSuccess()._run("my-slug", record, tmp_path)

        assert result == 1
        assert fake_forge.revoked == [
            (ForgeCredentialKind.PUSH, 42, 1),
            (ForgeCredentialKind.PACKAGE, 43, 2),
        ]
        assert "timed out" in capsys.readouterr().err


class TestSwapRecordedBeforeWait:
    """The credential swap is recorded and the old tokens revoked right after
    the box is up — before the (long) setup wait — so an interrupt during the
    wait can't leave the box on fresh tokens with the registry still recording
    (and `rm` still revoking) the old ones."""

    def test_registry_records_fresh_tokens_even_when_setup_fails(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        monkeypatch.setattr(
            setup_status,
            "wait_for_setup",
            lambda container, box, *, timeout: SetupStatus(
                state=SetupState.FAILED, script="s", exit_code=1
            ),
        )
        record = _make_record("my-slug", tmp_path)
        save_registry({"my-slug": record})

        result, *_ = TestExecuteSuccess()._run("my-slug", record, tmp_path)

        assert result == 1  # setup failed
        # ...but the swap is already on disk: the box runs on fresh tokens.
        app = load_registry()["my-slug"].boxes["app"]
        assert app.forge.push.token_id == 99

    def test_old_revoke_failure_stashes_old_ids_for_rm_to_retry(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        record = _make_record("my-slug", tmp_path)
        save_registry({"my-slug": record})

        class _OldRevokeFails(_FakeForge):
            def revoke(self, kind, scope_id, token_id):
                self.revoked.append((kind, scope_id, token_id))
                return False  # revocation of the old tokens fails

        result, _forge, _ = TestExecuteSuccess()._run(
            "my-slug",
            record,
            tmp_path,
            forge=_OldRevokeFails(package=_package_credential()),
        )

        assert result is None
        reloaded = load_registry()["my-slug"]
        # Box swapped to fresh tokens; old ids kept for `stx rm` to retry.
        assert reloaded.boxes["app"].forge.push.token_id == 99
        assert reloaded.pending_forge is not None
        assert reloaded.pending_forge.push.token_id == 1

    def test_concurrent_rm_during_refresh_revokes_the_fresh_tokens(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        record = _make_record("my-slug", tmp_path)
        # Deliberately NOT saved to disk: the top-level read is patched to see
        # it, but the locked registry (reading disk) finds it gone — as if a
        # concurrent `rm` removed it mid-refresh.
        result, fake_forge, _ = TestExecuteSuccess()._run("my-slug", record, tmp_path)

        assert result == 1
        # The fresh tokens just minted are orphaned, so they're revoked.
        assert (ForgeCredentialKind.PUSH, 42, 99) in fake_forge.revoked


def test_package_registry_mint_failure_revokes_freshly_minted_git_token_and_leaves_old_ones(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
    record = _make_record("my-slug", tmp_path)
    save_registry({"my-slug": record})
    fake_forge = _FakeForge(mint_package_error=ForgeError("insufficient permissions"))

    with (
        patch(f"{_MOD}.secrets.source_for"),
        patch(f"{_MOD}.load_registry", return_value={"my-slug": record}),
        patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
        patch("stx.core.compose.compose_config", return_value={}),
        patch(f"{_MOD}.sandbox_live_boxes", return_value=[]),
        patch(f"{_MOD}.forge_for", return_value=fake_forge),
    ):
        result = SandboxRefreshCommand().execute(_args("my-slug"))

    assert result == 1
    # The just-minted git token (id 99), not the old one (id 1).
    assert fake_forge.revoked == [(ForgeCredentialKind.PUSH, 42, 99)]
    assert load_registry()["my-slug"].boxes["app"].forge.push.token_id == 1


def test_compose_recreate_failure_rolls_back_both_fresh_tokens_and_leaves_old_ones_intact(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
    record = _make_record("my-slug", tmp_path)
    save_registry({"my-slug": record})
    fake_forge = _FakeForge(package=_package_credential())

    with (
        patch(f"{_MOD}.secrets.source_for"),
        patch(f"{_MOD}.load_registry", return_value={"my-slug": record}),
        patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
        patch("stx.core.routing.get_tailscale_ipv4", return_value=None),
        patch(f"{_MOD}.sandbox_live_boxes", return_value=[]),
        patch("stx.overlay.render", side_effect=_fake_render(tmp_path)),
        patch("stx.core.compose.compose_config", return_value={}),
        patch("stx.overlay.check_repo_compose"),
        patch("stx.overlay.check_merged"),
        patch("stx.commands.rebuild.ensure_image_fresh", return_value=None),
        patch(f"{_MOD}.identity_provenance_summary", return_value="git identity: fake"),
        patch(
            "stx.core.compose.compose_command", return_value=["docker", "compose", "up"]
        ),
        patch("subprocess.run", return_value=MagicMock(returncode=1)),
        patch(f"{_MOD}.forge_for", return_value=fake_forge),
    ):
        result = SandboxRefreshCommand().execute(_args("my-slug"))

    assert result == 1
    # The freshly minted tokens (99/88) are rolled back — the old, still
    # valid ones (1/2) are never touched.
    assert fake_forge.revoked == [
        (ForgeCredentialKind.PUSH, 42, 99),
        (ForgeCredentialKind.PACKAGE, 43, 88),
    ]
    assert load_registry()["my-slug"].boxes["app"].forge.push.token_id == 1


class TestFreshTokenSafety:
    """The fresh tokens are tracked on the entry the instant they're minted —
    before the slow recreate — and revoked (keeping any whose revoke failed)
    on any failure or interrupt, so they're never leaked. The old tokens are
    revoked against the record read under the lock, not a stale snapshot."""

    def test_fresh_tokens_tracked_on_pending_before_recreate(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        record = _make_record("my-slug", tmp_path)
        save_registry({"my-slug": record})
        fake_forge = _FakeForge(package=_package_credential())
        captured = {}

        def _capture(spec, **kwargs):
            # During the (slow) recreate the fresh tokens must already be on
            # record so a crash leaves them for `rm`/`prune` to revoke.
            pending = load_registry()["my-slug"].pending_forge
            captured["push"] = pending.push.token_id
            captured["package"] = pending.package.token_id

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.load_registry", return_value={"my-slug": record}),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.core.compose.compose_config", return_value={}),
            patch(f"{_MOD}.sandbox_live_boxes", return_value=[]),
            patch(
                f"{_MOD}.identity_provenance_summary", return_value="git identity: fake"
            ),
            patch(f"{_MOD}.refresh_box", side_effect=_capture),
            patch(f"{_MOD}.forge_for", return_value=fake_forge),
        ):
            result = SandboxRefreshCommand().execute(_args("my-slug"))

        assert result is None
        assert captured == {"push": 99, "package": 88}

    def test_failed_recreate_keeps_unrevoked_fresh_ids(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        record = _make_record("my-slug", tmp_path)
        save_registry({"my-slug": record})

        class _FreshRevokeFails(_FakeForge):
            def revoke(self, kind, scope_id, token_id):
                self.revoked.append((kind, scope_id, token_id))
                return False

        fake_forge = _FreshRevokeFails(package=_package_credential())
        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.load_registry", return_value={"my-slug": record}),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.core.routing.get_tailscale_ipv4", return_value=None),
            patch(f"{_MOD}.sandbox_live_boxes", return_value=[]),
            patch("stx.overlay.render", side_effect=_fake_render(tmp_path)),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.overlay.check_merged"),
            patch("stx.commands.rebuild.ensure_image_fresh", return_value=None),
            patch(
                f"{_MOD}.identity_provenance_summary", return_value="git identity: fake"
            ),
            patch(
                "stx.core.compose.compose_command",
                return_value=["docker", "compose", "up"],
            ),
            patch("subprocess.run", return_value=MagicMock(returncode=1)),
            patch(f"{_MOD}.forge_for", return_value=fake_forge),
        ):
            result = SandboxRefreshCommand().execute(_args("my-slug"))

        assert result == 1
        reloaded = load_registry()["my-slug"]
        # The fresh tokens, un-revoked, stay on record for `stx rm` to retry.
        assert reloaded.pending_forge.push.token_id == 99
        assert reloaded.pending_forge.package.token_id == 88
        # The box was never swapped — it still runs on the old tokens.
        assert reloaded.boxes["app"].forge.push.token_id == 1

    def test_revokes_forge_record_read_under_the_lock_not_stale_snapshot(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        # The snapshot this refresh read at the top records token id 1...
        stale = _make_record("my-slug", tmp_path)
        # ...but on disk a concurrent refresh already swapped the box onto a
        # newer generation (777/778).
        on_disk = _make_record("my-slug", tmp_path)
        on_disk.boxes["app"].forge.push.token_id = 777
        on_disk.boxes["app"].forge.package.token_id = 778
        save_registry({"my-slug": on_disk})
        fake_forge = _FakeForge(package=_package_credential())

        result, forge, _ = TestExecuteSuccess()._run(
            "my-slug", stale, tmp_path, forge=fake_forge
        )

        assert result is None
        # Revoked the generation read under the lock, never the stale snapshot.
        assert (ForgeCredentialKind.PUSH, 42, 777) in forge.revoked
        assert (ForgeCredentialKind.PACKAGE, 43, 778) in forge.revoked
        assert (ForgeCredentialKind.PUSH, 42, 1) not in forge.revoked

    def test_interrupt_during_recreate_revokes_fresh_and_reraises(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        record = _make_record("my-slug", tmp_path)
        save_registry({"my-slug": record})
        fake_forge = _FakeForge(package=_package_credential())

        def _interrupt(spec, **kwargs):
            raise KeyboardInterrupt

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.load_registry", return_value={"my-slug": record}),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.core.compose.compose_config", return_value={}),
            patch(f"{_MOD}.sandbox_live_boxes", return_value=[]),
            patch(
                f"{_MOD}.identity_provenance_summary", return_value="git identity: fake"
            ),
            patch(f"{_MOD}.refresh_box", side_effect=_interrupt),
            patch(f"{_MOD}.forge_for", return_value=fake_forge),
            pytest.raises(KeyboardInterrupt),
        ):
            SandboxRefreshCommand().execute(_args("my-slug"))

        # The box never swapped onto them, so the orphaned fresh tokens are
        # revoked before the process dies.
        assert fake_forge.revoked == [
            (ForgeCredentialKind.PUSH, 42, 99),
            (ForgeCredentialKind.PACKAGE, 43, 88),
        ]


class TestDmzSidecarRecreate:
    def _record_with_dmz(self, slug: str, root) -> SandboxRecord:
        record = _make_record(slug, root)
        record.boxes["dmz"] = BoxRecord(
            zone="dmz", user="devuser", image="widgetshop-devbox"
        )
        return record

    def test_overlay_rendered_with_dmz_image_and_snapshot(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        record = self._record_with_dmz("my-slug", tmp_path)
        save_registry({"my-slug": record})
        fake_forge = _FakeForge(package=_package_credential())
        render_calls = []

        def _render(*args, **kwargs):
            render_calls.append(kwargs)
            path = tmp_path / "overlay.json"
            path.write_text('{"services": {"app": {}, "dmz": {}}}')
            return path

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.load_registry", return_value={"my-slug": record}),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.core.routing.get_tailscale_ipv4", return_value=None),
            patch(f"{_MOD}.sandbox_live_boxes", return_value=[]),
            patch("stx.overlay.render", side_effect=_render),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.overlay.check_merged"),
            patch("stx.commands.rebuild.ensure_image_fresh", return_value=None),
            patch(
                f"{_MOD}.identity_provenance_summary", return_value="git identity: fake"
            ),
            patch(
                "stx.core.compose.compose_command",
                return_value=["docker", "compose", "up"],
            ),
            patch("subprocess.run", return_value=MagicMock(returncode=0)),
            patch(f"{_MOD}.forge_for", return_value=fake_forge),
        ):
            result = SandboxRefreshCommand().execute(_args("my-slug"))

        assert result is None
        assert len(render_calls) == 1
        assert render_calls[0]["dmz_image"] == "widgetshop-devbox"
        assert render_calls[0]["dmz_user"] == "devuser"
        assert render_calls[0]["dmz_snapshot"] is not None

    def test_recreates_dmz_alongside_app_box(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        record = self._record_with_dmz("my-slug", tmp_path)
        save_registry({"my-slug": record})
        fake_forge = _FakeForge(package=_package_credential())
        up_calls = []

        def mock_run(cmd, **kwargs):
            if "up" in cmd:
                up_calls.append(cmd)
            return MagicMock(returncode=0)

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.load_registry", return_value={"my-slug": record}),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.core.routing.get_tailscale_ipv4", return_value=None),
            patch(f"{_MOD}.sandbox_live_boxes", return_value=[]),
            patch("stx.overlay.render", side_effect=_fake_render(tmp_path)),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.overlay.check_merged"),
            patch("stx.commands.rebuild.ensure_image_fresh", return_value=None),
            patch(
                f"{_MOD}.identity_provenance_summary", return_value="git identity: fake"
            ),
            patch(
                "stx.core.compose.compose_command",
                side_effect=lambda files, *args: ["docker", "compose", *args],
            ),
            patch("subprocess.run", side_effect=mock_run),
            patch(f"{_MOD}.forge_for", return_value=fake_forge),
        ):
            result = SandboxRefreshCommand().execute(_args("my-slug"))

        assert result is None
        assert len(up_calls) == 1
        assert "app" in up_calls[0]
        assert "dmz" in up_calls[0]

    def test_no_dmz_sandbox_renders_without_dmz_args(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        record = _make_record("my-slug", tmp_path)
        save_registry({"my-slug": record})
        fake_forge = _FakeForge(package=_package_credential())
        render_calls = []

        def _render(*args, **kwargs):
            render_calls.append(kwargs)
            path = tmp_path / "overlay.json"
            path.write_text('{"services": {"app": {}}}')
            return path

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.load_registry", return_value={"my-slug": record}),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.core.routing.get_tailscale_ipv4", return_value=None),
            patch(f"{_MOD}.sandbox_live_boxes", return_value=[]),
            patch("stx.overlay.render", side_effect=_render),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.overlay.check_merged"),
            patch("stx.commands.rebuild.ensure_image_fresh", return_value=None),
            patch(
                f"{_MOD}.identity_provenance_summary", return_value="git identity: fake"
            ),
            patch(
                "stx.core.compose.compose_command",
                return_value=["docker", "compose", "up"],
            ),
            patch("subprocess.run", return_value=MagicMock(returncode=0)),
            patch(f"{_MOD}.forge_for", return_value=fake_forge),
        ):
            result = SandboxRefreshCommand().execute(_args("my-slug"))

        assert result is None
        assert render_calls[0]["dmz_image"] is None
        assert render_calls[0]["dmz_user"] is None
        assert render_calls[0]["dmz_snapshot"] is None


class TestRouteSync:
    """Refresh keeps the registry's route bookkeeping in lock-step with the
    manifest: a route dropped since `new` is pruned and unpublished, a route
    added reaches `BoxRecord.routes` (so `--link` to it is allowed) and gets a
    hosts entry."""

    def _run(self, record, manifest, tmp_path):
        render_mock = MagicMock(side_effect=_fake_render(tmp_path))
        wrote, removed, hosts = [], [], []
        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.load_registry", return_value={"my-slug": record}),
            patch(f"{_MOD}.Manifest.load", return_value=manifest),
            patch("stx.core.routing.get_tailscale_ipv4", return_value=None),
            patch("stx.network.route_bind_address", return_value="127.0.0.1"),
            patch(f"{_MOD}.sandbox_live_boxes", return_value=[]),
            patch(
                "stx.core.routing.write_traefik_route",
                side_effect=lambda slug, ports: wrote.append(ports),
            ),
            patch(
                "stx.core.routing.remove_traefik_route",
                side_effect=lambda slug: removed.append(slug),
            ),
            patch(
                f"{_MOD}.add_hosts_entry",
                side_effect=lambda slug, routes: hosts.append(routes),
            ),
            patch("stx.overlay.render", render_mock),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.overlay.check_build_context"),
            patch("stx.overlay.check_merged"),
            patch("stx.commands.rebuild.ensure_image_fresh", return_value=None),
            patch(
                f"{_MOD}.identity_provenance_summary", return_value="git identity: fake"
            ),
            patch(
                "stx.core.compose.compose_command",
                return_value=["docker", "compose", "up"],
            ),
            patch("subprocess.run", return_value=MagicMock(returncode=0)),
            patch(f"{_MOD}.forge_for", return_value=_FakeForge(package=None)),
        ):
            result = SandboxRefreshCommand().execute(_args("my-slug"))
        return result, render_mock, wrote, removed, hosts

    def test_route_dropped_from_manifest_is_pruned_and_unpublished(
        self, monkeypatch, tmp_path
    ):
        import dataclasses

        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        record = _make_record(
            "my-slug", tmp_path, route_ports={"api": 5101, "old": 5102}
        )
        save_registry({"my-slug": record})
        manifest = dataclasses.replace(_test_manifest(), routes={"api": 8080})
        result, render_mock, wrote, _removed, _hosts = self._run(
            record, manifest, tmp_path
        )

        assert result is None
        # `old` is gone; `api`'s existing host port is kept.
        assert load_registry()["my-slug"].route_ports == {"api": 5101}
        assert render_mock.call_args.kwargs["route_ports"] == {"api": 5101}
        assert wrote == [{"api": 5101}]

    def test_all_routes_dropped_removes_the_traefik_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        record = _make_record("my-slug", tmp_path, route_ports={"api": 5101})
        save_registry({"my-slug": record})
        manifest = _test_manifest()  # declares no routes
        result, _render, _wrote, removed, _hosts = self._run(record, manifest, tmp_path)

        assert result is None
        assert load_registry()["my-slug"].route_ports == {}
        assert removed == ["my-slug"]

    def test_added_route_reaches_box_routes(self, monkeypatch, tmp_path):
        import dataclasses

        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        record = _make_record("my-slug", tmp_path, route_ports={})
        save_registry({"my-slug": record})
        manifest = dataclasses.replace(_test_manifest(), routes={"web": 3000})
        result, *_ = self._run(record, manifest, tmp_path)

        assert result is None
        assert load_registry()["my-slug"].boxes["app"].routes == {"web": 3000}

    def test_added_route_gets_a_hosts_entry(self, monkeypatch, tmp_path):
        import dataclasses

        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        record = _make_record("my-slug", tmp_path, route_ports={})
        save_registry({"my-slug": record})
        manifest = dataclasses.replace(_test_manifest(), routes={"web": 3000})
        result, _render, _wrote, _removed, hosts = self._run(record, manifest, tmp_path)

        assert result is None
        assert hosts == [{"web": 3000}]


class TestWritesDeferredUntilValidation:
    """The egress allowlist and overlay are rewritten only once the box-contract
    check and image build pass — a failed refresh must leave the old box's own
    files untouched, not clobber them with a half-rendered set."""

    def test_contract_violation_does_not_rewrite_overlay_or_allowlist(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        record = _make_record("my-slug", tmp_path)
        save_registry({"my-slug": record})
        fake_forge = _FakeForge(package=_package_credential())
        render_mock = MagicMock(side_effect=_fake_render(tmp_path))
        egress_mock = MagicMock()

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.load_registry", return_value={"my-slug": record}),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.core.routing.get_tailscale_ipv4", return_value=None),
            patch(f"{_MOD}.sandbox_live_boxes", return_value=[]),
            patch("stx.overlay.render", render_mock),
            patch("stx.egress.render", egress_mock),
            patch("stx.core.compose.compose_config", return_value={}),
            patch(
                "stx.overlay.check_repo_compose",
                side_effect=StxError("compose.yaml: service 'app': key 'privileged'"),
            ),
            patch(f"{_MOD}.forge_for", return_value=fake_forge),
        ):
            result = SandboxRefreshCommand().execute(_args("my-slug"))

        assert result == 1
        render_mock.assert_not_called()
        egress_mock.assert_not_called()

    def test_image_rebuild_failure_does_not_rewrite_overlay_or_allowlist(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        record = _make_record("my-slug", tmp_path)
        save_registry({"my-slug": record})
        fake_forge = _FakeForge(package=_package_credential())
        render_mock = MagicMock(side_effect=_fake_render(tmp_path))
        egress_mock = MagicMock()

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.load_registry", return_value={"my-slug": record}),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.core.routing.get_tailscale_ipv4", return_value=None),
            patch(f"{_MOD}.sandbox_live_boxes", return_value=[]),
            patch("stx.overlay.render", render_mock),
            patch("stx.egress.render", egress_mock),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.overlay.check_build_context"),
            patch("stx.commands.rebuild.ensure_image_fresh", return_value=1),
            patch(f"{_MOD}.forge_for", return_value=fake_forge),
        ):
            result = SandboxRefreshCommand().execute(_args("my-slug"))

        assert result == 1
        render_mock.assert_not_called()
        egress_mock.assert_not_called()


class TestRoutePortsPersistedOnlyOnSuccess:
    """Newly allocated/pruned route ports are written to the registry only once
    `refresh_box` succeeds — a failed contract check or image build must leave
    the entry matching the still-running old box, so a dropped route's port
    isn't freed while the old box still binds it."""

    def test_image_rebuild_failure_leaves_route_ports_unchanged(
        self, monkeypatch, tmp_path
    ):
        import dataclasses

        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        record = _make_record(
            "my-slug", tmp_path, route_ports={"api": 5101, "old": 5102}
        )
        save_registry({"my-slug": record})
        manifest = dataclasses.replace(_test_manifest(), routes={"api": 8080})
        fake_forge = _FakeForge(package=_package_credential())

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.load_registry", return_value={"my-slug": record}),
            patch(f"{_MOD}.Manifest.load", return_value=manifest),
            patch("stx.core.routing.get_tailscale_ipv4", return_value=None),
            patch("stx.network.route_bind_address", return_value="127.0.0.1"),
            patch(f"{_MOD}.sandbox_live_boxes", return_value=[]),
            patch("stx.overlay.render", side_effect=_fake_render(tmp_path)),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.overlay.check_build_context"),
            patch("stx.commands.rebuild.ensure_image_fresh", return_value=1),
            patch(f"{_MOD}.forge_for", return_value=fake_forge),
        ):
            result = SandboxRefreshCommand().execute(_args("my-slug"))

        assert result == 1
        # The dropped route's port stays on record — the old box still binds it.
        assert load_registry()["my-slug"].route_ports == {"api": 5101, "old": 5102}

    def test_image_rebuild_failure_does_not_persist_a_newly_allocated_route(
        self, monkeypatch, tmp_path
    ):
        import dataclasses

        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        record = _make_record("my-slug", tmp_path, route_ports={})
        save_registry({"my-slug": record})
        manifest = dataclasses.replace(_test_manifest(), routes={"web": 3000})
        fake_forge = _FakeForge(package=_package_credential())

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.load_registry", return_value={"my-slug": record}),
            patch(f"{_MOD}.Manifest.load", return_value=manifest),
            patch("stx.core.routing.get_tailscale_ipv4", return_value=None),
            patch("stx.network.route_bind_address", return_value="127.0.0.1"),
            patch(f"{_MOD}.port_bindable", return_value=True),
            patch(f"{_MOD}.sandbox_live_boxes", return_value=[]),
            patch("stx.overlay.render", side_effect=_fake_render(tmp_path)),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.overlay.check_build_context"),
            patch("stx.commands.rebuild.ensure_image_fresh", return_value=1),
            patch(f"{_MOD}.forge_for", return_value=fake_forge),
        ):
            result = SandboxRefreshCommand().execute(_args("my-slug"))

        assert result == 1
        assert load_registry()["my-slug"].route_ports == {}


class TestRevalidatesBoxContractBeforeRecreating:
    def test_repo_compose_allow_list_violation_blocks_refresh(
        self, monkeypatch, tmp_path, capsys
    ):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        record = _make_record("my-slug", tmp_path)
        save_registry({"my-slug": record})
        fake_forge = _FakeForge(package=_package_credential())

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.load_registry", return_value={"my-slug": record}),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.core.routing.get_tailscale_ipv4", return_value=None),
            patch(f"{_MOD}.sandbox_live_boxes", return_value=[]),
            patch("stx.overlay.render", side_effect=_fake_render(tmp_path)),
            patch("stx.core.compose.compose_config", return_value={}),
            patch(
                "stx.overlay.check_repo_compose",
                side_effect=StxError("compose.yaml: service 'app': key 'privileged'"),
            ),
            patch(f"{_MOD}.forge_for", return_value=fake_forge),
        ):
            result = SandboxRefreshCommand().execute(_args("my-slug"))

        assert result == 1
        assert "privileged" in capsys.readouterr().err
        # The freshly minted tokens must be revoked — never left dangling.
        assert fake_forge.revoked == [
            (ForgeCredentialKind.PUSH, 42, 99),
            (ForgeCredentialKind.PACKAGE, 43, 88),
        ]

    def test_stale_image_rebuild_failure_revokes_and_returns(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        record = _make_record("my-slug", tmp_path)
        save_registry({"my-slug": record})
        fake_forge = _FakeForge(package=_package_credential())

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.load_registry", return_value={"my-slug": record}),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.core.routing.get_tailscale_ipv4", return_value=None),
            patch(f"{_MOD}.sandbox_live_boxes", return_value=[]),
            patch("stx.overlay.render", side_effect=_fake_render(tmp_path)),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.commands.rebuild.ensure_image_fresh", return_value=1),
            patch(f"{_MOD}.forge_for", return_value=fake_forge),
        ):
            result = SandboxRefreshCommand().execute(_args("my-slug"))

        assert result == 1
        assert fake_forge.revoked == [
            (ForgeCredentialKind.PUSH, 42, 99),
            (ForgeCredentialKind.PACKAGE, 43, 88),
        ]


class TestAuthModeSwitch:
    def _run(
        self, slug: str, record: SandboxRecord, args: argparse.Namespace, tmp_path
    ):
        compose_run_calls = []
        fake_forge = _FakeForge(package=_package_credential())

        def mock_run(cmd, **kwargs):
            compose_run_calls.append((cmd, kwargs))
            return MagicMock(returncode=0)

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.load_registry", return_value={slug: record}),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.core.routing.get_tailscale_ipv4", return_value=None),
            patch(f"{_MOD}.sandbox_live_boxes", return_value=[]),
            patch("stx.overlay.render", side_effect=_fake_render(tmp_path)),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.overlay.check_merged"),
            patch("stx.commands.rebuild.ensure_image_fresh", return_value=None),
            patch(
                f"{_MOD}.identity_provenance_summary", return_value="git identity: fake"
            ),
            patch(
                "stx.core.compose.compose_command",
                return_value=["docker", "compose", "up"],
            ),
            patch("subprocess.run", side_effect=mock_run),
            patch(f"{_MOD}.forge_for", return_value=fake_forge),
        ):
            result = SandboxRefreshCommand().execute(args)

        return result, compose_run_calls

    def test_oauth_flag_without_token_returns_1(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("STX_CONFIG_PATH", str(tmp_path / "config.toml"))
        monkeypatch.delenv("STX_CLAUDE_OAUTH_TOKEN", raising=False)
        record = _make_record(
            "my-slug", tmp_path, claude_auth_mode=ClaudeAuthMode.API_KEY
        )
        fake_forge = _FakeForge()

        with (
            patch(f"{_MOD}.load_registry", return_value={"my-slug": record}),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.core.compose.compose_config", return_value={}),
            patch(f"{_MOD}.forge_for", return_value=fake_forge),
        ):
            result = SandboxRefreshCommand().execute(_args("my-slug", oauth=True))

        assert result == 1
        assert "STX_CLAUDE_OAUTH_TOKEN" in capsys.readouterr().err
        assert fake_forge.mint_push_calls == []

    def test_oauth_flag_switches_mode_and_persists_after_success(
        self, monkeypatch, tmp_path, capsys
    ):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        monkeypatch.setenv("STX_CLAUDE_OAUTH_TOKEN", "oauth-fake")
        record = _make_record(
            "my-slug", tmp_path, claude_auth_mode=ClaudeAuthMode.API_KEY
        )
        save_registry({"my-slug": record})

        result, compose_calls = self._run(
            "my-slug", record, _args("my-slug", oauth=True), tmp_path
        )

        assert result is None
        assert len(compose_calls) == 1
        assert (
            load_registry()["my-slug"].boxes["app"].claude_auth_mode
            == ClaudeAuthMode.OAUTH
        )
        assert "switching Claude auth to oauth" in capsys.readouterr().out

    def test_api_key_flag_switches_from_oauth(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        monkeypatch.setenv("STX_CLAUDE_OAUTH_TOKEN", "oauth-fake")
        record = _make_record(
            "my-slug", tmp_path, claude_auth_mode=ClaudeAuthMode.OAUTH
        )
        save_registry({"my-slug": record})

        result, _ = self._run(
            "my-slug", record, _args("my-slug", api_key=True), tmp_path
        )

        assert result is None
        assert (
            load_registry()["my-slug"].boxes["app"].claude_auth_mode
            == ClaudeAuthMode.API_KEY
        )

    def test_no_flag_preserves_existing_oauth_mode_and_requires_token(
        self, monkeypatch, tmp_path, capsys
    ):
        monkeypatch.setenv("STX_CONFIG_PATH", str(tmp_path / "config.toml"))
        monkeypatch.delenv("STX_CLAUDE_OAUTH_TOKEN", raising=False)
        record = _make_record(
            "my-slug", tmp_path, claude_auth_mode=ClaudeAuthMode.OAUTH
        )
        fake_forge = _FakeForge()

        with (
            patch(f"{_MOD}.load_registry", return_value={"my-slug": record}),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.core.compose.compose_config", return_value={}),
            patch(f"{_MOD}.forge_for", return_value=fake_forge),
        ):
            result = SandboxRefreshCommand().execute(_args("my-slug"))

        assert result == 1
        assert "STX_CLAUDE_OAUTH_TOKEN" in capsys.readouterr().err
        assert fake_forge.mint_push_calls == []

    def test_no_flag_preserves_existing_oauth_mode_with_token_present(
        self, monkeypatch, tmp_path, capsys
    ):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        monkeypatch.setenv("STX_CLAUDE_OAUTH_TOKEN", "oauth-fake")
        record = _make_record(
            "my-slug", tmp_path, claude_auth_mode=ClaudeAuthMode.OAUTH
        )
        save_registry({"my-slug": record})

        result, _ = self._run("my-slug", record, _args("my-slug"), tmp_path)

        assert result is None
        assert (
            load_registry()["my-slug"].boxes["app"].claude_auth_mode
            == ClaudeAuthMode.OAUTH
        )
        assert "switching Claude auth" not in capsys.readouterr().out

    def test_failed_recreate_does_not_persist_mode_switch(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        monkeypatch.setenv("STX_CLAUDE_OAUTH_TOKEN", "oauth-fake")
        record = _make_record(
            "my-slug", tmp_path, claude_auth_mode=ClaudeAuthMode.API_KEY
        )
        save_registry({"my-slug": record})
        fake_forge = _FakeForge(package=_package_credential())

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.load_registry", return_value={"my-slug": record}),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.core.routing.get_tailscale_ipv4", return_value=None),
            patch(f"{_MOD}.sandbox_live_boxes", return_value=[]),
            patch("stx.overlay.render", side_effect=_fake_render(tmp_path)),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.overlay.check_merged"),
            patch("stx.commands.rebuild.ensure_image_fresh", return_value=None),
            patch(
                f"{_MOD}.identity_provenance_summary", return_value="git identity: fake"
            ),
            patch(
                "stx.core.compose.compose_command",
                return_value=["docker", "compose", "up"],
            ),
            patch("subprocess.run", return_value=MagicMock(returncode=1)),
            patch(f"{_MOD}.forge_for", return_value=fake_forge),
        ):
            result = SandboxRefreshCommand().execute(_args("my-slug", oauth=True))

        assert result == 1
        assert (
            load_registry()["my-slug"].boxes["app"].claude_auth_mode
            == ClaudeAuthMode.API_KEY
        )


class TestAgentInProgressGuard:
    def test_blocks_without_force_when_agent_alive(self, tmp_path, capsys):
        record = _make_record("my-slug", tmp_path)
        fake_forge = _FakeForge()

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.load_registry", return_value={"my-slug": record}),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.core.compose.compose_config", return_value={}),
            patch(f"{_MOD}.sandbox_live_boxes", return_value=["app"]),
            patch(f"{_MOD}.forge_for", return_value=fake_forge),
        ):
            result = SandboxRefreshCommand().execute(_args("my-slug"))

        assert result == 1
        assert "agent dispatch in progress" in capsys.readouterr().err
        assert fake_forge.mint_push_calls == []

    def test_allows_with_force_and_warns(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        record = _make_record("my-slug", tmp_path)
        save_registry({"my-slug": record})
        fake_forge = _FakeForge(package=_package_credential())

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.load_registry", return_value={"my-slug": record}),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.core.routing.get_tailscale_ipv4", return_value=None),
            patch(f"{_MOD}.sandbox_live_boxes", return_value=["app"]),
            patch("stx.overlay.render", side_effect=_fake_render(tmp_path)),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.overlay.check_merged"),
            patch("stx.commands.rebuild.ensure_image_fresh", return_value=None),
            patch(
                f"{_MOD}.identity_provenance_summary", return_value="git identity: fake"
            ),
            patch(
                "stx.core.compose.compose_command",
                return_value=["docker", "compose", "up"],
            ),
            patch("subprocess.run", return_value=MagicMock(returncode=0)),
            patch(f"{_MOD}.forge_for", return_value=fake_forge),
        ):
            result = SandboxRefreshCommand().execute(_args("my-slug", force=True))

        assert result is None
        out = capsys.readouterr().out
        assert "in its app box" in out
        assert "recreating anyway, killing it" in out
        assert len(fake_forge.mint_push_calls) == 1

    def test_proceeds_normally_when_dispatch_already_finished(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        record = _make_record("my-slug", tmp_path)
        save_registry({"my-slug": record})
        fake_forge = _FakeForge(package=_package_credential())

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.load_registry", return_value={"my-slug": record}),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.core.routing.get_tailscale_ipv4", return_value=None),
            patch(f"{_MOD}.sandbox_live_boxes", return_value=[]),
            patch("stx.overlay.render", side_effect=_fake_render(tmp_path)),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.overlay.check_merged"),
            patch("stx.commands.rebuild.ensure_image_fresh", return_value=None),
            patch(
                f"{_MOD}.identity_provenance_summary", return_value="git identity: fake"
            ),
            patch(
                "stx.core.compose.compose_command",
                return_value=["docker", "compose", "up"],
            ),
            patch("subprocess.run", return_value=MagicMock(returncode=0)),
            patch(f"{_MOD}.forge_for", return_value=fake_forge),
        ):
            result = SandboxRefreshCommand().execute(_args("my-slug"))

        assert result is None
        assert len(fake_forge.mint_push_calls) == 1

    def _record_with_dmz(self, slug: str, root) -> SandboxRecord:
        record = _make_record(slug, root)
        record.boxes["dmz"] = BoxRecord(
            zone="dmz", user="devuser", image="widgetshop-devbox"
        )
        return record

    def test_blocks_without_force_when_dmz_agent_alive(self, tmp_path, capsys):
        record = self._record_with_dmz("my-slug", tmp_path)
        fake_forge = _FakeForge()

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.load_registry", return_value={"my-slug": record}),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.core.compose.compose_config", return_value={}),
            patch(f"{_MOD}.sandbox_live_boxes", return_value=["dmz"]),
            patch(f"{_MOD}.forge_for", return_value=fake_forge),
        ):
            result = SandboxRefreshCommand().execute(_args("my-slug"))

        assert result == 1
        err = capsys.readouterr().err
        assert "agent dispatch in progress" in err
        assert "dmz" in err
        assert fake_forge.mint_push_calls == []

    def test_allows_with_force_when_dmz_agent_alive_and_warns(
        self, monkeypatch, tmp_path, capsys
    ):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        record = self._record_with_dmz("my-slug", tmp_path)
        save_registry({"my-slug": record})
        fake_forge = _FakeForge(package=_package_credential())

        with (
            patch(f"{_MOD}.secrets.source_for"),
            patch(f"{_MOD}.load_registry", return_value={"my-slug": record}),
            patch(f"{_MOD}.Manifest.load", return_value=_test_manifest()),
            patch("stx.core.routing.get_tailscale_ipv4", return_value=None),
            patch(f"{_MOD}.sandbox_live_boxes", return_value=["dmz"]),
            patch("stx.overlay.render", side_effect=_fake_render(tmp_path)),
            patch("stx.core.compose.compose_config", return_value={}),
            patch("stx.overlay.check_repo_compose"),
            patch("stx.overlay.check_merged"),
            patch("stx.commands.rebuild.ensure_image_fresh", return_value=None),
            patch(
                f"{_MOD}.identity_provenance_summary", return_value="git identity: fake"
            ),
            patch(
                "stx.core.compose.compose_command",
                return_value=["docker", "compose", "up"],
            ),
            patch("subprocess.run", return_value=MagicMock(returncode=0)),
            patch(f"{_MOD}.forge_for", return_value=fake_forge),
        ):
            result = SandboxRefreshCommand().execute(_args("my-slug", force=True))

        assert result is None
        out = capsys.readouterr().out
        assert "in its dmz box" in out
        assert "recreating anyway, killing it" in out
        assert len(fake_forge.mint_push_calls) == 1
