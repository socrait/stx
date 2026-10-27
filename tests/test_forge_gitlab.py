import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from stx.core.gitlab import ApiResponse, GitLabClient, GitLabParams
from stx.forge import ForgeCredentialKind, ForgeError
from stx.forge.gitlab import GitLabForge


def _utc_today() -> date:
    # Expiry dates are computed in UTC; a local-date comparison flips near midnight.
    return datetime.now(timezone.utc).date()


MINTER_TOKEN = "glpat-fictional-minter"


class FakeTransport:
    """Same mocking boundary as `tests/core/test_sandbox_gitlab.py` — stubs
    `urllib` via `GitLabClient`'s own transport seam, not `urllib` itself."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return self.responses.pop(0)


def _make_forge(*responses, project="widgetshop/widgetshop", package_group=None):
    transport = FakeTransport(responses)
    params = GitLabParams(minter_token=MINTER_TOKEN)
    client = GitLabClient(params, transport=transport, sleep=lambda seconds: None)
    forge = GitLabForge(
        project=project,
        package_group=package_group,
        host="gitlab.com",
        params=params,
        client=client,
    )
    return forge, transport


def _mint_response(**overrides) -> ApiResponse:
    body = {
        "id": 25329242,
        "token": "glpat-fictional-minted-secret",
        "name": "stx-widgetshop-fix",
        "scopes": ["write_repository"],
        "active": True,
        "revoked": False,
        "user_id": 39957223,
        "expires_at": "2026-07-05",
    }
    body.update(overrides)
    return ApiResponse(status=201, body=body)


def _deploy_mint_response(**overrides) -> ApiResponse:
    body = {
        "id": 9182736,
        "token": "gldt-fictional-minted-secret",
        "name": "stx-widgetshop-fix",
        "username": "gitlab+deploy-token-9182736",
        "scopes": ["read_package_registry"],
        "revoked": False,
        "expired": False,
        "expires_at": "2026-07-05",
    }
    body.update(overrides)
    return ApiResponse(status=201, body=body)


_RESOLVE_PROJECT = ApiResponse(status=200, body={"id": 66319962})
_RESOLVE_GROUP = ApiResponse(status=200, body={"id": 98849308})


class TestMintPush:
    def test_write_scoped_by_default(self):
        forge, transport = _make_forge(_RESOLVE_PROJECT, _mint_response())

        credential = forge.mint_push("widgetshop-fix", read_only=False)

        assert credential.kind is ForgeCredentialKind.PUSH
        assert credential.scope_id == 66319962
        assert credential.token_id == 25329242
        assert credential.scope == "write_repository"
        assert credential.secret == "glpat-fictional-minted-secret"
        mint_request = transport.requests[1]
        body = json.loads(mint_request.data)
        assert body["name"] == "stx-widgetshop-fix"
        assert body["scopes"] == ["write_repository"]
        assert body["access_level"] == 30  # Developer

    def test_read_only_uses_reporter_and_seven_day_lifetime(self):
        forge, transport = _make_forge(
            _RESOLVE_PROJECT, _mint_response(scopes=["read_repository"])
        )

        credential = forge.mint_push("widgetshop-fix", read_only=True)

        assert credential.scope == "read_repository"
        mint_request = transport.requests[1]
        body = json.loads(mint_request.data)
        assert body["scopes"] == ["read_repository"]
        assert body["access_level"] == 20  # Reporter

    def test_token_named_stx_slug(self):
        forge, transport = _make_forge(_RESOLVE_PROJECT, _mint_response())

        forge.mint_push("widgetshop-fix", read_only=False)

        mint_request = transport.requests[1]
        assert json.loads(mint_request.data)["name"] == "stx-widgetshop-fix"

    def test_api_failure_raises_forge_error(self):
        forge, _ = _make_forge(
            ApiResponse(status=404, body={"message": "404 Project Not Found"})
        )

        with pytest.raises(ForgeError):
            forge.mint_push("widgetshop-fix", read_only=False)


class TestMintPushExpiry:
    def test_write_scoped_expires_in_two_days(self):
        forge, _ = _make_forge(_RESOLVE_PROJECT, _mint_response())

        credential = forge.mint_push("widgetshop-fix", read_only=False)

        assert date.fromisoformat(credential.expires_at) == _utc_today() + timedelta(
            days=2
        )

    def test_read_only_expires_in_seven_days(self):
        forge, _ = _make_forge(
            _RESOLVE_PROJECT, _mint_response(scopes=["read_repository"])
        )

        credential = forge.mint_push("widgetshop-fix", read_only=True)

        assert date.fromisoformat(credential.expires_at) == _utc_today() + timedelta(
            days=7
        )


class TestMintPackage:
    def test_none_without_package_group(self):
        forge, transport = _make_forge(package_group=None)

        assert forge.mint_package("widgetshop-fix") is None
        assert transport.requests == []

    def test_mints_group_deploy_token(self):
        forge, transport = _make_forge(
            _RESOLVE_GROUP, _deploy_mint_response(), package_group="widgetshop"
        )

        credential = forge.mint_package("widgetshop-fix")

        assert credential.kind is ForgeCredentialKind.PACKAGE
        assert credential.scope_id == 98849308
        assert credential.token_id == 9182736
        assert credential.scope == "read_package_registry"
        assert credential.secret == "gldt-fictional-minted-secret"
        assert credential.username == "gitlab+deploy-token-9182736"
        assert date.fromisoformat(credential.expires_at) == _utc_today() + timedelta(
            days=2
        )
        mint_request = transport.requests[1]
        assert "/groups/98849308/deploy_tokens" in mint_request.full_url

    def test_api_failure_raises_forge_error(self):
        forge, _ = _make_forge(
            ApiResponse(status=403, body={"message": "403 Forbidden"}),
            package_group="widgetshop",
        )

        with pytest.raises(ForgeError):
            forge.mint_package("widgetshop-fix")


class TestRevoke:
    def test_push_revokes_project_token(self):
        forge, transport = _make_forge(ApiResponse(status=204, body=None))

        assert forge.revoke(ForgeCredentialKind.PUSH, 66319962, 25329242) is True

        request = transport.requests[0]
        assert request.get_method() == "DELETE"
        assert "/projects/66319962/access_tokens/25329242" in request.full_url

    def test_package_revokes_group_deploy_token(self):
        forge, transport = _make_forge(ApiResponse(status=204, body=None))

        forge.revoke(ForgeCredentialKind.PACKAGE, 98849308, 9182736)

        request = transport.requests[0]
        assert request.get_method() == "DELETE"
        assert "/groups/98849308/deploy_tokens/9182736" in request.full_url

    def test_returns_false_and_never_raises_on_failure(self, capsys):
        # DELETE is idempotent and retried 3 times on a 5xx before giving up.
        forge, _ = _make_forge(*[ApiResponse(status=500, body=None)] * 3)

        # No raise, and a False result so the caller keeps the id for a retry.
        assert forge.revoke(ForgeCredentialKind.PUSH, 66319962, 25329242) is False

        assert "warning" in capsys.readouterr().err


class TestFromManifest:
    def test_missing_minter_token_raises_forge_error(self, monkeypatch):
        monkeypatch.delenv("STX_GITLAB_MINTER_TOKEN", raising=False)
        manifest = SimpleNamespace(
            project="widgetshop/widgetshop", package_registry=None
        )

        with pytest.raises(ForgeError, match="STX_GITLAB_MINTER_TOKEN"):
            GitLabForge.from_manifest(manifest)

    def test_default_api_url_derives_gitlab_com_host(self, monkeypatch):
        monkeypatch.setenv("STX_GITLAB_MINTER_TOKEN", MINTER_TOKEN)
        monkeypatch.delenv("STX_GITLAB_API_URL", raising=False)
        manifest = SimpleNamespace(
            project="widgetshop/widgetshop", package_registry=None
        )

        forge = GitLabForge.from_manifest(manifest)

        assert forge.host == "gitlab.com"
        assert forge.git_user == "oauth2"

    def test_custom_api_url_derives_its_own_host(self, monkeypatch):
        monkeypatch.setenv("STX_GITLAB_MINTER_TOKEN", MINTER_TOKEN)
        monkeypatch.setenv(
            "STX_GITLAB_API_URL", "https://gitlab.fictional.example/api/v4"
        )
        manifest = SimpleNamespace(
            project="widgetshop/widgetshop", package_registry=None
        )

        forge = GitLabForge.from_manifest(manifest)

        assert forge.host == "gitlab.fictional.example"

    def test_package_group_read_from_manifest(self, monkeypatch):
        monkeypatch.setenv("STX_GITLAB_MINTER_TOKEN", MINTER_TOKEN)
        manifest = SimpleNamespace(
            project="widgetshop/widgetshop",
            package_registry=SimpleNamespace(group="widgetshop", env={}),
        )

        forge = GitLabForge.from_manifest(manifest)

        assert forge._package_group == "widgetshop"

    def test_no_package_registry_leaves_package_group_none(self, monkeypatch):
        monkeypatch.setenv("STX_GITLAB_MINTER_TOKEN", MINTER_TOKEN)
        manifest = SimpleNamespace(
            project="widgetshop/widgetshop", package_registry=None
        )

        forge = GitLabForge.from_manifest(manifest)

        assert forge._package_group is None

    def test_refuses_non_https_api_url(self, monkeypatch):
        monkeypatch.setenv("STX_GITLAB_MINTER_TOKEN", MINTER_TOKEN)
        monkeypatch.setenv(
            "STX_GITLAB_API_URL", "http://gitlab.fictional.example/api/v4"
        )
        manifest = SimpleNamespace(
            project="widgetshop/widgetshop", package_registry=None
        )

        with pytest.raises(ForgeError, match="https"):
            GitLabForge.from_manifest(manifest)

    def test_allows_non_https_on_localhost(self, monkeypatch):
        monkeypatch.setenv("STX_GITLAB_MINTER_TOKEN", MINTER_TOKEN)
        monkeypatch.setenv("STX_GITLAB_API_URL", "http://localhost:8080/api/v4")
        manifest = SimpleNamespace(
            project="widgetshop/widgetshop", package_registry=None
        )

        forge = GitLabForge.from_manifest(manifest)

        assert forge.host == "localhost"

    def test_refuses_non_https_on_loopback_ip(self, monkeypatch):
        # "localhost" the hostname specifically — 127.0.0.1 doesn't get the
        # same carve-out.
        monkeypatch.setenv("STX_GITLAB_MINTER_TOKEN", MINTER_TOKEN)
        monkeypatch.setenv("STX_GITLAB_API_URL", "http://127.0.0.1:8080/api/v4")
        manifest = SimpleNamespace(
            project="widgetshop/widgetshop", package_registry=None
        )

        with pytest.raises(ForgeError, match="https"):
            GitLabForge.from_manifest(manifest)


class TestFromSource:
    """Rebuilds a revoke-capable `Forge` from registry data alone (kind,
    host, project) — no manifest, and crucially no project-id/package-group
    resolution, since revoke only needs the already-minted scope/token ids.
    See `ForgeSourceRecord`/`forge_for_source`."""

    def test_missing_minter_token_raises_forge_error(self, monkeypatch):
        monkeypatch.delenv("STX_GITLAB_MINTER_TOKEN", raising=False)

        with pytest.raises(ForgeError, match="STX_GITLAB_MINTER_TOKEN"):
            GitLabForge.from_source(host="gitlab.com", project="widgetshop/widgetshop")

    def test_builds_from_host_and_project_alone(self, monkeypatch):
        monkeypatch.setenv("STX_GITLAB_MINTER_TOKEN", MINTER_TOKEN)

        forge = GitLabForge.from_source(
            host="gitlab.fictional.example", project="widgetshop/widgetshop"
        )

        assert forge.host == "gitlab.fictional.example"
        assert forge._project == "widgetshop/widgetshop"
        assert forge._package_group is None
        assert forge.git_user == "oauth2"


class TestUsername:
    def test_returns_glab_username(self, monkeypatch):
        forge, _ = _make_forge()

        def fake_run(argv, **kwargs):
            assert argv == ["glab", "config", "get", "-h", forge.host, "username"]
            return SimpleNamespace(returncode=0, stdout="fictional-user\n")

        monkeypatch.setattr("stx.forge.gitlab.subprocess.run", fake_run)

        assert forge.username() == "fictional-user"

    def test_no_prefix_stripping(self, monkeypatch):
        forge, _ = _make_forge()
        monkeypatch.setattr(
            "stx.forge.gitlab.subprocess.run",
            lambda *a, **k: SimpleNamespace(returncode=0, stdout="bot_12345\n"),
        )

        assert forge.username() == "bot_12345"

    def test_glab_absent_returns_none(self, monkeypatch):
        forge, _ = _make_forge()

        def raise_not_found(*a, **k):
            raise FileNotFoundError("glab")

        monkeypatch.setattr("stx.forge.gitlab.subprocess.run", raise_not_found)

        assert forge.username() is None

    def test_glab_no_identity_returns_none(self, monkeypatch):
        forge, _ = _make_forge()
        monkeypatch.setattr(
            "stx.forge.gitlab.subprocess.run",
            lambda *a, **k: SimpleNamespace(returncode=1, stdout=""),
        )

        assert forge.username() is None

    def test_empty_stdout_returns_none(self, monkeypatch):
        forge, _ = _make_forge()
        monkeypatch.setattr(
            "stx.forge.gitlab.subprocess.run",
            lambda *a, **k: SimpleNamespace(returncode=0, stdout="\n"),
        )

        assert forge.username() is None
