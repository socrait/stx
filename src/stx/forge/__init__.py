"""Short-lived git/package-registry credentials for a sandbox's trusted box —
see docs/secrets.md (Forge credentials)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from ..errors import StxError


class ForgeError(StxError):
    """A mint or lookup against the forge failed in a way that must abort the
    caller (`stx new`/`stx refresh`) — a missing minter credential, or the API
    call itself failing. Revoke failures never raise this; see `Forge.revoke`.
    """


class ForgeCredentialKind(StrEnum):
    PUSH = "push"
    PACKAGE = "package"


@dataclass(frozen=True)
class ForgeCredential:
    kind: ForgeCredentialKind
    scope_id: int  # project id (push) or group id (package)
    token_id: int
    scope: str  # e.g. write_repository, read_repository, read_package_registry
    # repr=False: the minted secret must never surface in logs or tracebacks
    secret: str = field(repr=False)
    expires_at: str  # ISO8601 date
    username: str | None = None  # deploy tokens carry their own username


class Forge(Protocol):
    host: str  # e.g. gitlab.com — STX_FORGE_HOST
    git_user: str  # https basic-auth user for push tokens — STX_FORGE_GIT_USER

    def mint_push(self, slug: str, *, read_only: bool) -> ForgeCredential:
        """Mint the sandbox's git push (or read-only) credential. Raises
        ForgeError."""
        ...

    def mint_package(self, slug: str) -> ForgeCredential | None:
        """Mint the sandbox's package-registry read credential, or None when
        the manifest declared no `package_registry`. Raises ForgeError."""
        ...

    def revoke(self, kind: ForgeCredentialKind, scope_id: int, token_id: int) -> bool:
        """Revoke a previously minted credential. Never raises — warns on
        stderr and leaves the token's own expiry as the backstop. Returns True
        if the token was revoked, False if the attempt failed, so a caller can
        keep the id on record for a retry rather than losing track of a token
        still live on the forge."""
        ...

    def username(self) -> str | None:
        """The forge account's own username, for the `{user}` branch-template
        fallback chain, or None when it can't be determined."""
        ...


def mint_all(forge: Forge, slug: str, *, read_only: bool) -> list[ForgeCredential]:
    """Mint the push credential, then the package credential if configured —
    see docs/secrets.md (Forge credentials)."""
    push = forge.mint_push(slug, read_only=read_only)
    try:
        package = forge.mint_package(slug)
    except ForgeError:
        forge.revoke(push.kind, push.scope_id, push.token_id)
        raise
    credentials = [push]
    if package is not None:
        credentials.append(package)
    return credentials


class _PackageRegistryManifest(Protocol):
    group: str


class _ForgeManifest(Protocol):
    kind: str
    project: str
    package_registry: "_PackageRegistryManifest | None"


class _Manifest(Protocol):
    forge: "_ForgeManifest | None"


def forge_for(manifest: _Manifest | None) -> Forge | None:
    """The manifest's configured `Forge`, or None when it has no `forge:` key.

    Raises ForgeError for a `forge.kind` this `stx` build doesn't know —
    today only `"gitlab"`."""
    if manifest is None or manifest.forge is None:
        return None
    if manifest.forge.kind == "gitlab":
        from .gitlab import GitLabForge

        return GitLabForge.from_manifest(manifest.forge)
    raise ForgeError(f"unsupported forge kind: {manifest.forge.kind!r}")


class _ForgeSource(Protocol):
    kind: str
    host: str
    project: str


def forge_for_source(source: _ForgeSource | None) -> Forge | None:
    """The `Forge` a `ForgeSourceRecord` (registry data) describes, or None
    when there is none to rebuild. Unlike `forge_for`, needs no manifest and
    resolves no project/group id — revocation only needs the client and the
    already-minted scope/token ids.

    Raises ForgeError for a `source.kind` this `stx` build doesn't know —
    today only `"gitlab"`."""
    if source is None:
        return None
    if source.kind == "gitlab":
        from .gitlab import GitLabForge

        return GitLabForge.from_source(host=source.host, project=source.project)
    raise ForgeError(f"unsupported forge kind: {source.kind!r}")
