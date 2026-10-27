"""Renders a minted credential set into the box env the overlay writes:
`STX_FORGE_TOKEN`/`_HOST`/`_GIT_USER` from the push credential, and the
manifest's own `package_registry.env` username/password variable names from
the package credential.

Takes a duck-typed manifest, like `forge_for` — see the package docstring.
"""

from __future__ import annotations

from typing import Protocol

from . import Forge, ForgeCredential, ForgeCredentialKind


class _PackageRegistryManifest(Protocol):
    env: dict  # {"username": VAR_NAME, "password": VAR_NAME}


class _ForgeManifest(Protocol):
    package_registry: "_PackageRegistryManifest | None"


class _Manifest(Protocol):
    forge: "_ForgeManifest | None"


def render_box_env(
    forge: Forge, credentials: list[ForgeCredential], manifest: _Manifest
) -> dict[str, str]:
    """The box env vars for `credentials` (as returned by `mint_all`).

    Empty when `credentials` holds no push credential (nothing was minted).
    The package vars are only added when both a package credential was minted
    and the manifest still declares `package_registry` (its `env` names where
    to put the username/password)."""
    env: dict[str, str] = {}
    push = _find(credentials, ForgeCredentialKind.PUSH)
    if push is not None:
        env["STX_FORGE_TOKEN"] = push.secret
        env["STX_FORGE_HOST"] = forge.host
        env["STX_FORGE_GIT_USER"] = forge.git_user
    package = _find(credentials, ForgeCredentialKind.PACKAGE)
    package_registry = manifest.forge.package_registry if manifest.forge else None
    if package is not None and package_registry is not None:
        env[package_registry.env["username"]] = package.username or ""
        env[package_registry.env["password"]] = package.secret
    return env


def _find(
    credentials: list[ForgeCredential], kind: ForgeCredentialKind
) -> ForgeCredential | None:
    for credential in credentials:
        if credential.kind is kind:
            return credential
    return None
