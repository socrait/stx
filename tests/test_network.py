from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from stx import network
from stx.errors import StxError


def test_sandbox_network_name():
    assert network.sandbox_network_name("fix-447") == "stx-fix-447--net"


class TestEnsureNetwork:
    def test_calls_docker_network_create(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda cmd, **kw: calls.append(cmd) or SimpleNamespace(returncode=0),
        )

        network.ensure_network("stx-demo--net")

        assert calls == [["docker", "network", "create", "stx-demo--net"]]

    def test_already_existing_network_is_idempotent(self, monkeypatch):
        def fake_run(cmd, **kw):
            if cmd[2] == "inspect":
                return SimpleNamespace(returncode=0, stderr="")
            return SimpleNamespace(
                returncode=1,
                stderr="Error: network with name stx-demo--net already exists\n",
            )

        monkeypatch.setattr(subprocess, "run", fake_run)

        network.ensure_network("stx-demo--net")  # no raise

    def test_raises_with_dockers_stderr_on_other_failure(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda cmd, **kw: SimpleNamespace(
                returncode=1, stderr="Cannot connect to the Docker daemon\n"
            ),
        )

        with pytest.raises(StxError, match="Cannot connect to the Docker daemon"):
            network.ensure_network("stx-demo--net")

    def test_a_wedged_daemon_raises_a_clear_timeout_error_instead_of_hanging(
        self, monkeypatch
    ):
        def hangs(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))

        monkeypatch.setattr(subprocess, "run", hangs)

        with pytest.raises(StxError, match="timed out"):
            network.ensure_network("stx-demo--net")


class TestRemoveNetwork:
    def test_missing_network_is_silent(self, monkeypatch, capsys):
        # Any wording: absence is decided by `inspect`, not by the rm message.
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda cmd, **kw: SimpleNamespace(
                returncode=1, stderr="network stx-demo--net not found\n"
            ),
        )

        network.remove_network("stx-demo--net")

        assert capsys.readouterr().err == ""

    def test_network_with_active_endpoints_warns_not_raises(self, monkeypatch, capsys):
        def fake_run(cmd, **kw):
            if cmd[2] == "inspect":
                return SimpleNamespace(returncode=0, stderr="")
            return SimpleNamespace(returncode=1, stderr="Error: has active endpoints\n")

        monkeypatch.setattr(subprocess, "run", fake_run)

        network.remove_network("stx-demo--net")

        assert "stx-demo--net" in capsys.readouterr().err


class TestConnect:
    def test_already_connected_is_silent(self, monkeypatch, capsys):
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda cmd, **kw: SimpleNamespace(
                returncode=1,
                stderr="Error response from daemon: endpoint already exists\n",
            ),
        )

        network.connect("stx-demo--net", "stx-postgres")

        assert capsys.readouterr().err == ""

    def test_other_failure_raises_by_default(self, monkeypatch):
        """A service the sandbox actually needs must not silently fail to
        join its network — that surfaces later as an unrelated-looking
        connection error instead of here, where the cause is still known."""
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda cmd, **kw: SimpleNamespace(
                returncode=1, stderr="Error: no such container\n"
            ),
        )

        with pytest.raises(StxError) as exc_info:
            network.connect("stx-demo--net", "stx-postgres")

        assert "stx-postgres" in str(exc_info.value)
        assert "stx-demo--net" in str(exc_info.value)

    def test_other_failure_with_best_effort_warns_instead_of_raising(
        self, monkeypatch, capsys
    ):
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda cmd, **kw: SimpleNamespace(
                returncode=1, stderr="Error: no such container\n"
            ),
        )

        network.connect("stx-demo--net", "stx-postgres", best_effort=True)

        err = capsys.readouterr().err
        assert "stx-postgres" in err
        assert "stx-demo--net" in err


class TestDisconnect:
    def test_never_raises_on_failure(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda cmd, **kw: SimpleNamespace(returncode=1, stderr="not connected"),
        )

        network.disconnect("stx-demo--net", "stx-postgres")  # no raise


class TestRouteBindAddress:
    def test_macos_binds_loopback_without_touching_docker(self):
        def _unexpected(*a, **kw):
            raise AssertionError("should not shell out on Docker Desktop")

        addr = network.route_bind_address(system="Darwin", runner=_unexpected)

        assert addr == "127.0.0.1"

    def test_windows_binds_loopback_without_touching_docker(self):
        def _unexpected(*a, **kw):
            raise AssertionError("should not shell out on Docker Desktop")

        addr = network.route_bind_address(system="Windows", runner=_unexpected)

        assert addr == "127.0.0.1"

    def test_linux_binds_the_default_bridges_gateway(self):
        def fake_run(cmd, **kw):
            assert cmd == [
                "docker",
                "network",
                "inspect",
                "bridge",
                "--format",
                "{{(index .IPAM.Config 0).Gateway}}",
            ]
            return SimpleNamespace(returncode=0, stdout="172.20.0.1\n")

        addr = network.route_bind_address(system="Linux", runner=fake_run)

        assert addr == "172.20.0.1"
        assert addr != "0.0.0.0"

    def test_linux_raises_when_the_network_has_no_gateway(self):
        def empty_run(cmd, **kw):
            return SimpleNamespace(returncode=0, stdout="")

        with pytest.raises(StxError, match="bridge"):
            network.route_bind_address(system="Linux", runner=empty_run)

    def test_linux_raises_when_docker_inspect_fails(self):
        def failing_run(cmd, **kw):
            return SimpleNamespace(returncode=1, stdout="")

        with pytest.raises(StxError):
            network.route_bind_address(system="Linux", runner=failing_run)
