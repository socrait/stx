"""Docker-marked integration test: confirms init-box.sh's deny-host set makes
the bridge gateway (the host, on a Linux bridge network) unreachable, against
a REAL Docker daemon. The pure rule-builders in test_init_box.py cover the
argument shapes with stubbed iptables; this confirms the kernel actually drops
the traffic. Docker is not available in the dev sandbox box this was written
in — run on the host with `pytest -m docker`.

Throwaway names only (`stxtest-*`), never `stx-*` or a real sandbox slug.
Every container and network this creates is torn down by the fixture.
"""

from __future__ import annotations

import subprocess
import uuid

import pytest

from stx import dmz
from stx.overlay import box_runtime_dir

pytestmark = pytest.mark.docker


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True)


@pytest.fixture
def box_on_bridge():
    """A box-image container (iptables, NET_ADMIN) on its own throwaway bridge
    network, with init-box.sh mounted read-only at /opt/stx/box — the same
    place the real overlay mounts it."""
    image = dmz.ensure_built()
    suffix = uuid.uuid4().hex[:8]
    net = f"stxtest-{suffix}-net"
    container = f"stxtest-{suffix}"

    assert _run(["docker", "network", "create", net]).returncode == 0
    try:
        gateway = subprocess.run(
            [
                "docker",
                "network",
                "inspect",
                "--format",
                "{{(index .IPAM.Config 0).Gateway}}",
                net,
            ],
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert gateway, "bridge network reported no gateway"
        result = _run(
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "--name",
                container,
                "--network",
                net,
                "--cap-add",
                "NET_ADMIN",
                "-v",
                f"{box_runtime_dir()}:/opt/stx/box:ro",
                "--user",
                "0:0",
                image,
                "sleep",
                "300",
            ]
        )
        assert result.returncode == 0, result.stderr
        yield container, gateway
    finally:
        _run(["docker", "rm", "-f", container])
        _run(["docker", "network", "rm", net])


def _can_ping(container: str, host: str) -> bool:
    return (
        _run(
            ["docker", "exec", container, "ping", "-c", "1", "-W", "2", host]
        ).returncode
        == 0
    )


def test_deny_set_makes_the_gateway_unreachable(box_on_bridge):
    container, gateway = box_on_bridge
    # Only a gateway that answers before the rules proves the DROP did
    # anything. Docker Desktop's gateway lives in its VM and never answers, so
    # there this has nothing to show; a Linux host runs it for real.
    if not _can_ping(container, gateway):
        pytest.skip("this Docker host's bridge gateway doesn't answer ping")
    applied = _run(
        [
            "docker",
            "exec",
            container,
            "bash",
            "-c",
            "source /opt/stx/box/init-box.sh && apply_deny_host_set",
        ]
    )
    assert applied.returncode == 0, applied.stderr
    assert not _can_ping(container, gateway)
