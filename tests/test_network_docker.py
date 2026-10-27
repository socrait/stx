"""Docker-marked integration test: confirms the per-sandbox network model against a
REAL Docker daemon — two throwaway sandbox-shaped containers, each on its own
per-sandbox network, can't reach each other; a container also attached to
the other's network (simulating a link) can; a stand-in for a shared
platform service, attached to both, is reachable from either. The pure
functions in test_network.py cover `network.py`'s own subprocess calls with
a fake runner; this confirms the actual network/firewall behavior those
calls assume. Docker is not available in the dev sandbox box this was
written in — run on the host with `pytest -m docker`.

Throwaway names only (`stxtest-*`), never `stx-*` or a real sandbox slug.
Every container and network this creates is torn down by the fixture.
"""

from __future__ import annotations

import platform
import subprocess
import uuid
from types import SimpleNamespace

import pytest

from stx import network

pytestmark = pytest.mark.docker

_IMAGE = "alpine:3.20"


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True)


def _run_container(name: str, net: str) -> None:
    result = _run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "--hostname",
            name,
            "--network",
            net,
            _IMAGE,
            "sleep",
            "300",
        ]
    )
    assert result.returncode == 0, result.stderr


def _can_reach(source_container: str, target_host: str) -> bool:
    result = _run(
        ["docker", "exec", source_container, "ping", "-c", "1", "-W", "2", target_host]
    )
    return result.returncode == 0


@pytest.fixture
def two_sandboxes():
    """Two throwaway 'sandbox-shaped' containers, each on its own
    `stx-<slug>--net`-style network — named `stxtest-<id>-a`/`-b`, never
    `stx-*` or a real slug. Containers, then networks, are always removed
    on teardown, regardless of what the test did to their connections."""
    suffix = uuid.uuid4().hex[:8]
    net_a = f"stxtest-{suffix}-a-net"
    net_b = f"stxtest-{suffix}-b-net"
    container_a = f"stxtest-{suffix}-a"
    container_b = f"stxtest-{suffix}-b"

    network.ensure_network(net_a)
    network.ensure_network(net_b)
    _run_container(container_a, net_a)
    _run_container(container_b, net_b)

    yield SimpleNamespace(
        net_a=net_a, net_b=net_b, container_a=container_a, container_b=container_b
    )

    for container in (container_a, container_b):
        _run(["docker", "rm", "-f", container])
    for net in (net_a, net_b):
        network.remove_network(net)


def _run_listener(
    name: str, net: str, *, port: int, published: str | None = None
) -> None:
    """A container that keeps accepting TCP connections on `port` — stands
    in for a box's own app process. `published` (`bind_addr:host_port`),
    when given, maps `port` onto the host the same way `overlay.render`
    publishes a route's own port."""
    cmd = ["docker", "run", "-d", "--rm", "--name", name, "--hostname", name]
    if published:
        cmd += ["-p", f"{published}:{port}"]
    cmd += [
        "--network",
        net,
        _IMAGE,
        "sh",
        "-c",
        f"while true; do nc -l -p {port}; done",
    ]
    result = _run(cmd)
    assert result.returncode == 0, result.stderr


def _run_traefik_standin(name: str, net: str) -> None:
    """A container that resolves `host.docker.internal` the same way the
    real Traefik service does (`extra_hosts: host-gateway`) — stands in for
    Traefik itself, joined to no sandbox network."""
    result = _run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "--hostname",
            name,
            "--network",
            net,
            "--add-host",
            "host.docker.internal:host-gateway",
            _IMAGE,
            "sleep",
            "300",
        ]
    )
    assert result.returncode == 0, result.stderr


def _can_reach_port(source_container: str, target_host: str, port: int) -> bool:
    result = _run(
        [
            "docker",
            "exec",
            source_container,
            "nc",
            "-z",
            "-w",
            "2",
            target_host,
            str(port),
        ]
    )
    return result.returncode == 0


@pytest.fixture
def published_box():
    """A throwaway 'box'-shaped container on its own `stx-<slug>--net`-style
    network, publishing one port on the host the same way a sandbox's own
    route does — bound to `network.route_bind_address`'s choice, never
    `0.0.0.0`. Named `stxtest-<id>-box`, on `stxtest-<id>-box-net`."""
    suffix = uuid.uuid4().hex[:8]
    net = f"stxtest-{suffix}-box-net"
    container = f"stxtest-{suffix}-box"
    container_port = 8080
    host_port = 20000 + (int(uuid.uuid4().hex[:4], 16) % 5000)

    network.ensure_network(net)
    bind_addr = network.route_bind_address()
    assert bind_addr != "0.0.0.0"
    _run_listener(
        container, net, port=container_port, published=f"{bind_addr}:{host_port}"
    )

    yield SimpleNamespace(
        net=net, container=container, host_port=host_port, container_port=container_port
    )

    _run(["docker", "rm", "-f", container])
    network.remove_network(net)


class TestTraefikReachesBoxesThroughTheHost:
    """Traefik joins no sandbox network — see docs/security.md (Isolation).
    It reaches a box's own route through the host's published port instead,
    via `host.docker.internal`."""

    def test_traefik_standin_reaches_the_published_port_via_host_docker_internal(
        self, published_box
    ):
        suffix = uuid.uuid4().hex[:8]
        traefik_net = f"stxtest-{suffix}-traefik-net"
        traefik = f"stxtest-{suffix}-traefik"
        network.ensure_network(traefik_net)
        try:
            _run_traefik_standin(traefik, traefik_net)

            assert _can_reach_port(
                traefik, "host.docker.internal", published_box.host_port
            )
        finally:
            _run(["docker", "rm", "-f", traefik])
            network.remove_network(traefik_net)

    def test_a_box_on_another_sandboxs_network_cannot_reach_the_standin(
        self, published_box
    ):
        suffix = uuid.uuid4().hex[:8]
        traefik_net = f"stxtest-{suffix}-traefik-net"
        traefik = f"stxtest-{suffix}-traefik"
        other_net = f"stxtest-{suffix}-other-net"
        other_box = f"stxtest-{suffix}-other-box"
        network.ensure_network(traefik_net)
        network.ensure_network(other_net)
        try:
            _run_traefik_standin(traefik, traefik_net)
            _run_container(other_box, other_net)

            assert not _can_reach(other_box, traefik)
        finally:
            for c in (traefik, other_box):
                _run(["docker", "rm", "-f", c])
            for n in (traefik_net, other_net):
                network.remove_network(n)

    def test_a_box_on_another_sandboxs_network_cannot_reach_the_published_port(
        self, published_box
    ):
        # Docker Desktop publishes on the host's loopback, which no container
        # reaches. On Linux the port sits on `docker0`'s address, which any
        # container can reach at the Docker level; there the box's own
        # firewall drops it (trusted: own subnet only; dmz: no RFC1918).
        if platform.system() not in ("Darwin", "Windows"):
            pytest.skip("on Linux the box firewall, not Docker, blocks this")
        suffix = uuid.uuid4().hex[:8]
        other_net = f"stxtest-{suffix}-other-net"
        other_box = f"stxtest-{suffix}-other-box"
        network.ensure_network(other_net)
        try:
            _run_container(other_box, other_net)

            # No `extra_hosts`, unlike Traefik — this box can't even resolve
            # the name, so the raw bind address stands in for it.
            bind_addr = network.route_bind_address()
            assert not _can_reach_port(other_box, bind_addr, published_box.host_port)
        finally:
            _run(["docker", "rm", "-f", other_box])
            network.remove_network(other_net)


class TestSandboxNetworkIsolation:
    def test_unlinked_sandboxes_cannot_reach_each_other(self, two_sandboxes):
        s = two_sandboxes
        assert not _can_reach(s.container_a, s.container_b)
        assert not _can_reach(s.container_b, s.container_a)

    def test_a_link_attaches_to_the_targets_network_and_can_reach_it(
        self, two_sandboxes
    ):
        s = two_sandboxes
        # Simulates `stx new <a> --link name=<b>`: the linking box joins the
        # target's own network, on top of its own.
        network.connect(s.net_b, s.container_a)

        assert _can_reach(s.container_a, s.container_b)
        # Sharing a network goes both ways: b reaches a's interface on it.
        assert _can_reach(s.container_b, s.container_a)

    def test_a_shared_platform_service_reaches_both_once_connected_to_both(
        self, two_sandboxes
    ):
        s = two_sandboxes
        platform_stub = f"stxtest-{uuid.uuid4().hex[:8]}-platform"
        _run_container(platform_stub, s.net_a)
        try:
            network.connect(s.net_b, platform_stub)

            assert _can_reach(s.container_a, platform_stub)
            assert _can_reach(s.container_b, platform_stub)
            # The two sandboxes are still isolated from each other even
            # though both reach the shared service.
            assert not _can_reach(s.container_a, s.container_b)
        finally:
            _run(["docker", "rm", "-f", platform_stub])
