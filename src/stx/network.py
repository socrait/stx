"""Per-sandbox Docker networks — see docs/security.md (Isolation). Platform
service containers belong to a different compose project than a sandbox's
own box, so attaching them to a sandbox's network is this module's job, not
Compose's.
"""

from __future__ import annotations

import platform
import subprocess
import sys
from collections.abc import Callable

from .core.compose import run_bounded
from .core.names import sandbox_network_name  # noqa: F401 — re-exported for callers
from .errors import StxError

CompletedProcessRunner = Callable[..., "subprocess.CompletedProcess[str]"]

#: `platform.system()` values Docker Desktop runs on — it forwards
#: `host.docker.internal` to the host's own loopback regardless of which
#: Docker network resolves it, so loopback is reachable from there. Linux has
#: no such forwarding — see `route_bind_address`.
_DOCKER_DESKTOP_SYSTEMS = ("Darwin", "Windows")

#: On Linux, `host-gateway` resolves to this network's gateway (`docker0`),
#: whichever network the resolving container is on.
_DEFAULT_BRIDGE_NETWORK = "bridge"

#: A hung daemon must fail a command, not hang it — see docs/reference.md.
_DOCKER_TIMEOUT_SECONDS = 30


def ensure_network(name: str, *, runner: CompletedProcessRunner | None = None) -> None:
    """`docker network create`, idempotent — already existing is success.
    Raises with Docker's own stderr on any other failure (e.g. the daemon
    is unreachable): a sandbox provisioned against a network that was never
    created fails confusingly later, as if stx itself were broken, instead
    of here, where the actual cause is still in hand. `runner` is a seam
    for tests to inject a fake subprocess result; resolved against
    `subprocess.run` at call time, not import time, so patching that
    attribute (as most of this codebase's own tests do) still works
    without passing `runner` through every call site."""
    runner = runner or subprocess.run
    result = run_bounded(
        ["docker", "network", "create", name],
        runner=runner,
        capture_output=True,
        text=True,
        timeout=_DOCKER_TIMEOUT_SECONDS,
    )
    if result.returncode != 0 and not _network_exists(name, runner=runner):
        raise StxError(f"could not create network '{name}': {result.stderr.strip()}")


def remove_network(name: str, *, runner: CompletedProcessRunner | None = None) -> None:
    """`docker network rm`, idempotent — absent is success. A network still
    holding an attached container (e.g. another sandbox's link) is left in
    place with a warning; never raises, so a caller mid-teardown isn't
    stopped by it. `runner` is a seam for tests to inject a fake subprocess
    result; see `ensure_network` for why it's resolved at call time."""
    runner = runner or subprocess.run
    result = run_bounded(
        ["docker", "network", "rm", name],
        runner=runner,
        capture_output=True,
        text=True,
        timeout=_DOCKER_TIMEOUT_SECONDS,
    )
    if result.returncode != 0 and _network_exists(name, runner=runner):
        print(
            f"  warning: could not remove network '{name}': {result.stderr.strip()}",
            file=sys.stderr,
        )


def _network_exists(name: str, *, runner: CompletedProcessRunner | None = None) -> bool:
    runner = runner or subprocess.run
    # Docker's "missing network" wording differs between versions; ask instead.
    result = run_bounded(
        ["docker", "network", "inspect", name],
        runner=runner,
        capture_output=True,
        text=True,
        timeout=_DOCKER_TIMEOUT_SECONDS,
    )
    return result.returncode == 0


def connect(
    network: str,
    container: str,
    *,
    best_effort: bool = False,
    runner: CompletedProcessRunner | None = None,
) -> None:
    """`docker network connect`, idempotent — already connected is success.
    Raises on any other failure (e.g. the container isn't up yet): a box
    that silently never joined a network it needs fails later with an
    unrelated-looking connection error instead of here. Pass `best_effort`
    for a reattachment that's allowed to fail without aborting its caller
    (e.g. reconciling every registered sandbox's network against a platform
    service that just restarted — a stale or already-torn-down sandbox
    among them is routine, not exceptional), which warns instead. `runner`
    is a seam for tests to inject a fake subprocess result; see
    `ensure_network` for why it's resolved at call time."""
    runner = runner or subprocess.run
    result = run_bounded(
        ["docker", "network", "connect", network, container],
        runner=runner,
        capture_output=True,
        text=True,
        timeout=_DOCKER_TIMEOUT_SECONDS,
    )
    if result.returncode == 0 or "already exists" in result.stderr:
        return
    message = f"could not connect '{container}' to '{network}': {result.stderr.strip()}"
    if best_effort:
        print(f"  warning: {message}", file=sys.stderr)
    else:
        raise StxError(message)


def disconnect(
    network: str, container: str, *, runner: CompletedProcessRunner | None = None
) -> None:
    """`docker network disconnect`, idempotent — not connected, missing
    network, and missing container are all success; never raises. `runner`
    is a seam for tests to inject a fake subprocess result; see
    `ensure_network` for why it's resolved at call time."""
    runner = runner or subprocess.run
    run_bounded(
        ["docker", "network", "disconnect", network, container],
        runner=runner,
        capture_output=True,
        timeout=_DOCKER_TIMEOUT_SECONDS,
    )


def route_bind_address(
    *,
    system: str | None = None,
    runner: CompletedProcessRunner = subprocess.run,
) -> str:
    """Host address a route's published port binds to so Traefik, reaching it
    via `host.docker.internal`, lands on the same address — never `0.0.0.0`.
    See docs/reference.md (Routing). `system`/`runner` are seams for tests.
    """
    system = system if system is not None else platform.system()
    if system in _DOCKER_DESKTOP_SYSTEMS:
        return "127.0.0.1"
    return _network_gateway(_DEFAULT_BRIDGE_NETWORK, runner=runner)


def _network_gateway(name: str, *, runner: CompletedProcessRunner) -> str:
    result = run_bounded(
        [
            "docker",
            "network",
            "inspect",
            name,
            "--format",
            "{{(index .IPAM.Config 0).Gateway}}",
        ],
        runner=runner,
        capture_output=True,
        text=True,
        timeout=_DOCKER_TIMEOUT_SECONDS,
    )
    gateway = result.stdout.strip() if result.returncode == 0 else ""
    if not gateway:
        raise StxError(f"could not determine the gateway address for network '{name}'")
    return gateway
