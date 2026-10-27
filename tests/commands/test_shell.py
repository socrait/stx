import argparse
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import stx.cli as _cli
import stx.commands.shell as _cmd
from stx.commands.shell import (
    SandboxShellCommand,
    _detached_exec_args,
    _detached_log_name,
    _docker_exec_args,
)
from stx.core import (
    BoxRecord,
    SandboxRecord,
    SandboxState,
)

_MOD = "stx.commands.shell"


def _args(
    slug: str = "my-slug",
    *,
    box: str | None = None,
    shell_command: str | None = None,
    trailing_argv: list[str] | None = None,
    detach: bool = False,
):
    return argparse.Namespace(
        slug=slug,
        box=box,
        shell_command=shell_command,
        trailing_argv=trailing_argv or [],
        detach=detach,
    )


@pytest.fixture(autouse=True)
def _box_is_running(monkeypatch):
    """Every exec/detach test below assumes the box is already up — the
    not-running/missing-container cases get their own tests in
    TestBoxNotRunning."""
    monkeypatch.setattr(_cmd, "container_status", lambda container: "running")


def _make_record(slug: str) -> SandboxRecord:
    return SandboxRecord(
        slug=slug,
        created="2026-06-26T00:00:00+00:00",
        state=SandboxState.READY,
        redis_index=1,
        tailnet_port=5099,
        boxes={"app": BoxRecord(zone="trusted", user="devuser")},
    )


class TestArgumentWiring:
    def test_command_flag_does_not_collide_with_top_level_command_dest(self):
        top = argparse.ArgumentParser()
        top_subparsers = top.add_subparsers(dest="command")
        group_parser = top_subparsers.add_parser("sandbox")
        group_subparsers = group_parser.add_subparsers(dest="subcommand")

        SandboxShellCommand().add_parser(group_subparsers)

        args = top.parse_args(["sandbox", "shell", "my-slug", "-c", "echo hi"])

        assert args.command == "sandbox"
        assert args.subcommand == "shell"
        assert args.shell_command == "echo hi"

    def test_trailing_argv_survives_full_cli_split_and_parse(self):
        top = argparse.ArgumentParser()
        top_subparsers = top.add_subparsers(dest="command")
        group_parser = top_subparsers.add_parser("sandbox")
        group_subparsers = group_parser.add_subparsers(dest="subcommand")
        SandboxShellCommand().add_parser(group_subparsers)

        raw = [
            "sandbox",
            "shell",
            "my-slug",
            "--box",
            "app",
            "--",
            "./dev.sh",
            "up",
            "-v",
        ]
        argv, trailing_argv = _cli.split_trailing_argv(raw)
        args = top.parse_args(argv)
        args.trailing_argv = trailing_argv

        assert args.box == "app"
        assert args.shell_command is None
        assert args.trailing_argv == ["./dev.sh", "up", "-v"]

    def test_box_flag_accepts_short_form(self):
        top = argparse.ArgumentParser()
        top_subparsers = top.add_subparsers(dest="command")
        group_parser = top_subparsers.add_parser("sandbox")
        group_subparsers = group_parser.add_subparsers(dest="subcommand")
        SandboxShellCommand().add_parser(group_subparsers)

        args = top.parse_args(["sandbox", "shell", "my-slug", "-b", "app"])

        assert args.box == "app"

    def test_box_is_not_a_positional_so_a_bare_word_after_slug_errors(self):
        top = argparse.ArgumentParser()
        top_subparsers = top.add_subparsers(dest="command")
        group_parser = top_subparsers.add_parser("sandbox")
        group_subparsers = group_parser.add_subparsers(dest="subcommand")
        SandboxShellCommand().add_parser(group_subparsers)

        with pytest.raises(SystemExit):
            top.parse_args(["sandbox", "shell", "my-slug", "app"])

    def test_no_trailing_dashdash_yields_empty_trailing_argv(self):
        argv, trailing_argv = _cli.split_trailing_argv(["sandbox", "shell", "my-slug"])

        assert argv == ["sandbox", "shell", "my-slug"]
        assert trailing_argv == []


class TestDockerExecArgs:
    def test_no_command_opens_interactive_shell(self):
        assert _docker_exec_args("stx-foo--app", "devuser", None, []) == [
            "docker",
            "exec",
            "-it",
            "-u",
            "devuser",
            "stx-foo--app",
            "bash",
        ]

    def test_command_string_runs_via_bash_c_without_tty(self):
        argv = _docker_exec_args("stx-foo--app", "devuser", "./dev.sh up", [])
        assert argv == [
            "docker",
            "exec",
            "-u",
            "devuser",
            "stx-foo--app",
            "bash",
            "-c",
            "./dev.sh up",
        ]
        assert "-it" not in argv

    def test_exec_argv_runs_directly_without_shell_or_tty(self):
        argv = _docker_exec_args(
            "stx-foo--app", "devuser", None, ["./dev.sh", "up", "-v"]
        )
        assert argv == [
            "docker",
            "exec",
            "-u",
            "devuser",
            "stx-foo--app",
            "./dev.sh",
            "up",
            "-v",
        ]
        assert "-it" not in argv
        assert "bash" not in argv

    def test_exec_argv_takes_precedence_over_command_string(self):
        argv = _docker_exec_args("stx-foo--app", "devuser", "ignored", ["ls", "-la"])
        assert argv == ["docker", "exec", "-u", "devuser", "stx-foo--app", "ls", "-la"]

    def test_command_string_gets_it_when_tty_requested(self):
        argv = _docker_exec_args("stx-foo--app", "devuser", "./dev.sh up", [], tty=True)
        assert argv[:3] == ["docker", "exec", "-it"]

    def test_exec_argv_gets_it_when_tty_requested(self):
        argv = _docker_exec_args(
            "stx-foo--app", "devuser", None, ["./dev.sh", "up"], tty=True
        )
        assert argv[:3] == ["docker", "exec", "-it"]


class TestDetachedLogName:
    def test_derives_from_exec_argv_first_token(self):
        assert _detached_log_name(None, ["./dev.sh", "up"]) == "dev.sh.log"

    def test_derives_from_command_string_first_token(self):
        assert _detached_log_name("./dev.sh up -v", []) == "dev.sh.log"

    def test_strips_leading_path_components(self):
        assert _detached_log_name(None, ["/usr/local/bin/foo"]) == "foo.log"

    def test_sanitizes_non_filename_characters(self):
        assert _detached_log_name(None, ["weird$name!"]) == "weird-name.log"

    def test_falls_back_when_command_is_blank(self):
        assert _detached_log_name("   ", []) == "detached.log"

    def test_falls_back_when_first_token_is_pure_shell_syntax(self):
        assert (
            _detached_log_name("[[ -f /workspace/foo ]] && echo yes", [])
            == "detached.log"
        )


class TestDetachedExecArgs:
    def test_wraps_exec_argv_in_shell_with_redirect(self):
        argv = _detached_exec_args(
            "stx-foo--app",
            "devuser",
            None,
            ["./dev.sh", "up", "-v"],
            "dev.sh.log",
        )
        assert argv[:6] == [
            "docker",
            "exec",
            "-d",
            "-u",
            "devuser",
            "stx-foo--app",
        ]
        assert argv[6:8] == ["bash", "-c"]
        assert argv[8] == "{ ./dev.sh up -v ; } > /logs/dev.sh.log 2>&1"

    def test_wraps_command_string_in_shell_with_redirect(self):
        argv = _detached_exec_args(
            "stx-foo--app", "devuser", "./dev.sh up", [], "dev.sh.log"
        )
        assert argv[6:8] == ["bash", "-c"]
        assert argv[8] == "{ ./dev.sh up ; } > /logs/dev.sh.log 2>&1"

    def test_uses_bash_not_dash_so_bash_syntax_in_dash_c_still_works(self):
        argv = _detached_exec_args(
            "stx-foo--app",
            "devuser",
            "[[ -f /workspace/foo ]] && echo yes",
            [],
            "cmd.log",
        )
        assert argv[6] == "bash"
        assert "sh" not in argv[:7]

    def test_never_requests_a_tty(self):
        argv = _detached_exec_args("stx-foo--app", "devuser", "ls", [], "ls.log")
        assert "-it" not in argv
        assert "-t" not in argv
        assert "-i" not in argv


class TestExecute:
    def test_unknown_slug_errors(self, capsys):
        with patch(f"{_MOD}.load_registry", return_value={}):
            result = SandboxShellCommand().execute(_args(slug="nope"))

        assert result == 1
        assert "no sandbox named 'nope'" in capsys.readouterr().err

    def test_box_not_found_errors(self, capsys):
        with (
            patch(
                f"{_MOD}.load_registry",
                return_value={"my-slug": _make_record("my-slug")},
            ),
            patch(f"{_MOD}.box_not_found_error", return_value="no dmz box"),
        ):
            result = SandboxShellCommand().execute(_args(box="dmz"))

        assert result == 1
        assert "no dmz box" in capsys.readouterr().err

    def test_both_command_and_trailing_argv_errors(self, capsys):
        with patch(
            f"{_MOD}.load_registry", return_value={"my-slug": _make_record("my-slug")}
        ):
            result = SandboxShellCommand().execute(
                _args(shell_command="echo hi", trailing_argv=["ls"])
            )

        assert result == 1
        assert "pass either -c or a command after --, not both" in (
            capsys.readouterr().err
        )

    def test_execs_interactive_shell_without_command(self, monkeypatch):
        calls = []
        monkeypatch.setattr(_cmd.os, "execvp", lambda *a: calls.append(a))
        with patch(
            f"{_MOD}.load_registry", return_value={"my-slug": _make_record("my-slug")}
        ):
            result = SandboxShellCommand().execute(_args())

        assert result == 1  # execvp mocked, falls through to the post-exec return
        assert len(calls) == 1
        assert calls[0][1][:3] == ["docker", "exec", "-it"]
        assert calls[0][1][-1] == "bash"

    def test_execs_command_string_without_tty_when_not_a_real_terminal(
        self, monkeypatch
    ):
        calls = []
        monkeypatch.setattr(_cmd.os, "execvp", lambda *a: calls.append(a))
        monkeypatch.setattr(_cmd.sys.stdin, "isatty", lambda: False)
        with patch(
            f"{_MOD}.load_registry", return_value={"my-slug": _make_record("my-slug")}
        ):
            result = SandboxShellCommand().execute(_args(shell_command="./dev.sh up"))

        assert result == 1
        docker_cmd = calls[0][1]
        assert "-it" not in docker_cmd
        assert docker_cmd[-3:] == ["bash", "-c", "./dev.sh up"]

    def test_execs_command_string_with_tty_when_real_terminal_attached(
        self, monkeypatch
    ):
        calls = []
        monkeypatch.setattr(_cmd.os, "execvp", lambda *a: calls.append(a))
        monkeypatch.setattr(_cmd.sys.stdin, "isatty", lambda: True)
        with patch(
            f"{_MOD}.load_registry", return_value={"my-slug": _make_record("my-slug")}
        ):
            result = SandboxShellCommand().execute(_args(shell_command="./dev.sh up"))

        assert result == 1
        docker_cmd = calls[0][1]
        assert docker_cmd[:3] == ["docker", "exec", "-it"]
        assert docker_cmd[-3:] == ["bash", "-c", "./dev.sh up"]

    def test_execs_trailing_argv_directly_without_tty_when_not_a_real_terminal(
        self, monkeypatch
    ):
        calls = []
        monkeypatch.setattr(_cmd.os, "execvp", lambda *a: calls.append(a))
        monkeypatch.setattr(_cmd.sys.stdin, "isatty", lambda: False)
        with patch(
            f"{_MOD}.load_registry", return_value={"my-slug": _make_record("my-slug")}
        ):
            result = SandboxShellCommand().execute(
                _args(trailing_argv=["./dev.sh", "up"])
            )

        assert result == 1
        docker_cmd = calls[0][1]
        assert "-it" not in docker_cmd
        assert "bash" not in docker_cmd
        assert docker_cmd[-2:] == ["./dev.sh", "up"]

    def test_execs_trailing_argv_with_tty_when_real_terminal_attached(
        self, monkeypatch
    ):
        calls = []
        monkeypatch.setattr(_cmd.os, "execvp", lambda *a: calls.append(a))
        monkeypatch.setattr(_cmd.sys.stdin, "isatty", lambda: True)
        with patch(
            f"{_MOD}.load_registry", return_value={"my-slug": _make_record("my-slug")}
        ):
            result = SandboxShellCommand().execute(
                _args(trailing_argv=["./dev.sh", "up"])
            )

        assert result == 1
        docker_cmd = calls[0][1]
        assert docker_cmd[:3] == ["docker", "exec", "-it"]
        assert docker_cmd[-2:] == ["./dev.sh", "up"]


class TestExecuteDetach:
    def test_detach_with_shell_command_uses_bash(self, monkeypatch):
        run_calls = []

        def fake_run(argv, **kwargs):
            run_calls.append(argv)
            return MagicMock(returncode=0)

        monkeypatch.setattr(_cmd.subprocess, "run", fake_run)
        monkeypatch.setattr(_cmd, "logs_dir", lambda slug, box: Path("/tmp/logs"))
        with patch(
            f"{_MOD}.load_registry", return_value={"my-slug": _make_record("my-slug")}
        ):
            SandboxShellCommand().execute(
                _args(shell_command="[[ -f /workspace/foo ]] && echo yes", detach=True)
            )

        assert run_calls[0][6] == "bash"

    def test_detach_without_command_errors(self, capsys):
        with patch(
            f"{_MOD}.load_registry", return_value={"my-slug": _make_record("my-slug")}
        ):
            result = SandboxShellCommand().execute(_args(detach=True))

        assert result == 1
        assert "--detach needs a command" in capsys.readouterr().err

    def test_detach_never_calls_execvp(self, monkeypatch):
        execvp_calls = []
        monkeypatch.setattr(_cmd.os, "execvp", lambda *a: execvp_calls.append(a))
        monkeypatch.setattr(
            _cmd.subprocess, "run", lambda *a, **kw: MagicMock(returncode=0)
        )
        monkeypatch.setattr(_cmd, "logs_dir", lambda slug, box: Path("/tmp/logs"))
        with patch(
            f"{_MOD}.load_registry", return_value={"my-slug": _make_record("my-slug")}
        ):
            SandboxShellCommand().execute(
                _args(trailing_argv=["./dev.sh", "up"], detach=True)
            )

        assert execvp_calls == []

    def test_detach_runs_detached_exec_and_prints_log_paths(self, monkeypatch, capsys):
        run_calls = []

        def fake_run(argv, **kwargs):
            run_calls.append(argv)
            return MagicMock(returncode=0)

        monkeypatch.setattr(_cmd.subprocess, "run", fake_run)
        monkeypatch.setattr(_cmd, "logs_dir", lambda slug, box: Path("/tmp/logs"))
        with patch(
            f"{_MOD}.load_registry", return_value={"my-slug": _make_record("my-slug")}
        ):
            result = SandboxShellCommand().execute(
                _args(trailing_argv=["./dev.sh", "up"], detach=True)
            )

        assert result is None
        assert len(run_calls) == 1
        assert run_calls[0][:3] == ["docker", "exec", "-d"]
        assert "dev.sh.log" in run_calls[0][-1]

        out = capsys.readouterr().out
        assert "started detached" in out
        assert "/logs/dev.sh.log" in out
        assert str(Path("/tmp/logs") / "dev.sh.log") in out
        assert "tail -f /logs/dev.sh.log" in out

    def test_detach_failure_returns_nonzero_without_printing_log_paths(
        self, monkeypatch, capsys
    ):
        monkeypatch.setattr(
            _cmd.subprocess, "run", lambda *a, **kw: MagicMock(returncode=1)
        )
        monkeypatch.setattr(_cmd, "logs_dir", lambda slug, box: Path("/tmp/logs"))
        with patch(
            f"{_MOD}.load_registry", return_value={"my-slug": _make_record("my-slug")}
        ):
            result = SandboxShellCommand().execute(
                _args(trailing_argv=["./dev.sh", "up"], detach=True)
            )

        assert result == 1
        assert "failed to start detached command" in capsys.readouterr().err

    def test_detach_with_dmz_box_prints_dmz_in_follow_hint(self, monkeypatch, capsys):
        record = SandboxRecord(
            slug="my-slug",
            created="2026-06-26T00:00:00+00:00",
            state=SandboxState.READY,
            redis_index=None,
            tailnet_port=None,
            boxes={"dmz": BoxRecord(zone="dmz", user="devuser")},
        )
        monkeypatch.setattr(
            _cmd.subprocess, "run", lambda *a, **kw: MagicMock(returncode=0)
        )
        monkeypatch.setattr(_cmd, "logs_dir", lambda slug, box: Path("/tmp/logs"))
        with patch(f"{_MOD}.load_registry", return_value={"my-slug": record}):
            SandboxShellCommand().execute(
                _args(
                    box="dmz", trailing_argv=["curl", "-s", "example.com"], detach=True
                )
            )

        assert "stx shell my-slug --box dmz --" in capsys.readouterr().out


class TestBoxNotRunning:
    def test_stopped_container_prints_start_hint_before_execing(
        self, monkeypatch, capsys
    ):
        monkeypatch.setattr(_cmd, "container_status", lambda container: "exited")
        calls = []
        monkeypatch.setattr(_cmd.os, "execvp", lambda *a: calls.append(a))
        with patch(
            f"{_MOD}.load_registry", return_value={"my-slug": _make_record("my-slug")}
        ):
            result = SandboxShellCommand().execute(_args())

        assert result == 1
        err = capsys.readouterr().err
        assert "box isn't running" in err
        assert "stx start my-slug" in err
        assert calls == []

    def test_missing_container_points_at_new_or_refresh(self, monkeypatch, capsys):
        monkeypatch.setattr(_cmd, "container_status", lambda container: None)
        calls = []
        monkeypatch.setattr(_cmd.os, "execvp", lambda *a: calls.append(a))
        with patch(
            f"{_MOD}.load_registry", return_value={"my-slug": _make_record("my-slug")}
        ):
            result = SandboxShellCommand().execute(_args())

        assert result == 1
        err = capsys.readouterr().err
        assert "doesn't exist" in err
        assert "stx new" in err
        assert "stx refresh" in err
        assert calls == []
