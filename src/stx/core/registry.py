from __future__ import annotations

import fcntl
import json
import socket
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path

from ..errors import StxError
from .dispatch import agent_pid_alive, read_agent_status
from .names import (
    DMZ_BOX,
    MAX_DNS_LABEL,
    container_name_for_box,
    sandbox_network_name,
    traefik_router_name,
)
from .paths import state_root

_SCHEMA = 3
_MAX_REDIS_DB = 15  # Redis defaults to 16 logical DBs (0-15); 0 reserved
_TAILNET_PORT_MIN = 5001
_TAILNET_PORT_MAX = 5099
_ROUTE_PORT_MIN = 5100
_ROUTE_PORT_MAX = 5599


class Zone(StrEnum):
    """A box's capability tier — derived from box kind, never user-selected."""

    TRUSTED = "trusted"
    DMZ = "dmz"


class SandboxState(StrEnum):
    """A sandbox's provisioning lifecycle — see docs/reference.md (State layout)."""

    PROVISIONING = "provisioning"
    READY = "ready"


class ClaudeAuthMode(StrEnum):
    """How the app box's harness authenticates — never both at once. See
    docs/secrets.md (Harness auth)."""

    API_KEY = "api_key"
    OAUTH = "oauth"


@dataclass
class ForgePushRecord:
    scope_id: int
    token_id: int
    scope: str
    expires_at: str  # ISO8601 date


@dataclass
class ForgePackageRecord:
    scope_id: int
    token_id: int
    expires_at: str  # ISO8601 date


@dataclass
class ForgeBoxRecord:
    kind: str
    push: ForgePushRecord | None = None
    package: ForgePackageRecord | None = None


@dataclass
class ForgeSourceRecord:
    """Everything `forge_for_source` needs to rebuild a `Forge` client for
    revocation without the manifest — never a secret (the minter token is
    always re-read fresh from the secret source). See `forge_for_source` in
    `..forge`."""

    kind: str
    host: str
    project: str


@dataclass
class LinkRecord:
    """One outbound `--link NAME=TARGET`, resolved at `new` and fixed for the
    sandbox's life — see docs/manifest.md (links)."""

    target: str
    route: str
    port: int
    container: str  # the target box's own container name

    @property
    def url(self) -> str:
        return f"http://{self.route}.{self.target}.localhost"

    @property
    def internal_url(self) -> str:
        return f"http://{self.container}:{self.port}"


@dataclass
class BoxRecord:
    zone: Zone
    user: str = ""
    branch: str = ""  # trusted (app) box only
    worktree: str = ""  # trusted (app) box only
    base_sha: str = ""  # trusted (app) box only; also the snapshot SHA for a dmz box
    image: str = ""
    claude_auth_mode: ClaudeAuthMode = ClaudeAuthMode.API_KEY  # trusted (app) box only
    forge: ForgeBoxRecord | None = None  # trusted (app) box only
    # trusted (app) box only — what `forge_for_source` needs to rebuild a
    # revoke-capable `Forge` client for this box without its manifest; see
    # `ForgeSourceRecord`. None iff `forge` is also None.
    forge_source: ForgeSourceRecord | None = None
    # trusted (app) box only — this sandbox's own manifest.routes, stored so a
    # future `--link` targeting it can resolve without needing this repo's
    # checkout (see docs/manifest.md, links).
    routes: dict[str, int] = field(default_factory=dict)
    # trusted (app) box only — this box's own outbound links.
    links: dict[str, LinkRecord] = field(default_factory=dict)


@dataclass
class SandboxRecord:
    slug: str
    created: str  # ISO8601
    state: SandboxState
    redis_index: int | None  # None unless the manifest declared platform.redis
    # Host port publishing the app box's `publish`-named route directly,
    # allocated from its own 5001-5099 pool (independent of redis_index) so
    # tailnet clients can reach it without hostname-based routing — MagicDNS
    # can't resolve per-slug subdomains, only this machine's one bare FQDN.
    # None unless the manifest sets `publish`.
    tailnet_port: int | None
    # Host ports publishing every manifest route's own container port, keyed
    # by route name, allocated from their own 5100-5599 pool — this is what
    # Traefik reaches through the host (`http://host.docker.internal:<port>`)
    # since it joins no sandbox network. Empty unless the manifest declares
    # `routes`. See docs/reference.md (Routing).
    route_ports: dict[str, int] = field(default_factory=dict)
    services: list[str] = field(default_factory=list)  # subset of the shared platform
    repo: str = ""  # repo dir name; "" for a --repo none sandbox
    repo_root: str = ""  # checkout that ran `stx new`; "" for --repo none
    # Rendered platform.postgres.databases names, recorded at reserve() time
    # (before any box exists) so `rm`/`prune` never need the manifest to know
    # what to drop — see docs/reference.md (Teardown guards). Empty iff the
    # manifest declares no platform.postgres (or there's no manifest at all).
    databases: tuple[str, ...] = ()
    # This sandbox's box names (the manifest's own box.name, plus "dmz" if
    # with_dmz), recorded at reserve() time — before `boxes` below is
    # populated — so the collision check at a later `reserve()` has
    # something to derive this sandbox's own container/network names from
    # even while this one is still mid-provisioning. See `LockedRegistry.reserve`.
    box_names: tuple[str, ...] = ()
    # Keyed by the manifest's own box.name (the dmz box's key is always literally
    # "dmz") — never a fixed "app" constant, since a repo names its own box.
    boxes: dict[str, BoxRecord] = field(default_factory=dict)
    # Forge tokens minted during `new`, recorded the instant they're minted —
    # before the box is built and `boxes` is populated — so a crash mid-`new`
    # (a signal, an OOM) leaves them tracked for `rm`/`prune` to revoke rather
    # than leaked. Cleared by `finish` once the full `boxes` set (which carries
    # the same ids on its own BoxRecord) lands. None for a ready sandbox or one
    # whose repo declares no forge.
    pending_forge: ForgeBoxRecord | None = None
    pending_forge_source: ForgeSourceRecord | None = None
    # The `stx new` process that owns this entry while it's still
    # `provisioning`, so `prune` can tell a crashed `new` (safe to clean up)
    # from one still running (must be left alone) — see docs/reference.md
    # (Teardown guards). `owner_started` is a pid-reuse guard. Both are
    # meaningless once the sandbox is `ready`.
    owner_pid: int | None = None
    owner_started: str = ""


def sandbox_live_boxes(slug: str, record: SandboxRecord) -> list[str]:
    """Box names in `record.boxes` with an agent dispatch currently working —
    the dispatch-state check `refresh` and `up --recreate` share before a
    recreate would kill it mid-task with no result captured."""
    live = []
    for name, box in record.boxes.items():
        container = container_name_for_box(name, slug)
        status = read_agent_status(container, box.user)
        if status is not None and agent_pid_alive(container, status.pid, user=box.user):
            live.append(name)
    return live


def trusted_box_name(boxes: dict[str, BoxRecord]) -> str | None:
    """The one trusted box's own name key, or None for a dmz-only sandbox.

    At most one trusted box exists per sandbox today — a repo's manifest
    describes exactly one box; multi-repo composition is not yet supported.
    """
    for name, box in boxes.items():
        if box.zone == Zone.TRUSTED:
            return name
    return None


def linkers_of(target_slug: str, registry: dict[str, SandboxRecord]) -> list[str]:
    """Slugs of registered sandboxes whose trusted box links to `target_slug`
    — used by `rm`'s linked-to guard (see docs/manifest.md, links)."""
    linkers = []
    for slug, record in registry.items():
        if slug == target_slug:
            continue
        trusted = trusted_box_name(record.boxes)
        if trusted is None:
            continue
        if any(
            link.target == target_slug for link in record.boxes[trusted].links.values()
        ):
            linkers.append(slug)
    return linkers


def sandbox_has_own_network(record: SandboxRecord) -> bool:
    """Whether this sandbox was provisioned with its own Docker network — true
    for any sandbox but a dmz-only one, which attaches to no network of its
    own. Read from the record's box *names*, not its live `boxes`, so a crash
    mid-provisioning (`boxes={}`, but `box_names` recorded at reserve()) still
    has its network torn down rather than orphaned — see docs/reference.md
    (Teardown guards)."""
    names = set(record.box_names) | set(record.boxes)
    return any(name != DMZ_BOX for name in names)


def default_box(record: SandboxRecord) -> str:
    """The box a bare `shell`/`code` call should target: the trusted box if
    present, else dmz."""
    return trusted_box_name(record.boxes) or DMZ_BOX


def box_not_found_error(slug: str, box: str, record: SandboxRecord) -> str:
    return (
        f"sandbox '{slug}' has no '{box}' box "
        f"(boxes: {', '.join(sorted(record.boxes)) or 'none'})"
    )


def _registry_path() -> Path:
    return state_root() / "registry.json"


def _lock_path() -> Path:
    return state_root() / "registry.lock"


def _parse_forge(raw: dict | None) -> ForgeBoxRecord | None:
    if raw is None:
        return None
    push = ForgePushRecord(**raw["push"]) if raw["push"] else None
    package = ForgePackageRecord(**raw["package"]) if raw["package"] else None
    return ForgeBoxRecord(kind=raw["kind"], push=push, package=package)


def _parse_link(raw: dict) -> LinkRecord:
    return LinkRecord(
        target=raw["target"],
        route=raw["route"],
        port=raw["port"],
        container=raw["container"],
    )


def _parse_forge_source(raw: dict | None) -> ForgeSourceRecord | None:
    if raw is None:
        return None
    return ForgeSourceRecord(kind=raw["kind"], host=raw["host"], project=raw["project"])


def _parse_box(raw: dict) -> BoxRecord:
    return BoxRecord(
        zone=Zone(raw["zone"]),
        user=raw["user"],
        branch=raw["branch"],
        worktree=raw["worktree"],
        base_sha=raw["base_sha"],
        image=raw["image"],
        claude_auth_mode=ClaudeAuthMode(raw["claude_auth_mode"]),
        forge=_parse_forge(raw["forge"]),
        forge_source=_parse_forge_source(raw["forge_source"]),
        routes=dict(raw["routes"]),
        links={name: _parse_link(v) for name, v in raw["links"].items()},
    )


def _parse_record(slug: str, raw: dict) -> SandboxRecord:
    return SandboxRecord(
        slug=raw["slug"],
        created=raw["created"],
        state=SandboxState(raw["state"]),
        redis_index=raw["redis_index"],
        tailnet_port=raw["tailnet_port"],
        route_ports=dict(raw["route_ports"]),
        services=list(raw["services"]),
        repo=raw["repo"],
        repo_root=raw["repo_root"],
        databases=tuple(raw["databases"]),
        box_names=tuple(raw["box_names"]),
        boxes={name: _parse_box(box) for name, box in raw["boxes"].items()},
        pending_forge=_parse_forge(raw.get("pending_forge")),
        pending_forge_source=_parse_forge_source(raw.get("pending_forge_source")),
        owner_pid=raw.get("owner_pid"),
        owner_started=raw.get("owner_started", ""),
    )


def _read_registry_file() -> dict | None:
    """Read and JSON-parse the registry file; None if it doesn't exist yet.
    Raises StxError if it exists but can't be read or parsed — see
    docs/reference.md (State layout)."""
    registry_path = _registry_path()
    try:
        text = registry_path.read_text()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise StxError(f"cannot read registry at {registry_path}: {e}") from e
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise StxError(f"registry at {registry_path} is corrupt: {e}") from e


def _raw_sandboxes() -> dict[str, dict]:
    raw = _read_registry_file()
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        # Valid JSON but not a registry object (e.g. a top-level array) —
        # reading it as empty would overwrite whatever it is on the next save.
        raise StxError(
            f"registry at {_registry_path()} is not a JSON object "
            f"(found {type(raw).__name__})"
        )
    schema = raw.get("schema")
    if schema != _SCHEMA:
        raise StxError(
            f"registry at {_registry_path()}: schema must be {_SCHEMA} (found {schema!r})"
        )
    return raw.get("sandboxes", {})


def _split_sandboxes(
    raw_sandboxes: dict[str, dict],
) -> tuple[dict[str, SandboxRecord], dict[str, tuple[dict, str]]]:
    """Parse every raw entry; one bad entry is skipped, not fatal to the rest.
    Each unparsed entry is paired with its own parse error — `stx prune`
    tears it down as a zombie and reports why; see docs/reference.md
    (Teardown guards)."""
    registry: dict[str, SandboxRecord] = {}
    unparsed: dict[str, tuple[dict, str]] = {}
    for slug, v in raw_sandboxes.items():
        try:
            registry[slug] = _parse_record(slug, v)
        except (TypeError, KeyError, ValueError) as e:
            print(
                f"warning: skipping unparseable registry entry '{slug}': {e} "
                "— run `stx prune` to clean it up",
                file=sys.stderr,
            )
            unparsed[slug] = (v, str(e))
    return registry, unparsed


def load_registry() -> dict[str, SandboxRecord]:
    """Load the registry, tolerating individual unparseable entries. Mutating
    callers should use `locked_registry()` instead — this alone doesn't guard
    against a concurrent read-modify-write race."""
    registry, _unparsed = _split_sandboxes(_raw_sandboxes())
    return registry


def load_registry_with_unparsed() -> tuple[
    dict[str, SandboxRecord], dict[str, tuple[dict, str]]
]:
    """Load the registry plus every entry that failed to parse, each paired
    with its own raw JSON and parse error — see `_split_sandboxes`. `stx
    prune` is the only caller that needs the unparsed half; every other
    reader wants `load_registry()`."""
    return _split_sandboxes(_raw_sandboxes())


def save_registry(
    registry: dict[str, SandboxRecord], *, extra_raw: dict[str, dict] | None = None
) -> None:
    """Write the registry atomically so a concurrent load_registry() never sees a torn file.

    Writes to a same-directory temp file and renames it into place —
    os.replace() is atomic on a single filesystem (POSIX and Windows both),
    unlike a direct write_text() to the target path. Doesn't by itself close
    the read-modify-write race between two concurrent mutating callers — use
    `locked_registry()` for that.

    `extra_raw` carries entries `load_registry()` couldn't parse, keyed by
    slug, so a save triggered by an unrelated mutation doesn't silently drop
    them — see `locked_registry()`.
    """
    registry_path = _registry_path()
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    sandboxes = {k: asdict(v) for k, v in registry.items()}
    if extra_raw:
        # A parsed record always wins over a same-slug raw carry-over —
        # `setdefault`, not `update`, so a reservation can never be written
        # back as its old unparseable self (see `reserve`).
        for slug, raw in extra_raw.items():
            sandboxes.setdefault(slug, raw)
    payload = {"schema": _SCHEMA, "sandboxes": sandboxes}
    tmp_path = registry_path.with_suffix(registry_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2))
    tmp_path.replace(registry_path)


def allocate_redis_index(registry: dict[str, SandboxRecord]) -> int:
    """Returns the highest free Redis db-index in range 1-_MAX_REDIS_DB.

    Allocates top-down (15, 14, ...) rather than bottom-up; costs nothing
    and leaves the low end free for anything else sharing the instance.
    """
    used = {r.redis_index for r in registry.values() if r.redis_index is not None}
    for i in range(_MAX_REDIS_DB, 0, -1):
        if i not in used:
            return i
    raise StxError(
        f"All Redis db-indexes (1-{_MAX_REDIS_DB}) are in use. "
        "Run `stx rm` on a finished sandbox to free one."
    )


def port_bindable(port: int, addr: str = "") -> bool:
    """Whether `port` can be bound on the host right now — a probe the host-port
    allocators use so they never hand out a number some other process already
    holds (which would then fail `compose up` on every retry). Binding the
    wildcard address (the default) fails if any address holds the port, so a
    success means it's free everywhere docker might publish it. No
    SO_REUSEADDR: a port in TIME_WAIT reads as busy, and skipping it costs
    nothing."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((addr, port))
            return True
        except OSError:
            return False


def allocate_tailnet_port(
    registry: dict[str, SandboxRecord],
    *,
    is_free: Callable[[int], bool] | None = None,
) -> int:
    """Returns the highest free tailnet port in range _TAILNET_PORT_MIN-_TAILNET_PORT_MAX.

    Top-down for the same reason as allocate_redis_index. `is_free`, when
    given, additionally rejects a port already in use on the host (see
    `port_bindable`) — otherwise a busy port is handed out and fails every
    retry.
    """
    used = {r.tailnet_port for r in registry.values() if r.tailnet_port is not None}
    for p in range(_TAILNET_PORT_MAX, _TAILNET_PORT_MIN - 1, -1):
        if p in used:
            continue
        if is_free is not None and not is_free(p):
            continue
        return p
    raise StxError(
        f"All tailnet ports ({_TAILNET_PORT_MIN}-{_TAILNET_PORT_MAX}) are in use. "
        "Run `stx rm` on a finished sandbox to free one."
    )


def allocate_route_ports(
    registry: dict[str, SandboxRecord],
    count: int,
    *,
    is_free: Callable[[int], bool] | None = None,
) -> list[int]:
    """Returns `count` free host ports from the route-port pool
    (_ROUTE_PORT_MIN-_ROUTE_PORT_MAX), one per manifest route — see
    `SandboxRecord.route_ports`.

    Top-down for the same reason as allocate_redis_index/allocate_tailnet_port.
    `is_free`, when given, additionally skips a port already in use on the
    host (see `port_bindable`).
    """
    used = {port for r in registry.values() for port in r.route_ports.values()}
    allocated: list[int] = []
    for p in range(_ROUTE_PORT_MAX, _ROUTE_PORT_MIN - 1, -1):
        if len(allocated) == count:
            break
        if p in used:
            continue
        if is_free is not None and not is_free(p):
            continue
        allocated.append(p)
    if len(allocated) < count:
        raise StxError(
            f"Not enough free route ports ({_ROUTE_PORT_MIN}-{_ROUTE_PORT_MAX}) for "
            f"{count} route(s). Run `stx rm` on a finished sandbox to free some."
        )
    return allocated


def _own_names(
    slug: str,
    *,
    box_names: tuple[str, ...],
    databases: tuple[str, ...],
    route_ports: dict[str, int],
) -> list[tuple[str, str]]:
    """`(category, name)` pairs a sandbox with these inputs would derive —
    the same shape `_check_collisions` compares against every other
    registered sandbox's own set."""
    names = [("container", container_name_for_box(box, slug)) for box in box_names]
    names.append(("network", sandbox_network_name(slug)))
    names += [("database", db) for db in databases]
    names += [
        ("traefik router", traefik_router_name(slug, route)) for route in route_ports
    ]
    if route_ports:
        names.append(("traefik router", traefik_router_name(slug)))
    names += [("route port", str(port)) for port in route_ports.values()]
    return names


def _check_collisions(
    slug: str,
    registry: dict[str, SandboxRecord],
    *,
    box_names: tuple[str, ...],
    databases: tuple[str, ...],
    route_ports: dict[str, int],
) -> None:
    """Raises StxError naming the other sandbox and the colliding name if
    anything `slug` would derive already belongs to a registered sandbox —
    checked under the registry lock, before `slug` itself is reserved. Most
    of these categories can't actually collide given the slug/box-name
    grammar (see `names.py`), but database names can: a manifest's own
    `platform.postgres.databases` template can render two different slugs to
    the same base, e.g. slug 'a-test' vs slug 'a' with entry '{db}_test'."""
    own = _own_names(
        slug, box_names=box_names, databases=databases, route_ports=route_ports
    )
    for other_slug, other in registry.items():
        other_names = set(
            _own_names(
                other_slug,
                box_names=other.box_names,
                databases=other.databases,
                route_ports=other.route_ports,
            )
        )
        for category, name in own:
            if (category, name) in other_names:
                raise StxError(
                    f"sandbox '{slug}' would collide with registered sandbox "
                    f"'{other_slug}' on {category} '{name}' — run `stx rm "
                    f"{other_slug}` first, or rename this sandbox"
                )


class LockedRegistry:
    """An in-memory registry snapshot held under the lock for one
    read-modify-write. Never constructed directly — use `locked_registry()`."""

    def __init__(
        self,
        registry: dict[str, SandboxRecord],
        unparsed: dict[str, tuple[dict, str]] | None = None,
    ) -> None:
        self._registry = registry
        self._unparsed = unparsed or {}

    def all(self) -> dict[str, SandboxRecord]:
        return self._registry

    def unparsed_raw(self) -> dict[str, dict]:
        """Raw JSON for entries this load couldn't parse — saved back verbatim."""
        return {slug: raw for slug, (raw, _err) in self._unparsed.items()}

    def __contains__(self, slug: str) -> bool:
        return slug in self._registry

    def __getitem__(self, slug: str) -> SandboxRecord:
        return self._registry[slug]

    def reserve(
        self,
        slug: str,
        *,
        created: str,
        redis_index: int | None,
        tailnet_port: int | None,
        route_ports: dict[str, int],
        services: list[str],
        repo: str,
        repo_root: str,
        databases: tuple[str, ...] = (),
        box_names: tuple[str, ...] = (),
        owner_pid: int | None = None,
        owner_started: str = "",
    ) -> SandboxRecord:
        """Claim `slug` provisioning, allocating indexes under the lock.
        Raises StxError if `slug` is already registered, or if any name this
        sandbox would derive collides with a registered sandbox's own (see
        `_check_collisions`). `databases`/`box_names` are recorded now,
        before any box exists, so a sandbox stuck mid-provisioning still has
        its database/box names in the registry for `rm`/a later collision
        check to use — see docs/reference.md (Teardown guards).
        `owner_pid`/`owner_started` stamp the owning `new` process so `prune`
        can skip an entry a live `new` still owns."""
        if slug in self._registry:
            raise StxError(f"sandbox '{slug}' is already registered")
        if slug in self._unparsed:
            # The slug is held by an entry this load couldn't parse. Reserving
            # it anyway would have `save_registry` carry the raw entry back
            # under the same key, clobbering the reservation — a later `new`
            # step then reads it unparseable again and dies with KeyError.
            raise StxError(
                f"sandbox '{slug}' has an unparseable registry entry — run "
                "`stx prune` to clean it up before reusing the slug"
            )
        for box in box_names:
            name = container_name_for_box(box, slug)
            if len(name) > MAX_DNS_LABEL:
                raise StxError(
                    f"container name '{name}' is {len(name)} chars, over the "
                    f"{MAX_DNS_LABEL}-char DNS-label limit — shorten the slug "
                    f"'{slug}' or the box name '{box}'"
                )
        _check_collisions(
            slug,
            self._registry,
            box_names=box_names,
            databases=databases,
            route_ports=route_ports,
        )
        record = SandboxRecord(
            slug=slug,
            created=created,
            state=SandboxState.PROVISIONING,
            redis_index=redis_index,
            tailnet_port=tailnet_port,
            route_ports=route_ports,
            services=services,
            repo=repo,
            repo_root=repo_root,
            databases=databases,
            box_names=box_names,
            boxes={},
            owner_pid=owner_pid,
            owner_started=owner_started,
        )
        self._registry[slug] = record
        return record

    def unreserve(self, slug: str) -> None:
        """Drop a failed `new`'s reservation, freeing its redis_index/tailnet_port/route_ports."""
        self._registry.pop(slug, None)

    def record_pending_forge(
        self,
        slug: str,
        forge: ForgeBoxRecord,
        source: ForgeSourceRecord,
    ) -> None:
        """Record the tokens `new` just minted on the still-provisioning entry,
        so a crash before `finish` leaves them tracked — see
        `SandboxRecord.pending_forge`."""
        record = self._registry[slug]
        record.pending_forge = forge
        record.pending_forge_source = source

    def finish(self, slug: str, boxes: dict[str, BoxRecord]) -> None:
        """Mark `slug` ready with its final box set, once every box is up. The
        final `boxes` carry the forge ids on their own BoxRecord, so the
        provisioning-only `pending_forge` is cleared here."""
        record = self._registry[slug]
        record.boxes = boxes
        record.state = SandboxState.READY
        record.pending_forge = None
        record.pending_forge_source = None

    def remove(self, slug: str) -> None:
        self._registry.pop(slug, None)
        self._unparsed.pop(slug, None)


@contextmanager
def locked_registry() -> Iterator[LockedRegistry]:
    """Hold `registry.lock` across the whole read-modify-write, saved on a
    clean exit — every registry mutation must happen inside this context."""
    lock_path = _lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            registry, unparsed = _split_sandboxes(_raw_sandboxes())
            locked = LockedRegistry(registry, unparsed)
            yield locked
            save_registry(locked.all(), extra_raw=locked.unparsed_raw())
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
