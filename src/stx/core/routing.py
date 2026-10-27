import json
import subprocess
from collections.abc import Callable
from pathlib import Path

from .compose import atomic_write_text, run_bounded
from .names import traefik_router_name
from .paths import state_root

CompletedProcessRunner = Callable[..., "subprocess.CompletedProcess[str]"]


def traefik_routes_dir() -> Path:
    return state_root() / "routes"


def get_tailscale_hostname(
    *, runner: CompletedProcessRunner = subprocess.run
) -> str | None:
    """Return the Tailscale FQDN for this machine, or None if Tailscale is not
    running. `runner` is a seam for tests to inject a fake subprocess result."""
    try:
        result = runner(
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        dns_name = data.get("Self", {}).get("DNSName", "")
        return dns_name.rstrip(".") if dns_name else None
    except (
        FileNotFoundError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
        OSError,
    ):
        return None


def get_tailscale_ipv4(
    *, runner: CompletedProcessRunner = subprocess.run
) -> str | None:
    """Return this machine's Tailscale IPv4 address, or None if Tailscale is
    not running. `runner` is a seam for tests to inject a fake subprocess
    result."""
    try:
        result = runner(
            ["tailscale", "ip", "-4"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        ip = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
        return ip or None
    except (
        FileNotFoundError,
        subprocess.TimeoutExpired,
        OSError,
    ):
        return None


def write_traefik_route(slug: str, route_ports: dict[str, int]) -> None:
    """Write one Traefik file-provider router+service per manifest route, plus
    one extra router serving the first route bare at `<slug>.localhost` (same
    service, no second service block) — local-only, see docs/reference.md
    (Routing). Each service points at the box's own route port published on
    the host (`route_ports`, keyed by route name), reached through
    `host.docker.internal` — Traefik joins no sandbox network."""
    router_blocks = [
        f"""\
    {traefik_router_name(slug, name)}:
      entryPoints:
        - web
      rule: "Host(`{name}.{slug}.localhost`)"
      service: {traefik_router_name(slug, name)}"""
        for name in route_ports
    ]
    if route_ports:
        first_route = next(iter(route_ports))
        router_blocks.append(
            f"""\
    {traefik_router_name(slug)}:
      entryPoints:
        - web
      rule: "Host(`{slug}.localhost`)"
      service: {traefik_router_name(slug, first_route)}"""
        )
    routers = "\n".join(router_blocks)
    services = "\n".join(
        f"""\
    {traefik_router_name(slug, name)}:
      loadBalancer:
        servers:
          - url: "http://host.docker.internal:{port}\""""
        for name, port in route_ports.items()
    )
    content = f"""\
http:
  routers:
{routers}
  services:
{services}
"""
    route_file = traefik_routes_dir() / f"{slug}.yaml"
    # Traefik's file provider watches this dir and hot-reloads on change —
    # a torn write here is a route Traefik briefly can't parse, not just a
    # stx-side inconsistency.
    atomic_write_text(route_file, content)


def remove_traefik_route(slug: str) -> None:
    """Remove the Traefik route file for a sandbox (idempotent)."""
    route_file = traefik_routes_dir() / f"{slug}.yaml"
    route_file.unlink(missing_ok=True)


def traefik_routes_mount_source(
    container: str, *, runner: CompletedProcessRunner = subprocess.run
) -> str | None:
    """The host path `container`'s live `/routes` bind mount resolves to, or
    None if the container doesn't exist or declares no such mount. `runner`
    is a seam for tests to inject a fake subprocess result."""
    result = run_bounded(
        ["docker", "inspect", "--format", "{{json .Mounts}}", container],
        runner=runner,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    try:
        mounts = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    for mount in mounts:
        if isinstance(mount, dict) and mount.get("Destination") == "/routes":
            return mount.get("Source")
    return None


def traefik_routes_warning(
    container: str, *, runner: CompletedProcessRunner = subprocess.run
) -> str | None:
    """A specific warning when `container` is running with a `/routes` mount
    that isn't stx's own routes dir — the only prior sign of this was a
    generic drift warning, which doesn't say routes are actually broken.
    None when the container isn't running (nothing to compare yet — `new`/
    `up` will mount it correctly on first start) or its mount already
    matches."""
    source = traefik_routes_mount_source(container, runner=runner)
    if source is None or Path(source).resolve() == traefik_routes_dir():
        return None
    return (
        f"warning: '{container}' is running with /routes mounted from "
        f"{source}, not stx's own routes dir ({traefik_routes_dir()}) — this "
        "sandbox's routes won't resolve until `stx up --recreate traefik` "
        "(cheap: traefik holds no database connections)"
    )
