import argparse
import tomllib

from stx.commands.config import SandboxConfigCommand


def _args(action: str, *, key: str = "", value: str = "") -> argparse.Namespace:
    return argparse.Namespace(config_action=action, key=key, value=value)


class TestList:
    def test_shows_unset_key(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("STX_CONFIG_PATH", str(tmp_path / "config.toml"))

        SandboxConfigCommand().execute(_args("list"))

        assert "branch.user: (unset)" in capsys.readouterr().out

    def test_shows_set_value(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("STX_CONFIG_PATH", str(tmp_path / "config.toml"))
        SandboxConfigCommand().execute(_args("set", key="branch.user", value="alex"))
        capsys.readouterr()

        SandboxConfigCommand().execute(_args("list"))

        assert "branch.user: alex" in capsys.readouterr().out


class TestGet:
    def test_unknown_key_errors(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("STX_CONFIG_PATH", str(tmp_path / "config.toml"))

        result = SandboxConfigCommand().execute(_args("get", key="nonexistent"))

        assert result == 1
        assert "unknown config key" in capsys.readouterr().err

    def test_unset_known_key_returns_1(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STX_CONFIG_PATH", str(tmp_path / "config.toml"))

        result = SandboxConfigCommand().execute(_args("get", key="branch.user"))

        assert result == 1

    def test_set_key_prints_value(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("STX_CONFIG_PATH", str(tmp_path / "config.toml"))
        SandboxConfigCommand().execute(_args("set", key="branch.user", value="alex"))
        capsys.readouterr()

        result = SandboxConfigCommand().execute(_args("get", key="branch.user"))

        assert result is None
        assert capsys.readouterr().out.strip() == "alex"


class TestSet:
    def test_unknown_key_errors(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("STX_CONFIG_PATH", str(tmp_path / "config.toml"))

        result = SandboxConfigCommand().execute(
            _args("set", key="nonexistent", value="x")
        )

        assert result == 1
        assert "unknown config key" in capsys.readouterr().err

    def test_invalid_value_rejected_by_validator(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("STX_CONFIG_PATH", str(tmp_path / "config.toml"))

        result = SandboxConfigCommand().execute(
            _args("set", key="branch.user", value="Not Valid!")
        )

        assert result == 1
        assert "must match" in capsys.readouterr().err

    def test_valid_value_persists_to_file(self, monkeypatch, tmp_path):
        config_path = tmp_path / "config.toml"
        monkeypatch.setenv("STX_CONFIG_PATH", str(config_path))

        result = SandboxConfigCommand().execute(
            _args("set", key="branch.user", value="alex")
        )

        assert result is None
        assert tomllib.loads(config_path.read_text()) == {"branch": {"user": "alex"}}

    def test_unknown_key_rejected_before_corrupting_user_toml(
        self, monkeypatch, tmp_path, capsys
    ):
        config_path = tmp_path / "config.toml"
        monkeypatch.setenv("STX_CONFIG_PATH", str(config_path))

        result = SandboxConfigCommand().execute(
            _args("set", key="dmz.image", value="widgetshop-devbox")
        )

        assert result == 1
        assert "unknown config key" in capsys.readouterr().err
        assert not config_path.exists()

    def test_old_flat_branch_username_key_rejected(self, monkeypatch, tmp_path, capsys):
        # the flat `branch_username` key is gone — `branch.user` is the only spelling.
        monkeypatch.setenv("STX_CONFIG_PATH", str(tmp_path / "config.toml"))

        result = SandboxConfigCommand().execute(
            _args("set", key="branch_username", value="alex")
        )

        assert result == 1
        assert "unknown config key" in capsys.readouterr().err


class TestUnset:
    def test_unknown_key_errors(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("STX_CONFIG_PATH", str(tmp_path / "config.toml"))

        result = SandboxConfigCommand().execute(_args("unset", key="nonexistent"))

        assert result == 1
        assert "unknown config key" in capsys.readouterr().err

    def test_removes_existing_value(self, monkeypatch, tmp_path):
        config_path = tmp_path / "config.toml"
        monkeypatch.setenv("STX_CONFIG_PATH", str(config_path))
        SandboxConfigCommand().execute(_args("set", key="branch.user", value="alex"))

        result = SandboxConfigCommand().execute(_args("unset", key="branch.user"))

        assert result is None
        assert tomllib.loads(config_path.read_text()) == {"branch": {}}

    def test_idempotent_on_already_unset_key(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STX_CONFIG_PATH", str(tmp_path / "config.toml"))

        result = SandboxConfigCommand().execute(_args("unset", key="branch.user"))

        assert result is None
