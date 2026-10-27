"""The Registry v2 forge record — the typed `boxes.<name>.forge` value stored in
`registry.json` — built from a minted `ForgeCredential` list, and read back for
revoke.

Built on `core.registry`'s own `ForgeBoxRecord`/`ForgePushRecord`/
`ForgePackageRecord` dataclasses (not a plain dict) so a malformed or
hand-edited registry entry fails to parse loudly at `load_registry()` time —
see that module's `_parse_forge` — rather than surfacing as a confusing
`KeyError` deep inside a revoke call.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from ..core.registry import (
    BoxRecord,
    ForgeBoxRecord,
    ForgePackageRecord,
    ForgePushRecord,
    SandboxRecord,
)
from . import ForgeCredential, ForgeCredentialKind

EXPIRY_WARNING_WINDOW = timedelta(hours=24)


def build_forge_record(kind: str, credentials: list[ForgeCredential]) -> ForgeBoxRecord:
    """The `ForgeBoxRecord` to store at `boxes.<name>.forge` for this `kind`
    (the manifest's `forge.kind`, e.g. `"gitlab"`) and the credentials
    `mint_all` returned. `package` is `None` when no package credential was
    minted."""
    push: ForgePushRecord | None = None
    package: ForgePackageRecord | None = None
    for credential in credentials:
        if credential.kind is ForgeCredentialKind.PUSH:
            push = ForgePushRecord(
                scope_id=credential.scope_id,
                token_id=credential.token_id,
                scope=credential.scope,
                expires_at=credential.expires_at,
            )
        elif credential.kind is ForgeCredentialKind.PACKAGE:
            package = ForgePackageRecord(
                scope_id=credential.scope_id,
                token_id=credential.token_id,
                expires_at=credential.expires_at,
            )
    return ForgeBoxRecord(kind=kind, push=push, package=package)


def revoke_targets(
    record: ForgeBoxRecord,
) -> list[tuple[ForgeCredentialKind, int, int]]:
    """`(kind, scope_id, token_id)` for every credential in `record`, in the
    order they should be revoked (push, then package) — pass each straight to
    `Forge.revoke`."""
    targets: list[tuple[ForgeCredentialKind, int, int]] = []
    if record.push:
        targets.append(
            (ForgeCredentialKind.PUSH, record.push.scope_id, record.push.token_id)
        )
    if record.package:
        targets.append(
            (
                ForgeCredentialKind.PACKAGE,
                record.package.scope_id,
                record.package.token_id,
            )
        )
    return targets


def _expires_within(expires_at: str, *, within: timedelta, now: date) -> bool:
    return date.fromisoformat(expires_at) - now <= within


def earliest_box_expiry(box: BoxRecord | None) -> date | None:
    """The earliest of `box`'s push/package token expiry dates, or `None`
    when `box` has no forge record, or neither credential was minted."""
    if box is None or box.forge is None:
        return None
    dates = [
        date.fromisoformat(token.expires_at)
        for token in (box.forge.push, box.forge.package)
        if token is not None
    ]
    return min(dates) if dates else None


def box_forge_expired(box: BoxRecord, *, now: date | None = None) -> bool:
    """Whether `box`'s push or package forge token has already expired as of
    `now` (today, by default)."""
    if box.forge is None:
        return False
    now = now or datetime.now(timezone.utc).date()
    expiry = earliest_box_expiry(box)
    return expiry is not None and expiry < now


def sandboxes_with_expired_forge_tokens(
    registry: dict[str, SandboxRecord], *, now: date | None = None
) -> list[str]:
    """Slugs with at least one box whose forge token has already expired as
    of `now` (today, by default), sorted — for the `stx ls` warning."""
    now = now or datetime.now(timezone.utc).date()
    return sorted(
        slug
        for slug, record in registry.items()
        if any(box_forge_expired(box, now=now) for box in record.boxes.values())
    )


def box_forge_expires_soon(
    box: BoxRecord,
    *,
    within: timedelta = EXPIRY_WARNING_WINDOW,
    now: date | None = None,
) -> bool:
    """Whether `box`'s push or package forge token expires within `within`
    of `now` (today, by default)."""
    if box.forge is None:
        return False
    now = now or datetime.now(timezone.utc).date()
    push, package = box.forge.push, box.forge.package
    return (
        push is not None and _expires_within(push.expires_at, within=within, now=now)
    ) or (
        package is not None
        and _expires_within(package.expires_at, within=within, now=now)
    )


def sandboxes_with_expiring_forge_tokens(
    registry: dict[str, SandboxRecord],
    *,
    within: timedelta = EXPIRY_WARNING_WINDOW,
    now: date | None = None,
) -> list[str]:
    """Slugs with at least one box whose forge token expires within `within`
    of `now` (today, by default), sorted — for the `stx ls`/`stx guide`
    warning."""
    now = now or datetime.now(timezone.utc).date()
    return sorted(
        slug
        for slug, record in registry.items()
        if any(
            box_forge_expires_soon(box, within=within, now=now)
            for box in record.boxes.values()
        )
    )
