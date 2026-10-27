import argparse

import pytest

from stx.commands.paths import SandboxPathsCommand
from stx.errors import StxError


@pytest.fixture
def homes(monkeypatch, tmp_path):
    for name in (
        "STX_STATE_ROOT",
        "XDG_STATE_HOME",
        "STX_CONFIG_PATH",
        "XDG_CONFIG_HOME",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _run(path_name=None, slug=None):
    return SandboxPathsCommand().execute(
        argparse.Namespace(path_name=path_name, slug=slug)
    )


class TestTable:
    def test_defaults_name_their_origin(self, homes, capsys):
        _run()

        out = capsys.readouterr().out
        assert f"state     {homes}/.local/state/stx  (default)" in out
        assert f"config    {homes}/.config/stx/config.toml  (default)" in out
        assert f"dotenv    {homes}/.config/stx/secrets.env  (default)" in out
        assert f"platform  {homes}/.local/state/stx/platform\n" in out

    def test_env_overrides_name_the_variable(self, homes, monkeypatch, capsys):
        monkeypatch.setenv("XDG_STATE_HOME", str(homes / "xdg"))
        monkeypatch.setenv("STX_CONFIG_PATH", str(homes / "elsewhere.toml"))

        _run()

        out = capsys.readouterr().out
        assert f"{homes}/xdg/stx  (XDG_STATE_HOME)" in out
        assert f"{homes}/elsewhere.toml  (STX_CONFIG_PATH)" in out
        assert f"{homes}/secrets.env  (default)" in out

    def test_configured_dotenv_unused_under_op(self, homes, monkeypatch, capsys):
        config = homes / "config.toml"
        config.write_text('[secrets]\nsource = "op"\ndotenv = { path = "/x/s.env" }\n')
        monkeypatch.setenv("STX_CONFIG_PATH", str(config))

        _run()

        out = capsys.readouterr().out
        assert "/x/s.env  (secrets.dotenv.path; unused, secrets.source=op)" in out

    def test_slug_lists_that_sandboxs_paths(self, homes, capsys):
        _run(slug="demo")

        out = capsys.readouterr().out
        state = homes / ".local/state/stx"
        assert f"worktree   {state}/worktrees/demo\n" in out
        assert f"compose    {state}/compose/demo.json\n" in out
        assert "state " not in out


class TestSingle:
    def test_global_name_prints_bare_path(self, homes, capsys):
        assert _run("state") is None

        assert capsys.readouterr().out == f"{homes}/.local/state/stx\n"

    def test_slug_name_prints_bare_path(self, homes, capsys):
        assert _run("drop-dmz", slug="demo") is None

        assert capsys.readouterr().out == f"{homes}/.local/state/stx/drop-dmz/demo\n"

    def test_slug_name_without_slug_errors(self, homes, capsys):
        assert _run("worktree") == 1

        assert "needs --slug" in capsys.readouterr().err

    def test_unknown_name_lists_known_ones(self, homes, capsys):
        assert _run("bogus") == 1

        assert "known: state, config" in capsys.readouterr().err

    def test_invalid_slug_refused(self, homes):
        with pytest.raises(StxError, match="not a valid sandbox slug"):
            _run("worktree", slug="../escape")
