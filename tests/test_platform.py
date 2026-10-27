import re
import subprocess
from types import SimpleNamespace

import pytest
import yaml

import stx.platform as _platform
from stx.errors import StxError
from stx.platform import (
    compose_file,
    ensure_platform_files,
    required_secrets,
    services_for,
)


@pytest.fixture(autouse=True)
def _isolate_platform_dir(tmp_path, monkeypatch):
    """Every test here either reads `compose_file()` (which now syncs
    package files into the state root) or drives `run_platform_up` — neither
    should touch the real `~/.local/state/stx/platform/`."""
    monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))


class _FakeSecretSource:
    def __init__(self, calls):
        self._calls = calls

    def ensure_loaded(self, required, *, optional=()):
        self._calls.append(("secrets", tuple(required)))


def _manifest(*, routes=None, postgres=None, redis=False, otel=False):
    platform = SimpleNamespace(postgres=postgres, redis=redis, otel=otel)
    return SimpleNamespace(routes=routes or {}, platform=platform)


class TestServicesFor:
    def test_no_manifest_starts_nothing(self):
        assert services_for(None) == []

    def test_bare_manifest_starts_nothing(self):
        assert services_for(_manifest()) == []

    def test_platform_omitted_entirely_starts_nothing(self):
        manifest = SimpleNamespace(routes={}, platform=None)
        assert services_for(manifest) == []

    def test_full_manifest_starts_all_four_in_order(self):
        manifest = _manifest(
            routes={"api": 8080}, postgres=object(), redis=True, otel=True
        )
        assert services_for(manifest) == ["traefik", "postgres", "redis", "otel"]

    def test_routes_alone_starts_only_traefik(self):
        assert services_for(_manifest(routes={"api": 8080})) == ["traefik"]

    def test_postgres_alone_starts_only_postgres(self):
        assert services_for(_manifest(postgres=object())) == ["postgres"]

    def test_redis_alone_starts_only_redis(self):
        assert services_for(_manifest(redis=True)) == ["redis"]

    def test_otel_alone_starts_only_otel(self):
        assert services_for(_manifest(otel=True)) == ["otel"]

    def test_empty_routes_mapping_does_not_start_traefik(self):
        assert services_for(_manifest(routes={})) == []


class TestRequiredSecrets:
    def test_no_services_needs_nothing(self):
        assert required_secrets([]) == []

    def test_traefik_and_otel_need_no_secrets(self):
        assert required_secrets(["traefik", "otel"]) == []

    def test_postgres_needs_its_password(self):
        assert required_secrets(["postgres"]) == ["STX_POSTGRES_PASSWORD"]

    def test_redis_needs_both_passwords(self):
        assert required_secrets(["redis"]) == [
            "STX_REDIS_PASSWORD",
            "STX_REDIS_APP_PASSWORD",
        ]

    def test_all_four_services_dedupes(self):
        names = required_secrets(["traefik", "postgres", "redis", "otel"])
        assert names == [
            "STX_POSTGRES_PASSWORD",
            "STX_REDIS_PASSWORD",
            "STX_REDIS_APP_PASSWORD",
        ]

    def test_repeated_service_does_not_duplicate_names(self):
        assert required_secrets(["postgres", "postgres"]) == ["STX_POSTGRES_PASSWORD"]


# Single-$ compose interpolations only — `$${...}` (escaped for the container's
# own shell, e.g. inside a healthcheck) is not a host-level reference.
_HOST_VAR_RE = re.compile(r"(?<!\$)\$\{(\w+)(?::-[^}]*)?\}")

# Kept by design: Traefik routes still come from TRAEFIK_ROUTES_PATH — a path,
# not a credential, so exempt from the STX_*-only check.
_EXEMPT_HOST_VARS = {"TRAEFIK_ROUTES_PATH"}


class TestComposeFile:
    def test_loads_as_yaml(self):
        data = yaml.safe_load(compose_file().read_text())
        assert set(data["services"]) == {"postgres", "traefik", "otel", "redis"}

    def test_volume_mounts_stay_within_platform_dir(self):
        data = yaml.safe_load(compose_file().read_text())
        base = compose_file().parent
        named_volumes = set(data.get("volumes", {}))
        for name, service in data["services"].items():
            for entry in service.get("volumes", []):
                source = (
                    entry["source"]
                    if isinstance(entry, dict)
                    else entry.split(":", 1)[0]
                )
                if source in named_volumes or source.startswith("${"):
                    continue
                resolved = (base / source).resolve()
                assert resolved.is_relative_to(base), (
                    f"{name}: {entry!r} escapes {base}"
                )

    def test_references_only_stx_host_variables(self):
        text = compose_file().read_text()
        referenced = set(_HOST_VAR_RE.findall(text)) - _EXEMPT_HOST_VARS
        non_stx = {name for name in referenced if not name.startswith("STX_")}
        assert non_stx == set()

    def test_no_initdb_mount(self):
        text = compose_file().read_text()
        assert "initdb" not in text

    def test_no_platform_volume_starts_with_the_shared_cache_prefix(self):
        # A repo's own compose file may only name a volume with the
        # `stx-cache-` prefix (see overlay.check_repo_compose) — a platform
        # volume starting with it would let that allow-list collide with
        # the platform's own data.
        from stx.overlay import SHARED_CACHE_VOLUME_PREFIX

        data = yaml.safe_load(compose_file().read_text())
        for name, vol_def in (data.get("volumes") or {}).items():
            own_name = (vol_def or {}).get("name", name)
            assert not own_name.startswith(SHARED_CACHE_VOLUME_PREFIX)

    def test_postgres_env_carries_no_repo_database_or_roles(self):
        data = yaml.safe_load(compose_file().read_text())
        env = data["services"]["postgres"]["environment"]
        assert set(env) == {"POSTGRES_USER", "POSTGRES_PASSWORD", "PGDATA"}

    def test_healthchecks_read_only_the_service_own_environment(self):
        # `$$NAME` is expanded by the container's shell, so it must name a
        # variable the service sets, not a host variable.
        data = yaml.safe_load(compose_file().read_text())
        for name, service in data["services"].items():
            test = service.get("healthcheck", {}).get("test", [])
            used = set(re.findall(r"\$\$\{?([A-Za-z_][A-Za-z0-9_]*)", " ".join(test)))
            declared = set(service.get("environment", {}))
            assert used <= declared, f"{name}: {sorted(used - declared)}"

    def test_images_are_pinned_to_exact_versions(self):
        # Floating tags (`postgres:16`, `redis:7.2`, `traefik:v3`) move
        # whenever anything pulls them, recreating the container out from
        # under every live sandbox's DB/Redis connection.
        data = yaml.safe_load(compose_file().read_text())
        assert data["services"]["postgres"]["image"] == "postgres:16.14"
        assert data["services"]["redis"]["image"] == "redis:7.2.14"
        assert data["services"]["traefik"]["image"] == "traefik:v3.7.6"

    def test_each_service_carries_a_definition_hash_label(self):
        data = yaml.safe_load(compose_file().read_text())
        for name, service in data["services"].items():
            assert "stx.definition_hash" in service.get("labels", {}), name

    def test_stx_net_is_external_so_compose_never_warns_about_owning_it(self):
        # stx itself creates stx-net (network.ensure_network) before `compose
        # up` ever runs, so compose must treat it as external — otherwise it
        # warns that a network it didn't create already exists.
        data = yaml.safe_load(compose_file().read_text())
        assert data["networks"]["stx-net"].get("external") is True


class TestEnsurePlatformFiles:
    def test_copies_every_package_file_into_the_state_root(self, tmp_path):
        directory = ensure_platform_files()
        assert directory == tmp_path / "state" / "platform"
        for name in (
            "compose.platform.yaml",
            "postgresql.conf",
            "redis.conf",
            "redis-entrypoint.sh",
            "traefik.yaml",
            "config.metrics.local.yaml",
        ):
            assert (directory / name).read_bytes(), name

    def test_compose_file_points_inside_the_synced_directory(self, tmp_path):
        assert (
            compose_file() == tmp_path / "state" / "platform" / "compose.platform.yaml"
        )

    def test_rewrite_is_a_no_op_when_content_is_unchanged(self):
        directory = ensure_platform_files()
        path = directory / "postgresql.conf"
        before = path.stat().st_mtime_ns

        ensure_platform_files()

        assert path.stat().st_mtime_ns == before

    def test_a_stale_local_copy_is_overwritten(self):
        directory = ensure_platform_files()
        path = directory / "postgresql.conf"
        path.write_bytes(b"stale content from an older stx install")

        ensure_platform_files()

        assert path.read_bytes() != b"stale content from an older stx install"

    def test_a_rewrite_leaves_no_fixed_name_tmp_sibling(self):
        """A fixed `<name>.tmp` can collide between two concurrent writers
        (two sandboxes provisioning at once, each syncing platform files) —
        the write must use a unique temp name that never lingers."""
        directory = ensure_platform_files()
        path = directory / "postgresql.conf"
        path.write_bytes(b"stale content from an older stx install")

        ensure_platform_files()

        assert not (directory / "postgresql.conf.tmp").exists()
        assert not any(p.name.endswith(".tmp") for p in directory.iterdir())


class _FakeRunner:
    """Dispatches by matching a substring in the joined command — the
    classifier issues several different `docker`/`compose` calls that only
    differ by their format string or sub-command. Checked in registration
    order, so register the most specific marker first."""

    def __init__(self):
        self._expectations: list[tuple[str, int, str]] = []
        self.calls: list[list[str]] = []

    def expect(
        self, marker: str, *, returncode: int = 0, stdout: str = ""
    ) -> "_FakeRunner":
        self._expectations.append((marker, returncode, stdout))
        return self

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        joined = " ".join(cmd)
        for marker, returncode, stdout in self._expectations:
            if marker in joined:
                return subprocess.CompletedProcess(cmd, returncode, stdout=stdout)
        raise AssertionError(f"no fake response registered for: {joined}")


class TestServiceDefinitionHash:
    """`_service_definition_hash` is what makes drift detection independent
    of whether the secret source is loaded — see classify_service below."""

    @pytest.fixture(autouse=True)
    def _stub_files_hash(self, monkeypatch):
        monkeypatch.setattr(
            _platform, "_service_files_hash", lambda service: "filehash"
        )

    def test_same_result_whether_or_not_secrets_are_present(self):
        # The exact bug: `docker compose config --hash` bakes in whatever a
        # secret variable actually resolves to. A real render returns the
        # same rendered hash regardless, since the value it sees is always
        # the masked placeholder — never the caller's real environ.
        runner = _FakeRunner().expect("config --hash", stdout="postgres same-render")

        with_secret = _platform._service_definition_hash(
            "postgres", environ={"STX_POSTGRES_PASSWORD": "real-secret"}, runner=runner
        )
        without_secret = _platform._service_definition_hash(
            "postgres", environ={}, runner=runner
        )

        assert with_secret == without_secret

    def test_masks_secret_values_before_rendering(self):
        captured = {}

        def runner(cmd, **kwargs):
            captured["env"] = kwargs["env"]
            return subprocess.CompletedProcess(cmd, 0, stdout="postgres hash")

        _platform._service_definition_hash(
            "postgres", environ={"STX_POSTGRES_PASSWORD": "real-secret"}, runner=runner
        )

        assert captured["env"]["STX_POSTGRES_PASSWORD"] != "real-secret"

    def test_blanks_every_services_definition_hash_var_before_rendering(self):
        # Breaks the circularity: the label's own env var must not feed back
        # into the hash that becomes its value.
        captured = {}

        def runner(cmd, **kwargs):
            captured["env"] = kwargs["env"]
            return subprocess.CompletedProcess(cmd, 0, stdout="postgres hash")

        _platform._service_definition_hash(
            "postgres",
            environ={"STX_POSTGRES_DEFINITION_HASH": "stale-value"},
            runner=runner,
        )

        for service in _platform.ALL_SERVICES:
            assert captured["env"][_platform._definition_hash_env(service)] == ""

    def test_changes_when_mounted_file_content_changes(self, monkeypatch):
        runner = _FakeRunner().expect("config --hash", stdout="postgres same-render")

        monkeypatch.setattr(_platform, "_service_files_hash", lambda service: "hash-a")
        before = _platform._service_definition_hash(
            "postgres", environ={}, runner=runner
        )

        monkeypatch.setattr(_platform, "_service_files_hash", lambda service: "hash-b")
        after = _platform._service_definition_hash(
            "postgres", environ={}, runner=runner
        )

        assert before != after


class TestClassifyService:
    @pytest.fixture(autouse=True)
    def _stub_definition_hash(self, monkeypatch):
        monkeypatch.setattr(
            _platform,
            "_service_definition_hash",
            lambda service, **_: f"{service}-definition-hash",
        )

    def test_missing_when_container_does_not_exist(self):
        runner = _FakeRunner().expect("State.Status", returncode=1)

        drift = _platform.classify_service("postgres", runner=runner)

        assert drift == _platform.ServiceDrift("postgres", _platform.DriftState.MISSING)

    def test_stopped_when_container_exists_but_is_not_running(self):
        runner = _FakeRunner().expect("State.Status", stdout="exited")

        drift = _platform.classify_service("postgres", runner=runner)

        assert drift == _platform.ServiceDrift("postgres", _platform.DriftState.STOPPED)

    def test_matching_when_hash_and_image_both_agree(self):
        runner = (
            _FakeRunner()
            .expect("State.Status", stdout="running")
            .expect("stx.definition_hash", stdout="postgres-definition-hash")
            .expect(
                "config --format json",
                stdout='{"services": {"postgres": {"image": "postgres:16.14"}}}',
            )
            .expect("image inspect", stdout="sha256:local")
            .expect("{{.Image}}", stdout="sha256:local")
        )

        drift = _platform.classify_service("postgres", runner=runner)

        assert drift == _platform.ServiceDrift(
            "postgres", _platform.DriftState.MATCHING
        )

    def test_differing_when_definition_hash_mismatches(self):
        runner = (
            _FakeRunner()
            .expect("State.Status", stdout="running")
            .expect("stx.definition_hash", stdout="stale-hash")
        )

        drift = _platform.classify_service("postgres", runner=runner)

        assert drift.state == _platform.DriftState.DIFFERING
        assert "config" in drift.detail

    def test_differing_when_container_has_no_definition_hash_label(self):
        # A container created before this label existed — correct to treat
        # as differing; one recreate stamps it.
        runner = (
            _FakeRunner()
            .expect("State.Status", stdout="running")
            .expect("stx.definition_hash", stdout="<no value>")
        )

        drift = _platform.classify_service("postgres", runner=runner)

        assert drift.state == _platform.DriftState.DIFFERING
        assert "config" in drift.detail

    def test_differing_when_image_id_mismatches(self):
        runner = (
            _FakeRunner()
            .expect("State.Status", stdout="running")
            .expect("stx.definition_hash", stdout="postgres-definition-hash")
            .expect(
                "config --format json",
                stdout='{"services": {"postgres": {"image": "postgres:16.14"}}}',
            )
            .expect("image inspect", stdout="sha256:fresh")
            .expect("{{.Image}}", stdout="sha256:stale")
        )

        drift = _platform.classify_service("postgres", runner=runner)

        assert drift.state == _platform.DriftState.DIFFERING
        assert "image" in drift.detail

    def test_classify_services_classifies_each_independently(self):
        runner = _FakeRunner().expect("State.Status", returncode=1)

        drifts = _platform.classify_services(["postgres", "redis"], runner=runner)

        assert [d.service for d in drifts] == ["postgres", "redis"]
        assert all(d.state == _platform.DriftState.MISSING for d in drifts)


class TestComposeEnv:
    @pytest.fixture(autouse=True)
    def _stub_definition_hash(self, monkeypatch):
        # Computing the real thing shells out to `docker compose config
        # --hash` per service — irrelevant to the env-assembly behavior
        # these tests cover; see TestServiceDefinitionHash for that.
        monkeypatch.setattr(
            _platform,
            "_service_definition_hash",
            lambda service, **_: f"{service}-definition-hash",
        )

    def test_includes_a_definition_hash_for_every_service_regardless_of_the_subset_passed(
        self,
    ):
        env = _platform.compose_env(["postgres"], environ={})
        assert set(
            _platform._definition_hash_env(s) for s in _platform.ALL_SERVICES
        ) <= set(env)

    def test_omits_an_override_left_unset(self):
        env = _platform.compose_env(["postgres"], environ={})
        assert "STX_POSTGRES_USER" not in env
        assert "STX_POSTGRES_PORT" not in env

    def test_passes_through_postgres_overrides_when_set(self):
        environ = {"STX_POSTGRES_USER": "dba", "STX_POSTGRES_PORT": "5433"}
        env = _platform.compose_env(["postgres"], environ=environ)
        assert env["STX_POSTGRES_USER"] == "dba"
        assert env["STX_POSTGRES_PORT"] == "5433"

    def test_passes_through_redis_overrides_when_set(self):
        environ = {
            "STX_REDIS_USER": "admin2",
            "STX_REDIS_APP_USER": "app2",
            "STX_REDIS_PORT": "6380",
        }
        env = _platform.compose_env(["redis"], environ=environ)
        assert env["STX_REDIS_USER"] == "admin2"
        assert env["STX_REDIS_APP_USER"] == "app2"
        assert env["STX_REDIS_PORT"] == "6380"

    def test_a_service_not_requested_does_not_pull_in_its_overrides(self):
        environ = {"STX_POSTGRES_USER": "dba"}
        env = _platform.compose_env(["redis"], environ=environ)
        assert "STX_POSTGRES_USER" not in env

    def test_binds_a_blank_placeholder_for_every_other_services_required_secret(self):
        # Compose always interpolates the whole file regardless of which
        # services are targeted — a subset start/recreate must still resolve
        # the rest's required (no-default) vars, or compose warns about them.
        env = _platform.compose_env(["postgres"], environ={})
        assert env["STX_REDIS_PASSWORD"] == ""
        assert env["STX_REDIS_APP_PASSWORD"] == ""

    def test_a_started_services_own_required_secret_is_never_blanked(self):
        environ = {"STX_POSTGRES_PASSWORD": "real-secret"}
        env = _platform.compose_env(["postgres"], environ=environ)
        assert env["STX_POSTGRES_PASSWORD"] == "real-secret"

    def test_requesting_every_service_binds_no_placeholders(self):
        env = _platform.compose_env(list(_platform.ALL_SERVICES), environ={})
        assert "STX_REDIS_PASSWORD" not in env
        assert "STX_POSTGRES_PASSWORD" not in env


def _missing(services: list[str]) -> list[_platform.ServiceDrift]:
    return [_platform.ServiceDrift(s, _platform.DriftState.MISSING) for s in services]


class TestReconcileSandboxNetworks:
    def test_connects_a_registered_sandbox_using_the_service(self, monkeypatch):
        monkeypatch.setattr(
            _platform,
            "load_registry",
            lambda: {"fix-447": SimpleNamespace(services=["postgres"])},
        )
        calls = []
        monkeypatch.setattr(
            _platform.stx_network,
            "connect",
            lambda net, container, **kw: calls.append((net, container)),
        )

        _platform.reconcile_sandbox_networks(["postgres"])

        assert calls == [("stx-fix-447--net", "stx-postgres")]

    def test_skips_a_sandbox_not_using_the_service(self, monkeypatch):
        monkeypatch.setattr(
            _platform,
            "load_registry",
            lambda: {"fix-447": SimpleNamespace(services=["redis"])},
        )
        calls = []
        monkeypatch.setattr(
            _platform.stx_network,
            "connect",
            lambda net, container, **kw: calls.append((net, container)),
        )

        _platform.reconcile_sandbox_networks(["postgres"])

        assert calls == []

    def test_no_registered_sandboxes_connects_nothing(self, monkeypatch):
        monkeypatch.setattr(_platform, "load_registry", lambda: {})
        calls = []
        monkeypatch.setattr(
            _platform.stx_network,
            "connect",
            lambda net, container, **kw: calls.append((net, container)),
        )

        _platform.reconcile_sandbox_networks(["postgres"])

        assert calls == []

    def test_never_connects_traefik_even_when_a_sandbox_uses_it(self, monkeypatch):
        monkeypatch.setattr(
            _platform,
            "load_registry",
            lambda: {"fix-447": SimpleNamespace(services=["traefik"])},
        )
        calls = []
        monkeypatch.setattr(
            _platform.stx_network,
            "connect",
            lambda net, container, **kw: calls.append((net, container)),
        )

        _platform.reconcile_sandbox_networks(["traefik", "postgres"])

        assert calls == []

    def test_connects_as_best_effort_not_required(self, monkeypatch):
        """This loops over every *registered* sandbox regardless of whether
        its box is actually up — a stale or torn-down one failing to
        connect must stay non-fatal, so `connect` is told `best_effort`."""
        monkeypatch.setattr(
            _platform,
            "load_registry",
            lambda: {"fix-447": SimpleNamespace(services=["postgres"])},
        )
        seen_kwargs = {}
        monkeypatch.setattr(
            _platform.stx_network,
            "connect",
            lambda net, container, **kw: seen_kwargs.update(kw),
        )

        _platform.reconcile_sandbox_networks(["postgres"])

        assert seen_kwargs.get("best_effort") is True


class TestRunPlatformUp:
    @pytest.fixture(autouse=True)
    def _provisioner_role_noop(self, monkeypatch):
        # Ensuring the real `stx_provisioner` role needs a live Postgres
        # connection — out of scope for these unit tests; see
        # TestProvisionerParams here and the docker-marked round-trip in
        # test_platform_docker.py.
        monkeypatch.setenv("STX_POSTGRES_PASSWORD", "x")
        monkeypatch.setattr(_platform, "ensure_provisioner_role", lambda *a, **kw: None)

    def test_ensures_secrets_loaded_before_compose(self, monkeypatch, tmp_path):
        calls = []
        monkeypatch.setattr(
            _platform.secrets, "source_for", lambda cfg: _FakeSecretSource(calls)
        )
        monkeypatch.setattr(_platform, "classify_services", _missing)
        monkeypatch.setattr(
            _platform.subprocess,
            "run",
            lambda cmd, **kw: calls.append("compose")
            or SimpleNamespace(returncode=0, stdout=""),
        )

        result = _platform.run_platform_up(["postgres"])

        assert result is None
        assert calls[0] == ("secrets", ("STX_POSTGRES_PASSWORD",))
        assert "compose" in calls

    def test_returns_1_when_compose_fails(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(
            _platform.secrets, "source_for", lambda cfg: _FakeSecretSource([])
        )
        monkeypatch.setattr(_platform, "classify_services", _missing)

        def fake_run(cmd, **kw):
            # Only the `compose up` itself fails — a blanket failure would
            # also fail the preceding network-create/-inspect calls, which
            # now raise their own (differently worded) error.
            if "up" in cmd:
                return SimpleNamespace(returncode=1, stdout="", stderr="boom")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(_platform.subprocess, "run", fake_run)

        result = _platform.run_platform_up(["postgres"])

        assert result == 1
        assert "failed to start" in capsys.readouterr().err

    def test_sets_traefik_routes_path_from_state_root(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            _platform.secrets, "source_for", lambda cfg: _FakeSecretSource([])
        )
        monkeypatch.setattr(_platform, "classify_services", _missing)
        captured = {}

        def mock_run(cmd, **kw):
            if "env" in kw:
                captured.update(kw["env"])
            return SimpleNamespace(returncode=0, stdout="")

        monkeypatch.setattr(_platform.subprocess, "run", mock_run)

        result = _platform.run_platform_up(["traefik"])

        assert result is None
        assert captured["TRAEFIK_ROUTES_PATH"] == str(tmp_path / "state" / "routes")

    def test_a_differing_service_is_left_running_and_warns(self, monkeypatch, capsys):
        monkeypatch.setattr(
            _platform.secrets, "source_for", lambda cfg: _FakeSecretSource([])
        )
        monkeypatch.setattr(
            _platform,
            "classify_services",
            lambda services: [
                _platform.ServiceDrift(
                    "postgres", _platform.DriftState.DIFFERING, "config changed"
                )
            ],
        )
        monkeypatch.setattr(_platform, "container_health", lambda container: None)
        compose_calls = []
        monkeypatch.setattr(
            _platform.subprocess,
            "run",
            lambda cmd, **kw: compose_calls.append(cmd)
            or SimpleNamespace(returncode=0, stdout=""),
        )

        result = _platform.run_platform_up(["postgres"])

        assert result is None
        # Only the network-create call — no `compose up` for the drifted service.
        assert all("up" not in c for c in compose_calls)
        err = capsys.readouterr().out
        assert "config changed" in err
        assert "stx up --recreate" in err

    def test_a_matching_service_is_left_alone_with_no_compose_up_call(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            _platform.secrets, "source_for", lambda cfg: _FakeSecretSource([])
        )
        monkeypatch.setattr(
            _platform,
            "classify_services",
            lambda services: [
                _platform.ServiceDrift("postgres", _platform.DriftState.MATCHING)
            ],
        )
        monkeypatch.setattr(_platform, "container_health", lambda container: "healthy")
        compose_calls = []
        monkeypatch.setattr(
            _platform.subprocess,
            "run",
            lambda cmd, **kw: compose_calls.append(cmd)
            or SimpleNamespace(returncode=0, stdout=""),
        )

        result = _platform.run_platform_up(["postgres"])

        assert result is None
        assert all("up" not in c for c in compose_calls)

    def test_an_unhealthy_already_running_service_is_an_error(
        self, monkeypatch, capsys
    ):
        monkeypatch.setattr(
            _platform.secrets, "source_for", lambda cfg: _FakeSecretSource([])
        )
        monkeypatch.setattr(
            _platform,
            "classify_services",
            lambda services: [
                _platform.ServiceDrift("postgres", _platform.DriftState.MATCHING)
            ],
        )
        monkeypatch.setattr(
            _platform, "container_health", lambda container: "unhealthy"
        )
        monkeypatch.setattr(
            _platform.subprocess,
            "run",
            lambda cmd, **kw: SimpleNamespace(returncode=0, stdout=""),
        )

        result = _platform.run_platform_up(["postgres"])

        assert result == 1
        assert "unhealthy" in capsys.readouterr().err

    def test_only_missing_or_stopped_services_are_passed_to_compose_up(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            _platform.secrets, "source_for", lambda cfg: _FakeSecretSource([])
        )
        monkeypatch.setattr(
            _platform,
            "classify_services",
            lambda services: [
                _platform.ServiceDrift("postgres", _platform.DriftState.MISSING),
                _platform.ServiceDrift("redis", _platform.DriftState.STOPPED),
                _platform.ServiceDrift(
                    "traefik", _platform.DriftState.DIFFERING, "image changed"
                ),
            ],
        )
        monkeypatch.setattr(_platform, "container_health", lambda container: None)
        up_calls = []

        def mock_run(cmd, **kw):
            if "up" in cmd:
                up_calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout="")

        monkeypatch.setattr(_platform.subprocess, "run", mock_run)

        result = _platform.run_platform_up(["postgres", "redis", "traefik"])

        assert result is None
        assert len(up_calls) == 1
        assert "postgres" in up_calls[0]
        assert "redis" in up_calls[0]
        assert "traefik" not in up_calls[0]

    def test_reconciles_only_the_freshly_started_services(self, monkeypatch):
        monkeypatch.setattr(
            _platform.secrets, "source_for", lambda cfg: _FakeSecretSource([])
        )
        monkeypatch.setattr(
            _platform,
            "classify_services",
            lambda services: [
                _platform.ServiceDrift("postgres", _platform.DriftState.MISSING),
                _platform.ServiceDrift("redis", _platform.DriftState.MATCHING),
            ],
        )
        monkeypatch.setattr(_platform, "container_health", lambda container: "healthy")
        monkeypatch.setattr(
            _platform.subprocess,
            "run",
            lambda cmd, **kw: SimpleNamespace(returncode=0, stdout=""),
        )
        reconciled = []
        monkeypatch.setattr(
            _platform,
            "reconcile_sandbox_networks",
            lambda services: reconciled.append(services),
        )

        result = _platform.run_platform_up(["postgres", "redis"])

        assert result is None
        assert reconciled == [["postgres"]]

    def test_empty_services_only_ensures_network_and_loads_no_secrets(
        self, monkeypatch, tmp_path
    ):
        calls = []
        monkeypatch.setattr(
            _platform.secrets, "source_for", lambda cfg: _FakeSecretSource(calls)
        )
        run_calls = []
        monkeypatch.setattr(
            _platform.subprocess,
            "run",
            lambda cmd, **kw: run_calls.append(cmd)
            or SimpleNamespace(returncode=0, stdout=""),
        )

        result = _platform.run_platform_up([])

        assert result is None
        assert calls == [("secrets", ())]
        assert run_calls == [["docker", "network", "create", _platform.NETWORK]]

    def test_a_network_create_failure_raises_naming_dockers_own_stderr(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            _platform.secrets, "source_for", lambda cfg: _FakeSecretSource([])
        )
        monkeypatch.setattr(
            _platform.subprocess,
            "run",
            lambda cmd, **kw: SimpleNamespace(
                returncode=1, stdout="", stderr="Cannot connect to the Docker daemon"
            ),
        )

        with pytest.raises(StxError, match="Cannot connect to the Docker daemon"):
            _platform.run_platform_up([])

    def test_postgres_requested_ensures_the_provisioner_role(self, monkeypatch):
        monkeypatch.setattr(
            _platform.secrets, "source_for", lambda cfg: _FakeSecretSource([])
        )
        monkeypatch.setattr(_platform, "classify_services", _missing)
        monkeypatch.setattr(
            _platform.subprocess,
            "run",
            lambda cmd, **kw: SimpleNamespace(returncode=0, stdout=""),
        )
        calls = []
        monkeypatch.setattr(
            _platform,
            "ensure_provisioner_role",
            lambda admin, provisioner: calls.append((admin, provisioner)),
        )

        result = _platform.run_platform_up(["postgres"])

        assert result is None
        assert len(calls) == 1
        admin, provisioner = calls[0]
        assert admin.user == "admin"
        assert provisioner.user == _platform.PROVISIONER_USER

    def test_a_service_other_than_postgres_never_touches_the_provisioner_role(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            _platform.secrets, "source_for", lambda cfg: _FakeSecretSource([])
        )
        monkeypatch.setattr(_platform, "classify_services", _missing)
        monkeypatch.setattr(
            _platform.subprocess,
            "run",
            lambda cmd, **kw: SimpleNamespace(returncode=0, stdout=""),
        )
        monkeypatch.setattr(
            _platform,
            "ensure_provisioner_role",
            lambda *a, **kw: (_ for _ in ()).throw(AssertionError),
        )

        result = _platform.run_platform_up(["traefik"])

        assert result is None


class TestProvisionerParams:
    def test_derived_password_differs_from_the_admins_own(self):
        admin = _platform.PostgresAdminParams(
            host="localhost", port=5432, user="admin", password="admin-secret"
        )
        provisioner = _platform.provisioner_params(admin)
        assert provisioner.password != admin.password
        assert provisioner.user == _platform.PROVISIONER_USER
        assert provisioner.host == admin.host
        assert provisioner.port == admin.port

    def test_derivation_is_deterministic(self):
        admin = _platform.PostgresAdminParams(
            host="localhost", port=5432, user="admin", password="admin-secret"
        )
        assert _platform.provisioner_params(admin) == _platform.provisioner_params(
            admin
        )

    def test_different_admin_passwords_derive_different_provisioner_passwords(self):
        one = _platform.PostgresAdminParams(
            host="localhost", port=5432, user="admin", password="one"
        )
        two = _platform.PostgresAdminParams(
            host="localhost", port=5432, user="admin", password="two"
        )
        assert (
            _platform.provisioner_params(one).password
            != _platform.provisioner_params(two).password
        )


class TestRunPlatformRecreate:
    def _patch_common(self, monkeypatch, *, drifts, registry=None, live_boxes=None):
        monkeypatch.setattr(
            _platform.secrets, "source_for", lambda cfg: _FakeSecretSource([])
        )
        monkeypatch.setattr(_platform, "classify_services", lambda services: drifts)
        monkeypatch.setattr(
            _platform, "load_registry", lambda: registry if registry else {}
        )
        monkeypatch.setattr(
            _platform, "sandbox_live_boxes", lambda slug, record: live_boxes or []
        )

    def test_nothing_to_recreate_when_everything_matches(self, monkeypatch, capsys):
        self._patch_common(
            monkeypatch,
            drifts=[_platform.ServiceDrift("postgres", _platform.DriftState.MATCHING)],
        )
        calls = []
        monkeypatch.setattr(
            _platform.subprocess,
            "run",
            lambda cmd, **kw: calls.append(cmd)
            or SimpleNamespace(returncode=0, stdout=""),
        )

        result = _platform.run_platform_recreate(["postgres"])

        assert result is None
        assert all("up" not in c for c in calls)
        assert "nothing to recreate" in capsys.readouterr().out

    def test_recreates_a_differing_service(self, monkeypatch):
        self._patch_common(
            monkeypatch,
            drifts=[
                _platform.ServiceDrift(
                    "postgres", _platform.DriftState.DIFFERING, "config changed"
                )
            ],
        )
        up_calls = []

        def mock_run(cmd, **kw):
            if "up" in cmd:
                up_calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout="")

        monkeypatch.setattr(_platform.subprocess, "run", mock_run)

        result = _platform.run_platform_recreate(["postgres"])

        assert result is None
        assert len(up_calls) == 1
        assert "postgres" in up_calls[0]

    def test_reconciles_the_recreated_service(self, monkeypatch):
        self._patch_common(
            monkeypatch,
            drifts=[
                _platform.ServiceDrift(
                    "postgres", _platform.DriftState.DIFFERING, "config changed"
                )
            ],
        )
        monkeypatch.setattr(
            _platform.subprocess,
            "run",
            lambda cmd, **kw: SimpleNamespace(returncode=0, stdout=""),
        )
        reconciled = []
        monkeypatch.setattr(
            _platform,
            "reconcile_sandbox_networks",
            lambda services: reconciled.append(services),
        )

        result = _platform.run_platform_recreate(["postgres"])

        assert result is None
        assert reconciled == [["postgres"]]

    def test_also_starts_missing_or_stopped_alongside_a_recreate(self, monkeypatch):
        self._patch_common(
            monkeypatch,
            drifts=[
                _platform.ServiceDrift("postgres", _platform.DriftState.MISSING),
                _platform.ServiceDrift(
                    "redis", _platform.DriftState.DIFFERING, "image changed"
                ),
            ],
        )
        up_calls = []

        def mock_run(cmd, **kw):
            if "up" in cmd:
                up_calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout="")

        monkeypatch.setattr(_platform.subprocess, "run", mock_run)

        result = _platform.run_platform_recreate(["postgres", "redis"])

        assert result is None
        assert "postgres" in up_calls[0]
        assert "redis" in up_calls[0]

    def test_returns_1_when_compose_fails(self, monkeypatch, capsys):
        self._patch_common(
            monkeypatch,
            drifts=[
                _platform.ServiceDrift(
                    "postgres", _platform.DriftState.DIFFERING, "config changed"
                )
            ],
        )

        def fake_run(cmd, **kw):
            # Only the `compose up` itself fails — a blanket failure would
            # also fail the preceding network-create/-inspect calls, which
            # now raise their own (differently worded) error.
            if "up" in cmd:
                return SimpleNamespace(returncode=1, stdout="", stderr="boom")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(_platform.subprocess, "run", fake_run)

        result = _platform.run_platform_recreate(["postgres"])

        assert result == 1
        assert "failed to recreate" in capsys.readouterr().err

    def test_recreating_one_service_binds_placeholders_for_the_others(
        self, monkeypatch
    ):
        # Regression guard: recreating just `postgres` must not leave redis's
        # required secrets unresolved — compose interpolates the whole file.
        self._patch_common(
            monkeypatch,
            drifts=[
                _platform.ServiceDrift(
                    "postgres", _platform.DriftState.DIFFERING, "config changed"
                )
            ],
        )
        envs = []

        def mock_run(cmd, **kw):
            if "up" in cmd:
                envs.append(kw["env"])
            return SimpleNamespace(returncode=0, stdout="")

        monkeypatch.setattr(_platform.subprocess, "run", mock_run)

        result = _platform.run_platform_recreate(["postgres"])

        assert result is None
        assert envs[0]["STX_REDIS_PASSWORD"] == ""
        assert envs[0]["STX_REDIS_APP_PASSWORD"] == ""

    def test_refuses_when_a_busy_sandbox_uses_a_differing_service(
        self, monkeypatch, capsys
    ):
        registry = {"fix-447": SimpleNamespace(services=["postgres"])}
        self._patch_common(
            monkeypatch,
            drifts=[
                _platform.ServiceDrift(
                    "postgres", _platform.DriftState.DIFFERING, "config changed"
                )
            ],
            registry=registry,
            live_boxes=["app"],
        )
        calls = []
        monkeypatch.setattr(
            _platform.subprocess,
            "run",
            lambda cmd, **kw: calls.append(cmd)
            or SimpleNamespace(returncode=0, stdout=""),
        )

        result = _platform.run_platform_recreate(["postgres"])

        assert result == 1
        assert all("up" not in c for c in calls)
        err = capsys.readouterr().err
        assert "fix-447" in err
        assert "--yes" in err

    def test_yes_overrides_the_busy_sandbox_refusal_and_warns(
        self, monkeypatch, capsys
    ):
        registry = {"fix-447": SimpleNamespace(services=["postgres"])}
        self._patch_common(
            monkeypatch,
            drifts=[
                _platform.ServiceDrift(
                    "postgres", _platform.DriftState.DIFFERING, "config changed"
                )
            ],
            registry=registry,
            live_boxes=["app"],
        )
        up_calls = []

        def mock_run(cmd, **kw):
            if "up" in cmd:
                up_calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout="")

        monkeypatch.setattr(_platform.subprocess, "run", mock_run)

        result = _platform.run_platform_recreate(["postgres"], yes=True)

        assert result is None
        assert len(up_calls) == 1
        out = capsys.readouterr().out
        assert "fix-447" in out

    def test_a_busy_sandbox_not_using_the_differing_service_does_not_block(
        self, monkeypatch
    ):
        registry = {"fix-447": SimpleNamespace(services=["redis"])}
        self._patch_common(
            monkeypatch,
            drifts=[
                _platform.ServiceDrift(
                    "postgres", _platform.DriftState.DIFFERING, "config changed"
                )
            ],
            registry=registry,
            live_boxes=["app"],
        )
        up_calls = []

        def mock_run(cmd, **kw):
            if "up" in cmd:
                up_calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout="")

        monkeypatch.setattr(_platform.subprocess, "run", mock_run)

        result = _platform.run_platform_recreate(["postgres"])

        assert result is None
        assert len(up_calls) == 1
