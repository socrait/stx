import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import IntEnum, StrEnum
from typing import Any

_REQUEST_TIMEOUT_SECONDS = 30

# A request is only retried if its method can't double-apply: GET reads
# nothing back and DELETE (revoke, here) is already idempotent server-side.
# POST (token minting) never retries — a retried POST could mint a second
# token.
_IDEMPOTENT_METHODS = frozenset({"GET", "DELETE"})
_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = 0.5


class GitLabError(Exception):
    """GitLab API request failed or returned an unusable result.

    status is the HTTP status code when the failure was an API response,
    None when the request never completed (network failure).
    """

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class _GitLabConnectionError(GitLabError):
    """A transport-level failure (timeout, network unreachable) — retried
    for idempotent requests. A malformed response body is a plain
    GitLabError instead, since retrying it would get the same body again."""


class GitLabTokenScope(StrEnum):
    READ_API = "read_api"
    READ_REPOSITORY = "read_repository"
    WRITE_REPOSITORY = "write_repository"


class GitLabDeployTokenScope(StrEnum):
    """Deploy tokens have their own scope vocabulary, distinct from project
    access tokens (GitLabTokenScope)."""

    READ_PACKAGE_REGISTRY = "read_package_registry"


class GitLabAccessLevel(IntEnum):
    GUEST = 10
    REPORTER = 20
    DEVELOPER = 30
    MAINTAINER = 40


@dataclass(frozen=True)
class GitLabParams:
    # repr=False: the minter credential must never surface in logs or tracebacks
    minter_token: str = field(repr=False)
    base_url: str = "https://gitlab.com/api/v4"


@dataclass(frozen=True)
class MintedToken:
    token_id: int
    # repr=False: the token secret must never surface in logs or tracebacks
    secret: str = field(repr=False)
    name: str
    scopes: tuple[GitLabTokenScope, ...]
    expires_at: date
    bot_user_id: int | None


@dataclass(frozen=True)
class MintedDeployToken:
    token_id: int
    # repr=False: the token secret must never surface in logs or tracebacks
    secret: str = field(repr=False)
    name: str
    # The username to pair with `secret` for HTTP Basic auth — deploy tokens
    # (unlike project access tokens) require their own assigned username, not
    # an arbitrary placeholder.
    username: str
    scopes: tuple[GitLabDeployTokenScope, ...]
    expires_at: date


@dataclass(frozen=True)
class ApiResponse:
    status: int
    body: Any  # decoded JSON, or None when the response had no body


HttpTransport = Callable[[urllib.request.Request], ApiResponse]


class GitLabClient:
    """Minimal GitLab API client for the sandbox token lifecycle.

    Authenticates every call with the minter credential from params. The
    transport seam exists so tests can stub HTTP without patching urllib.
    """

    def __init__(
        self,
        params: GitLabParams,
        *,
        transport: HttpTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._params = params
        self._transport = transport or _urllib_transport
        self._sleep = sleep

    def resolve_project_id(self, project_path: str) -> int:
        """Returns the numeric project id for a 'group/name' path.

        Raises GitLabError when the project is missing or the minter
        credential cannot read it.
        """
        encoded = urllib.parse.quote(project_path, safe="")
        response = self._request("GET", f"/projects/{encoded}")
        if response.status != 200:
            raise _api_error(f"cannot resolve project '{project_path}'", response)
        return int(response.body["id"])

    def resolve_group_id(self, group_path: str) -> int:
        """Returns the numeric group id for a 'group/subgroup' path.

        Raises GitLabError when the group is missing or the minter
        credential cannot read it.
        """
        encoded = urllib.parse.quote(group_path, safe="")
        response = self._request("GET", f"/groups/{encoded}")
        if response.status != 200:
            raise _api_error(f"cannot resolve group '{group_path}'", response)
        return int(response.body["id"])

    def mint_project_token(
        self,
        project_id: int,
        *,
        name: str,
        scopes: Sequence[GitLabTokenScope],
        access_level: GitLabAccessLevel = GitLabAccessLevel.DEVELOPER,
        lifetime_days: int = 2,
    ) -> MintedToken:
        """Creates a project access token and returns it with its secret —
        see docs/security.md (Credential lifetime). Raises GitLabError on
        failure, including a 201 whose token is not active."""
        if lifetime_days < 1:
            raise ValueError("lifetime_days must be >= 1")
        expires_at = datetime.now(timezone.utc).date() + timedelta(days=lifetime_days)
        response = self._request(
            "POST",
            f"/projects/{project_id}/access_tokens",
            body={
                "name": name,
                "scopes": [scope.value for scope in scopes],
                "access_level": int(access_level),
                "expires_at": expires_at.isoformat(),
            },
        )
        if response.status != 201:
            raise _api_error(f"cannot mint token '{name}'", response)
        token = response.body
        if not token.get("token"):
            raise GitLabError(
                f"mint of '{name}' returned no secret", status=response.status
            )
        if token.get("active") is not True:
            raise GitLabError(
                f"token '{name}' is inactive at creation "
                f"(expires_at={token.get('expires_at')})",
                status=response.status,
            )
        return MintedToken(
            token_id=int(token["id"]),
            secret=token["token"],
            name=token.get("name", name),
            scopes=tuple(GitLabTokenScope(scope) for scope in token.get("scopes", [])),
            expires_at=expires_at,
            bot_user_id=token.get("user_id"),
        )

    def revoke_project_token(self, project_id: int, token_id: int) -> None:
        """Revokes a project access token.

        Idempotent: an already-revoked, expired, or deleted token (404) is
        not an error, so teardown can always call this unconditionally.
        """
        response = self._request(
            "DELETE", f"/projects/{project_id}/access_tokens/{token_id}"
        )
        if response.status not in (204, 404):
            raise _api_error(f"cannot revoke token id={token_id}", response)

    def mint_group_deploy_token(
        self,
        group_id: int,
        *,
        name: str,
        scopes: Sequence[GitLabDeployTokenScope],
        lifetime_days: int = 2,
    ) -> MintedDeployToken:
        """Creates a group deploy token and returns it with its secret.
        Group-scoped, not project-scoped — see docs/manifest.md
        (`forge.package_registry.group`). Requires Maintainer/Owner on
        `group_id` for the minter credential."""
        if lifetime_days < 1:
            raise ValueError("lifetime_days must be >= 1")
        expires_at = datetime.now(timezone.utc).date() + timedelta(days=lifetime_days)
        response = self._request(
            "POST",
            f"/groups/{group_id}/deploy_tokens",
            body={
                "name": name,
                "scopes": [scope.value for scope in scopes],
                "expires_at": expires_at.isoformat(),
            },
        )
        if response.status != 201:
            raise _api_error(f"cannot mint deploy token '{name}'", response)
        token = response.body
        if not token.get("token"):
            raise GitLabError(
                f"mint of deploy token '{name}' returned no secret",
                status=response.status,
            )
        if token.get("revoked") or token.get("expired"):
            raise GitLabError(
                f"deploy token '{name}' is revoked/expired at creation "
                f"(expires_at={token.get('expires_at')})",
                status=response.status,
            )
        return MintedDeployToken(
            token_id=int(token["id"]),
            secret=token["token"],
            name=token.get("name", name),
            username=token["username"],
            scopes=tuple(
                GitLabDeployTokenScope(scope) for scope in token.get("scopes", [])
            ),
            expires_at=expires_at,
        )

    def revoke_group_deploy_token(self, group_id: int, token_id: int) -> None:
        """Revokes a group deploy token.

        Idempotent: an already-deleted token (404) is not an error, so
        teardown can always call this unconditionally.
        """
        response = self._request(
            "DELETE", f"/groups/{group_id}/deploy_tokens/{token_id}"
        )
        if response.status not in (204, 404):
            raise _api_error(f"cannot revoke deploy token id={token_id}", response)

    def _request(
        self, method: str, path: str, *, body: dict[str, Any] | None = None
    ) -> ApiResponse:
        """Issues one API call, retrying up to `_MAX_ATTEMPTS` times with a
        short backoff for a 5xx status or a connection failure — but only
        when `method` is idempotent (`_IDEMPOTENT_METHODS`); a non-idempotent
        method (POST) gets exactly one attempt."""
        request = urllib.request.Request(
            f"{self._params.base_url}{path}",
            method=method,
            headers={
                "PRIVATE-TOKEN": self._params.minter_token,
                "Content-Type": "application/json",
            },
            data=json.dumps(body).encode() if body is not None else None,
        )
        attempts = _MAX_ATTEMPTS if method in _IDEMPOTENT_METHODS else 1
        for attempt in range(1, attempts + 1):
            try:
                response = self._transport(request)
            except _GitLabConnectionError:
                if attempt == attempts:
                    raise
                self._sleep(_RETRY_BACKOFF_SECONDS)
                continue
            if response.status < 500 or attempt == attempts:
                return response
            self._sleep(_RETRY_BACKOFF_SECONDS)
        raise AssertionError("unreachable: loop always returns or raises")


def _api_error(context: str, response: ApiResponse) -> GitLabError:
    detail = ""
    if isinstance(response.body, dict):
        # GitLab error bodies carry one of these keys depending on the endpoint
        detail = str(
            response.body.get("message")
            or response.body.get("error_description")
            or response.body.get("error")
            or ""
        )[:200]
    message = f"{context}: HTTP {response.status}"
    return GitLabError(
        f"{message} ({detail})" if detail else message, status=response.status
    )


def _urllib_transport(request: urllib.request.Request) -> ApiResponse:
    try:
        with urllib.request.urlopen(
            request, timeout=_REQUEST_TIMEOUT_SECONDS
        ) as response:
            raw = response.read()
            try:
                body = json.loads(raw) if raw else None
            except json.JSONDecodeError as e:
                raise GitLabError(
                    f"GitLab API returned a non-JSON HTTP {response.status} response"
                ) from e
            return ApiResponse(status=response.status, body=body)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            body = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            body = None
        return ApiResponse(status=e.code, body=body)
    except TimeoutError as e:
        raise _GitLabConnectionError("GitLab API request timed out") from e
    except urllib.error.URLError as e:
        raise _GitLabConnectionError("GitLab API unreachable") from e
