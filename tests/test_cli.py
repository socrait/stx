from importlib.metadata import version

import pytest

from stx.cli import main
from stx.commands.ls import SandboxLsCommand
from stx.errors import StxError


class TestMainTopLevelErrorHandling:
    def test_stx_error_prints_clean_message_and_exits_1(self, monkeypatch, capsys):
        monkeypatch.setattr(
            SandboxLsCommand,
            "execute",
            lambda self, args: (_ for _ in ()).throw(StxError("registry is corrupt")),
        )
        monkeypatch.setattr("sys.argv", ["stx", "ls"])

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert err.strip() == "error: registry is corrupt"

    def test_unexpected_exception_still_tracebacks(self, monkeypatch):
        monkeypatch.setattr(
            SandboxLsCommand,
            "execute",
            lambda self, args: (_ for _ in ()).throw(ValueError("boom")),
        )
        monkeypatch.setattr("sys.argv", ["stx", "ls"])

        with pytest.raises(ValueError, match="boom"):
            main()

    def test_normal_return_code_still_used_for_exit(self, monkeypatch):
        monkeypatch.setattr(SandboxLsCommand, "execute", lambda self, args: 1)
        monkeypatch.setattr("sys.argv", ["stx", "ls"])

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1


@pytest.mark.parametrize("command", ["new", "rm", "refresh", "agent"])
def test_dry_run_is_rejected_where_it_would_be_ignored(command, monkeypatch, capsys):
    # A mutating command that accepted --dry-run without honoring it would make the
    # real change while the caller believed nothing happened.
    monkeypatch.setattr("sys.argv", ["stx", command, "--dry-run", "demo"])
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 2
    assert "--dry-run" in capsys.readouterr().err


def test_version_flag_prints_package_version(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["stx", "--version"])

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 0
    assert capsys.readouterr().out == f"stx {version('stx')}\n"
