"""`compose_config`'s subprocess-failure -> StxError conversion, pure: a fake
`runner` stands in for `subprocess.run` so this needs no Docker. Also
`base_env`/`compose_variables`/`resolve_compose_env`, the trimmed `docker
compose` process environment building blocks."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from stx.core.compose import (
    ComposeVariable,
    atomic_write_bytes,
    atomic_write_text,
    base_env,
    compose_config,
    compose_variable_placeholders,
    compose_variables,
    compose_version,
    container_recent_logs,
    container_started_at,
    container_status,
    refuse_base_env_variables,
    refuse_stx_credential_variables,
    resolve_compose_env,
    resolve_dockerfile_path,
    run_bounded,
    teardown_env,
)
from stx.errors import StxError


def _fake_runner(returncode: int, *, stdout: str = "", stderr: str = ""):
    def runner(args, **kwargs):
        return subprocess.CompletedProcess(
            args, returncode, stdout=stdout, stderr=stderr
        )

    return runner


def _hanging_runner(timeout_arg):
    def runner(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs.get("timeout", timeout_arg))

    return runner


class TestRunBounded:
    def test_passes_through_a_successful_result(self):
        result = run_bounded(
            ["docker", "inspect", "x"], runner=_fake_runner(0, stdout="ok")
        )
        assert result.returncode == 0
        assert result.stdout == "ok"

    def test_forwards_the_timeout_to_the_runner(self):
        seen = {}

        def runner(args, **kwargs):
            seen.update(kwargs)
            return subprocess.CompletedProcess(args, 0)

        run_bounded(["docker", "inspect", "x"], runner=runner, timeout=5)

        assert seen["timeout"] == 5

    def test_a_hang_becomes_a_failed_result_not_an_exception(self):
        """A wedged daemon must fail the call the same way a real failure
        does — every existing `if result.returncode != 0` branch already
        knows what to do with that, with no caller-side changes needed."""
        result = run_bounded(
            ["docker", "inspect", "x"], runner=_hanging_runner(30), timeout=30
        )
        assert result.returncode != 0
        assert "timed out" in result.stderr


class TestComposeVersion:
    def test_parses_major_minor_patch(self):
        assert compose_version(runner=_fake_runner(0, stdout="v2.20.3\n")) == (
            2,
            20,
            3,
        )

    def test_returns_none_on_nonzero_exit(self):
        assert compose_version(runner=_fake_runner(1, stderr="not found")) is None

    def test_returns_none_on_unparseable_output(self):
        assert compose_version(runner=_fake_runner(0, stdout="unexpected\n")) is None


class TestComposeConfig:
    def test_returns_parsed_json_on_success(self):
        config = compose_config(
            [Path("compose.yaml")],
            cwd=Path("."),
            env={},
            runner=_fake_runner(0, stdout='{"services": {}}'),
        )
        assert config == {"services": {}}

    def test_raises_stx_error_with_stderr_on_failure(self):
        with pytest.raises(StxError, match="no configuration file provided"):
            compose_config(
                [Path("compose.yaml")],
                cwd=Path("."),
                env={},
                runner=_fake_runner(1, stderr="no configuration file provided\n"),
            )

    def test_project_pins_the_compose_project_name(self):
        captured = {}

        def runner(args, **kwargs):
            captured["args"] = args
            return subprocess.CompletedProcess(args, 0, stdout="{}", stderr="")

        compose_config(
            [Path("compose.yaml")],
            cwd=Path("."),
            env={},
            project="stx-demo",
            runner=runner,
        )
        args = captured["args"]
        assert args[:5] == ["docker", "compose", "--env-file", "/dev/null", "-p"]
        assert args[5] == "stx-demo"

    def test_no_project_adds_no_dash_p(self):
        captured = {}

        def runner(args, **kwargs):
            captured["args"] = args
            return subprocess.CompletedProcess(args, 0, stdout="{}", stderr="")

        compose_config([Path("compose.yaml")], cwd=Path("."), env={}, runner=runner)
        assert "-p" not in captured["args"]


class TestBaseEnv:
    def test_keeps_only_path_home_and_docker_star(self):
        environ = {
            "PATH": "/usr/bin",
            "HOME": "/home/dev",
            "DOCKER_HOST": "unix:///var/run/docker.sock",
            "DOCKER_CONTEXT": "default",
            "STX_GITLAB_MINTER_TOKEN": "super-secret",
            "ANTHROPIC_API_KEY": "sk-leaked",
            "SOME_OTHER_VAR": "whatever",
        }
        assert base_env(environ) == {
            "PATH": "/usr/bin",
            "HOME": "/home/dev",
            "DOCKER_HOST": "unix:///var/run/docker.sock",
            "DOCKER_CONTEXT": "default",
        }

    def test_empty_environ_yields_empty_env(self):
        assert base_env({}) == {}


class TestComposeVariables:
    def test_scans_required_and_optional_from_uninterpolated_config(self):
        config = {
            "services": {
                "app": {
                    "environment": {
                        "A": "${REQUIRED_VAR}",
                        "B": "${OPTIONAL_VAR:-default}",
                        "C": "${OTHER_OPTIONAL-x}",
                    }
                }
            }
        }
        variables = compose_variables(config)
        assert {(v.name, v.required) for v in variables} == {
            ("REQUIRED_VAR", True),
            ("OPTIONAL_VAR", False),
            ("OTHER_OPTIONAL", False),
        }

    def test_required_wins_when_the_same_name_appears_both_ways(self):
        config = {
            "services": {
                "app": {
                    "environment": {
                        "A": "${VAR:-default}",
                        "B": "${VAR}",
                    }
                }
            }
        }
        variables = compose_variables(config)
        assert variables == [
            type(variables[0])(name="VAR", required=True),
        ]

    def test_no_references_yields_empty_list(self):
        assert compose_variables({"services": {"app": {}}}) == []

    def test_order_follows_first_appearance(self):
        config = {"services": {"app": {"environment": {"A": "${B} ${A}"}}}}
        assert [v.name for v in compose_variables(config)] == ["B", "A"]

    def test_bare_dollar_form_is_required(self):
        config = {
            "services": {"app": {"environment": {"A": "prefix-$BARE_VAR-suffix"}}}
        }
        assert compose_variables(config) == [
            ComposeVariable(name="BARE_VAR", required=True)
        ]

    def test_question_mark_forms_are_required(self):
        config = {
            "services": {
                "app": {
                    "environment": {
                        "A": "${COLON_Q:?missing}",
                        "B": "${BARE_Q?missing}",
                    }
                }
            }
        }
        assert {(v.name, v.required) for v in compose_variables(config)} == {
            ("COLON_Q", True),
            ("BARE_Q", True),
        }

    def test_plus_forms_are_optional(self):
        config = {
            "services": {
                "app": {
                    "environment": {
                        "A": "${COLON_PLUS:+alt}",
                        "B": "${BARE_PLUS+alt}",
                    }
                }
            }
        }
        assert {(v.name, v.required) for v in compose_variables(config)} == {
            ("COLON_PLUS", False),
            ("BARE_PLUS", False),
        }

    def test_double_dollar_is_an_escape_not_a_reference(self):
        config = {"services": {"app": {"environment": {"A": "$$LITERAL_DOLLAR"}}}}
        assert compose_variables(config) == []

    def test_required_wins_across_every_form(self):
        config = {
            "services": {
                "app": {
                    "environment": {
                        "A": "${VAR:-default}",
                        "B": "${VAR:?error}",
                    }
                }
            }
        }
        assert compose_variables(config) == [ComposeVariable(name="VAR", required=True)]

    @pytest.mark.parametrize(
        "value", ["${A:-${B}}", "${A:-$B}", "${A:+${B}}", "${A:?${B}}"]
    )
    def test_nested_interpolation_is_refused(self, value):
        config = {"services": {"app": {"environment": {"X": value}}}}
        with pytest.raises(StxError, match="nested"):
            compose_variables(config)

    def test_double_dollar_in_a_default_is_not_nested_interpolation(self):
        # `$$` is Compose's literal-dollar escape, not a variable reference.
        config = {"services": {"app": {"environment": {"X": "${A:-cost $$5}"}}}}
        assert compose_variables(config) == [ComposeVariable(name="A", required=False)]


class TestRefuseStxCredentialVariables:
    def test_refuses_each_stx_credential_name(self):
        for name in (
            "STX_GITLAB_MINTER_TOKEN",
            "STX_CLAUDE_OAUTH_TOKEN",
            "STX_DMZ_ANTHROPIC_API_KEY",
            "STX_POSTGRES_PASSWORD",
            "STX_REDIS_PASSWORD",
        ):
            with pytest.raises(StxError, match=name):
                refuse_stx_credential_variables(
                    compose_variables(
                        {"services": {"app": {"environment": {"A": f"${{{name}}}"}}}}
                    )
                )

    def test_accepts_ordinary_names(self):
        refuse_stx_credential_variables(
            compose_variables(
                {
                    "services": {
                        "app": {"environment": {"A": "${DATABASE_APP_PASSWORD}"}}
                    }
                }
            )
        )

    @pytest.mark.parametrize(
        "value",
        [
            "$STX_POSTGRES_PASSWORD",
            "${STX_POSTGRES_PASSWORD}",
            "${STX_POSTGRES_PASSWORD:-x}",
            "${STX_POSTGRES_PASSWORD-x}",
            "${STX_POSTGRES_PASSWORD:?e}",
            "${STX_POSTGRES_PASSWORD?e}",
            "${STX_POSTGRES_PASSWORD:+x}",
            "${STX_POSTGRES_PASSWORD+x}",
        ],
    )
    def test_refuses_stx_postgres_password_in_every_grammar_form(self, value):
        config = {"services": {"app": {"environment": {"A": value}}}}
        with pytest.raises(StxError, match="STX_POSTGRES_PASSWORD"):
            refuse_stx_credential_variables(compose_variables(config))


class TestRefuseBaseEnvVariables:
    @pytest.mark.parametrize(
        "name", ["PATH", "HOME", "DOCKER_HOST", "DOCKER_AUTH_CONFIG"]
    )
    def test_refuses_names_base_env_would_carry_from_the_shell(self, name):
        with pytest.raises(StxError, match=name):
            refuse_base_env_variables(
                compose_variables(
                    {"services": {"app": {"environment": {"A": f"${{{name}}}"}}}}
                )
            )

    @pytest.mark.parametrize(
        "value",
        [
            "$HOME",
            "${HOME}",
            "${HOME:-}",
            "${HOME-x}",
            "${DOCKER_AUTH_CONFIG:-}",
            "${DOCKER_AUTH_CONFIG:+x}",
        ],
    )
    def test_refuses_an_optional_base_env_reference_in_every_form(self, value):
        # An optional `${HOME:-}` is left out of the resolved secret env but
        # would still interpolate from `base_env`'s own PATH/HOME/DOCKER_* floor
        # — so it must be refused outright, not silently filled from the shell.
        config = {"services": {"app": {"environment": {"A": value}}}}
        with pytest.raises(StxError, match="HOME|DOCKER_AUTH_CONFIG"):
            refuse_base_env_variables(compose_variables(config))

    def test_accepts_ordinary_names(self):
        refuse_base_env_variables(
            compose_variables(
                {"services": {"app": {"environment": {"A": "${DATABASE_URL}"}}}}
            )
        )

    def test_accepts_a_name_that_merely_contains_docker(self):
        # The refusal is the `DOCKER_` prefix, not the substring.
        refuse_base_env_variables(
            compose_variables(
                {"services": {"app": {"environment": {"A": "${MY_DOCKER_TAG}"}}}}
            )
        )


class TestResolveComposeEnv:
    def test_resolves_every_listed_name(self):
        environ = {"DATABASE_APP_PASSWORD": "pw1", "JWT_SECRET_PRIVATE": "pw2"}
        variables = [
            ComposeVariable(name="DATABASE_APP_PASSWORD", required=True),
            ComposeVariable(name="JWT_SECRET_PRIVATE", required=True),
        ]
        assert resolve_compose_env(variables, environ) == environ

    def test_no_variables_yields_empty_dict(self):
        assert resolve_compose_env([], {"ANYTHING": "x"}) == {}

    def test_missing_required_raises_naming_it(self):
        with pytest.raises(StxError, match="DATABASE_APP_PASSWORD"):
            resolve_compose_env(
                [ComposeVariable(name="DATABASE_APP_PASSWORD", required=True)], {}
            )

    def test_empty_required_value_raises_naming_it(self):
        with pytest.raises(StxError, match="DATABASE_APP_PASSWORD"):
            resolve_compose_env(
                [ComposeVariable(name="DATABASE_APP_PASSWORD", required=True)],
                {"DATABASE_APP_PASSWORD": ""},
            )

    def test_optional_set_is_included(self):
        assert resolve_compose_env(
            [ComposeVariable(name="GCP_SA_KEY", required=False)],
            {"GCP_SA_KEY": "json"},
        ) == {"GCP_SA_KEY": "json"}

    def test_optional_unset_is_omitted_not_raised(self):
        assert (
            resolve_compose_env(
                [ComposeVariable(name="GCP_SA_KEY", required=False)], {}
            )
            == {}
        )


class TestComposeVariablePlaceholders:
    def test_binds_every_name_to_empty_string(self):
        variables = [
            ComposeVariable(name="A", required=True),
            ComposeVariable(name="B", required=False),
        ]
        assert compose_variable_placeholders(variables) == {"A": "", "B": ""}


class TestTeardownEnv:
    def test_binds_every_referenced_name_to_empty_string(self, monkeypatch):
        monkeypatch.setenv("PATH", "/usr/bin")
        config = (
            '{"services": {"app": {"environment": '
            '{"A": "${REQUIRED_VAR}", "B": "${OPTIONAL_VAR:-x}"}}}}'
        )
        env = teardown_env(
            [Path("compose.yaml")],
            cwd=Path("."),
            runner=_fake_runner(0, stdout=config),
        )
        assert env["REQUIRED_VAR"] == ""
        assert env["OPTIONAL_VAR"] == ""
        assert env["PATH"] == "/usr/bin"

    def test_no_references_yields_just_base_env(self, monkeypatch):
        monkeypatch.setenv("PATH", "/usr/bin")
        env = teardown_env(
            [Path("compose.yaml")],
            cwd=Path("."),
            runner=_fake_runner(0, stdout='{"services": {"app": {}}}'),
        )
        assert env == base_env()


class TestContainerStatus:
    def test_a_hung_docker_inspect_is_treated_as_missing_not_a_crash(self):
        """Before `run_bounded`, a wedged `docker inspect` (daemon stuck)
        hung this call forever instead of failing it — and every other
        command waiting on the same daemon behind it."""
        assert container_status("stx-test--app", runner=_hanging_runner(30)) is None


class TestContainerStartedAt:
    def test_parses_the_raw_timestamp(self):
        assert (
            container_started_at(
                "stx-test--app",
                runner=_fake_runner(0, stdout="2026-07-05T00:00:05.123456789Z\n"),
            )
            == "2026-07-05T00:00:05.123456789Z"
        )

    def test_none_when_container_missing(self):
        assert (
            container_started_at(
                "stx-test--app", runner=_fake_runner(1, stderr="no such")
            )
            is None
        )


class TestContainerRecentLogs:
    def test_combines_stdout_and_stderr_trimmed(self):
        logs = container_recent_logs(
            "stx-test--app",
            runner=_fake_runner(0, stdout="out line\n", stderr="err line\n"),
        )
        assert logs == "out line\nerr line"

    def test_empty_when_container_missing(self):
        assert container_recent_logs("stx-test--app", runner=_fake_runner(1)) == ""


class TestResolveDockerfilePath:
    def test_resolves_against_the_context_not_elsewhere(self):
        context = Path("/repo/docker")
        build = {"context": "docker", "dockerfile": "Dockerfile.dev"}
        assert resolve_dockerfile_path(context, build) == context / "Dockerfile.dev"

    def test_defaults_to_dockerfile_when_unset(self):
        context = Path("/repo")
        assert resolve_dockerfile_path(context, {}) == context / "Dockerfile"


class TestAtomicWrite:
    def test_write_text_round_trips_and_leaves_no_tmp_sibling(self, tmp_path):
        path = tmp_path / "allowlist"
        atomic_write_text(path, "^pypi\\.org$\n")
        assert path.read_text() == "^pypi\\.org$\n"
        assert list(tmp_path.iterdir()) == [path]

    def test_write_bytes_creates_parent_dirs(self, tmp_path):
        path = tmp_path / "nested" / "dir" / "file.yaml"
        atomic_write_bytes(path, b"content")
        assert path.read_bytes() == b"content"

    def test_default_mode_is_other_readable_not_mkstemps_owner_only(self, tmp_path):
        """These files are read from inside a container as a uid that never
        matches the host writer's own — `mkstemp`'s owner-only default
        would silently break that read."""
        path = tmp_path / "allowlist"
        atomic_write_text(path, "content")
        assert path.stat().st_mode & 0o044 == 0o044

    def test_mode_is_overridable(self, tmp_path):
        path = tmp_path / "secret-ish"
        atomic_write_text(path, "content", mode=0o600)
        assert path.stat().st_mode & 0o777 == 0o600

    def test_overwrite_replaces_content_atomically(self, tmp_path):
        path = tmp_path / "route.yaml"
        atomic_write_text(path, "old")
        atomic_write_text(path, "new")
        assert path.read_text() == "new"
        assert list(tmp_path.iterdir()) == [path]

    def test_failed_write_leaves_the_original_untouched_and_no_tmp_behind(
        self, tmp_path, monkeypatch
    ):
        path = tmp_path / "allowlist"
        atomic_write_text(path, "original")

        def _boom(*a, **kw):
            raise OSError("disk full")

        monkeypatch.setattr("os.fsync", _boom)
        with pytest.raises(OSError, match="disk full"):
            atomic_write_text(path, "new content that never lands")

        assert path.read_text() == "original"
        assert list(tmp_path.iterdir()) == [path]
