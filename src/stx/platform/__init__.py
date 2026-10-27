"""The shared platform (Postgres, Redis, Traefik, OTel) as package data —
see docs/reference.md (Platform services on demand)."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from importlib import resources
from pathlib import Path
from typing import Protocol

from .. import network as stx_network
from .. import secrets
from ..config import load_config
from ..core.compose import (
    CompletedProcessRunner,
    atomic_write_bytes,
    base_env,
    compose_command,
    compose_config,
    compose_config_hash,
    container_health,
    container_image_id,
    container_label,
    container_status,
)
from ..core.images import image_id
from ..core.names import PLATFORM_NETWORK, platform_container_name
from ..core.paths import state_root
from ..core.registry import load_registry, sandbox_live_boxes
from ..core.resources import ensure_provisioner_role
from ..core.routing import traefik_routes_dir
from ..errors import StxError
from ..secrets import SecretsError

_PACKAGE = "stx.platform"

_COMPOSE_FILENAME = "compose.platform.yaml"

#: Every file the platform package ships that a service bind-mounts, or that
#: `docker compose` itself reads — copied into the stable platform dir so
#: Compose's project directory never depends on where `stx` is installed
#: (an editable checkout vs. `uv tool`, a Python minor upgrade).
_PACKAGE_FILES = (
    _COMPOSE_FILENAME,
    "postgresql.conf",
    "redis.conf",
    "redis-entrypoint.sh",
    "traefik.yaml",
    "config.metrics.local.yaml",
)

#: Every service name `services_for` can return, in the order `up`/`new` should
#: bring them up: Traefik's file provider needs no other service; Postgres and
#: Redis are independent; OTel is independent too.
ALL_SERVICES = ("traefik", "postgres", "redis", "otel")

#: The platform's own Docker network — always created, even for a bare sandbox.
NETWORK = PLATFORM_NETWORK


def container_name(service: str) -> str:
    """The platform container name for one of `ALL_SERVICES`, e.g. 'postgres'
    -> 'stx-postgres' — matches `compose.platform.yaml`'s own
    `container_name:` entries."""
    return platform_container_name(service)


def connect_services_to_sandbox_network(slug: str, services: list[str]) -> None:
    """Ensure a sandbox's own network exists and (re)connect every non-traefik
    platform service it uses. The box itself attaches via the overlay's own
    `networks:` entry when its container starts; only the platform's
    containers (a different compose project) need an explicit connect. Traefik
    is always skipped — it reaches a box's routes through the host, never by
    joining the sandbox network. Shared by `new`'s provisioning and `start`'s
    reattach so the two never drift. See docs/security.md (Isolation)."""
    net = stx_network.sandbox_network_name(slug)
    stx_network.ensure_network(net)
    for service in services:
        if service == "traefik":
            continue
        stx_network.connect(net, container_name(service))


# Only the credentials with no safe default in compose.platform.yaml.
_SERVICE_SECRETS: dict[str, tuple[str, ...]] = {
    "traefik": (),
    "postgres": ("STX_POSTGRES_PASSWORD",),
    "redis": ("STX_REDIS_PASSWORD", "STX_REDIS_APP_PASSWORD"),
    "otel": (),
}

#: Vars with a safe compose-level default (admin/app usernames, ports) —
#: passed through only when the operator has actually set them, to dodge a
#: same-host collision with another instance of the platform.
_SERVICE_OVERRIDES: dict[str, tuple[str, ...]] = {
    "traefik": (),
    "postgres": ("STX_POSTGRES_USER", "STX_POSTGRES_PORT"),
    "redis": ("STX_REDIS_USER", "STX_REDIS_APP_USER", "STX_REDIS_PORT"),
    "otel": (),
}

#: Files each service bind-mounts, folded into `_service_definition_hash` so
#: a content-only change (no path or image change) still registers as
#: drift — a bind mount is by path, not content, so Compose's own config
#: render can't otherwise see it.
_SERVICE_FILES: dict[str, tuple[str, ...]] = {
    "traefik": ("traefik.yaml",),
    "postgres": ("postgresql.conf",),
    "redis": ("redis.conf", "redis-entrypoint.sh"),
    "otel": ("config.metrics.local.yaml",),
}

#: The label `classify_service` reads back to detect drift — stamped with
#: `_service_definition_hash`'s value, never Compose's own
#: `com.docker.compose.config-hash` (which bakes in actual secret values and
#: so differs between a run with the secret source loaded and one without).
_DEFINITION_HASH_LABEL = "stx.definition_hash"

#: Stands in for every `_SERVICE_SECRETS` name while rendering a service's
#: definition hash, so the result never depends on whether the secret
#: source happens to be loaded.
_DEFINITION_HASH_SECRET_PLACEHOLDER = "stx-masked-secret"


def _definition_hash_env(service: str) -> str:
    return f"STX_{service.upper()}_DEFINITION_HASH"


def _service_files_hash(service: str) -> str:
    digest = hashlib.sha256()
    for name in _SERVICE_FILES.get(service, ()):
        digest.update(resources.files(_PACKAGE).joinpath(name).read_bytes())
    return digest.hexdigest()


def _masked_definition_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment `_service_definition_hash` renders a service's
    config against: every `_SERVICE_SECRETS` name bound to a fixed
    placeholder (never the real value, loaded or not) and every service's
    own definition-hash var blanked, so the label's own env var can't feed
    back into the hash that becomes its value. Overrides (no safe-default
    concern — not secrets) pass through from `environ` same as
    `compose_env`."""
    environ = environ if environ is not None else os.environ
    env = base_env(environ)
    env["TRAEFIK_ROUTES_PATH"] = str(traefik_routes_dir())
    for names in _SERVICE_SECRETS.values():
        for name in names:
            env[name] = _DEFINITION_HASH_SECRET_PLACEHOLDER
    for service in ALL_SERVICES:
        for name in _SERVICE_OVERRIDES.get(service, ()):
            value = environ.get(name)
            if value:
                env[name] = value
        env[_definition_hash_env(service)] = ""
    return env


def _service_definition_hash(
    service: str,
    *,
    environ: Mapping[str, str] | None = None,
    runner: CompletedProcessRunner = subprocess.run,
) -> str:
    """The hash stamped as `service`'s `stx.definition_hash` label: Compose's
    own config-hash for `service`, rendered with every secret masked, folded
    together with its bind-mounted files' content hash. Secret-independent
    by construction — see `_masked_definition_env`."""
    env = _masked_definition_env(environ)
    config_hash = compose_config_hash(
        [compose_file()], service, cwd=compose_file().parent, env=env, runner=runner
    )
    digest = hashlib.sha256()
    digest.update((config_hash or "").encode())
    digest.update(_service_files_hash(service).encode())
    return digest.hexdigest()


class _PlatformManifest(Protocol):
    postgres: object | None
    redis: bool
    otel: bool


class _Manifest(Protocol):
    routes: dict
    platform: _PlatformManifest | None


def platform_dir() -> Path:
    """The stable on-disk copy of the platform package's files — see
    `ensure_platform_files` and docs/reference.md (Platform services on
    demand)."""
    return state_root() / "platform"


def _write_if_changed(path: Path, content: bytes) -> None:
    if path.exists() and path.read_bytes() == content:
        return
    atomic_write_bytes(path, content)


def ensure_platform_files() -> Path:
    """Copy the installed package's platform files into `platform_dir()`,
    atomically and only where content differs, so Compose's bind-mount
    source paths resolve to somewhere stable regardless of where `stx`
    itself is installed (an editable checkout vs. `uv tool`, a Python minor
    upgrade). Returns the directory."""
    directory = platform_dir()
    for name in _PACKAGE_FILES:
        content = resources.files(_PACKAGE).joinpath(name).read_bytes()
        _write_if_changed(directory / name, content)
    return directory


def compose_file() -> Path:
    """Path to the stable on-disk `compose.platform.yaml` — see
    `ensure_platform_files`."""
    return ensure_platform_files() / _COMPOSE_FILENAME


def services_for(manifest: _Manifest | None) -> list[str]:
    """Which platform services a sandbox for `manifest` needs started.

    `None` (a `--repo none` dmz-only sandbox) needs none. Traefik starts iff
    `routes` is non-empty; Postgres/Redis/OTel each iff the matching
    `platform.*` field is set/true. Order follows `ALL_SERVICES`.
    """
    if manifest is None:
        return []
    wanted = set()
    if manifest.routes:
        wanted.add("traefik")
    platform = manifest.platform
    if platform is not None:
        if platform.postgres is not None:
            wanted.add("postgres")
        if platform.redis:
            wanted.add("redis")
        if platform.otel:
            wanted.add("otel")
    return [service for service in ALL_SERVICES if service in wanted]


def required_secrets(services: list[str]) -> list[str]:
    """The `STX_*` host secret names the given platform services need.

    Only names with no compose-level default — see `_SERVICE_SECRETS`.
    """
    names: list[str] = []
    for service in services:
        for name in _SERVICE_SECRETS.get(service, ()):
            if name not in names:
                names.append(name)
    return names


def compose_env(
    services: list[str],
    *,
    environ: Mapping[str, str] | None = None,
    runner: CompletedProcessRunner = subprocess.run,
) -> dict[str, str]:
    """The trimmed `docker compose` process environment for the platform's
    own compose file: base vars, `TRAEFIK_ROUTES_PATH`, the `STX_*`
    credentials `services` need, any of the five user/port overrides (see
    `_SERVICE_OVERRIDES`) the operator has actually set, and every service's
    definition-hash label (always, independent of `services` — Compose
    still needs the whole file to interpolate cleanly regardless of which
    subset is being brought up). Every other service's own required
    secrets (the ones with no compose-level default — see
    `_SERVICE_SECRETS`) are bound to a blank placeholder, the same way
    `teardown_env` does, so starting or recreating a subset doesn't warn
    about the rest's credentials being unset."""
    environ = environ if environ is not None else os.environ
    env = base_env(environ)
    env["TRAEFIK_ROUTES_PATH"] = str(traefik_routes_dir())
    for name in required_secrets(services):
        value = environ.get(name)
        if value:
            env[name] = value
    for service in services:
        for name in _SERVICE_OVERRIDES.get(service, ()):
            value = environ.get(name)
            if value:
                env[name] = value
    not_started = [s for s in ALL_SERVICES if s not in services]
    for name in required_secrets(not_started):
        env.setdefault(name, "")
    for service in ALL_SERVICES:
        env[_definition_hash_env(service)] = _service_definition_hash(
            service, environ=environ, runner=runner
        )
    return env


class DriftState(StrEnum):
    """One platform service's relationship to its own compose definition —
    see docs/reference.md (Platform services on demand)."""

    MISSING = "missing"
    STOPPED = "stopped"
    MATCHING = "matching"
    DIFFERING = "differing"


@dataclass(frozen=True)
class ServiceDrift:
    service: str
    state: DriftState
    detail: str = ""  # set only for DIFFERING: "config changed" / "image changed"


def classify_service(
    service: str, *, runner: CompletedProcessRunner = subprocess.run
) -> ServiceDrift:
    """Where `service` stands relative to its own compose definition:
    missing (no container), stopped (container exists but isn't running),
    differing (running, but its `stx.definition_hash` label or image id no
    longer matches), or matching. Never reads the secret source — the
    definition hash is computed with every secret masked, so it means the
    same thing whether or not the secret source happens to be loaded.
    `runner` is a seam for tests to inject a fake subprocess result."""
    container = container_name(service)
    status = container_status(container, runner=runner)
    if status is None:
        return ServiceDrift(service, DriftState.MISSING)
    if status != "running":
        return ServiceDrift(service, DriftState.STOPPED)

    files = [compose_file()]
    cwd = compose_file().parent
    env = compose_env(ALL_SERVICES, runner=runner)

    expected_hash = env[_definition_hash_env(service)]
    actual_hash = container_label(container, _DEFINITION_HASH_LABEL, runner=runner)
    if expected_hash != actual_hash:
        return ServiceDrift(service, DriftState.DIFFERING, "config changed")

    config = compose_config(files, cwd=cwd, env=env, runner=runner)
    configured_image = config.get("services", {}).get(service, {}).get("image")
    if configured_image:
        expected_image_id = image_id(configured_image, runner=runner)
        actual_image_id = container_image_id(container, runner=runner)
        if expected_image_id is not None and expected_image_id != actual_image_id:
            return ServiceDrift(service, DriftState.DIFFERING, "image changed")

    return ServiceDrift(service, DriftState.MATCHING)


def classify_services(
    services: list[str], *, runner: CompletedProcessRunner = subprocess.run
) -> list[ServiceDrift]:
    """`classify_service` over each of `services`, in order."""
    return [classify_service(service, runner=runner) for service in services]


@dataclass(frozen=True)
class PostgresAdminParams:
    """Connection params for admin queries against the platform Postgres —
    always the bootstrap `postgres` database."""

    host: str
    port: int
    user: str
    password: str

    @classmethod
    def build(cls) -> PostgresAdminParams:
        password = os.environ.get("STX_POSTGRES_PASSWORD")
        if not password:
            raise SecretsError(["STX_POSTGRES_PASSWORD"])
        return cls(
            host=os.environ.get("STX_POSTGRES_HOST", "localhost"),
            port=int(os.environ.get("STX_POSTGRES_PORT", "5432")),
            user=os.environ.get("STX_POSTGRES_USER", "admin"),
            password=password,
        )


#: Name of the per-platform, non-superuser provisioning role — see
#: `provisioner_params` and docs/secrets.md (Provisioning credentials).
PROVISIONER_USER = "stx_provisioner"


def provisioner_params(admin: PostgresAdminParams) -> PostgresAdminParams:
    """`stx_provisioner`'s own connection params — same host/port as the
    platform admin connection, a dedicated CREATEDB CREATEROLE (non-superuser)
    role whose password is deterministically derived from the admin's own
    (never stored separately, never equal to it) — so a leaked provisioning
    credential can't double as the admin's own password."""
    derived = hashlib.sha256(f"stx-provisioner:{admin.password}".encode()).hexdigest()
    return PostgresAdminParams(
        host=admin.host, port=admin.port, user=PROVISIONER_USER, password=derived
    )


@dataclass(frozen=True)
class RedisAdminParams:
    """Connection params for admin queries against the platform Redis —
    authenticates as the admin user, not a sandbox's own `app` user."""

    host: str
    port: int
    user: str
    password: str

    @classmethod
    def build(cls) -> RedisAdminParams:
        password = os.environ.get("STX_REDIS_PASSWORD")
        if not password:
            raise SecretsError(["STX_REDIS_PASSWORD"])
        return cls(
            host=os.environ.get("STX_REDIS_HOST", "localhost"),
            port=int(os.environ.get("STX_REDIS_PORT", "6379")),
            user=os.environ.get("STX_REDIS_USER", "admin"),
            password=password,
        )


def _ensure_network() -> None:
    stx_network.ensure_network(NETWORK)


def reconcile_sandbox_networks(services: list[str]) -> None:
    """Connect each of `services`' platform containers to every registered
    sandbox's own network that needs it — a freshly (re)started container has
    none. Traefik is always skipped: it reaches a box's routes through the
    host, never by joining a sandbox's own network — see docs/reference.md
    (Platform services on demand) and docs/security.md (Isolation).

    `best_effort`: this loops over every *registered* sandbox regardless of
    whether its own box is actually up, so one that's stopped or mid-teardown
    failing to connect is routine, not a reason to abort reconciling the
    rest."""
    registry = load_registry()
    for service in services:
        if service == "traefik":
            continue
        container = container_name(service)
        for slug, record in registry.items():
            if service in record.services:
                stx_network.connect(
                    stx_network.sandbox_network_name(slug),
                    container,
                    best_effort=True,
                )


def run_platform_up(services: list[str]) -> int | None:
    """Bring up `services` (and the platform network, always) — starts only
    the ones missing or stopped. A service already running is left alone
    either way: matching, it's already what Compose would configure;
    differing, it's reported but never silently recreated out from under a
    live sandbox's DB/Redis connection (that recreate is `stx up
    --recreate`'s job). See docs/reference.md (Platform services on
    demand)."""
    secrets.source_for(load_config()).ensure_loaded(required_secrets(services))
    _ensure_network()
    if not services:
        print("✓ platform network ready (no services requested)")
        return None

    drifts = classify_services(services)
    to_start = [
        d.service for d in drifts if d.state in (DriftState.MISSING, DriftState.STOPPED)
    ]

    if to_start:
        result = subprocess.run(
            compose_command([compose_file()], "up", "-d", "--wait", *to_start),
            env=compose_env(to_start, runner=subprocess.run),
        )
        if result.returncode != 0:
            print("error: platform failed to start", file=sys.stderr)
            return 1
        reconcile_sandbox_networks(to_start)

    for drift in drifts:
        if drift.state not in (DriftState.MATCHING, DriftState.DIFFERING):
            continue
        if drift.state == DriftState.DIFFERING:
            print(
                f"  warning: {drift.service} has drifted ({drift.detail}) — "
                "run `stx up --recreate` when no sandbox is busy"
            )
        if container_health(container_name(drift.service)) == "unhealthy":
            print(
                f"error: platform service '{drift.service}' is unhealthy",
                file=sys.stderr,
            )
            return 1

    if "postgres" in services:
        admin = PostgresAdminParams.build()
        try:
            ensure_provisioner_role(admin, provisioner_params(admin))
        except StxError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

    print(f"✓ platform up ({', '.join(services)})")
    return None


def _busy_sandboxes_using(services: list[str]) -> set[str]:
    """Slugs of registered sandboxes that declare any of `services` and have
    an agent dispatch currently working in one of their boxes — recreating
    out from under that dispatch would kill it with no result captured."""
    wanted = set(services)
    busy = set()
    for slug, record in load_registry().items():
        if wanted & set(record.services) and sandbox_live_boxes(slug, record):
            busy.add(slug)
    return busy


def run_platform_recreate(services: list[str], *, yes: bool = False) -> int | None:
    """Recreate whichever of `services` have drifted, after also starting
    any that are missing or stopped — unlike `run_platform_up`, this is the
    explicit, operator-requested path that's allowed to recreate a running
    container. Refuses when a registered sandbox using a differing service
    has a live dispatch, unless `yes`. See docs/reference.md (Platform
    services on demand)."""
    secrets.source_for(load_config()).ensure_loaded(required_secrets(services))
    _ensure_network()

    drifts = classify_services(services)
    differing = [d.service for d in drifts if d.state == DriftState.DIFFERING]
    to_start = [
        d.service for d in drifts if d.state in (DriftState.MISSING, DriftState.STOPPED)
    ]

    if differing:
        busy = _busy_sandboxes_using(differing)
        if busy and not yes:
            print(
                f"error: recreating {', '.join(sorted(differing))} would disrupt "
                f"sandbox(es) with an agent dispatch in progress: "
                f"{', '.join(sorted(busy))} — wait for it to finish, or pass "
                "--yes to recreate anyway",
                file=sys.stderr,
            )
            return 1
        if busy:
            print(
                f"  warning: recreating anyway with a live dispatch in: "
                f"{', '.join(sorted(busy))}"
            )

    targets = [*to_start, *differing]
    if not targets:
        print("✓ platform matches its compose definition (nothing to recreate)")
        return None

    result = subprocess.run(
        compose_command([compose_file()], "up", "-d", "--wait", *targets),
        env=compose_env(targets, runner=subprocess.run),
    )
    if result.returncode != 0:
        print("error: platform failed to recreate", file=sys.stderr)
        return 1
    reconcile_sandbox_networks(targets)
    print(f"✓ platform recreated ({', '.join(targets)})")
    return None
