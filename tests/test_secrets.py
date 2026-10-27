import sys
from pathlib import Path

import pytest

from stx.errors import StxError
from stx.secrets import (
    DotenvSource,
    OpSource,
    SecretsError,
    dotenv_mode_ok,
    source_for,
)


class TestDotenvSourceParsing:
    def test_missing_file_behaves_like_empty(self, tmp_path):
        source = DotenvSource(tmp_path / "missing.env")
        source.ensure_loaded([])

    def test_missing_file_still_reports_missing_required(self, tmp_path, monkeypatch):
        monkeypatch.delenv("STX_FICTIONAL_TOKEN", raising=False)
        source = DotenvSource(tmp_path / "missing.env")
        with pytest.raises(SecretsError) as exc_info:
            source.ensure_loaded(["STX_FICTIONAL_TOKEN"])
        assert exc_info.value.missing == ["STX_FICTIONAL_TOKEN"]

    def test_simple_key_value_loaded(self, tmp_path, monkeypatch):
        monkeypatch.delenv("STX_FICTIONAL_TOKEN", raising=False)
        (tmp_path / "secrets.env").write_text("STX_FICTIONAL_TOKEN=widget-sprocket-1\n")
        DotenvSource(tmp_path / "secrets.env").ensure_loaded(["STX_FICTIONAL_TOKEN"])
        import os

        assert os.environ["STX_FICTIONAL_TOKEN"] == "widget-sprocket-1"

    def test_double_quotes_stripped(self, tmp_path, monkeypatch):
        monkeypatch.delenv("STX_FICTIONAL_TOKEN", raising=False)
        (tmp_path / "secrets.env").write_text(
            'STX_FICTIONAL_TOKEN="widget # sprocket"\n'
        )
        DotenvSource(tmp_path / "secrets.env").ensure_loaded([])
        import os

        assert os.environ["STX_FICTIONAL_TOKEN"] == "widget # sprocket"

    def test_single_quotes_stripped(self, tmp_path, monkeypatch):
        monkeypatch.delenv("STX_FICTIONAL_TOKEN", raising=False)
        (tmp_path / "secrets.env").write_text("STX_FICTIONAL_TOKEN='widget sprocket'\n")
        DotenvSource(tmp_path / "secrets.env").ensure_loaded([])
        import os

        assert os.environ["STX_FICTIONAL_TOKEN"] == "widget sprocket"

    def test_unquoted_inline_comment_stripped(self, tmp_path, monkeypatch):
        monkeypatch.delenv("STX_FICTIONAL_TOKEN", raising=False)
        (tmp_path / "secrets.env").write_text(
            "STX_FICTIONAL_TOKEN=widget-sprocket # a comment\n"
        )
        DotenvSource(tmp_path / "secrets.env").ensure_loaded([])
        import os

        assert os.environ["STX_FICTIONAL_TOKEN"] == "widget-sprocket"

    def test_full_line_comments_and_blank_lines_skipped(self, tmp_path, monkeypatch):
        monkeypatch.delenv("STX_FICTIONAL_TOKEN", raising=False)
        (tmp_path / "secrets.env").write_text(
            "# a comment\n\nSTX_FICTIONAL_TOKEN=widget-sprocket\n"
        )
        DotenvSource(tmp_path / "secrets.env").ensure_loaded(["STX_FICTIONAL_TOKEN"])

    def test_export_prefix_supported(self, tmp_path, monkeypatch):
        monkeypatch.delenv("STX_FICTIONAL_TOKEN", raising=False)
        (tmp_path / "secrets.env").write_text(
            "export STX_FICTIONAL_TOKEN=widget-sprocket\n"
        )
        DotenvSource(tmp_path / "secrets.env").ensure_loaded(["STX_FICTIONAL_TOKEN"])
        import os

        assert os.environ["STX_FICTIONAL_TOKEN"] == "widget-sprocket"

    def test_existing_env_var_takes_precedence_over_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_FICTIONAL_TOKEN", "from-host-env")
        (tmp_path / "secrets.env").write_text("STX_FICTIONAL_TOKEN=from-file\n")
        DotenvSource(tmp_path / "secrets.env").ensure_loaded(["STX_FICTIONAL_TOKEN"])
        import os

        assert os.environ["STX_FICTIONAL_TOKEN"] == "from-host-env"

    def test_returns_the_parsed_file_mapping_not_the_ambient_shell(
        self, tmp_path, monkeypatch
    ):
        # The returned mapping is what compose/provision variables resolve
        # from — it must be the secret source's own values, never an ambient
        # shell var the file doesn't define.
        monkeypatch.setenv("AMBIENT_ONLY", "leaked-from-shell")
        (tmp_path / "secrets.env").write_text("FROM_FILE=file-value\n")
        loaded = DotenvSource(tmp_path / "secrets.env").ensure_loaded([])
        assert loaded == {"FROM_FILE": "file-value"}

    def test_returned_mapping_excludes_process_control_vars(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("STX_FICTIONAL_TOKEN", raising=False)
        (tmp_path / "secrets.env").write_text(
            "PATH=/evil\nSTX_FICTIONAL_TOKEN=widget-sprocket\n"
        )
        loaded = DotenvSource(tmp_path / "secrets.env").ensure_loaded([])
        assert "PATH" not in loaded
        assert loaded["STX_FICTIONAL_TOKEN"] == "widget-sprocket"

    def test_never_reexecs(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr("os.execvp", lambda *a, **k: calls.append((a, k)))
        DotenvSource(tmp_path / "missing.env").ensure_loaded([])
        assert calls == []

    def test_empty_value_is_treated_as_missing(self, tmp_path, monkeypatch):
        monkeypatch.delenv("STX_FICTIONAL_TOKEN", raising=False)
        (tmp_path / "secrets.env").write_text("STX_FICTIONAL_TOKEN=\n")
        with pytest.raises(SecretsError) as exc_info:
            DotenvSource(tmp_path / "secrets.env").ensure_loaded(
                ["STX_FICTIONAL_TOKEN"]
            )
        assert exc_info.value.missing == ["STX_FICTIONAL_TOKEN"]

    def test_existing_empty_env_var_is_treated_as_missing(self, monkeypatch):
        monkeypatch.setenv("STX_FICTIONAL_TOKEN", "")
        with pytest.raises(SecretsError) as exc_info:
            DotenvSource(Path("/nonexistent")).ensure_loaded(["STX_FICTIONAL_TOKEN"])
        assert exc_info.value.missing == ["STX_FICTIONAL_TOKEN"]

    def test_unterminated_double_quote_raises_naming_the_line(
        self, tmp_path, monkeypatch
    ):
        path = tmp_path / "secrets.env"
        path.write_text('STX_FICTIONAL_TOKEN="widget-sprocket\n')
        with pytest.raises(StxError, match=r"secrets\.env:1.*unterminated quote"):
            DotenvSource(path).ensure_loaded([])

    def test_unterminated_single_quote_raises(self, tmp_path, monkeypatch):
        path = tmp_path / "secrets.env"
        path.write_text("STX_FICTIONAL_TOKEN='widget-sprocket\n")
        with pytest.raises(StxError, match="unterminated quote"):
            DotenvSource(path).ensure_loaded([])

    def test_hash_glued_to_value_is_not_a_comment(self, tmp_path, monkeypatch):
        monkeypatch.delenv("STX_FICTIONAL_TOKEN", raising=False)
        (tmp_path / "secrets.env").write_text("STX_FICTIONAL_TOKEN=widget#sprocket\n")
        DotenvSource(tmp_path / "secrets.env").ensure_loaded([])
        import os

        assert os.environ["STX_FICTIONAL_TOKEN"] == "widget#sprocket"

    def test_hash_at_value_start_is_a_comment(self, tmp_path, monkeypatch):
        monkeypatch.delenv("STX_FICTIONAL_TOKEN", raising=False)
        (tmp_path / "secrets.env").write_text("STX_FICTIONAL_TOKEN=#sprocket\n")
        DotenvSource(tmp_path / "secrets.env").ensure_loaded([])
        import os

        assert os.environ["STX_FICTIONAL_TOKEN"] == ""

    @pytest.mark.parametrize(
        "key",
        [
            "PATH",
            "HOME",
            "SHELL",
            "IFS",
            "LD_PRELOAD",
            "DYLD_INSERT_LIBRARIES",
            "PYTHONPATH",
        ],
    )
    def test_process_control_vars_are_never_loaded(self, tmp_path, monkeypatch, key):
        monkeypatch.delenv(key, raising=False)
        (tmp_path / "secrets.env").write_text(f"{key}=malicious-value\n")
        DotenvSource(tmp_path / "secrets.env").ensure_loaded([])
        import os

        assert key not in os.environ

    def test_process_control_var_warns(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("LD_PRELOAD", raising=False)
        (tmp_path / "secrets.env").write_text("LD_PRELOAD=malicious.so\n")
        DotenvSource(tmp_path / "secrets.env").ensure_loaded([])
        assert "LD_PRELOAD" in capsys.readouterr().err

    def test_process_control_var_does_not_block_other_keys(self, tmp_path, monkeypatch):
        monkeypatch.delenv("STX_FICTIONAL_TOKEN", raising=False)
        (tmp_path / "secrets.env").write_text(
            "PATH=/evil\nSTX_FICTIONAL_TOKEN=widget-sprocket\n"
        )
        DotenvSource(tmp_path / "secrets.env").ensure_loaded(["STX_FICTIONAL_TOKEN"])
        import os

        assert os.environ["STX_FICTIONAL_TOKEN"] == "widget-sprocket"


class TestOpSourceReexec:
    def test_reexecs_with_expected_argv_and_sets_guard(self, monkeypatch):
        monkeypatch.delenv("_STX_SECRETS_LOADED", raising=False)
        calls = []
        monkeypatch.setattr("os.execvpe", lambda *a: calls.append(a))

        OpSource("fictional-environment-id").ensure_loaded(["STX_FICTIONAL_TOKEN"])

        assert len(calls) == 1
        program, argv, env = calls[0]
        assert program == "op"
        assert argv == [
            "op",
            "run",
            "--environment",
            "fictional-environment-id",
            "--",
            sys.executable,
            *sys.argv,
        ]
        # The guard rides in the child's explicit env, not this process's own.
        assert env["_STX_SECRETS_LOADED"] == "1"

    def test_strips_requested_names_from_the_child_env_before_reexec(self, monkeypatch):
        # `op run` passes the whole parent environment through, so a requested
        # name already set in the operator's shell would reach the re-exec'd
        # process and be indistinguishable from an op-injected value. Every
        # requested name (required and optional alike) is removed from the
        # child's env first; anything unrelated is kept.
        monkeypatch.delenv("_STX_SECRETS_LOADED", raising=False)
        monkeypatch.setenv("STX_FICTIONAL_TOKEN", "from-operator-shell")
        monkeypatch.setenv("FICTIONAL_OPTIONAL", "also-from-shell")
        monkeypatch.setenv("UNRELATED_AMBIENT", "kept")
        captured = {}
        monkeypatch.setattr(
            "os.execvpe", lambda program, argv, env: captured.update(env=env)
        )

        OpSource("fictional-environment-id").ensure_loaded(
            ["STX_FICTIONAL_TOKEN"], optional=["FICTIONAL_OPTIONAL"]
        )

        env = captured["env"]
        assert "STX_FICTIONAL_TOKEN" not in env
        assert "FICTIONAL_OPTIONAL" not in env
        assert env["UNRELATED_AMBIENT"] == "kept"
        assert env["_STX_SECRETS_LOADED"] == "1"

    def test_guard_set_and_required_present_returns_without_reexec(self, monkeypatch):
        monkeypatch.setenv("_STX_SECRETS_LOADED", "1")
        monkeypatch.setenv("STX_FICTIONAL_TOKEN", "already-here")
        calls = []
        monkeypatch.setattr("os.execvpe", lambda *a: calls.append(a))

        OpSource("fictional-environment-id").ensure_loaded(["STX_FICTIONAL_TOKEN"])

        assert calls == []

    def test_returns_only_the_names_it_was_asked_to_load(self, monkeypatch):
        # op injects far more than it was asked for; the mapping compose vars
        # resolve from is restricted to the requested names so an unrelated
        # ambient/op var can't satisfy a repo's ${...}.
        monkeypatch.setenv("_STX_SECRETS_LOADED", "1")
        monkeypatch.setenv("STX_FICTIONAL_TOKEN", "requested")
        monkeypatch.setenv("UNRELATED_AMBIENT", "not-requested")
        loaded = OpSource("fictional-environment-id").ensure_loaded(
            ["STX_FICTIONAL_TOKEN"]
        )
        assert loaded == {"STX_FICTIONAL_TOKEN": "requested"}

    def test_returns_optional_names_that_are_set_and_skips_unset_ones(
        self, monkeypatch
    ):
        monkeypatch.setenv("_STX_SECRETS_LOADED", "1")
        monkeypatch.setenv("STX_FICTIONAL_TOKEN", "requested")
        monkeypatch.setenv("FICTIONAL_OPTIONAL", "present")
        monkeypatch.delenv("FICTIONAL_ABSENT", raising=False)
        loaded = OpSource("fictional-environment-id").ensure_loaded(
            ["STX_FICTIONAL_TOKEN"], optional=["FICTIONAL_OPTIONAL", "FICTIONAL_ABSENT"]
        )
        assert loaded == {
            "STX_FICTIONAL_TOKEN": "requested",
            "FICTIONAL_OPTIONAL": "present",
        }

    def test_guard_set_and_required_missing_raises(self, monkeypatch):
        monkeypatch.setenv("_STX_SECRETS_LOADED", "1")
        monkeypatch.delenv("STX_FICTIONAL_TOKEN", raising=False)

        with pytest.raises(SecretsError) as exc_info:
            OpSource("fictional-environment-id").ensure_loaded(["STX_FICTIONAL_TOKEN"])
        assert exc_info.value.missing == ["STX_FICTIONAL_TOKEN"]

    def test_op_not_installed_raises_secrets_error_naming_the_fix(self, monkeypatch):
        monkeypatch.delenv("_STX_SECRETS_LOADED", raising=False)
        monkeypatch.setattr(
            "os.execvpe", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError())
        )

        with pytest.raises(SecretsError, match="1Password CLI"):
            OpSource("fictional-environment-id").ensure_loaded(["STX_FICTIONAL_TOKEN"])


class TestSourceFor:
    def test_defaults_to_dotenv_at_default_path(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STX_CONFIG_PATH", str(tmp_path / "stx" / "config.toml"))
        source = source_for({})
        assert isinstance(source, DotenvSource)
        assert source._path == tmp_path / "stx" / "secrets.env"

    def test_dotenv_with_explicit_path(self, tmp_path):
        source = source_for(
            {
                "secrets": {
                    "source": "dotenv",
                    "dotenv": {"path": str(tmp_path / "custom.env")},
                }
            }
        )
        assert isinstance(source, DotenvSource)
        assert source._path == tmp_path / "custom.env"

    def test_op_with_environment(self):
        source = source_for(
            {
                "secrets": {
                    "source": "op",
                    "op": {"environment": "fictional-environment-id"},
                }
            }
        )
        assert isinstance(source, OpSource)
        assert source._environment == "fictional-environment-id"

    def test_op_without_environment_raises(self):
        with pytest.raises(SecretsError):
            source_for({"secrets": {"source": "op"}})

    def test_unknown_source_raises(self):
        from stx.errors import StxError

        with pytest.raises(StxError):
            source_for({"secrets": {"source": "keychain"}})


class TestDotenvModeOk:
    def test_missing_file_is_ok(self, tmp_path):
        assert dotenv_mode_ok(tmp_path / "missing.env") is True

    def test_mode_600_is_ok(self, tmp_path):
        path = tmp_path / "secrets.env"
        path.write_text("STX_FICTIONAL_TOKEN=widget-sprocket\n")
        path.chmod(0o600)
        assert dotenv_mode_ok(path) is True

    def test_mode_400_is_ok(self, tmp_path):
        path = tmp_path / "secrets.env"
        path.write_text("STX_FICTIONAL_TOKEN=widget-sprocket\n")
        path.chmod(0o400)
        assert dotenv_mode_ok(path) is True

    def test_group_readable_is_not_ok(self, tmp_path):
        path = tmp_path / "secrets.env"
        path.write_text("STX_FICTIONAL_TOKEN=widget-sprocket\n")
        path.chmod(0o640)
        assert dotenv_mode_ok(path) is False

    def test_world_readable_is_not_ok(self, tmp_path):
        path = tmp_path / "secrets.env"
        path.write_text("STX_FICTIONAL_TOKEN=widget-sprocket\n")
        path.chmod(0o644)
        assert dotenv_mode_ok(path) is False


def test_secrets_error_message_names_every_missing_key():
    error = SecretsError(["STX_FICTIONAL_A", "STX_FICTIONAL_B"])
    assert "STX_FICTIONAL_A" in str(error)
    assert "STX_FICTIONAL_B" in str(error)
