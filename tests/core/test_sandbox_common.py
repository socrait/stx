import itertools
import json
import platform
import shutil
import subprocess
import time
from dataclasses import asdict
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import stx.core as _common
from stx.core import (
    branch_is_fully_pushed,
    branch_name,
    db_name,
    find_tmp_branches,
    get_tailscale_hostname,
    get_tailscale_ipv4,
    host_git_identity,
    host_only_error,
    identity_provenance_summary,
    in_sandbox_box,
    remove_locked_worktree,
    remove_traefik_route,
    state_root,
    traefik_routes_dir,
    validate_box_name,
    validate_slug,
    write_traefik_route,
)
from stx.core.box_env import SYSTEM_PATH
from stx.errors import StxError


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, capture_output=True, check=True)


class TestHostGitIdentity:
    def test_reads_local_config(self, tmp_path):
        _init_repo(tmp_path)
        subprocess.run(
            ["git", "config", "user.name", "Alex Doe"], cwd=tmp_path, check=True
        )
        subprocess.run(
            ["git", "config", "user.email", "alex@mycompany.com"],
            cwd=tmp_path,
            check=True,
        )

        assert host_git_identity(tmp_path) == ("Alex Doe", "alex@mycompany.com")

    def test_falls_back_to_placeholders_when_unconfigured(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)

        assert host_git_identity(tmp_path) == ("Sandbox", "sandbox@localhost")


class TestIdentityProvenanceSummary:
    def test_reports_name_email_and_origin_file(self, tmp_path):
        _init_repo(tmp_path)
        subprocess.run(
            ["git", "config", "user.name", "Alex Doe"], cwd=tmp_path, check=True
        )
        subprocess.run(
            ["git", "config", "user.email", "alex@mycompany.com"],
            cwd=tmp_path,
            check=True,
        )

        summary = identity_provenance_summary(tmp_path)

        assert "Alex Doe <alex@mycompany.com>" in summary
        assert ".git/config" in summary
        assert "warning" not in summary

    def test_flags_placeholder_email(self, tmp_path):
        _init_repo(tmp_path)
        subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"], cwd=tmp_path, check=True
        )

        summary = identity_provenance_summary(tmp_path)

        assert "warning" in summary
        assert "placeholder" in summary

    def test_no_warning_for_a_real_looking_identity(self, tmp_path):
        _init_repo(tmp_path)
        subprocess.run(
            ["git", "config", "user.name", "Alex Doe"], cwd=tmp_path, check=True
        )
        subprocess.run(
            ["git", "config", "user.email", "alex@mycompany.com"],
            cwd=tmp_path,
            check=True,
        )

        assert "warning" not in identity_provenance_summary(tmp_path)

    def test_flags_a_config_key_shaped_name(self, tmp_path):
        # Found live running `stx doctor` for the first time: a
        # fat-fingered `git config user.name user.email` left this exact
        # value sitting in a real repo's local config.
        _init_repo(tmp_path)
        subprocess.run(
            ["git", "config", "user.name", "user.email"], cwd=tmp_path, check=True
        )
        subprocess.run(
            ["git", "config", "user.email", "alex@mycompany.com"],
            cwd=tmp_path,
            check=True,
        )

        summary = identity_provenance_summary(tmp_path)

        assert "warning" in summary

    def test_reports_separate_origins_when_name_and_email_differ(
        self, tmp_path, monkeypatch
    ):
        # GIT_CONFIG_GLOBAL (set ambiently by the package's own
        # _isolate_git_env fixture) overrides the usual $HOME/.gitconfig
        # lookup, so this writes there directly rather than relying on HOME.
        global_gitconfig = tmp_path / "gitconfig-global"
        global_gitconfig.write_text("[user]\n\temail = global@example.com\n")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_gitconfig))

        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Alex Doe"], cwd=repo, check=True)

        summary = identity_provenance_summary(repo)

        assert "name from" in summary
        assert "email from" in summary


class TestDbName:
    def test_simple_slug(self):
        assert db_name("foo") == "stx_foo"

    def test_hyphens_become_underscores(self):
        assert db_name("fix-447") == "stx_fix_447"

    def test_multiple_hyphens(self):
        assert db_name("my-cool-feature") == "stx_my_cool_feature"

    def test_already_underscored(self):
        assert db_name("feat1") == "stx_feat1"


class TestSandBranchName:
    def test_simple_slug(self):
        assert branch_name("foo") == "stx/foo"

    def test_hyphens_preserved(self):
        assert branch_name("fix-447") == "stx/fix-447"


class TestInSandboxBox:
    def test_true_when_zone_set(self, monkeypatch):
        monkeypatch.setenv("STX_ZONE", "trusted")
        assert in_sandbox_box() is True

    def test_false_when_zone_unset(self, monkeypatch):
        monkeypatch.delenv("STX_ZONE", raising=False)
        assert in_sandbox_box() is False


def test_host_only_error_names_the_verb_and_the_reason():
    message = host_only_error("rm")
    assert "stx rm" in message
    assert "host-only" in message


class TestValidateSlug:
    def test_accepts_lowercase_alpha(self):
        validate_slug("foo")  # no error

    def test_accepts_digits(self):
        validate_slug("fix447")

    def test_accepts_hyphens(self):
        validate_slug("fix-447")

    def test_rejects_uppercase(self):
        with pytest.raises(ValueError):
            validate_slug("Foo")

    def test_rejects_underscore(self):
        with pytest.raises(ValueError):
            validate_slug("foo_bar")

    def test_rejects_space(self):
        with pytest.raises(ValueError):
            validate_slug("foo bar")

    def test_rejects_empty(self):
        with pytest.raises(ValueError):
            validate_slug("")

    def test_rejects_slash(self):
        with pytest.raises(ValueError):
            validate_slug("feat/foo")

    def test_rejects_dot(self):
        # A slug is always a single DNS label (no dots) — same as a route
        # name — so a bare `<slug>.localhost` (2 labels) and a route-prefixed
        # `<route>.<slug>.localhost` (3 labels) can never collide.
        with pytest.raises(ValueError):
            validate_slug("foo.bar")

    def test_accepts_a_single_char(self):
        validate_slug("a")

    def test_rejects_leading_hyphen(self):
        with pytest.raises(ValueError):
            validate_slug("-foo")

    def test_rejects_trailing_hyphen(self):
        with pytest.raises(ValueError):
            validate_slug("foo-")

    def test_rejects_doubled_hyphen(self):
        # `--` is reserved as the slug/box-name separator in a container
        # name — a slug containing it would make `stx-<slug>--<box>`
        # ambiguous.
        with pytest.raises(ValueError):
            validate_slug("foo--bar")

    def test_accepts_40_chars(self):
        validate_slug("a" * 40)

    def test_rejects_41_chars(self):
        with pytest.raises(ValueError):
            validate_slug("a" * 41)

    def test_rejects_trailing_newline(self):
        # `fullmatch`, not `match` — `$` alone would let a trailing
        # newline sneak past validation.
        with pytest.raises(ValueError):
            validate_slug("foo\n")

    @pytest.mark.parametrize(
        "slug", ["postgres", "redis", "traefik", "otel", "net", "cache"]
    )
    def test_rejects_reserved_slugs(self, slug):
        with pytest.raises(ValueError):
            validate_slug(slug)

    def test_rejects_cache_prefix(self):
        with pytest.raises(ValueError):
            validate_slug("cache-npm")


class TestValidateBoxName:
    def test_accepts_lowercase_alpha(self):
        validate_box_name("app")  # no error

    def test_rejects_uppercase(self):
        with pytest.raises(ValueError):
            validate_box_name("App")

    def test_rejects_dmz(self):
        # "dmz" is reserved for the generated sidecar's own fixed box key.
        with pytest.raises(ValueError):
            validate_box_name("dmz")

    def test_accepts_20_chars(self):
        validate_box_name("a" * 20)

    def test_rejects_21_chars(self):
        with pytest.raises(ValueError):
            validate_box_name("a" * 21)

    def test_rejects_doubled_hyphen(self):
        with pytest.raises(ValueError):
            validate_box_name("app--server")


class TestGetTailscaleHostname:
    def test_returns_fqdn_when_running(self):
        payload = {"Self": {"DNSName": "alex-laptop.host.example.ts.net."}}

        def mock_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = json.dumps(payload)
            return result

        assert (
            get_tailscale_hostname(runner=mock_run) == "alex-laptop.host.example.ts.net"
        )

    def test_returns_none_when_not_installed(self):
        def mock_run(cmd, **kwargs):
            raise FileNotFoundError

        assert get_tailscale_hostname(runner=mock_run) is None

    def test_returns_none_when_not_running(self):
        def mock_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 1
            result.stdout = ""
            return result

        assert get_tailscale_hostname(runner=mock_run) is None


class TestGetTailscaleIpv4:
    def test_returns_first_ip_when_running(self):
        def mock_run(cmd, **kwargs):
            return MagicMock(returncode=0, stdout="100.64.0.1\n")

        assert get_tailscale_ipv4(runner=mock_run) == "100.64.0.1"

    def test_returns_none_when_not_installed(self):
        def mock_run(cmd, **kwargs):
            raise FileNotFoundError

        assert get_tailscale_ipv4(runner=mock_run) is None

    def test_returns_none_when_not_running(self):
        def mock_run(cmd, **kwargs):
            return MagicMock(returncode=1, stdout="")

        assert get_tailscale_ipv4(runner=mock_run) is None

    def test_returns_none_on_empty_stdout(self):
        def mock_run(cmd, **kwargs):
            return MagicMock(returncode=0, stdout="")

        assert get_tailscale_ipv4(runner=mock_run) is None


class TestStateRoot:
    def test_env_var_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "nested"))
        assert state_root() == (tmp_path / "nested").resolve()
        assert (tmp_path / "nested").is_dir()

    def test_default_is_xdg_state_home_stx(self, monkeypatch, tmp_path):
        monkeypatch.delenv("STX_STATE_ROOT", raising=False)
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert state_root() == tmp_path / ".local" / "state" / "stx"

    def test_xdg_state_home_override(self, monkeypatch, tmp_path):
        monkeypatch.delenv("STX_STATE_ROOT", raising=False)
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
        assert state_root() == tmp_path / "xdg-state" / "stx"


class TestTraefikRoutes:
    def _use_state_root(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))

    def test_routes_dir_under_state_root(self, monkeypatch, tmp_path):
        self._use_state_root(monkeypatch, tmp_path)
        assert traefik_routes_dir() == tmp_path / "routes"

    def test_write_creates_file(self, monkeypatch, tmp_path):
        self._use_state_root(monkeypatch, tmp_path)
        write_traefik_route("payments", {"api": 5101})
        assert (traefik_routes_dir() / "payments.yaml").exists()

    def test_write_is_atomic_no_tmp_file_left_for_traefiks_watcher(
        self, monkeypatch, tmp_path
    ):
        """Traefik's file provider watches this dir and hot-reloads on
        change — a stray temp sibling or a torn file is something it could
        actually try to parse."""
        self._use_state_root(monkeypatch, tmp_path)
        write_traefik_route("payments", {"api": 5101})
        assert list(traefik_routes_dir().iterdir()) == [
            traefik_routes_dir() / "payments.yaml"
        ]

    def test_write_contains_hostname_rule(self, monkeypatch, tmp_path):
        self._use_state_root(monkeypatch, tmp_path)
        write_traefik_route("payments", {"api": 5101})
        content = (traefik_routes_dir() / "payments.yaml").read_text()
        assert "api.payments.localhost" in content

    def test_write_contains_host_docker_internal_url(self, monkeypatch, tmp_path):
        self._use_state_root(monkeypatch, tmp_path)
        write_traefik_route("payments", {"api": 5101})
        content = (traefik_routes_dir() / "payments.yaml").read_text()
        assert "host.docker.internal:5101" in content

    def test_write_hyphenated_slug(self, monkeypatch, tmp_path):
        self._use_state_root(monkeypatch, tmp_path)
        write_traefik_route("fix-447", {"api": 5101})
        content = (traefik_routes_dir() / "fix-447.yaml").read_text()
        assert "fix-447.localhost" in content
        assert "host.docker.internal:5101" in content

    def test_write_multiple_routes(self, monkeypatch, tmp_path):
        self._use_state_root(monkeypatch, tmp_path)
        write_traefik_route("my-slug", {"api": 5101, "admin": 5102})
        content = (traefik_routes_dir() / "my-slug.yaml").read_text()
        assert "api.my-slug.localhost" in content
        assert "admin.my-slug.localhost" in content
        assert "host.docker.internal:5101" in content
        assert "host.docker.internal:5102" in content

    def test_write_omits_tailscale_hosts(self, monkeypatch, tmp_path):
        self._use_state_root(monkeypatch, tmp_path)
        write_traefik_route("my-slug", {"api": 5101})
        content = (traefik_routes_dir() / "my-slug.yaml").read_text()
        assert "api.my-slug.localhost" in content
        assert "host.example" not in content

    def test_write_adds_bare_router_for_first_route(self, monkeypatch, tmp_path):
        self._use_state_root(monkeypatch, tmp_path)
        write_traefik_route("my-slug", {"admin": 5102, "api": 5101})
        content = (traefik_routes_dir() / "my-slug.yaml").read_text()
        assert 'rule: "Host(`my-slug.localhost`)"' in content

    def test_write_bare_router_reuses_first_route_service_not_a_new_one(
        self, monkeypatch, tmp_path
    ):
        self._use_state_root(monkeypatch, tmp_path)
        write_traefik_route("my-slug", {"admin": 5102, "api": 5101})
        content = (traefik_routes_dir() / "my-slug.yaml").read_text()
        lines = content.splitlines()
        bare_idx = next(i for i, ln in enumerate(lines) if ln.strip() == "stx_my-slug:")
        assert lines[bare_idx + 4].strip() == "service: stx_my-slug_admin"
        # still exactly one service per route — no extra service for the bare router
        assert content.count("loadBalancer:") == 2

    def test_router_names_never_collide_across_sandboxes(self, monkeypatch, tmp_path):
        # Every sandbox's file shares one Traefik namespace: sandbox `web` with
        # route `mock` and sandbox `web-mock` must not both define one name.
        self._use_state_root(monkeypatch, tmp_path)
        write_traefik_route("web", {"mock": 5101})
        write_traefik_route("web-mock", {"web": 5102})

        def names(slug: str) -> set[str]:
            text = (traefik_routes_dir() / f"{slug}.yaml").read_text()
            return {
                ln.strip().rstrip(":")
                for ln in text.splitlines()
                if ln.startswith("    ") and not ln.startswith("     ")
            }

        assert names("web").isdisjoint(names("web-mock"))

    def test_remove_deletes_file(self, monkeypatch, tmp_path):
        self._use_state_root(monkeypatch, tmp_path)
        write_traefik_route("payments", {"api": 5101})
        remove_traefik_route("payments")
        assert not (traefik_routes_dir() / "payments.yaml").exists()

    def test_remove_is_idempotent(self, monkeypatch, tmp_path):
        self._use_state_root(monkeypatch, tmp_path)
        # No error when the file doesn't exist
        remove_traefik_route("nonexistent")


def _mounts_run(mounts: list[dict] | None, *, returncode: int = 0):
    def _run(cmd, **kw):
        if mounts is None:
            return MagicMock(returncode=1, stdout="")
        return MagicMock(returncode=returncode, stdout=json.dumps(mounts))

    return _run


class TestTraefikRoutesMountSource:
    def test_returns_none_when_container_absent(self):
        assert (
            _common.routing.traefik_routes_mount_source(
                "stx-traefik", runner=_mounts_run(None)
            )
            is None
        )

    def test_returns_none_when_no_routes_mount(self):
        mounts = [{"Destination": "/etc/traefik", "Source": "/somewhere"}]
        assert (
            _common.routing.traefik_routes_mount_source(
                "stx-traefik", runner=_mounts_run(mounts)
            )
            is None
        )

    def test_returns_the_routes_mount_source(self):
        mounts = [
            {"Destination": "/etc/traefik", "Source": "/somewhere"},
            {"Destination": "/routes", "Source": "/host/routes"},
        ]
        assert (
            _common.routing.traefik_routes_mount_source(
                "stx-traefik", runner=_mounts_run(mounts)
            )
            == "/host/routes"
        )

    def test_a_hung_docker_inspect_is_treated_as_absent_not_a_hang(self):
        def hangs(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))

        assert (
            _common.routing.traefik_routes_mount_source("stx-traefik", runner=hangs)
            is None
        )


class TestTraefikRoutesWarning:
    def _use_state_root(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))

    def test_none_when_container_not_running(self, monkeypatch, tmp_path):
        self._use_state_root(monkeypatch, tmp_path)
        assert (
            _common.traefik_routes_warning("stx-traefik", runner=_mounts_run(None))
            is None
        )

    def test_none_when_mount_matches(self, monkeypatch, tmp_path):
        self._use_state_root(monkeypatch, tmp_path)
        mounts = [{"Destination": "/routes", "Source": str(traefik_routes_dir())}]
        assert (
            _common.traefik_routes_warning("stx-traefik", runner=_mounts_run(mounts))
            is None
        )

    def test_warns_when_mount_points_elsewhere(self, monkeypatch, tmp_path):
        self._use_state_root(monkeypatch, tmp_path)
        mounts = [{"Destination": "/routes", "Source": "/some/other/routes"}]
        warning = _common.traefik_routes_warning(
            "stx-traefik", runner=_mounts_run(mounts)
        )
        assert warning is not None
        assert "stx-traefik" in warning
        assert "/some/other/routes" in warning
        assert "stx up --recreate traefik" in warning
        assert "no database connections" in warning


def _capturing_run(captured: dict, *, returncode: int = 0):
    """Fake `subprocess.run` that reads back the temp file content while it
    still exists — `_apply_hosts_lines` unlinks it in a `finally` right after
    the call returns, so a post-hoc read would always 404."""

    def _run(cmd, **kw):
        captured["cmd"] = cmd
        captured["content"] = Path(cmd[-2]).read_text()
        return MagicMock(returncode=returncode)

    return _run


class TestHostsEntry:
    def _use_hosts_path(self, monkeypatch, tmp_path, *, initial: str = "") -> Path:
        hosts = tmp_path / "hosts"
        hosts.write_text(initial)
        monkeypatch.setenv("STX_HOSTS_PATH", str(hosts))
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        return hosts

    def test_add_creates_block(self, monkeypatch, tmp_path):
        self._use_hosts_path(monkeypatch, tmp_path, initial="127.0.0.1\tlocalhost\n")
        captured: dict = {}
        _common.add_hosts_entry(
            "payments", {"api": 8080}, runner=_capturing_run(captured)
        )

        content = captured["content"]
        assert "127.0.0.1\tpayments.localhost api.payments.localhost" in content
        assert "127.0.0.1\tlocalhost" in content  # preserves existing lines

    def test_add_multiple_routes_one_line(self, monkeypatch, tmp_path):
        self._use_hosts_path(monkeypatch, tmp_path, initial="127.0.0.1\tlocalhost\n")
        captured: dict = {}
        _common.add_hosts_entry(
            "payments",
            {"api": 8080, "admin": 8081},
            runner=_capturing_run(captured),
        )

        content = captured["content"]
        assert (
            "127.0.0.1\tpayments.localhost api.payments.localhost "
            "admin.payments.localhost" in content
        )

    def test_add_no_routes_is_a_noop(self, monkeypatch, tmp_path):
        self._use_hosts_path(monkeypatch, tmp_path, initial="127.0.0.1\tlocalhost\n")
        run = MagicMock()
        _common.add_hosts_entry("payments", {}, runner=run)

        run.assert_not_called()

    def test_add_never_interpolates_a_path_into_the_script(self, monkeypatch, tmp_path):
        # The hosts path and temp path are positional shell parameters
        # ($1/$2), not string-formatted into the script text.
        hosts = self._use_hosts_path(
            monkeypatch, tmp_path, initial="127.0.0.1\tlocalhost\n"
        )
        run = MagicMock(return_value=MagicMock(returncode=0))
        _common.add_hosts_entry("payments", {"api": 8080}, runner=run)

        cmd = run.call_args.args[0]
        script = cmd[3]
        assert str(hosts) not in script
        assert "$1" in script and "$2" in script
        assert cmd[-1] == str(hosts)

    def test_add_is_idempotent_skips_sudo_if_present(self, monkeypatch, tmp_path):
        self._use_hosts_path(
            monkeypatch,
            tmp_path,
            initial=(
                f"{_common._HOSTS_BLOCK_BEGIN}\n"
                "127.0.0.1\tpayments.localhost api.payments.localhost\n"
                f"{_common._HOSTS_BLOCK_END}\n"
            ),
        )
        run = MagicMock()
        _common.add_hosts_entry("payments", {"api": 8080}, runner=run)

        run.assert_not_called()

    def test_add_raises_on_sudo_failure(self, monkeypatch, tmp_path):
        self._use_hosts_path(monkeypatch, tmp_path, initial="127.0.0.1\tlocalhost\n")
        run = MagicMock(return_value=MagicMock(returncode=1))
        with pytest.raises(OSError):
            _common.add_hosts_entry("payments", {"api": 8080}, runner=run)

    def test_add_isolates_write_exit_code_from_flush_failure(
        self, monkeypatch, tmp_path
    ):
        self._use_hosts_path(monkeypatch, tmp_path, initial="127.0.0.1\tlocalhost\n")
        monkeypatch.setattr(
            _common.hosts, "_dns_flush_command", lambda: "flush-that-always-fails"
        )
        run = MagicMock(return_value=MagicMock(returncode=0))
        _common.add_hosts_entry("payments", {"api": 8080}, runner=run)  # must not raise

        script = run.call_args.args[0][3]
        assert "; ec=$?; flush-that-always-fails; exit $ec" in script
        assert "&&" not in script

    def test_add_takes_the_hosts_lock(self, monkeypatch, tmp_path):
        self._use_hosts_path(monkeypatch, tmp_path, initial="127.0.0.1\tlocalhost\n")
        calls = []
        real_flock = _common.hosts.fcntl.flock

        def _spy_flock(fh, op):
            calls.append(op)
            return real_flock(fh, op)

        monkeypatch.setattr(_common.hosts.fcntl, "flock", _spy_flock)
        run = MagicMock(return_value=MagicMock(returncode=0))
        _common.add_hosts_entry("payments", {"api": 8080}, runner=run)

        assert _common.hosts.fcntl.LOCK_EX in calls
        assert _common.hosts.fcntl.LOCK_UN in calls
        assert _common.hosts._hosts_lock_path().parent.exists()

    def test_remove_drops_empty_block(self, monkeypatch, tmp_path):
        self._use_hosts_path(
            monkeypatch,
            tmp_path,
            initial=(
                f"{_common._HOSTS_BLOCK_BEGIN}\n"
                "127.0.0.1\tpayments.localhost api.payments.localhost\n"
                f"{_common._HOSTS_BLOCK_END}\n"
            ),
        )
        captured: dict = {}
        _common.remove_hosts_entry("payments", runner=_capturing_run(captured))

        assert _common._HOSTS_BLOCK_BEGIN not in captured["content"]

    def test_remove_keeps_other_entries(self, monkeypatch, tmp_path):
        self._use_hosts_path(
            monkeypatch,
            tmp_path,
            initial=(
                f"{_common._HOSTS_BLOCK_BEGIN}\n"
                "127.0.0.1\tpayments.localhost api.payments.localhost\n"
                "127.0.0.1\tother.localhost api.other.localhost\n"
                f"{_common._HOSTS_BLOCK_END}\n"
            ),
        )
        captured: dict = {}
        _common.remove_hosts_entry("payments", runner=_capturing_run(captured))

        assert "api.other.localhost" in captured["content"]
        assert "api.payments.localhost" not in captured["content"]

    def test_remove_matches_by_hostname_not_routes(self, monkeypatch, tmp_path):
        # No routes are passed in — removal doesn't need them, it matches by
        # hostname suffix, which also sweeps up an old single-name
        # `api.<slug>.localhost` line left by a prior stx version.
        self._use_hosts_path(
            monkeypatch,
            tmp_path,
            initial=(
                f"{_common._HOSTS_BLOCK_BEGIN}\n"
                "127.0.0.1\tapi.payments.localhost\n"
                f"{_common._HOSTS_BLOCK_END}\n"
            ),
        )
        captured: dict = {}
        removed = _common.remove_hosts_entry(
            "payments", runner=_capturing_run(captured)
        )

        assert removed == ["api.payments.localhost"]
        assert _common._HOSTS_BLOCK_BEGIN not in captured["content"]

    def test_remove_returns_every_hostname_removed(self, monkeypatch, tmp_path):
        self._use_hosts_path(
            monkeypatch,
            tmp_path,
            initial=(
                f"{_common._HOSTS_BLOCK_BEGIN}\n"
                "127.0.0.1\tpayments.localhost api.payments.localhost "
                "admin.payments.localhost\n"
                f"{_common._HOSTS_BLOCK_END}\n"
            ),
        )
        removed = _common.remove_hosts_entry(
            "payments", runner=MagicMock(return_value=MagicMock(returncode=0))
        )

        assert removed == [
            "payments.localhost",
            "api.payments.localhost",
            "admin.payments.localhost",
        ]

    def test_remove_returns_empty_list_when_absent(self, monkeypatch, tmp_path):
        self._use_hosts_path(monkeypatch, tmp_path, initial="127.0.0.1\tlocalhost\n")
        assert _common.remove_hosts_entry("payments", runner=MagicMock()) == []

    def test_remove_is_idempotent_skips_sudo_if_absent(self, monkeypatch, tmp_path):
        self._use_hosts_path(monkeypatch, tmp_path, initial="127.0.0.1\tlocalhost\n")
        run = MagicMock()
        _common.remove_hosts_entry("payments", runner=run)

        run.assert_not_called()

    def test_remove_raises_on_sudo_failure(self, monkeypatch, tmp_path):
        self._use_hosts_path(
            monkeypatch,
            tmp_path,
            initial=(
                f"{_common._HOSTS_BLOCK_BEGIN}\n"
                "127.0.0.1\tapi.payments.localhost\n"
                f"{_common._HOSTS_BLOCK_END}\n"
            ),
        )
        run = MagicMock(return_value=MagicMock(returncode=1))
        with pytest.raises(OSError):
            _common.remove_hosts_entry("payments", runner=run)

    def test_hosts_entry_exists(self, monkeypatch, tmp_path):
        self._use_hosts_path(
            monkeypatch,
            tmp_path,
            initial=(
                f"{_common._HOSTS_BLOCK_BEGIN}\n"
                "127.0.0.1\tpayments.localhost api.payments.localhost\n"
                f"{_common._HOSTS_BLOCK_END}\n"
            ),
        )
        assert _common.hosts_entry_exists("payments", {"api": 8080}) is True
        assert _common.hosts_entry_exists("other", {"api": 8080}) is False
        # a different route set builds a different line, so it isn't a match
        assert _common.hosts_entry_exists("payments", {"admin": 8081}) is False


class TestDnsFlushCommand:
    def test_darwin(self, monkeypatch):
        monkeypatch.setattr(platform, "system", lambda: "Darwin")
        cmd = _common._dns_flush_command()
        assert "dscacheutil" in cmd
        assert "mDNSResponder" in cmd

    def test_linux_with_resolvectl(self, monkeypatch):
        monkeypatch.setattr(platform, "system", lambda: "Linux")
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/resolvectl")
        assert _common._dns_flush_command() == "resolvectl flush-caches"

    def test_linux_without_resolvectl(self, monkeypatch):
        monkeypatch.setattr(platform, "system", lambda: "Linux")
        monkeypatch.setattr(shutil, "which", lambda name: None)
        assert _common._dns_flush_command() is None


class TestBoxContainerNames:
    def test_app_container_name(self):
        assert _common.container_name_for_box("app", "foo") == "stx-foo--app"

    def test_dmz_container_name(self):
        assert _common.dmz_container_name("foo") == "stx-foo--dmz"


class TestHandoffDirPaths:
    def test_drop_dir(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        assert _common.drop_dir("foo") == tmp_path / "drop" / "foo"

    def test_drop_dmz_dir(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        assert _common.drop_dmz_dir("foo") == tmp_path / "drop-dmz" / "foo"

    def test_code_dmz_dir(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        assert _common.code_dmz_dir("foo", "widgetshop") == (
            tmp_path / "code-dmz" / "foo" / "widgetshop"
        )

    def test_code_dmz_root(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        assert _common.code_dmz_root("foo") == tmp_path / "code-dmz" / "foo"

    def test_worktree_path(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        assert _common.worktree_path("foo") == tmp_path / "worktrees" / "foo"


_PASSWD_LINE = "devuser:x:1000:1000::/home/devuser:/bin/bash"


def _mock_run_for(cat_result: MagicMock):
    """Fake `subprocess.run` honoring the two-step lookup `read_agent_status`
    now makes: `getent passwd` to resolve the home dir, then `cat` the status
    file under it — distinguished by which command the call list contains."""

    def _run(cmd, **kw):
        if "getent" in cmd:
            return MagicMock(returncode=0, stdout=_PASSWD_LINE)
        return cat_result

    return _run


class TestReadAgentStatus:
    def test_parses_working_status(self):
        raw = json.dumps(
            {
                "state": "working",
                "pid": 42,
                "started_at": "2026-07-05T00:00:00Z",
            }
        )
        status = _common.read_agent_status(
            "stx-test--app",
            "devuser",
            runner=_mock_run_for(MagicMock(returncode=0, stdout=raw)),
        )

        assert status == _common.AgentStatus(
            state=_common.AgentState.WORKING, pid=42, started_at="2026-07-05T00:00:00Z"
        )

    def test_parses_index(self):
        raw = json.dumps(
            {
                "state": "working",
                "pid": 42,
                "started_at": "2026-07-05T00:00:00Z",
                "index": 3,
            }
        )
        status = _common.read_agent_status(
            "stx-test--app",
            "devuser",
            runner=_mock_run_for(MagicMock(returncode=0, stdout=raw)),
        )

        assert status.index == 3

    def test_missing_index_defaults_to_zero(self):
        raw = json.dumps({"state": "working", "pid": 42})
        status = _common.read_agent_status(
            "stx-test--app",
            "devuser",
            runner=_mock_run_for(MagicMock(returncode=0, stdout=raw)),
        )

        assert status.index == 0

    def test_parses_done_status_with_exit_code(self):
        raw = json.dumps(
            {
                "state": "done",
                "pid": 42,
                "exit_code": 1,
                "finished_at": "2026-07-05T00:00:05Z",
            }
        )
        status = _common.read_agent_status(
            "stx-test--app",
            "devuser",
            runner=_mock_run_for(MagicMock(returncode=0, stdout=raw)),
        )

        assert status.state == _common.AgentState.DONE
        assert status.exit_code == 1

    def test_returns_none_when_file_absent(self):
        status = _common.read_agent_status(
            "stx-test--app",
            "devuser",
            runner=_mock_run_for(MagicMock(returncode=1, stdout="")),
        )

        assert status is None

    def test_returns_none_on_corrupt_json(self):
        status = _common.read_agent_status(
            "stx-test--app",
            "devuser",
            runner=_mock_run_for(MagicMock(returncode=0, stdout="not json")),
        )

        assert status is None

    def test_returns_none_when_getent_fails(self):
        status = _common.read_agent_status(
            "stx-test--app",
            "devuser",
            runner=lambda cmd, **kw: MagicMock(returncode=1, stdout=""),
        )

        assert status is None

    def test_resolves_home_via_getent_not_a_shell(self):
        # No `sh -c`/`eval` shell layer at all — the home dir comes from
        # a direct `getent passwd` exec, and the status file is cat'd at the
        # resolved path, also via a direct exec.
        calls = []

        def _run(cmd, **kw):
            calls.append(cmd)
            if "getent" in cmd:
                return MagicMock(returncode=0, stdout=_PASSWD_LINE)
            return MagicMock(
                returncode=0, stdout=json.dumps({"state": "working", "pid": 1})
            )

        _common.read_agent_status("stx-test--app", "devuser", runner=_run)

        # getent runs as root but with a pinned PATH, so a box user can't
        # shadow it; the status file is cat'd as the box user, also pinned.
        assert calls[0] == [
            "docker",
            "exec",
            "--env",
            f"PATH={SYSTEM_PATH}",
            "stx-test--app",
            "getent",
            "passwd",
            "devuser",
        ]
        # As the box user: root in the box can't read a mode-700 home.
        assert calls[1] == [
            "docker",
            "exec",
            "-u",
            "devuser",
            "--env",
            f"PATH={SYSTEM_PATH}",
            "stx-test--app",
            "cat",
            "/home/devuser/.stx-agent/status.json",
        ]
        for cmd in calls:
            assert "sh" not in cmd
            assert "-c" not in cmd


def test_agent_pid_alive_checks_liveness_as_devuser():
    calls = []

    def _run(cmd, **kwargs):
        calls.append(cmd)
        return MagicMock(returncode=0)

    assert (
        _common.agent_pid_alive("stx-test--app", 42, user="devuser", runner=_run)
        is True
    )
    # The shell's own `kill` (no procps needed), the pid passed as an argument.
    assert calls == [
        [
            "docker",
            "exec",
            "-u",
            "devuser",
            "--env",
            f"PATH={SYSTEM_PATH}",
            "stx-test--app",
            "sh",
            "-c",
            'kill -0 "$1"',
            "sh",
            "42",
        ]
    ]


class TestAgentPidAliveAcrossRestart:
    def test_false_when_container_restarted_after_started_at(self):
        """A box that stopped and restarted gets a fresh pid namespace — an
        unrelated process can end up with the exact pid the old agent had,
        and `kill -0` alone can't tell them apart."""

        def _run(cmd, **kwargs):
            if cmd[1] == "inspect":
                return MagicMock(returncode=0, stdout="2030-01-01T00:10:00Z\n")
            return MagicMock(returncode=0)  # kill -0 says "alive" — a reused pid

        assert (
            _common.agent_pid_alive(
                "stx-test--app",
                42,
                user="devuser",
                started_at="2030-01-01T00:00:00Z",
                runner=_run,
            )
            is False
        )

    def test_true_when_container_predates_started_at(self):
        def _run(cmd, **kwargs):
            if cmd[1] == "inspect":
                return MagicMock(returncode=0, stdout="2030-01-01T00:00:00Z\n")
            return MagicMock(returncode=0)

        assert (
            _common.agent_pid_alive(
                "stx-test--app",
                42,
                user="devuser",
                started_at="2030-01-01T00:10:00Z",
                runner=_run,
            )
            is True
        )

    def test_skips_the_restart_check_without_started_at(self):
        calls = []

        def _run(cmd, **kwargs):
            calls.append(cmd[1])
            return MagicMock(returncode=0)

        assert (
            _common.agent_pid_alive("stx-test--app", 42, user="devuser", runner=_run)
            is True
        )
        assert "inspect" not in calls


class TestWaitForAgent:
    def _write_result_capture(self, tmp_path, slug: str, box: str, index: int) -> None:
        capture_path = _common.logs_dir(slug, box) / f"{index}.jsonl"
        capture_path.parent.mkdir(parents=True, exist_ok=True)
        envelope = {
            "t": "2026-07-05T00:00:05Z",
            "e": {
                "type": "result",
                "result": "the answer",
                "session_id": "sess-1",
                "total_cost_usd": 0.05,
            },
        }
        capture_path.write_text(json.dumps(envelope) + "\n")

    def test_polls_until_done_then_prints_result_and_returns_exit_code(
        self, monkeypatch, capsys, tmp_path
    ):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        self._write_result_capture(tmp_path, "test", "app", 1)
        statuses = iter(
            [
                {"state": "working", "pid": 42, "index": 1},
                {
                    "state": "done",
                    "pid": 42,
                    "exit_code": 0,
                    "finished_at": "2026-07-05T00:00:05Z",
                    "index": 1,
                },
            ]
        )
        alive_sequence = iter([True, True, False])

        def _run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            if 'kill -0 "$1"' in cmd:
                result.returncode = 0 if next(alive_sequence) else 1
            elif "getent" in cmd:
                result.stdout = _PASSWD_LINE
            elif cmd[-1].endswith("status.json"):
                result.stdout = json.dumps(next(statuses))
            return result

        monkeypatch.setattr(time, "sleep", lambda seconds: None)

        exit_code = _common.wait_for_agent(
            "test", "app", "stx-test--app", user="devuser", runner=_run
        )

        assert exit_code == 0
        assert "the answer" in capsys.readouterr().out

    def test_returns_none_when_never_dispatched(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        monkeypatch.setattr(time, "sleep", lambda seconds: None)

        assert (
            _common.wait_for_agent(
                "test",
                "app",
                "stx-test--app",
                user="devuser",
                runner=lambda cmd, **kw: MagicMock(returncode=1),
            )
            is None
        )

    def test_raises_after_timeout_waiting_for_agent_to_start(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        monkeypatch.setattr(time, "sleep", lambda seconds: None)
        clock = itertools.count(0, 10)
        monkeypatch.setattr(time, "monotonic", lambda: next(clock))

        with pytest.raises(StxError, match="timed out"):
            _common.wait_for_agent(
                "test",
                "app",
                "stx-test--app",
                user="devuser",
                timeout=1.0,
                runner=lambda cmd, **kw: MagicMock(returncode=1),
            )

    def test_raises_after_timeout_waiting_for_agent_to_finish(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))

        def _run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            if 'kill -0 "$1"' in cmd:
                result.returncode = 0  # always alive — the agent never finishes
            elif "getent" in cmd:
                result.stdout = _PASSWD_LINE
            elif cmd[-1].endswith("status.json"):
                result.stdout = json.dumps({"state": "working", "pid": 42, "index": 1})
            return result

        monkeypatch.setattr(time, "sleep", lambda seconds: None)
        clock = itertools.count(0, 10)
        monkeypatch.setattr(time, "monotonic", lambda: next(clock))

        with pytest.raises(StxError, match="timed out"):
            _common.wait_for_agent(
                "test", "app", "stx-test--app", user="devuser", timeout=1.0, runner=_run
            )

    def test_records_manifest_finish_entry(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        self._write_result_capture(tmp_path, "test", "app", 1)
        statuses = iter(
            [
                {
                    "state": "done",
                    "pid": 42,
                    "exit_code": 0,
                    "finished_at": "2026-07-05T00:00:05Z",
                    "index": 1,
                },
            ]
            * 2
        )

        def _run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            if 'kill -0 "$1"' in cmd:
                result.returncode = 1
            elif "getent" in cmd:
                result.stdout = _PASSWD_LINE
            elif cmd[-1].endswith("status.json"):
                result.stdout = json.dumps(next(statuses))
            return result

        monkeypatch.setattr(time, "sleep", lambda seconds: None)

        _common.wait_for_agent(
            "test", "app", "stx-test--app", user="devuser", runner=_run
        )

        finishes = [
            e
            for e in _common.read_manifest("test")
            if e.kind == _common.ManifestEntryKind.FINISH
        ]
        assert len(finishes) == 1
        assert finishes[0].session_id == "sess-1"
        assert finishes[0].total_cost_usd == 0.05

    def test_missing_capture_prints_fallback_message(
        self, monkeypatch, capsys, tmp_path
    ):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        statuses = iter(
            [
                {
                    "state": "done",
                    "pid": 42,
                    "exit_code": 1,
                    "finished_at": "2026-07-05T00:00:05Z",
                    "index": 1,
                },
            ]
            * 2
        )

        def _run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            if 'kill -0 "$1"' in cmd:
                result.returncode = 1
            elif "getent" in cmd:
                result.stdout = _PASSWD_LINE
            elif cmd[-1].endswith("status.json"):
                result.stdout = json.dumps(next(statuses))
            return result

        monkeypatch.setattr(time, "sleep", lambda seconds: None)

        exit_code = _common.wait_for_agent(
            "test", "app", "stx-test--app", user="devuser", runner=_run
        )

        assert exit_code == 1
        assert "no result captured" in capsys.readouterr().out

    def test_raises_when_box_dies_before_agent_finishes(self, monkeypatch, tmp_path):
        """Before this fix, a dead container made `kill -0` fail the same
        way a finished agent would, and the stale 'working' status read
        beforehand was used as a silent fallback — reporting a box that
        died mid-dispatch as if it had quietly finished with no exit code."""
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        monkeypatch.setattr(time, "sleep", lambda seconds: None)
        alive = {"value": True}

        def _run(cmd, **kwargs):
            if not alive["value"]:
                return MagicMock(returncode=1, stdout="", stderr="")
            result = MagicMock(returncode=0)
            if 'kill -0 "$1"' in cmd:
                alive["value"] = False  # the box dies right after this poll
            elif "getent" in cmd:
                result.stdout = _PASSWD_LINE
            elif cmd[-1].endswith("status.json"):
                result.stdout = json.dumps({"state": "working", "pid": 42, "index": 1})
            return result

        with pytest.raises(StxError, match="never recorded this dispatch finishing"):
            _common.wait_for_agent(
                "test", "app", "stx-test--app", user="devuser", runner=_run
            )

    def test_raises_when_pid_was_reused_after_a_box_restart(
        self, monkeypatch, tmp_path
    ):
        """A box restart resets the pid namespace; if an unrelated process
        now holds the agent's old pid, `kill -0` alone would say "alive"
        forever even though the dispatched agent is long gone."""
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        monkeypatch.setattr(time, "sleep", lambda seconds: None)

        def _run(cmd, **kwargs):
            result = MagicMock(returncode=0)
            if cmd[1] == "inspect":
                result.stdout = "2030-01-01T00:10:00Z\n"  # restarted after dispatch
            elif 'kill -0 "$1"' in cmd:
                pass  # would say "alive" — a reused pid, not checked here
            elif "getent" in cmd:
                result.stdout = _PASSWD_LINE
            elif cmd[-1].endswith("status.json"):
                result.stdout = json.dumps(
                    {
                        "state": "working",
                        "pid": 42,
                        "started_at": "2030-01-01T00:00:00Z",
                        "index": 1,
                    }
                )
            return result

        with pytest.raises(StxError, match="never recorded this dispatch finishing"):
            _common.wait_for_agent(
                "test", "app", "stx-test--app", user="devuser", runner=_run
            )


class TestManifest:
    def test_round_trips_start_and_finish_entries(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        start = _common.ManifestEntry(
            kind=_common.ManifestEntryKind.START,
            index=1,
            box="app",
            task="do a thing",
            model="sonnet",
            started_at="2026-07-07T00:00:00Z",
        )
        finish = _common.ManifestEntry(
            kind=_common.ManifestEntryKind.FINISH,
            index=1,
            box="app",
            exit_code=0,
            finished_at="2026-07-07T00:01:00Z",
            session_id="sess-1",
            total_cost_usd=0.12,
        )

        _common.append_manifest_entry("test", start)
        _common.append_manifest_entry("test", finish)

        entries = _common.read_manifest("test")
        assert entries == [start, finish]

    def test_missing_manifest_returns_empty_list(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))

        assert _common.read_manifest("nonexistent") == []

    def test_skips_unparseable_lines(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        path = _common.manifest_path("test")
        path.parent.mkdir(parents=True, exist_ok=True)
        good = _common.ManifestEntry(
            kind=_common.ManifestEntryKind.START, index=1, box="app"
        )
        path.write_text("not json\n" + json.dumps(asdict(good)) + "\n")

        entries = _common.read_manifest("test")

        assert entries == [good]


class TestAllocateDispatchIndex:
    def test_first_dispatch_is_index_one(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))

        assert _common.allocate_dispatch_index("test", "app") == 1

    def test_increments_from_prior_starts_in_same_box(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        _common.append_manifest_entry(
            "test",
            _common.ManifestEntry(
                kind=_common.ManifestEntryKind.START, index=1, box="app"
            ),
        )
        _common.append_manifest_entry(
            "test",
            _common.ManifestEntry(
                kind=_common.ManifestEntryKind.START, index=2, box="app"
            ),
        )

        assert _common.allocate_dispatch_index("test", "app") == 3

    def test_indexes_are_scoped_per_box(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        _common.append_manifest_entry(
            "test",
            _common.ManifestEntry(
                kind=_common.ManifestEntryKind.START, index=1, box="app"
            ),
        )

        assert _common.allocate_dispatch_index("test", "dmz") == 1


class TestParseDispatchResult:
    def test_returns_none_when_capture_missing(self, tmp_path):
        assert _common.parse_dispatch_result(tmp_path / "missing.jsonl") is None

    def test_extracts_result_event(self, tmp_path):
        capture_path = tmp_path / "1.jsonl"
        lines = [
            json.dumps({"t": "t0", "e": {"type": "system", "subtype": "init"}}),
            json.dumps(
                {
                    "t": "t1",
                    "e": {
                        "type": "result",
                        "result": "final answer",
                        "session_id": "sess-9",
                        "total_cost_usd": 0.02,
                    },
                }
            ),
        ]
        capture_path.write_text("\n".join(lines) + "\n")

        result = _common.parse_dispatch_result(capture_path)

        assert result == _common.DispatchResult(
            text="final answer", session_id="sess-9", total_cost_usd=0.02
        )

    def test_takes_last_result_event_when_multiple(self, tmp_path):
        capture_path = tmp_path / "1.jsonl"
        lines = [
            json.dumps({"t": "t0", "e": {"type": "result", "result": "first"}}),
            json.dumps({"t": "t1", "e": {"type": "result", "result": "second"}}),
        ]
        capture_path.write_text("\n".join(lines) + "\n")

        result = _common.parse_dispatch_result(capture_path)

        assert result.text == "second"

    def test_returns_none_when_no_result_event(self, tmp_path):
        capture_path = tmp_path / "1.jsonl"
        capture_path.write_text(
            json.dumps({"t": "t0", "e": {"type": "system", "subtype": "init"}}) + "\n"
        )

        assert _common.parse_dispatch_result(capture_path) is None


class TestRecordDispatchFinishIfAbsent:
    def test_noop_when_agent_still_working(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        status = _common.AgentStatus(state=_common.AgentState.WORKING, pid=1, index=1)

        _common.record_dispatch_finish_if_absent("test", "app", status)

        assert _common.read_manifest("test") == []

    def test_appends_finish_entry_once(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        capture_path = _common.logs_dir("test", "app") / "1.jsonl"
        capture_path.parent.mkdir(parents=True, exist_ok=True)
        capture_path.write_text(
            json.dumps(
                {
                    "t": "t0",
                    "e": {"type": "result", "result": "done", "session_id": "s1"},
                }
            )
            + "\n"
        )
        status = _common.AgentStatus(
            state=_common.AgentState.DONE,
            pid=1,
            index=1,
            exit_code=0,
            finished_at="2026-07-07T00:00:00Z",
        )

        _common.record_dispatch_finish_if_absent("test", "app", status)
        _common.record_dispatch_finish_if_absent("test", "app", status)

        finishes = [
            e
            for e in _common.read_manifest("test")
            if e.kind == _common.ManifestEntryKind.FINISH
        ]
        assert len(finishes) == 1
        assert finishes[0].session_id == "s1"


class TestRemoveLockedWorktree:
    def _make_locked_worktree(self, tmp_path: Path) -> tuple[Path, Path]:
        main = tmp_path / "main"
        main.mkdir()
        _init_repo(main)
        (main / "file.txt").write_text("hello")
        subprocess.run(["git", "add", "."], cwd=main, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=main, check=True)

        wt_path = tmp_path / "wt"
        subprocess.run(
            ["git", "worktree", "add", "--lock", "-b", "stx/foo", str(wt_path)],
            cwd=main,
            check=True,
            capture_output=True,
        )
        return main, wt_path

    def test_single_force_fails_on_a_locked_worktree(self, tmp_path):
        main, wt_path = self._make_locked_worktree(tmp_path)

        result = subprocess.run(
            ["git", "worktree", "remove", "--force", str(wt_path)],
            cwd=main,
            capture_output=True,
            text=True,
        )

        assert result.returncode != 0
        assert wt_path.exists()

    def test_removes_a_locked_worktree(self, tmp_path):
        main, wt_path = self._make_locked_worktree(tmp_path)

        assert remove_locked_worktree(wt_path, cwd=main) is True
        assert not wt_path.exists()

    def test_returns_false_when_path_does_not_exist(self, tmp_path):
        main = tmp_path / "main"
        main.mkdir()
        _init_repo(main)
        (main / "file.txt").write_text("hello")
        subprocess.run(["git", "add", "."], cwd=main, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=main, check=True)

        assert remove_locked_worktree(tmp_path / "nonexistent", cwd=main) is False


def _init_repo_with_commit(path: Path) -> None:
    _init_repo(path)
    (path / "f.txt").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True)


class TestFindTmpBranches:
    def test_finds_branches_for_slug(self, tmp_path):
        _init_repo_with_commit(tmp_path)
        subprocess.run(["git", "branch", "tmp/foo/task1"], cwd=tmp_path, check=True)
        subprocess.run(["git", "branch", "tmp/foo/task2"], cwd=tmp_path, check=True)

        assert set(find_tmp_branches(tmp_path, "foo")) == {
            "tmp/foo/task1",
            "tmp/foo/task2",
        }

    def test_excludes_other_slugs(self, tmp_path):
        _init_repo_with_commit(tmp_path)
        subprocess.run(["git", "branch", "tmp/foo/task1"], cwd=tmp_path, check=True)
        subprocess.run(["git", "branch", "tmp/bar/task1"], cwd=tmp_path, check=True)

        assert find_tmp_branches(tmp_path, "foo") == ["tmp/foo/task1"]

    def test_empty_when_no_tmp_branches(self, tmp_path):
        _init_repo_with_commit(tmp_path)

        assert find_tmp_branches(tmp_path, "foo") == []


class TestBranchIsFullyPushed:
    def test_true_when_every_commit_is_on_a_remote(self, tmp_path):
        remote = tmp_path / "remote.git"
        remote.mkdir()
        subprocess.run(["git", "init", "--bare"], cwd=remote, check=True)

        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo_with_commit(repo)
        subprocess.run(
            ["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True
        )
        subprocess.run(["git", "checkout", "-b", "tmp/foo/task1"], cwd=repo, check=True)
        (repo / "g.txt").write_text("more")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-m", "cross-branch work"], cwd=repo, check=True
        )
        subprocess.run(
            ["git", "push", "origin", "HEAD:feat/other/target"],
            cwd=repo,
            capture_output=True,
            check=True,
        )
        subprocess.run(["git", "fetch", "origin"], cwd=repo, capture_output=True)

        assert branch_is_fully_pushed(repo, "tmp/foo/task1") is True

    def test_false_when_a_commit_is_unpushed(self, tmp_path):
        remote = tmp_path / "remote.git"
        remote.mkdir()
        subprocess.run(["git", "init", "--bare"], cwd=remote, check=True)

        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo_with_commit(repo)
        subprocess.run(
            ["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True
        )
        subprocess.run(["git", "checkout", "-b", "tmp/foo/task1"], cwd=repo, check=True)
        (repo / "g.txt").write_text("more")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "never pushed"], cwd=repo, check=True)

        assert branch_is_fully_pushed(repo, "tmp/foo/task1") is False
