"""The single source for every name `stx` derives from a slug or box name:
containers, networks, compose projects, branches, databases, and Traefik
routers/services. Nothing else formats one of these — see
docs/reference.md and docs/manifest.md.
"""

from __future__ import annotations

import re

#: The dmz box's own key in a sandbox's `boxes` mapping — never a repo-chosen
#: name (see `validate_box_name`).
DMZ_BOX = "dmz"

#: Slug grammar: lowercase alnum, interior single hyphens only (no leading,
#: trailing, or doubled hyphen), 1 char or 2-40 chars. `--` can never appear
#: in a slug, which is what makes `stx-<slug>--<box>` unambiguous. 40 leaves
#: room for `stx_` + `_test` inside Postgres's 63-byte identifier limit. The
#: container name `stx-<slug>--<box>` can still overflow a DNS label on its
#: own, though (see `MAX_DNS_LABEL`): a 40-char slug plus a 20-char box is 66
#: chars, so the two lengths are checked *together* at `reserve`, not left to
#: the per-field grammar alone.
SLUG_RE = re.compile(r"[a-z0-9](?:[a-z0-9]|-(?!-)){0,38}[a-z0-9]|[a-z0-9]")

#: Box (service) name grammar — same shape as a slug, max 20.
BOX_NAME_RE = re.compile(r"[a-z0-9](?:[a-z0-9]|-(?!-)){0,18}[a-z0-9]|[a-z0-9]")

#: A container name is a DNS label on its sandbox's network, so it must fit the
#: 63-char limit — enforced at `reserve` since slug+box can exceed it.
MAX_DNS_LABEL = 63

#: Slugs that would make `stx-<slug>` collide with a platform container name,
#: or a platform/shared-cache volume prefix — refused at `new`.
RESERVED_SLUGS = frozenset({"postgres", "redis", "traefik", "otel", "net", "cache"})
_RESERVED_SLUG_PREFIX = "cache-"


def validate_slug(slug: str) -> None:
    """Raises ValueError if `slug` doesn't match `SLUG_RE`, or is reserved
    (see `RESERVED_SLUGS`)."""
    if not SLUG_RE.fullmatch(slug):
        raise ValueError(
            f"slug '{slug}' must be lowercase alphanumeric with single interior "
            "hyphens only (no leading/trailing/doubled hyphen), 1-40 chars"
        )
    if slug in RESERVED_SLUGS or slug.startswith(_RESERVED_SLUG_PREFIX):
        raise ValueError(
            f"slug '{slug}' is reserved (reserved: {', '.join(sorted(RESERVED_SLUGS))}, "
            f"or starting with '{_RESERVED_SLUG_PREFIX}')"
        )


def validate_box_name(name: str) -> None:
    """Raises ValueError if `name` doesn't match `BOX_NAME_RE`, or is `dmz`
    (reserved for the dmz box's own fixed key)."""
    if not BOX_NAME_RE.fullmatch(name):
        raise ValueError(
            f"box name '{name}' must be lowercase alphanumeric with single "
            "interior hyphens only (no leading/trailing/doubled hyphen), 1-20 chars"
        )
    if name == DMZ_BOX:
        raise ValueError(f"box name '{name}' is reserved for the dmz box")


def db_name(slug: str) -> str:
    """The Postgres database base name for a slug.

    Hyphens are replaced with underscores because unquoted PG identifiers
    can't contain hyphens (e.g. 'fix-447' -> 'stx_fix_447').
    """
    return "stx_" + slug.replace("-", "_")


def branch_name(slug: str) -> str:
    """The local branch a sandbox worktree always checks out — see
    docs/reference.md (Branches)."""
    return f"stx/{slug}"


def sandbox_compose_project(slug: str) -> str:
    """The sandbox overlay's own `docker compose` project name."""
    return f"stx-{slug}"


def container_name_for_box(box: str, slug: str) -> str:
    """A box's container name. `--` separates slug from box name,
    unambiguously — neither can contain it (see `SLUG_RE`/`BOX_NAME_RE`)."""
    return f"stx-{slug}--{box}"


def dmz_container_name(slug: str) -> str:
    return container_name_for_box(DMZ_BOX, slug)


def sandbox_network_name(slug: str) -> str:
    return f"stx-{slug}--net"


def platform_container_name(service: str) -> str:
    """The platform container name for one of the platform's own services,
    e.g. 'postgres' -> 'stx-postgres' — matches `compose.platform.yaml`'s
    own `container_name:` entries."""
    return f"stx-{service}"


#: The platform's own Docker network — always created, even for a bare
#: sandbox. Matches `compose.platform.yaml`'s `networks` entry.
PLATFORM_NETWORK = "stx-net"


def traefik_router_name(slug: str, route: str | None = None) -> str:
    """A sandbox's Traefik router/service name: per-route when `route` is
    given, else the bare router serving the first route at
    `<slug>.localhost` — see docs/reference.md (Routing)."""
    return f"stx_{slug}_{route}" if route is not None else f"stx_{slug}"
