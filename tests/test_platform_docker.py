"""Docker-marked integration tests: confirm against a REAL Compose/Docker
that bringing the platform up never recreates a service that doesn't need
it — the pure-function unit tests in test_platform.py cover the classifier
and run_platform_up's decision logic with a fake runner; these confirm the
actual `docker compose up` round-trip behaves the way that logic assumes.
Docker is not available in the dev sandbox box this was written in — run on
the host with `pytest -m docker`.

Each test drives its own throwaway copy of the platform: a unique project
name, container names, network, volume names, and host ports (`stxtest-*`),
rewritten from the real compose.platform.yaml into tmp_path. Never touches
the real `stx-*` singleton. The fixture tears its copy down with
`compose down -v` after every test.
"""

from __future__ import annotations

import socket
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest
import yaml

from stx import network
from stx import platform as stx_platform
from stx.core.compose import compose_command, teardown_env
from stx.platform import PostgresAdminParams

pytestmark = pytest.mark.docker


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _rewrite_compose_for_test(text: str, prefix: str) -> str:
    """A copy of the platform compose file scoped to one test run: its own
    project name, container names, network, volumes, and host ports (Docker
    picks a free one when the host port is left blank) — never the real
    `stx-*` singleton."""
    data = yaml.safe_load(text)
    data["name"] = prefix

    network_name = f"{prefix}-net"
    # External, like the real file's: stx creates the network itself before
    # `compose up`, and Compose 2.x refuses a non-external network it didn't
    # create.
    data["networks"] = {network_name: {"name": network_name, "external": True}}

    volume_renames = {
        old_name: f"{prefix}-{old_name.removeprefix('stx-')}"
        for old_name in data["volumes"]
    }
    data["volumes"] = {new: {"name": new} for new in volume_renames.values()}

    for service_name, service in data["services"].items():
        service["container_name"] = f"{prefix}-{service_name}"
        service["networks"] = [network_name]
        service["volumes"] = [
            _rename_mount_source(entry, volume_renames)
            for entry in service.get("volumes", [])
        ]
        if service_name == "postgres":
            # Left as `${STX_POSTGRES_PORT:-5432}` — the fixture pins this
            # env var to a real, pre-chosen port so a test can open its own
            # psycopg connection; every other service still gets an
            # auto-assigned (dropped) host port.
            continue
        service["ports"] = [
            _drop_host_port(entry) for entry in service.get("ports", [])
        ]

    return yaml.safe_dump(data, sort_keys=False)


def _rename_mount_source(entry: str | dict, volume_renames: dict[str, str]):
    if isinstance(entry, dict):
        return {**entry, "source": volume_renames.get(entry["source"], entry["source"])}
    source, _, rest = entry.partition(":")
    renamed = volume_renames.get(source, source)
    return f"{renamed}:{rest}"


def _drop_host_port(entry: str | dict):
    if isinstance(entry, dict):
        return {k: v for k, v in entry.items() if k != "published"}
    bind_ip, _, remainder = entry.partition(":")
    container_port = remainder.rsplit(":", 1)[-1]
    return f"{bind_ip}::{container_port}"


def _container_id(container: str) -> str | None:
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{.Id}}", container],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


@pytest.fixture
def platform_test_env(tmp_path, monkeypatch):
    """Points the platform module at a throwaway compose project instead of
    the real one: a fake package dir holding a rewritten compose.platform.yaml
    (plus the unmodified sibling files it bind-mounts), a renamed
    `container_name`, and a renamed `NETWORK` — then tears the project down.
    Postgres's own host port is pinned (not auto-assigned, unlike every other
    service) to a pre-chosen free port, so a test can open its own psycopg
    connection against this throwaway instance — see `postgres_port`."""
    monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("STX_POSTGRES_PASSWORD", "stx-docker-test-password")
    postgres_port = _free_port()
    monkeypatch.setenv("STX_POSTGRES_PORT", str(postgres_port))

    prefix = f"stxtest-{uuid.uuid4().hex[:8]}"
    real_dir = Path(stx_platform.__file__).parent
    pkg_dir = tmp_path / "fake-platform-pkg"
    pkg_dir.mkdir()
    for name in stx_platform._PACKAGE_FILES:
        content = (real_dir / name).read_bytes()
        if name == "compose.platform.yaml":
            content = _rewrite_compose_for_test(content.decode(), prefix).encode()
        (pkg_dir / name).write_bytes(content)
    compose_path = pkg_dir / "compose.platform.yaml"

    monkeypatch.setattr(stx_platform.resources, "files", lambda pkg: pkg_dir)
    monkeypatch.setattr(
        stx_platform, "container_name", lambda service: f"{prefix}-{service}"
    )
    monkeypatch.setattr(stx_platform, "NETWORK", f"{prefix}-net")

    yield SimpleNamespace(
        prefix=prefix,
        pkg_dir=pkg_dir,
        compose_path=compose_path,
        postgres_port=postgres_port,
    )

    subprocess.run(
        compose_command([compose_path], "down", "-v", "--remove-orphans"),
        cwd=str(compose_path.parent),
        env=teardown_env([compose_path], cwd=compose_path.parent),
        capture_output=True,
    )
    # `up` creates the network outside Compose, so `down` leaves it behind.
    subprocess.run(["docker", "network", "rm", f"{prefix}-net"], capture_output=True)


class TestUpNeverRecreates:
    def test_running_up_twice_with_the_same_inputs_recreates_nothing(
        self, platform_test_env
    ):
        result = stx_platform.run_platform_up(["postgres"])
        assert result is None
        first_id = _container_id(stx_platform.container_name("postgres"))
        assert first_id is not None

        result = stx_platform.run_platform_up(["postgres"])
        assert result is None
        second_id = _container_id(stx_platform.container_name("postgres"))

        assert second_id == first_id

    def test_a_changed_install_path_with_unchanged_content_recreates_nothing(
        self, platform_test_env, tmp_path, monkeypatch
    ):
        # Simulate `stx` resolving its package data from a different install
        # location (an editable checkout moved, a `uv tool` reinstall, a
        # Python minor upgrade) while the file *content* is unchanged —
        # exactly the case that used to make Compose's bind-mount source
        # path move and recreate the container underneath it.
        alt_install = tmp_path / "alt-install"
        alt_install.mkdir()
        for name in stx_platform._PACKAGE_FILES:
            (alt_install / name).write_bytes(
                (platform_test_env.pkg_dir / name).read_bytes()
            )

        result = stx_platform.run_platform_up(["postgres"])
        assert result is None
        first_id = _container_id(stx_platform.container_name("postgres"))
        assert first_id is not None

        monkeypatch.setattr(stx_platform.resources, "files", lambda pkg: alt_install)

        result = stx_platform.run_platform_up(["postgres"])
        assert result is None
        second_id = _container_id(stx_platform.container_name("postgres"))

        assert second_id == first_id

    def test_a_container_predating_the_definition_hash_label_is_differing_and_not_recreated(
        self, platform_test_env, capsys
    ):
        # Simulate a container stamped before `stx.definition_hash` existed:
        # same compose file, same container name, just started without that
        # one env var set — Compose falls back to its blank `:-` default, so
        # the label reads back absent, same as a pre-upgrade container would.
        env = platform_test_env
        start_env = stx_platform.compose_env(["postgres"])
        del start_env["STX_POSTGRES_DEFINITION_HASH"]
        network.ensure_network(stx_platform.NETWORK)

        result = subprocess.run(
            compose_command([env.compose_path], "up", "-d", "--wait", "postgres"),
            cwd=str(env.compose_path.parent),
            env=start_env,
        )
        assert result.returncode == 0

        legacy_id = _container_id(stx_platform.container_name("postgres"))
        assert legacy_id is not None

        result = stx_platform.run_platform_up(["postgres"])

        assert result is None
        assert _container_id(stx_platform.container_name("postgres")) == legacy_id
        out = capsys.readouterr().out
        assert "config changed" in out
        assert "stx up --recreate" in out


class TestProvisionerRole:
    """`run_platform_up` ensures the non-superuser `stx_provisioner` role
    (CREATEDB CREATEROLE) — these confirm it's sufficient for the
    create-roles-and-grant-on-its-databases pattern a repo's own
    `provision.steps` need (e.g. the widgetshop example's
    scripts/box-provision.sh), and that it stays bounded to roles it
    itself created (Postgres 16 semantics) — see docs/secrets.md
    (Provisioning credentials)."""

    def _connect(self, params: PostgresAdminParams):
        return psycopg.connect(
            host=params.host,
            port=params.port,
            dbname="postgres",
            user=params.user,
            password=params.password,
            autocommit=True,
            connect_timeout=5,
        )

    def test_widgetshop_provision_roles_pattern_succeeds_under_the_role(
        self, platform_test_env
    ):
        result = stx_platform.run_platform_up(["postgres"])
        assert result is None

        admin = PostgresAdminParams.build()
        provisioner = stx_platform.provisioner_params(admin)

        with self._connect(provisioner) as conn:
            # create roles (CREATEROLE) ...
            conn.execute("CREATE ROLE widgetshop_app LOGIN PASSWORD 'x'")
            # ... and grant on its databases (CREATEDB, then GRANT as owner)
            conn.execute("CREATE DATABASE widgetshop_db")
            conn.execute(
                "GRANT ALL PRIVILEGES ON DATABASE widgetshop_db TO widgetshop_app"
            )

        with self._connect(admin) as conn:
            assert conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", ("widgetshop_db",)
            ).fetchone()
            assert conn.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s", ("widgetshop_app",)
            ).fetchone()

    def test_cannot_alter_a_role_it_did_not_create(self, platform_test_env):
        # Postgres 16 semantics: CREATEROLE alone administers only roles the
        # grantee itself created — never the bootstrap admin role.
        result = stx_platform.run_platform_up(["postgres"])
        assert result is None

        admin = PostgresAdminParams.build()
        provisioner = stx_platform.provisioner_params(admin)

        with self._connect(provisioner) as conn:
            with pytest.raises(psycopg.Error):
                conn.execute(f"ALTER ROLE \"{admin.user}\" PASSWORD 'hijacked'")
