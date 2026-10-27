"""`Forge` for GitLab, behind the `Forge` protocol. Reads the minter
credential and API base from `STX_GITLAB_MINTER_TOKEN`/`STX_GITLAB_API_URL`,
raising `ForgeError` (not `ValueError`) on a missing minter token."""

from __future__ import annotations

import os
import subprocess
import sys
import urllib.parse
from typing import Protocol

from ..core.gitlab import (
    GitLabAccessLevel,
    GitLabClient,
    GitLabDeployTokenScope,
    GitLabError,
    GitLabParams,
    GitLabTokenScope,
)
from . import ForgeCredential, ForgeCredentialKind, ForgeError

_DEFAULT_BASE_URL = "https://gitlab.com/api/v4"
_MINTER_TOKEN_ENV = "STX_GITLAB_MINTER_TOKEN"
_API_URL_ENV = "STX_GITLAB_API_URL"


def _validate_api_url(url: str) -> None:
    """Refuse a non-https `STX_GITLAB_API_URL` — the minter token travels in
    a plain header, so plaintext HTTP would send it in the clear — unless
    the host is localhost (a local test/dev GitLab instance)."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" and parsed.hostname != "localhost":
        raise ForgeError(
            f"{_API_URL_ENV} must use https (got {parsed.scheme!r} for "
            f"host {parsed.hostname!r}) — only a localhost host may use "
            "plain http"
        )


class _PackageRegistryManifest(Protocol):
    group: str


class _ForgeManifest(Protocol):
    project: str
    package_registry: "_PackageRegistryManifest | None"


class GitLabForge:
    """`Forge` for a single GitLab project (`forge.project`) and optional
    package-registry group (`forge.package_registry.group`)."""

    git_user = "oauth2"

    def __init__(
        self,
        *,
        project: str,
        package_group: str | None,
        host: str,
        params: GitLabParams,
        client: GitLabClient | None = None,
    ) -> None:
        self._project = project
        self._package_group = package_group
        self.host = host
        self._client = client or GitLabClient(params)

    @classmethod
    def from_manifest(
        cls, forge_manifest: _ForgeManifest, *, client: GitLabClient | None = None
    ) -> "GitLabForge":
        minter_token = os.environ.get(_MINTER_TOKEN_ENV)
        if not minter_token:
            raise ForgeError(f"{_MINTER_TOKEN_ENV} is not set")
        base_url = os.environ.get(_API_URL_ENV, _DEFAULT_BASE_URL)
        _validate_api_url(base_url)
        params = GitLabParams(
            minter_token=minter_token,
            base_url=base_url,
        )
        package_group = (
            forge_manifest.package_registry.group
            if forge_manifest.package_registry is not None
            else None
        )
        host = urllib.parse.urlparse(base_url).hostname or base_url
        return cls(
            project=forge_manifest.project,
            package_group=package_group,
            host=host,
            params=params,
            client=client,
        )

    @classmethod
    def from_source(
        cls, *, host: str, project: str, client: GitLabClient | None = None
    ) -> "GitLabForge":
        """Rebuilds a revoke-capable `Forge` from registry data alone (see
        `ForgeSourceRecord`/`forge_for_source`) — no manifest, and no
        package-group (revoke never resolves it). Still reads the minter
        token fresh from the secret source, never from the registry."""
        minter_token = os.environ.get(_MINTER_TOKEN_ENV)
        if not minter_token:
            raise ForgeError(f"{_MINTER_TOKEN_ENV} is not set")
        params = GitLabParams(
            minter_token=minter_token, base_url=f"https://{host}/api/v4"
        )
        return cls(
            project=project, package_group=None, host=host, params=params, client=client
        )

    def mint_push(self, slug: str, *, read_only: bool) -> ForgeCredential:
        """Write-scoped by default; `read_only=True` mints the lower-risk
        token instead — see docs/security.md (Credential lifetime)."""
        try:
            project_id = self._client.resolve_project_id(self._project)
            if read_only:
                minted = self._client.mint_project_token(
                    project_id,
                    name=f"stx-{slug}",
                    scopes=[GitLabTokenScope.READ_REPOSITORY],
                    access_level=GitLabAccessLevel.REPORTER,
                    lifetime_days=7,
                )
                scope = GitLabTokenScope.READ_REPOSITORY
            else:
                minted = self._client.mint_project_token(
                    project_id,
                    name=f"stx-{slug}",
                    scopes=[GitLabTokenScope.WRITE_REPOSITORY],
                )
                scope = GitLabTokenScope.WRITE_REPOSITORY
        except GitLabError as e:
            raise ForgeError(str(e)) from e
        return ForgeCredential(
            kind=ForgeCredentialKind.PUSH,
            scope_id=project_id,
            token_id=minted.token_id,
            scope=scope.value,
            secret=minted.secret,
            expires_at=minted.expires_at.isoformat(),
        )

    def mint_package(self, slug: str) -> ForgeCredential | None:
        """Group-scoped, not project-scoped — see docs/manifest.md
        (`forge.package_registry.group`)."""
        if self._package_group is None:
            return None
        try:
            group_id = self._client.resolve_group_id(self._package_group)
            minted = self._client.mint_group_deploy_token(
                group_id,
                name=f"stx-{slug}",
                scopes=[GitLabDeployTokenScope.READ_PACKAGE_REGISTRY],
            )
        except GitLabError as e:
            raise ForgeError(str(e)) from e
        return ForgeCredential(
            kind=ForgeCredentialKind.PACKAGE,
            scope_id=group_id,
            token_id=minted.token_id,
            scope=GitLabDeployTokenScope.READ_PACKAGE_REGISTRY.value,
            secret=minted.secret,
            username=minted.username,
            expires_at=minted.expires_at.isoformat(),
        )

    def revoke(self, kind: ForgeCredentialKind, scope_id: int, token_id: int) -> bool:
        try:
            if kind is ForgeCredentialKind.PUSH:
                self._client.revoke_project_token(scope_id, token_id)
            else:
                self._client.revoke_group_deploy_token(scope_id, token_id)
        except GitLabError as e:
            print(f"warning: GitLab token revoke failed: {e}", file=sys.stderr)
            return False
        return True

    def username(self) -> str | None:
        """The `glab` username for `self.host`, with no prefix stripping, or
        None when `glab` has no identity configured (or isn't installed)."""
        try:
            result = subprocess.run(
                ["glab", "config", "get", "-h", self.host, "username"],
                capture_output=True,
                text=True,
            )
        except FileNotFoundError:
            return None
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None
