import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from stx.core.gitlab import (
    ApiResponse,
    GitLabAccessLevel,
    GitLabClient,
    GitLabDeployTokenScope,
    GitLabError,
    GitLabParams,
    GitLabTokenScope,
    MintedDeployToken,
    MintedToken,
    _GitLabConnectionError,
    _urllib_transport,
)

MINTER_TOKEN = "glpat-minter-secret"


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return self.responses.pop(0)


class ScriptedTransport:
    """Like FakeTransport, but a scripted step may also be an exception
    instance to raise instead of a response to return — for exercising the
    connection-error retry path."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


@pytest.fixture
def make_client():
    def _make(*responses: ApiResponse) -> tuple[GitLabClient, FakeTransport]:
        transport = FakeTransport(responses)
        params = GitLabParams(minter_token=MINTER_TOKEN)
        return (
            GitLabClient(params, transport=transport, sleep=lambda seconds: None),
            transport,
        )

    return _make


def _mint_response(**overrides) -> ApiResponse:
    body = {
        "id": 25329242,
        "token": "glpat-minted-secret",
        "name": "stx-payments",
        "scopes": ["read_api", "write_repository"],
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
        "token": "gldt-minted-secret",
        "name": "stx-packages",
        "username": "gitlab+deploy-token-9182736",
        "scopes": ["read_package_registry"],
        "revoked": False,
        "expired": False,
        "expires_at": "2026-07-05",
    }
    body.update(overrides)
    return ApiResponse(status=201, body=body)


def test_gitlab_params_repr_hides_minter_token():
    params = GitLabParams(minter_token=MINTER_TOKEN)

    assert MINTER_TOKEN not in repr(params)


class TestResolveProjectId:
    def test_returns_id(self, make_client):
        client, transport = make_client(
            ApiResponse(status=200, body={"id": 66319962, "path": "widgetshop"})
        )

        assert client.resolve_project_id("widgetshop/widgetshop") == 66319962

        request = transport.requests[0]
        assert request.get_method() == "GET"
        assert "/projects/widgetshop%2Fwidgetshop" in request.full_url
        assert request.get_header("Private-token") == MINTER_TOKEN

    def test_raises_on_not_found(self, make_client):
        client, _ = make_client(
            ApiResponse(status=404, body={"message": "404 Project Not Found"})
        )

        with pytest.raises(GitLabError, match="HTTP 404") as exc_info:
            client.resolve_project_id("widgetshop/nonexistent")
        assert exc_info.value.status == 404


class TestResolveGroupId:
    def test_returns_id(self, make_client):
        client, transport = make_client(
            ApiResponse(status=200, body={"id": 98849308, "path": "widgetshop"})
        )

        assert client.resolve_group_id("widgetshop") == 98849308

        request = transport.requests[0]
        assert request.get_method() == "GET"
        assert "/groups/widgetshop" in request.full_url
        assert request.get_header("Private-token") == MINTER_TOKEN

    def test_raises_on_not_found(self, make_client):
        client, _ = make_client(
            ApiResponse(status=404, body={"message": "404 Group Not Found"})
        )

        with pytest.raises(GitLabError, match="HTTP 404") as exc_info:
            client.resolve_group_id("nonexistent")
        assert exc_info.value.status == 404


class TestMintProjectToken:
    def test_returns_typed_token(self, make_client):
        client, _ = make_client(_mint_response())

        token = client.mint_project_token(
            66319962,
            name="stx-payments",
            scopes=[GitLabTokenScope.READ_API, GitLabTokenScope.WRITE_REPOSITORY],
        )

        assert token == MintedToken(
            token_id=25329242,
            secret="glpat-minted-secret",
            name="stx-payments",
            scopes=(GitLabTokenScope.READ_API, GitLabTokenScope.WRITE_REPOSITORY),
            expires_at=datetime.now(timezone.utc).date() + timedelta(days=2),
            bot_user_id=39957223,
        )

    def test_sends_expected_request(self, make_client):
        client, transport = make_client(_mint_response())

        client.mint_project_token(
            66319962,
            name="stx-payments",
            scopes=[GitLabTokenScope.READ_REPOSITORY],
            access_level=GitLabAccessLevel.REPORTER,
            lifetime_days=3,
        )

        request = transport.requests[0]
        assert request.get_method() == "POST"
        assert "/projects/66319962/access_tokens" in request.full_url
        body = json.loads(request.data)
        assert body["name"] == "stx-payments"
        assert body["scopes"] == ["read_repository"]
        assert body["access_level"] == 20
        expected = datetime.now(timezone.utc).date() + timedelta(days=3)
        assert body["expires_at"] == expected.isoformat()

    def test_raises_when_inactive_at_birth(self, make_client):
        client, _ = make_client(_mint_response(active=False))

        with pytest.raises(GitLabError, match="inactive at creation"):
            client.mint_project_token(
                66319962, name="sand-x", scopes=[GitLabTokenScope.READ_API]
            )

    def test_raises_when_secret_missing(self, make_client):
        client, _ = make_client(_mint_response(token=""))

        with pytest.raises(GitLabError, match="no secret"):
            client.mint_project_token(
                66319962, name="sand-x", scopes=[GitLabTokenScope.READ_API]
            )

    def test_raises_on_api_error_with_detail(self, make_client):
        client, _ = make_client(
            ApiResponse(status=403, body={"message": "403 Forbidden"})
        )

        with pytest.raises(GitLabError, match="403 Forbidden") as exc_info:
            client.mint_project_token(
                66319962, name="sand-x", scopes=[GitLabTokenScope.READ_API]
            )
        assert exc_info.value.status == 403

    def test_rejects_nonpositive_lifetime(self, make_client):
        client, _ = make_client()

        with pytest.raises(ValueError, match="lifetime_days"):
            client.mint_project_token(
                66319962,
                name="sand-x",
                scopes=[GitLabTokenScope.READ_API],
                lifetime_days=0,
            )

    def test_repr_hides_secret(self, make_client):
        client, _ = make_client(_mint_response())

        token = client.mint_project_token(
            66319962, name="sand-x", scopes=[GitLabTokenScope.READ_API]
        )

        assert token.secret not in repr(token)


class TestRevokeProjectToken:
    def test_revokes(self, make_client):
        client, transport = make_client(ApiResponse(status=204, body=None))

        client.revoke_project_token(66319962, 25329242)

        request = transport.requests[0]
        assert request.get_method() == "DELETE"
        assert "/projects/66319962/access_tokens/25329242" in request.full_url

    def test_tolerates_already_gone(self, make_client):
        client, _ = make_client(ApiResponse(status=404, body=None))

        client.revoke_project_token(66319962, 25329242)  # no error

    def test_raises_on_server_error(self, make_client):
        # DELETE is idempotent and retried 3 times on a 5xx before giving up.
        client, _ = make_client(*[ApiResponse(status=500, body=None)] * 3)

        with pytest.raises(GitLabError, match="HTTP 500"):
            client.revoke_project_token(66319962, 25329242)


class TestMintGroupDeployToken:
    def test_returns_typed_token(self, make_client):
        client, _ = make_client(_deploy_mint_response())

        token = client.mint_group_deploy_token(
            98849308,
            name="stx-packages",
            scopes=[GitLabDeployTokenScope.READ_PACKAGE_REGISTRY],
        )

        assert token == MintedDeployToken(
            token_id=9182736,
            secret="gldt-minted-secret",
            name="stx-packages",
            username="gitlab+deploy-token-9182736",
            scopes=(GitLabDeployTokenScope.READ_PACKAGE_REGISTRY,),
            expires_at=datetime.now(timezone.utc).date() + timedelta(days=2),
        )

    def test_sends_expected_request(self, make_client):
        client, transport = make_client(_deploy_mint_response())

        client.mint_group_deploy_token(
            98849308,
            name="stx-packages",
            scopes=[GitLabDeployTokenScope.READ_PACKAGE_REGISTRY],
            lifetime_days=3,
        )

        request = transport.requests[0]
        assert request.get_method() == "POST"
        assert "/groups/98849308/deploy_tokens" in request.full_url
        body = json.loads(request.data)
        assert body["name"] == "stx-packages"
        assert body["scopes"] == ["read_package_registry"]
        expected = datetime.now(timezone.utc).date() + timedelta(days=3)
        assert body["expires_at"] == expected.isoformat()

    def test_raises_when_revoked_at_birth(self, make_client):
        client, _ = make_client(_deploy_mint_response(revoked=True))

        with pytest.raises(GitLabError, match="revoked/expired at creation"):
            client.mint_group_deploy_token(
                98849308,
                name="sand-x",
                scopes=[GitLabDeployTokenScope.READ_PACKAGE_REGISTRY],
            )

    def test_raises_when_expired_at_birth(self, make_client):
        client, _ = make_client(_deploy_mint_response(expired=True))

        with pytest.raises(GitLabError, match="revoked/expired at creation"):
            client.mint_group_deploy_token(
                98849308,
                name="sand-x",
                scopes=[GitLabDeployTokenScope.READ_PACKAGE_REGISTRY],
            )

    def test_raises_when_secret_missing(self, make_client):
        client, _ = make_client(_deploy_mint_response(token=""))

        with pytest.raises(GitLabError, match="no secret"):
            client.mint_group_deploy_token(
                98849308,
                name="sand-x",
                scopes=[GitLabDeployTokenScope.READ_PACKAGE_REGISTRY],
            )

    def test_raises_on_api_error_with_detail(self, make_client):
        client, _ = make_client(
            ApiResponse(status=403, body={"message": "403 Forbidden"})
        )

        with pytest.raises(GitLabError, match="403 Forbidden") as exc_info:
            client.mint_group_deploy_token(
                98849308,
                name="sand-x",
                scopes=[GitLabDeployTokenScope.READ_PACKAGE_REGISTRY],
            )
        assert exc_info.value.status == 403

    def test_rejects_nonpositive_lifetime(self, make_client):
        client, _ = make_client()

        with pytest.raises(ValueError, match="lifetime_days"):
            client.mint_group_deploy_token(
                98849308,
                name="sand-x",
                scopes=[GitLabDeployTokenScope.READ_PACKAGE_REGISTRY],
                lifetime_days=0,
            )

    def test_repr_hides_secret(self, make_client):
        client, _ = make_client(_deploy_mint_response())

        token = client.mint_group_deploy_token(
            98849308,
            name="sand-x",
            scopes=[GitLabDeployTokenScope.READ_PACKAGE_REGISTRY],
        )

        assert token.secret not in repr(token)


class TestRevokeGroupDeployToken:
    def test_revokes(self, make_client):
        client, transport = make_client(ApiResponse(status=204, body=None))

        client.revoke_group_deploy_token(98849308, 9182736)

        request = transport.requests[0]
        assert request.get_method() == "DELETE"
        assert "/groups/98849308/deploy_tokens/9182736" in request.full_url

    def test_tolerates_already_gone(self, make_client):
        client, _ = make_client(ApiResponse(status=404, body=None))

        client.revoke_group_deploy_token(98849308, 9182736)  # no error

    def test_raises_on_server_error(self, make_client):
        # DELETE is idempotent and retried 3 times on a 5xx before giving up.
        client, _ = make_client(*[ApiResponse(status=500, body=None)] * 3)

        with pytest.raises(GitLabError, match="HTTP 500"):
            client.revoke_group_deploy_token(98849308, 9182736)


class TestRequestRetry:
    def _make_client(self, steps) -> tuple[GitLabClient, ScriptedTransport, list]:
        transport = ScriptedTransport(steps)
        sleeps = []
        params = GitLabParams(minter_token=MINTER_TOKEN)
        client = GitLabClient(
            params, transport=transport, sleep=lambda seconds: sleeps.append(seconds)
        )
        return client, transport, sleeps

    def test_get_retries_5xx_then_succeeds(self):
        client, transport, sleeps = self._make_client(
            [
                ApiResponse(status=500, body=None),
                ApiResponse(status=200, body={"id": 66319962}),
            ]
        )

        assert client.resolve_project_id("widgetshop/widgetshop") == 66319962
        assert len(transport.requests) == 2
        assert sleeps == [0.5]

    def test_get_retries_connection_error_then_succeeds(self):
        client, transport, sleeps = self._make_client(
            [
                _GitLabConnectionError("GitLab API unreachable"),
                ApiResponse(status=200, body={"id": 66319962}),
            ]
        )

        assert client.resolve_project_id("widgetshop/widgetshop") == 66319962
        assert len(transport.requests) == 2
        assert sleeps == [0.5]

    def test_get_gives_up_after_three_attempts(self):
        client, transport, sleeps = self._make_client(
            [ApiResponse(status=500, body=None)] * 3
        )

        with pytest.raises(GitLabError, match="HTTP 500"):
            client.resolve_project_id("widgetshop/widgetshop")
        assert len(transport.requests) == 3
        assert sleeps == [0.5, 0.5]

    def test_get_raises_after_three_connection_errors(self):
        client, transport, sleeps = self._make_client(
            [_GitLabConnectionError("GitLab API unreachable")] * 3
        )

        with pytest.raises(_GitLabConnectionError):
            client.resolve_project_id("widgetshop/widgetshop")
        assert len(transport.requests) == 3
        assert sleeps == [0.5, 0.5]

    def test_post_never_retries_a_5xx(self):
        client, transport, sleeps = self._make_client(
            [ApiResponse(status=500, body=None)]
        )

        with pytest.raises(GitLabError, match="HTTP 500"):
            client.mint_project_token(
                66319962, name="sand-x", scopes=[GitLabTokenScope.WRITE_REPOSITORY]
            )
        assert len(transport.requests) == 1
        assert sleeps == []

    def test_post_never_retries_a_connection_error(self):
        client, transport, sleeps = self._make_client(
            [_GitLabConnectionError("GitLab API unreachable")]
        )

        with pytest.raises(_GitLabConnectionError):
            client.mint_project_token(
                66319962, name="sand-x", scopes=[GitLabTokenScope.WRITE_REPOSITORY]
            )
        assert len(transport.requests) == 1
        assert sleeps == []


class TestUrllibTransport:
    def _request(self) -> urllib.request.Request:
        return urllib.request.Request("https://gitlab.example.com/api/v4/x")

    def test_timeout_raises_gitlab_error(self, monkeypatch):
        def _raise(*args, **kwargs):
            raise TimeoutError("timed out")

        monkeypatch.setattr(urllib.request, "urlopen", _raise)

        with pytest.raises(GitLabError, match="timed out"):
            _urllib_transport(self._request())

    def test_unreachable_raises_gitlab_error(self, monkeypatch):
        def _raise(*args, **kwargs):
            raise urllib.error.URLError("no route to host")

        monkeypatch.setattr(urllib.request, "urlopen", _raise)

        with pytest.raises(GitLabError, match="unreachable"):
            _urllib_transport(self._request())

    def test_non_json_2xx_body_raises_gitlab_error(self, monkeypatch):
        class _FakeResponse:
            status = 200

            def read(self):
                return b"<html>proxy error</html>"

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **kw: _FakeResponse())

        with pytest.raises(GitLabError, match="JSON"):
            _urllib_transport(self._request())
