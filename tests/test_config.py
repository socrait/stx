from pathlib import Path
from unittest.mock import MagicMock

import pytest

import stx.config as _config
from stx.config import (
    KNOWN_CONFIG_KEYS,
    config_path,
    config_value,
    default_dotenv_path,
    load_config,
    resolve_branch_username,
    set_config_value,
    unset_config_value,
)
from stx.errors import StxError


class TestConfigPath:
    def test_env_var_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STX_CONFIG_PATH", str(tmp_path / "nested" / "config.toml"))
        assert config_path() == (tmp_path / "nested" / "config.toml").resolve()

    def test_default_is_xdg_config_home_stx_config_toml(self, monkeypatch, tmp_path):
        monkeypatch.delenv("STX_CONFIG_PATH", raising=False)
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setattr(_config.Path, "home", lambda: tmp_path)
        assert config_path() == tmp_path / ".config" / "stx" / "config.toml"

    def test_xdg_config_home_override(self, monkeypatch, tmp_path):
        monkeypatch.delenv("STX_CONFIG_PATH", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
        assert config_path() == tmp_path / "xdg-config" / "stx" / "config.toml"


def test_default_dotenv_path_is_beside_config_toml(monkeypatch, tmp_path):
    monkeypatch.setenv("STX_CONFIG_PATH", str(tmp_path / "stx" / "config.toml"))
    assert default_dotenv_path() == tmp_path / "stx" / "secrets.env"


class TestLoadConfig:
    def test_load_config_missing_file_returns_empty(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STX_CONFIG_PATH", str(tmp_path / "config.toml"))
        assert load_config() == {}

    def test_load_config_corrupt_file_raises_naming_the_file(
        self, monkeypatch, tmp_path
    ):
        path = tmp_path / "config.toml"
        path.write_text("not = [valid")
        monkeypatch.setenv("STX_CONFIG_PATH", str(path))

        with pytest.raises(StxError, match=str(path)):
            load_config()

    def test_load_config_unreadable_file_raises(self, monkeypatch, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text('[branch]\nuser = "alex"\n')
        monkeypatch.setenv("STX_CONFIG_PATH", str(path))

        def _raise_read_bytes(self, *a, **kw):
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "read_bytes", _raise_read_bytes)

        with pytest.raises(StxError, match="cannot read config"):
            load_config()

    def test_load_config_returns_nested_tables(self, monkeypatch, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text('[branch]\nuser = "alex"\n')
        monkeypatch.setenv("STX_CONFIG_PATH", str(path))

        assert load_config() == {"branch": {"user": "alex"}}


class TestConfigValue:
    def test_returns_none_for_missing_key(self):
        assert config_value({}, "branch.user") is None

    def test_returns_none_when_parent_table_missing(self):
        assert config_value({"branch": {}}, "branch.user") is None

    def test_returns_nested_value(self):
        assert config_value({"branch": {"user": "alex"}}, "branch.user") == "alex"

    def test_three_level_path(self):
        config = {"secrets": {"op": {"environment": "env-id"}}}
        assert config_value(config, "secrets.op.environment") == "env-id"


class TestSetUnsetConfigValue:
    def test_set_then_load_round_trips(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STX_CONFIG_PATH", str(tmp_path / "config.toml"))
        set_config_value("branch.user", "alex")
        assert load_config() == {"branch": {"user": "alex"}}

    def test_set_is_atomic_no_tmp_file_left_behind(self, monkeypatch, tmp_path):
        path = tmp_path / "config.toml"
        monkeypatch.setenv("STX_CONFIG_PATH", str(path))
        set_config_value("branch.user", "alex")
        assert list(tmp_path.glob("*.tmp")) == []

    def test_set_keeps_a_user_comment(self, monkeypatch, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text('# my own notes\n[branch]\nuser = "alex"\n')
        monkeypatch.setenv("STX_CONFIG_PATH", str(path))

        set_config_value("secrets.source", "op")

        text = path.read_text()
        assert "# my own notes" in text
        assert 'user = "alex"' in text

    def test_set_preserves_a_sibling_key_in_the_same_table(self, monkeypatch, tmp_path):
        path = tmp_path / "config.toml"
        monkeypatch.setenv("STX_CONFIG_PATH", str(path))
        set_config_value("secrets.op.environment", "env-id")

        set_config_value("secrets.source", "op")

        assert load_config() == {
            "secrets": {"source": "op", "op": {"environment": "env-id"}}
        }

    def test_unset_removes_only_the_named_key(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STX_CONFIG_PATH", str(tmp_path / "config.toml"))
        set_config_value("secrets.op.environment", "env-id")
        set_config_value("secrets.source", "op")

        unset_config_value("secrets.source")

        assert load_config() == {"secrets": {"op": {"environment": "env-id"}}}

    def test_unset_idempotent_on_already_unset_key(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STX_CONFIG_PATH", str(tmp_path / "config.toml"))
        unset_config_value("branch.user")
        assert load_config() == {}


class TestKnownConfigKeys:
    def test_four_keys_present(self):
        assert set(KNOWN_CONFIG_KEYS) == {
            "branch.user",
            "secrets.source",
            "secrets.dotenv.path",
            "secrets.op.environment",
        }

    def test_branch_user_rejects_invalid(self):
        spec = KNOWN_CONFIG_KEYS["branch.user"]
        try:
            spec.validator("Not Valid!")
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")

    def test_branch_user_accepts_valid(self):
        KNOWN_CONFIG_KEYS["branch.user"].validator("alex")

    def test_secrets_source_rejects_unknown(self):
        spec = KNOWN_CONFIG_KEYS["secrets.source"]
        try:
            spec.validator("keychain")
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")

    def test_secrets_source_accepts_dotenv_and_op(self):
        spec = KNOWN_CONFIG_KEYS["secrets.source"]
        spec.validator("dotenv")
        spec.validator("op")

    def test_secrets_dotenv_path_rejects_empty(self):
        spec = KNOWN_CONFIG_KEYS["secrets.dotenv.path"]
        try:
            spec.validator("   ")
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")

    def test_secrets_op_environment_rejects_empty(self):
        spec = KNOWN_CONFIG_KEYS["secrets.op.environment"]
        try:
            spec.validator("")
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")


class TestResolveBranchUsername:
    def test_uses_config_override_when_set(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STX_CONFIG_PATH", str(tmp_path / "config.toml"))
        set_config_value("branch.user", "alex")
        monkeypatch.setattr(_config, "git_username", lambda: "should-not-be-used")

        assert resolve_branch_username() == "alex"

    def test_config_override_wins_even_with_a_forge(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STX_CONFIG_PATH", str(tmp_path / "config.toml"))
        set_config_value("branch.user", "alex")
        forge = MagicMock(username=MagicMock(return_value="forge-user"))

        assert resolve_branch_username(forge) == "alex"

    def test_falls_back_to_forge_username_when_config_unset(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("STX_CONFIG_PATH", str(tmp_path / "config.toml"))
        forge = MagicMock(username=MagicMock(return_value="forge-user"))

        assert resolve_branch_username(forge) == "forge-user"

    def test_falls_back_to_git_username_when_forge_username_is_none(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("STX_CONFIG_PATH", str(tmp_path / "config.toml"))
        monkeypatch.setattr(_config, "git_username", lambda: "derived-user")
        forge = MagicMock(username=MagicMock(return_value=None))

        assert resolve_branch_username(forge) == "derived-user"

    def test_falls_back_to_git_username_when_no_forge(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STX_CONFIG_PATH", str(tmp_path / "config.toml"))
        monkeypatch.setattr(_config, "git_username", lambda: "derived-user")

        assert resolve_branch_username() == "derived-user"


class TestGitUsername:
    def test_returns_git_email_local_part(self, monkeypatch):
        def mock_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = "alex@mycompany.com\n"
            return result

        monkeypatch.setattr(_config.subprocess, "run", mock_run)
        assert _config.git_username() == "alex"

    def test_falls_back_to_dev_with_no_identity_at_all(self, monkeypatch):
        def mock_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            return result

        monkeypatch.setattr(_config.subprocess, "run", mock_run)
        assert _config.git_username() == "dev"
