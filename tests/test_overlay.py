"""The box contract: golden overlay JSON for the synthetic example repo in all
three shapes (box, box + dmz, dmz only), the repo compose allow-list (one
refusal per refused key, plus accepted shapes), and the post-merge check.

Every fixture here is either the synthetic example repo (examples/widgetshop)
or a `docker compose ... config --format json`-shaped dict sanitized from a real
Compose capture under tests/fixtures/compose/ (see `_load_fixture`). No
real-world repo value appears in this file. `check_repo_compose`/`check_merged`
are pure functions over already-parsed JSON, so none of this needs Docker; the
real `docker compose config` invocation is exercised separately by the
`docker`-marked tests in test_overlay_docker.py.

Every harness/forge secret entry is a bare `${HOST_VAR}` pass-through, never a
literal value — the actual secret only ever reaches the `docker compose`
process's own environment (`overlay.box_secret_env`, exercised in
test_new.py/test_refresh.py), not this file. The golden JSON assertions below
are what would catch a regression back toward writing a literal secret into
the overlay.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from stx import overlay
from stx.core.registry import ClaudeAuthMode
from stx.errors import StxError
from stx.manifest import Manifest

_WIDGETSHOP = Path(__file__).parent.parent / "examples" / "widgetshop"
_FIXTURES = Path(__file__).parent / "fixtures" / "compose"
_FAKE_IDENTITY = ("Test Operator", "test-operator@example.com")
#: A stand-in for the real `.git` common dir — golden-JSON assertions build
#: their expected paths from this same stub, so the fixture's own checkout
#: doesn't need to be a discoverable git repo (it isn't, inside a box: see
#: core/paths.repo_git_dir).
_FAKE_GIT_COMMON_DIR = Path("/fake/git-common-dir")

#: The real `_ensure_git_rw_dirs`, captured before the autouse fixture below
#: stubs it out — TestEnsureGitRwDirs exercises the host-side dir creation the
#: JSON-shape tests deliberately skip.
_REAL_ENSURE_GIT_RW_DIRS = overlay._ensure_git_rw_dirs


def _load_fixture(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text())


@pytest.fixture(autouse=True)
def _fixed_git_identity(monkeypatch):
    monkeypatch.setattr(overlay, "host_git_identity", lambda root: _FAKE_IDENTITY)
    monkeypatch.setattr(overlay, "repo_git_dir", lambda root: _FAKE_GIT_COMMON_DIR)
    # The git common dir is a non-existent stub here (see _FAKE_GIT_COMMON_DIR);
    # the JSON-shape tests don't exercise the host-side rw-dir creation, which
    # the Docker tests cover against a real repo.
    monkeypatch.setattr(overlay, "_ensure_git_rw_dirs", lambda git_dir, slug: None)


@pytest.fixture
def manifest() -> Manifest:
    return Manifest.load(_WIDGETSHOP)


def _box_runtime_volume(read_only: bool = True) -> dict:
    return {
        "type": "bind",
        "source": str(overlay.box_runtime_dir()),
        "target": "/opt/stx/box",
        "read_only": read_only,
    }


class TestRenderBoxOnly:
    def test_manifest_env_never_overrides_what_confines_the_box(
        self, manifest, tmp_path, monkeypatch
    ):
        # A manifest that got past the loader's own check must still not win.
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        tampered = dataclasses.replace(
            manifest, env={**manifest.env, "STX_ZONE": "dmz"}
        )
        with pytest.raises(StxError, match="STX_ZONE"):
            overlay.render(
                "demo",
                tampered,
                user="devuser",
                worktree=tmp_path / "wt",
                repo_root=_WIDGETSHOP,
                redis_index=3,
                tailnet_port=None,
                tailscale_ip=None,
                claude_auth_mode=ClaudeAuthMode.API_KEY,
            )

    def test_forge_passthrough_cannot_override_an_owned_key(
        self, manifest, tmp_path, monkeypatch
    ):
        # package_registry.env names are repo-controlled and injected via the
        # forge passthrough. ANTHROPIC_API_KEY isn't a reserved name, so the
        # loader lets it through — but it's a key the overlay owns, so the
        # passthrough must still be refused rather than clobber it.
        from stx.manifest import ForgeManifest, PackageRegistryManifest

        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        tampered = dataclasses.replace(
            manifest,
            forge=ForgeManifest(
                kind="gitlab",
                project="a/b",
                package_registry=PackageRegistryManifest(
                    group="a",
                    env={"username": "ANTHROPIC_API_KEY", "password": "PKG_TOKEN"},
                ),
            ),
        )
        with pytest.raises(StxError, match="ANTHROPIC_API_KEY"):
            overlay.render(
                "demo",
                tampered,
                user="devuser",
                worktree=tmp_path / "wt",
                repo_root=_WIDGETSHOP,
                redis_index=3,
                tailnet_port=None,
                tailscale_ip=None,
                claude_auth_mode=ClaudeAuthMode.API_KEY,
            )

    def test_golden_json(self, manifest, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        worktree = tmp_path / "wt"
        path = overlay.render(
            "demo",
            manifest,
            user="devuser",
            worktree=worktree,
            repo_root=_WIDGETSHOP,
            redis_index=3,
            tailnet_port=5003,
            tailscale_ip=None,
            claude_auth_mode=ClaudeAuthMode.API_KEY,
        )
        doc = json.loads(path.read_text())

        assert doc["name"] == "stx-demo"
        assert set(doc["services"]) == {"app"}
        assert doc["networks"] == {"stx-demo--net": {"external": True}}

        service = doc["services"]["app"]
        assert service["image"] == "widgetshop-devbox"
        assert service["user"] == "0:0"
        # `-p` (privileged mode): bash never imports a function definition from
        # the environment, so a repo-set `BASH_FUNC_iptables%%` can't run in the
        # root NET_ADMIN entrypoint even if the env reservation were bypassed.
        assert service["entrypoint"] == ["/bin/bash", "-p", "/opt/stx/box/init-box.sh"]
        assert service["command"] == ["sleep", "infinity"]
        assert service["container_name"] == "stx-demo--app"
        assert service["hostname"] == "demo"
        assert service["working_dir"] == "/workspace"
        assert service["networks"] == ["stx-demo--net"]
        assert service["init"] is True
        assert service["cap_drop"] == ["ALL"]
        assert service["cap_add"] == ["NET_ADMIN", "SETUID", "SETGID"]
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert service["ports"] == [
            {"target": 8080, "published": "5003", "host_ip": "127.0.0.1"}
        ]
        assert service["labels"] == {
            "stx.slug": "demo",
            "stx.box": "app",
            "stx.redis_index": "3",
            "stx.repo_root": str(_WIDGETSHOP),
            "stx.databases": "",
            "devcontainer.metadata": json.dumps([{"remoteUser": "devuser"}]),
        }
        assert service["volumes"] == [
            {
                "type": "bind",
                "source": str(worktree),
                "target": "/workspace",
                "read_only": False,
            },
            {
                "type": "bind",
                "source": str(overlay.repo_git_dir(_WIDGETSHOP)),
                "target": "/git",
                "read_only": True,
            },
            {
                "type": "bind",
                "source": f"{overlay.repo_git_dir(_WIDGETSHOP)}/objects",
                "target": "/git/objects",
                "read_only": False,
            },
            {
                "type": "bind",
                "source": f"{overlay.repo_git_dir(_WIDGETSHOP)}/refs/heads/stx",
                "target": "/git/refs/heads/stx",
                "read_only": False,
            },
            {
                "type": "bind",
                "source": f"{overlay.repo_git_dir(_WIDGETSHOP)}/refs/heads/tmp/demo",
                "target": "/git/refs/heads/tmp/demo",
                "read_only": False,
            },
            {
                "type": "bind",
                "source": f"{overlay.repo_git_dir(_WIDGETSHOP)}/refs/remotes",
                "target": "/git/refs/remotes",
                "read_only": False,
            },
            {
                "type": "bind",
                "source": f"{overlay.repo_git_dir(_WIDGETSHOP)}/logs",
                "target": "/git/logs",
                "read_only": False,
            },
            {
                "type": "bind",
                "source": f"{overlay.repo_git_dir(_WIDGETSHOP)}/worktrees/demo",
                "target": "/git/worktrees/demo",
                "read_only": False,
            },
            {
                "type": "bind",
                "source": f"{overlay.repo_git_dir(_WIDGETSHOP)}/worktrees/demo/commondir",
                "target": "/git/worktrees/demo/commondir",
                "read_only": True,
            },
            {
                "type": "bind",
                "source": f"{overlay.repo_git_dir(_WIDGETSHOP)}/worktrees/demo/gitdir",
                "target": "/git/worktrees/demo/gitdir",
                "read_only": True,
            },
            {
                "type": "bind",
                "source": str(overlay.drop_dir("demo")),
                "target": "/drop",
                "read_only": False,
            },
            {
                "type": "bind",
                "source": str(overlay.drop_dmz_dir("demo")),
                "target": "/drop-dmz",
                "read_only": True,
            },
            {
                "type": "bind",
                "source": str(overlay.logs_dir("demo", "app")),
                "target": "/logs",
                "read_only": False,
            },
            _box_runtime_volume(),
            {
                "type": "bind",
                "source": str(overlay.allowlist_path("demo")),
                "target": "/etc/tinyproxy/allowlist",
                "read_only": True,
            },
        ]

        env = service["environment"]
        assert env["STX_SLUG"] == "demo"
        assert env["STX_ZONE"] == "trusted"
        assert env["GIT_DIR"] == "/git/worktrees/demo"
        assert env["GIT_WORK_TREE"] == "/workspace"
        assert env["GIT_CONFIG_COUNT"] == "6"
        assert env["GIT_CONFIG_KEY_1"] == "user.name"
        assert env["GIT_CONFIG_VALUE_1"] == "Test Operator (sandbox)"
        assert env["GIT_CONFIG_KEY_2"] == "user.email"
        assert env["GIT_CONFIG_VALUE_2"] == "test-operator@example.com"
        # Auto-gc and fetch.prune off: the read-only top of /git can't take the
        # packed-refs rewrites either would trigger.
        assert env["GIT_CONFIG_KEY_4"] == "gc.auto"
        assert env["GIT_CONFIG_VALUE_4"] == "0"
        assert env["GIT_CONFIG_KEY_5"] == "fetch.prune"
        assert env["GIT_CONFIG_VALUE_5"] == "false"
        assert env["HTTPS_PROXY"] == "http://127.0.0.1:3128"
        assert env["NO_PROXY"] == env["no_proxy"]
        assert "stx-postgres" in env["NO_PROXY"]
        assert env["STX_BOX_USER"] == "devuser"
        assert env["STX_BOX_SETUP"] == "scripts/box-init.sh"
        assert env["STX_EGRESS_PORTS"] == "*/udp"
        # Harness auth: api_key mode sets exactly ANTHROPIC_API_KEY, never oauth —
        # and as a bare pass-through reference, never the secret value itself.
        assert env["ANTHROPIC_API_KEY"] == "${ANTHROPIC_API_KEY}"
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
        # Forge wiring: bare pass-through entries, no secret values.
        assert env["STX_FORGE_TOKEN"] == "${STX_FORGE_TOKEN}"
        assert env["STX_FORGE_HOST"] == "${STX_FORGE_HOST}"
        assert env["STX_FORGE_GIT_USER"] == "${STX_FORGE_GIT_USER}"
        assert env["UV_INDEX_WIDGETSHOP_USERNAME"] == "${UV_INDEX_WIDGETSHOP_USERNAME}"
        assert env["UV_INDEX_WIDGETSHOP_PASSWORD"] == "${UV_INDEX_WIDGETSHOP_PASSWORD}"
        # The manifest's own rendered env, literal — no leftover template vars.
        assert env["DATABASE_DB"] == "stx_demo"
        assert env["DATABASE_TEST_DB"] == "stx_demo_test"
        assert env["REDIS_DB"] == "3"
        assert env["PUBLIC_BASE_URL"] == "http://api.demo.localhost"

    def test_databases_label_joins_names_with_commas(
        self, manifest, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        path = overlay.render(
            "demo",
            manifest,
            user="devuser",
            worktree=tmp_path / "wt",
            repo_root=_WIDGETSHOP,
            redis_index=3,
            claude_auth_mode=ClaudeAuthMode.API_KEY,
            databases=("stx_demo", "stx_demo_test"),
        )
        doc = json.loads(path.read_text())
        assert (
            doc["services"]["app"]["labels"]["stx.databases"]
            == "stx_demo,stx_demo_test"
        )

    def test_file_and_directory_are_owner_only(self, manifest, tmp_path, monkeypatch):
        import stat

        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        path = overlay.render(
            "demo",
            manifest,
            user="devuser",
            worktree=tmp_path / "wt",
            repo_root=_WIDGETSHOP,
            redis_index=3,
            claude_auth_mode=ClaudeAuthMode.API_KEY,
        )
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700

    def test_no_secret_value_appears_in_the_overlay(
        self, manifest, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        monkeypatch.setenv("STX_CLAUDE_OAUTH_TOKEN", "sekret-oauth-value")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sekret-api-key-value")
        monkeypatch.setenv("STX_DMZ_ANTHROPIC_API_KEY", "sekret-dmz-value")
        path = overlay.render(
            "demo",
            manifest,
            user="devuser",
            worktree=tmp_path / "wt",
            repo_root=_WIDGETSHOP,
            redis_index=3,
            claude_auth_mode=ClaudeAuthMode.OAUTH,
            dmz_image="widgetshop-devbox",
            dmz_user="devuser",
        )
        text = path.read_text()
        for secret in (
            "sekret-oauth-value",
            "sekret-api-key-value",
            "sekret-dmz-value",
            "forge-tok",
        ):
            assert secret not in text

    def test_oauth_mode_omits_api_key(self, manifest, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        path = overlay.render(
            "demo",
            manifest,
            user="devuser",
            worktree=tmp_path / "wt",
            repo_root=_WIDGETSHOP,
            redis_index=3,
            claude_auth_mode=ClaudeAuthMode.OAUTH,
        )
        env = json.loads(path.read_text())["services"]["app"]["environment"]
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "${STX_CLAUDE_OAUTH_TOKEN}"
        assert "ANTHROPIC_API_KEY" not in env

    def _app_env(self, manifest, tmp_path, monkeypatch, **kwargs) -> dict:
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        path = overlay.render(
            "demo",
            manifest,
            user="devuser",
            worktree=tmp_path / "wt",
            repo_root=_WIDGETSHOP,
            redis_index=3,
            **kwargs,
        )
        return json.loads(path.read_text())["services"]["app"]["environment"]

    def test_postgres_repo_gets_libpq_address_and_own_database(
        self, manifest, tmp_path, monkeypatch
    ):
        env = self._app_env(
            manifest, tmp_path, monkeypatch, databases=("stx_demo", "stx_demo_test")
        )

        assert env["PGHOST"] == "stx-postgres"
        assert env["PGPORT"] == "5432"
        assert env["PGDATABASE"] == "stx_demo"
        assert "PGUSER" not in env
        assert "PGPASSWORD" not in env

    def test_manifest_env_overrides_the_libpq_defaults(
        self, manifest, tmp_path, monkeypatch
    ):
        custom = dataclasses.replace(
            manifest, env={**manifest.env, "PGDATABASE": "{db}_test"}
        )

        env = self._app_env(custom, tmp_path, monkeypatch, databases=("stx_demo",))

        assert env["PGDATABASE"] == "stx_demo_test"

    def test_repo_without_postgres_gets_no_libpq_vars(
        self, manifest, tmp_path, monkeypatch
    ):
        no_postgres = dataclasses.replace(
            manifest, platform=dataclasses.replace(manifest.platform, postgres=None)
        )

        env = self._app_env(no_postgres, tmp_path, monkeypatch)

        assert not {"PGHOST", "PGPORT", "PGDATABASE"} & set(env)

    def test_no_tailnet_port_omits_ports(self, manifest, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        path = overlay.render(
            "demo",
            manifest,
            user="devuser",
            worktree=tmp_path / "wt",
            repo_root=_WIDGETSHOP,
            redis_index=3,
        )
        assert "ports" not in json.loads(path.read_text())["services"]["app"]

    def test_tailscale_detected_binds_its_own_ipv4(
        self, manifest, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        path = overlay.render(
            "demo",
            manifest,
            user="devuser",
            worktree=tmp_path / "wt",
            repo_root=_WIDGETSHOP,
            redis_index=3,
            tailnet_port=5003,
            tailscale_ip="100.101.102.103",
        )
        ports = json.loads(path.read_text())["services"]["app"]["ports"]
        assert ports[0]["host_ip"] == "100.101.102.103"

    def test_no_tailscale_binds_loopback_not_wildcard(
        self, manifest, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        path = overlay.render(
            "demo",
            manifest,
            user="devuser",
            worktree=tmp_path / "wt",
            repo_root=_WIDGETSHOP,
            redis_index=3,
            tailnet_port=5003,
            tailscale_ip=None,
        )
        ports = json.loads(path.read_text())["services"]["app"]["ports"]
        assert ports[0]["host_ip"] == "127.0.0.1"
        assert ports[0]["host_ip"] != "0.0.0.0"

    def test_publish_selects_the_named_route_port(
        self, manifest, tmp_path, monkeypatch
    ):
        import dataclasses

        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        multi_route = dataclasses.replace(
            manifest, routes={"api": 8080, "admin": 9090}, publish="admin"
        )
        path = overlay.render(
            "demo",
            multi_route,
            user="devuser",
            worktree=tmp_path / "wt",
            repo_root=_WIDGETSHOP,
            redis_index=3,
            tailnet_port=5003,
        )
        ports = json.loads(path.read_text())["services"]["app"]["ports"]
        assert ports[0]["target"] == 9090

    def test_routes_without_publish_omits_ports(self, manifest, tmp_path, monkeypatch):
        import dataclasses

        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        unpublished = dataclasses.replace(manifest, publish=None)
        path = overlay.render(
            "demo",
            unpublished,
            user="devuser",
            worktree=tmp_path / "wt",
            repo_root=_WIDGETSHOP,
            redis_index=3,
            tailnet_port=5003,
        )
        assert "ports" not in json.loads(path.read_text())["services"]["app"]

    def test_routes_publish_each_route_port_at_the_chosen_host_address(
        self, manifest, tmp_path, monkeypatch
    ):
        import dataclasses

        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        multi_route = dataclasses.replace(
            manifest, routes={"api": 8080, "admin": 9090}, publish=None
        )
        path = overlay.render(
            "demo",
            multi_route,
            user="devuser",
            worktree=tmp_path / "wt",
            repo_root=_WIDGETSHOP,
            redis_index=3,
            route_ports={"api": 5101, "admin": 5102},
            route_bind_addr="172.20.0.1",
        )
        ports = json.loads(path.read_text())["services"]["app"]["ports"]
        assert {"target": 8080, "published": "5101", "host_ip": "172.20.0.1"} in ports
        assert {"target": 9090, "published": "5102", "host_ip": "172.20.0.1"} in ports
        assert all(p["host_ip"] != "0.0.0.0" for p in ports)

    def test_no_routes_publishes_no_route_ports(self, tmp_path, monkeypatch):
        import dataclasses

        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        no_routes = dataclasses.replace(
            Manifest.load(_WIDGETSHOP), routes={}, publish=None
        )
        path = overlay.render(
            "demo",
            no_routes,
            user="devuser",
            worktree=tmp_path / "wt",
            repo_root=_WIDGETSHOP,
            redis_index=3,
            route_ports={},
            route_bind_addr="172.20.0.1",
        )
        assert "ports" not in json.loads(path.read_text())["services"]["app"]

    def test_route_port_falls_back_to_loopback_never_wildcard_when_unset(
        self, manifest, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        path = overlay.render(
            "demo",
            manifest,
            user="devuser",
            worktree=tmp_path / "wt",
            repo_root=_WIDGETSHOP,
            redis_index=3,
            route_ports={"api": 5101},
        )
        ports = json.loads(path.read_text())["services"]["app"]["ports"]
        route_port = next(p for p in ports if p["published"] == "5101")
        assert route_port["host_ip"] == "127.0.0.1"
        assert route_port["host_ip"] != "0.0.0.0"

    def test_requires_user_when_manifest_given(self, manifest, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        with pytest.raises(StxError):
            overlay.render(
                "demo",
                manifest,
                worktree=tmp_path / "wt",
                repo_root=_WIDGETSHOP,
                redis_index=3,
            )


class TestEnsureGitRwDirs:
    """`_ensure_git_rw_dirs` creates, on the host, exactly the subtrees the
    /git mounts reopen read-write — so Docker binds a real dir, not a
    root-owned empty one the box user can't write."""

    def test_creates_both_writable_branch_namespaces_not_all_of_refs_heads(
        self, tmp_path
    ):
        git_dir = tmp_path / ".git"
        _REAL_ENSURE_GIT_RW_DIRS(git_dir, "demo")
        # The box writes its own branch under refs/heads/stx/ and its scratch
        # branches under refs/heads/tmp/<slug>/ — those two dirs are the mounts,
        # so both must exist before `compose up`.
        assert (git_dir / "refs" / "heads" / "stx").is_dir()
        assert (git_dir / "refs" / "heads" / "tmp" / "demo").is_dir()
        # A sibling slug's scratch namespace is *not* created or mounted here —
        # refs/heads/tmp/ stays read-only, so a box can't write tmp/<other>/.
        assert not (git_dir / "refs" / "heads" / "tmp" / "other").exists()

    def test_creates_the_other_reopened_subtrees(self, tmp_path):
        git_dir = tmp_path / ".git"
        _REAL_ENSURE_GIT_RW_DIRS(git_dir, "demo")
        for sub in ("objects", "refs/remotes", "logs", "worktrees/demo"):
            assert (git_dir / sub).is_dir()


class TestRenderWithLinks:
    def _links(self) -> dict:
        from stx.core.registry import LinkRecord

        return {
            "api": LinkRecord(
                target="be-1", route="api", port=8080, container="stx-be-1--app"
            )
        }

    def test_attaches_to_the_target_network(self, manifest, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        path = overlay.render(
            "web-1",
            manifest,
            user="devuser",
            worktree=tmp_path / "wt",
            repo_root=_WIDGETSHOP,
            redis_index=3,
            links=self._links(),
        )
        doc = json.loads(path.read_text())

        assert doc["networks"] == {
            "stx-web-1--net": {"external": True},
            "stx-be-1--net": {"external": True},
        }
        assert doc["services"]["app"]["networks"] == [
            "stx-web-1--net",
            "stx-be-1--net",
        ]

    def test_target_container_added_to_no_proxy(self, manifest, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        path = overlay.render(
            "web-1",
            manifest,
            user="devuser",
            worktree=tmp_path / "wt",
            repo_root=_WIDGETSHOP,
            redis_index=3,
            links=self._links(),
        )
        env = json.loads(path.read_text())["services"]["app"]["environment"]

        assert "stx-be-1--app" in env["NO_PROXY"]
        assert env["NO_PROXY"] == env["no_proxy"]

    def test_no_links_attaches_only_its_own_network(
        self, manifest, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        path = overlay.render(
            "web-1",
            manifest,
            user="devuser",
            worktree=tmp_path / "wt",
            repo_root=_WIDGETSHOP,
            redis_index=3,
        )
        doc = json.loads(path.read_text())

        assert doc["networks"] == {"stx-web-1--net": {"external": True}}
        assert doc["services"]["app"]["networks"] == ["stx-web-1--net"]
        assert "stx-be-1--app" not in doc["services"]["app"]["environment"]["NO_PROXY"]


class TestRenderBoxAndDmz:
    def test_golden_json(self, manifest, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        snapshot = tmp_path / "snapshot"
        path = overlay.render(
            "demo",
            manifest,
            user="devuser",
            worktree=tmp_path / "wt",
            repo_root=_WIDGETSHOP,
            redis_index=3,
            dmz_image="widgetshop-devbox",
            dmz_user="devuser",
            dmz_snapshot=snapshot,
        )
        doc = json.loads(path.read_text())

        assert set(doc["services"]) == {"app", "dmz"}
        assert doc["networks"] == {
            "stx-demo--net": {"external": True},
            "dmz-net": {},
        }

        dmz = doc["services"]["dmz"]
        assert dmz["image"] == "widgetshop-devbox"
        assert dmz["user"] == "0:0"
        assert dmz["container_name"] == "stx-demo--dmz"
        assert dmz["hostname"] == "demo-dmz"
        assert dmz["networks"] == ["dmz-net"]
        assert dmz["environment"] == {
            "STX_SLUG": "demo",
            "STX_ZONE": "dmz",
            "ENVIRONMENT": "local",
            # Bare pass-through — never the secret value itself.
            "ANTHROPIC_API_KEY": "${STX_DMZ_ANTHROPIC_API_KEY}",
            "STX_BOX_USER": "devuser",
        }
        assert dmz["volumes"] == [
            {
                "type": "bind",
                "source": str(overlay.drop_dmz_dir("demo")),
                "target": "/drop",
                "read_only": False,
            },
            {
                "type": "bind",
                "source": str(overlay.logs_dir("demo", "dmz")),
                "target": "/logs",
                "read_only": False,
            },
            _box_runtime_volume(),
            {
                "type": "bind",
                "source": str(snapshot),
                "target": "/workspace",
                "read_only": True,
            },
        ]
        assert dmz["labels"] == {
            "stx.slug": "demo",
            "stx.box": "dmz",
            "devcontainer.metadata": json.dumps([{"remoteUser": "devuser"}]),
        }

    def test_no_snapshot_omits_workspace_mount(self, manifest, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        path = overlay.render(
            "demo",
            manifest,
            user="devuser",
            worktree=tmp_path / "wt",
            repo_root=_WIDGETSHOP,
            redis_index=3,
            dmz_image="widgetshop-devbox",
            dmz_user="devuser",
        )
        dmz_volumes = json.loads(path.read_text())["services"]["dmz"]["volumes"]
        assert all(v["target"] != "/workspace" for v in dmz_volumes)


class TestRenderDmzOnly:
    def test_golden_json(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        path = overlay.render(
            "triage",
            None,
            dmz_image="widgetshop-devbox",
            dmz_user="devuser",
        )
        doc = json.loads(path.read_text())

        assert doc["name"] == "stx-triage"
        assert set(doc["services"]) == {"dmz"}
        assert doc["networks"] == {"dmz-net": {}}

    def test_requires_user(self):
        with pytest.raises(StxError):
            overlay.render("triage", None, dmz_image="widgetshop-devbox")

    def test_requires_at_least_one_of_manifest_or_dmz_image(self):
        with pytest.raises(StxError):
            overlay.render("triage", None)


def _valid_repo_config() -> dict:
    """Sanitized real `docker compose -f compose.yaml config --format json
    --no-interpolate` output for examples/widgetshop/compose.yaml — the accepted
    baseline every refusal test below mutates one key on."""
    return _load_fixture("repo_widgetshop.json")


class TestCheckRepoCompose:
    def test_accepts_the_widgetshop_shape(self):
        overlay.check_repo_compose(
            _valid_repo_config(),
            box_name="app",
            source="compose.yaml",
            repo_id="widgetshop",
        )

    def test_top_level_refusal_says_top_level_not_root(self):
        config = _valid_repo_config()
        config["networks"] = {"whatever": {}}
        with pytest.raises(StxError, match="top level"):
            overlay.check_repo_compose(
                config, box_name="app", source="compose.yaml", repo_id="widgetshop"
            )

    def test_top_level_refusal_never_says_service_root(self):
        config = _valid_repo_config()
        config["networks"] = {"whatever": {}}
        with pytest.raises(StxError) as exc_info:
            overlay.check_repo_compose(
                config, box_name="app", source="compose.yaml", repo_id="widgetshop"
            )
        assert "service '<root>'" not in str(exc_info.value)

    def test_explanation_stays_out_of_the_key_name(self):
        config = _valid_repo_config()
        config["services"]["other"] = config["services"].pop("app")
        with pytest.raises(StxError, match=r"key 'services' is not allowed \("):
            overlay.check_repo_compose(
                config, box_name="app", source="compose.yaml", repo_id="widgetshop"
            )

    @pytest.mark.parametrize(
        "environment",
        [
            {"STX_SETUP_LOG_FILE": "/etc/passwd"},
            ["STX_ZONE=dmz"],
        ],
    )
    def test_refuses_environment_names_that_confine_the_box(self, environment):
        config = _valid_repo_config()
        config["services"]["app"]["environment"] = environment
        with pytest.raises(StxError, match="reserved"):
            overlay.check_repo_compose(
                config, box_name="app", source="compose.yaml", repo_id="widgetshop"
            )

    def test_accepts_shm_size(self):
        config = _valid_repo_config()
        config["services"]["app"]["shm_size"] = "128mb"
        overlay.check_repo_compose(
            config, box_name="app", source="compose.yaml", repo_id="widgetshop"
        )

    def test_accepts_x_prefixed_top_level_keys(self):
        config = _valid_repo_config()
        config["x-devbox-build"] = {"image": "whatever"}
        overlay.check_repo_compose(
            config, box_name="app", source="compose.yaml", repo_id="widgetshop"
        )

    def test_accepts_build_args_and_target(self):
        config = _valid_repo_config()
        config["services"]["app"]["build"]["args"] = {"FOO": "bar"}
        config["services"]["app"]["build"]["target"] = "runtime"
        overlay.check_repo_compose(
            config, box_name="app", source="compose.yaml", repo_id="widgetshop"
        )

    @pytest.mark.parametrize("key", ["networks", "configs", "secrets"])
    def test_refuses_disallowed_top_level_section(self, key):
        config = _valid_repo_config()
        config[key] = {"whatever": {}}
        with pytest.raises(StxError, match=key):
            overlay.check_repo_compose(
                config, box_name="app", source="compose.yaml", repo_id="widgetshop"
            )

    def test_refuses_a_second_service(self):
        config = _valid_repo_config()
        config["services"]["dmz"] = {"build": {"context": "."}}
        with pytest.raises(StxError):
            overlay.check_repo_compose(
                config, box_name="app", source="compose.yaml", repo_id="widgetshop"
            )

    def test_refuses_wrong_single_service_name(self):
        config = _valid_repo_config()
        config["services"] = {"other": config["services"]["app"]}
        with pytest.raises(StxError):
            overlay.check_repo_compose(
                config, box_name="app", source="compose.yaml", repo_id="widgetshop"
            )

    @pytest.mark.parametrize(
        "key,value",
        [
            ("privileged", True),
            ("network_mode", "host"),
            ("pid", "host"),
            ("ipc", "host"),
            ("devices", ["/dev/kvm"]),
            ("cap_add", ["SYS_ADMIN"]),
            ("security_opt", ["seccomp:unconfined"]),
            ("networks", ["stx-net"]),
            ("ports", ["8080:8080"]),
            ("extra_hosts", ["foo:1.2.3.4"]),
            ("dns", ["8.8.8.8"]),
            ("sysctls", {"net.core.somaxconn": "1024"}),
            ("tmpfs", ["/tmp"]),
            ("secrets", ["my-secret"]),
            ("configs", ["my-config"]),
            ("links", ["other"]),
            ("user", "root"),
            ("profiles", ["dev"]),
            ("entrypoint", ["/bin/sh"]),
        ],
    )
    def test_refuses_each_disallowed_service_key(self, key, value):
        config = _valid_repo_config()
        config["services"]["app"][key] = value
        with pytest.raises(StxError, match=key):
            overlay.check_repo_compose(
                config, box_name="app", source="compose.yaml", repo_id="widgetshop"
            )

    def test_refuses_disallowed_build_subkey(self):
        config = _valid_repo_config()
        config["services"]["app"]["build"]["labels"] = {"foo": "bar"}
        with pytest.raises(StxError, match="build.labels"):
            overlay.check_repo_compose(
                config, box_name="app", source="compose.yaml", repo_id="widgetshop"
            )

    def test_refuses_external_named_volume(self):
        config = _valid_repo_config()
        config["volumes"]["widgetshop-cache"]["external"] = True
        with pytest.raises(StxError, match="external"):
            overlay.check_repo_compose(
                config, box_name="app", source="compose.yaml", repo_id="widgetshop"
            )

    def test_refuses_named_volume_with_driver_opts(self):
        config = _valid_repo_config()
        config["volumes"]["widgetshop-cache"]["driver_opts"] = {"foo": "bar"}
        with pytest.raises(StxError, match="driver_opts"):
            overlay.check_repo_compose(
                config, box_name="app", source="compose.yaml", repo_id="widgetshop"
            )

    def test_accepts_composes_own_default_volume_name(self):
        # Compose fills this in itself (`<project>_<volume-key>`) even when
        # the repo's own file never wrote a `name:` — never author intent.
        config = _valid_repo_config()
        assert config["volumes"]["widgetshop-cache"]["name"] == (
            f"{config['name']}_widgetshop-cache"
        )
        overlay.check_repo_compose(
            config, box_name="app", source="compose.yaml", repo_id="widgetshop"
        )

    def test_accepts_an_explicit_shared_cache_name_scoped_to_this_repo(self):
        config = _valid_repo_config()
        config["volumes"]["widgetshop-cache"]["name"] = "stx-cache-widgetshop--npm"
        overlay.check_repo_compose(
            config, box_name="app", source="compose.yaml", repo_id="widgetshop"
        )

    def test_refuses_a_shared_cache_name_scoped_to_another_repo(self):
        # The bare `stx-cache-` prefix is no longer enough — a name must carry
        # this repo's own id, so one repo can't name its way onto another's
        # (or a differently-named sandbox's) shared cache volume.
        config = _valid_repo_config()
        config["volumes"]["widgetshop-cache"]["name"] = "stx-cache-otherrepo--npm"
        with pytest.raises(StxError, match="stx-cache-widgetshop--"):
            overlay.check_repo_compose(
                config, box_name="app", source="compose.yaml", repo_id="widgetshop"
            )

    def test_refuses_a_prefix_repos_cache_name_for_a_longer_named_repo(self):
        # `app`'s prefix `stx-cache-app-` was also a prefix of `app-web`'s
        # `stx-cache-app-web--node`, so repo `app` could name (and mount) repo
        # `app-web`'s cache. The `--` delimiter stops it: `stx-cache-app--` is
        # not a prefix of `stx-cache-app-web--node`.
        config = _valid_repo_config()
        config["volumes"]["widgetshop-cache"]["name"] = "stx-cache-app-web--node"
        with pytest.raises(StxError, match="stx-cache-app--"):
            overlay.check_repo_compose(
                config, box_name="app", source="compose.yaml", repo_id="app"
            )

    def test_accepts_a_longer_named_repos_own_cache_name(self):
        config = _valid_repo_config()
        config["volumes"]["widgetshop-cache"]["name"] = "stx-cache-app-web--node"
        overlay.check_repo_compose(
            config, box_name="app", source="compose.yaml", repo_id="app-web"
        )

    @pytest.mark.parametrize("repo_id", ["app-", "ab--cd"])
    def test_refuses_a_repo_id_that_cannot_delimit_its_cache_prefix(self, repo_id):
        # A repo id ending in `-` or containing `--` would blur the `--` that
        # separates the id from the cache name — refuse it and say to rename.
        with pytest.raises(StxError, match="rename the directory"):
            overlay.check_repo_compose(
                _valid_repo_config(),
                box_name="app",
                source="compose.yaml",
                repo_id=repo_id,
            )

    def test_refuses_an_explicit_name_without_the_shared_cache_prefix(self):
        config = _valid_repo_config()
        config["volumes"]["widgetshop-cache"]["name"] = "stx-postgres-data"
        with pytest.raises(StxError, match="stx-cache-widgetshop-"):
            overlay.check_repo_compose(
                config, box_name="app", source="compose.yaml", repo_id="widgetshop"
            )

    def test_refuses_named_volume_with_labels(self):
        config = _valid_repo_config()
        config["volumes"]["widgetshop-cache"]["labels"] = {"foo": "bar"}
        with pytest.raises(StxError, match="labels"):
            overlay.check_repo_compose(
                config, box_name="app", source="compose.yaml", repo_id="widgetshop"
            )

    def test_refuses_named_volume_with_non_local_driver(self):
        config = _valid_repo_config()
        config["volumes"]["widgetshop-cache"]["driver"] = "nfs"
        with pytest.raises(StxError, match="driver"):
            overlay.check_repo_compose(
                config, box_name="app", source="compose.yaml", repo_id="widgetshop"
            )

    def test_accepts_named_volume_with_explicit_local_driver(self):
        config = _valid_repo_config()
        config["volumes"]["widgetshop-cache"]["driver"] = "local"
        overlay.check_repo_compose(
            config, box_name="app", source="compose.yaml", repo_id="widgetshop"
        )

    def test_refuses_bind_mount(self):
        config = _valid_repo_config()
        config["services"]["app"]["volumes"].append(
            {"type": "bind", "source": "/etc/passwd", "target": "/etc/passwd"}
        )
        with pytest.raises(StxError, match="bind mount"):
            overlay.check_repo_compose(
                config, box_name="app", source="compose.yaml", repo_id="widgetshop"
            )

    def test_refuses_undeclared_named_volume(self):
        config = _valid_repo_config()
        config["services"]["app"]["volumes"].append(
            {"type": "volume", "source": "not-declared", "target": "/opt/x"}
        )
        with pytest.raises(StxError, match="undeclared"):
            overlay.check_repo_compose(
                config, box_name="app", source="compose.yaml", repo_id="widgetshop"
            )

    @pytest.mark.parametrize(
        "target",
        [
            "/workspace",
            "/git",
            "/drop",
            "/drop-dmz",
            "/logs/sub",
            "/opt/stx/box",
            "/etc/tinyproxy",
            "/run/stx",
        ],
    )
    def test_refuses_named_volume_at_reserved_path(self, target):
        config = _valid_repo_config()
        config["volumes"]["reserved"] = {}
        config["services"]["app"]["volumes"].append(
            {"type": "volume", "source": "reserved", "target": target}
        )
        with pytest.raises(StxError, match="reserved path"):
            overlay.check_repo_compose(
                config, box_name="app", source="compose.yaml", repo_id="widgetshop"
            )


class TestRefuseRepoProjectName:
    def test_accepts_a_file_with_no_top_level_name(self, tmp_path):
        compose = tmp_path / "compose.yaml"
        compose.write_text("services:\n  app:\n    image: x\n")
        overlay.refuse_repo_project_name(compose, source=str(compose))

    def test_refuses_a_repo_authored_top_level_name(self, tmp_path):
        compose = tmp_path / "compose.yaml"
        compose.write_text("name: stx-redis\nservices:\n  app:\n    image: x\n")
        with pytest.raises(StxError, match="name"):
            overlay.refuse_repo_project_name(compose, source=str(compose))

    def test_unreadable_file_is_left_for_compose_config_to_report(self, tmp_path):
        # Not valid YAML — refuse_repo_project_name stays silent so the real
        # `docker compose config` surfaces it, rather than raising a confusing
        # "name" error.
        compose = tmp_path / "compose.yaml"
        compose.write_text("name: [unterminated\n")
        overlay.refuse_repo_project_name(compose, source=str(compose))


class TestCheckBuildContext:
    def test_no_build_is_a_noop(self, tmp_path):
        config = {"services": {"app": {}}}
        overlay.check_build_context(
            config, box_name="app", root=tmp_path, source="compose.yaml"
        )

    def test_no_context_is_a_noop(self, tmp_path):
        config = {"services": {"app": {"build": {"dockerfile": "Dockerfile"}}}}
        overlay.check_build_context(
            config, box_name="app", root=tmp_path, source="compose.yaml"
        )

    def test_accepts_dot_context(self, tmp_path):
        config = {"services": {"app": {"build": {"context": "."}}}}
        overlay.check_build_context(
            config, box_name="app", root=tmp_path, source="compose.yaml"
        )

    def test_accepts_nested_subdir_context(self, tmp_path):
        (tmp_path / ".devcontainer").mkdir()
        config = {"services": {"app": {"build": {"context": "./.devcontainer"}}}}
        overlay.check_build_context(
            config, box_name="app", root=tmp_path, source="compose.yaml"
        )

    def test_refuses_dotdot_escape(self, tmp_path):
        config = {"services": {"app": {"build": {"context": ".."}}}}
        with pytest.raises(StxError, match="outside the repo root"):
            overlay.check_build_context(
                config, box_name="app", root=tmp_path, source="compose.yaml"
            )

    def test_refuses_absolute_path_outside_root(self, tmp_path):
        config = {"services": {"app": {"build": {"context": "/etc"}}}}
        with pytest.raises(StxError, match="outside the repo root"):
            overlay.check_build_context(
                config, box_name="app", root=tmp_path, source="compose.yaml"
            )

    def test_accepts_absolute_path_inside_root(self, tmp_path):
        config = {"services": {"app": {"build": {"context": str(tmp_path)}}}}
        overlay.check_build_context(
            config, box_name="app", root=tmp_path, source="compose.yaml"
        )

    def test_refuses_symlink_escape(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (root / "evil").symlink_to(outside)
        config = {"services": {"app": {"build": {"context": "./evil"}}}}
        with pytest.raises(StxError, match="outside the repo root"):
            overlay.check_build_context(
                config, box_name="app", root=root, source="compose.yaml"
            )

    @pytest.mark.parametrize(
        "context",
        [
            "https://github.com/example/repo.git",
            "git://github.com/example/repo.git",
            "git@github.com:example/repo.git",
            "http://example.com/context.tar.gz",
        ],
    )
    def test_refuses_url_or_git_context(self, tmp_path, context):
        config = {"services": {"app": {"build": {"context": context}}}}
        with pytest.raises(StxError, match="URL/git context"):
            overlay.check_build_context(
                config, box_name="app", root=tmp_path, source="compose.yaml"
            )


class TestCheckMerged:
    def _fixtures(self, *, dmz: bool = False) -> tuple[dict, dict]:
        suffix = "_dmz" if dmz else ""
        return (
            _load_fixture(f"merged_app{suffix}.json"),
            _load_fixture(f"overlay_alone_app{suffix}.json"),
        )

    def test_accepts_exact_match(self):
        merged, overlay_alone = self._fixtures()
        overlay.check_merged(merged, overlay_alone, source="overlay.json")

    def test_accepts_exact_match_with_dmz(self):
        merged, overlay_alone = self._fixtures(dmz=True)
        overlay.check_merged(merged, overlay_alone, source="overlay.json")

    def test_refuses_mismatched_cap_add(self):
        merged, overlay_alone = self._fixtures()
        merged["services"]["app"]["cap_add"] = ["NET_ADMIN"]
        with pytest.raises(StxError, match="cap_add"):
            overlay.check_merged(merged, overlay_alone, source="overlay.json")

    def test_refuses_privileged_reappearing(self):
        merged, overlay_alone = self._fixtures()
        merged["services"]["app"]["privileged"] = True
        with pytest.raises(StxError, match="privileged"):
            overlay.check_merged(merged, overlay_alone, source="overlay.json")

    def test_refuses_missing_reserved_mount(self):
        merged, overlay_alone = self._fixtures()
        merged["services"]["app"]["volumes"] = []
        with pytest.raises(StxError, match="volumes"):
            overlay.check_merged(merged, overlay_alone, source="overlay.json")

    def test_refuses_missing_service_entirely(self):
        _, overlay_alone = self._fixtures()
        with pytest.raises(StxError, match="missing"):
            overlay.check_merged({"services": {}}, overlay_alone, source="overlay.json")

    def test_refuses_extra_dmz_environment_key(self):
        merged, overlay_alone = self._fixtures(dmz=True)
        merged["services"]["dmz"]["environment"]["DATABASE_PASSWORD"] = "leak"
        with pytest.raises(StxError, match="environment"):
            overlay.check_merged(merged, overlay_alone, source="overlay.json")

    def test_accepts_dmz_environment_within_allowlist(self):
        merged, overlay_alone = self._fixtures(dmz=True)
        merged["services"]["dmz"]["environment"] = {
            "STX_SLUG": "x",
            "STX_BOX_USER": "devuser",
        }
        overlay.check_merged(merged, overlay_alone, source="overlay.json")
