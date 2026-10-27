import argparse
from types import SimpleNamespace

import pytest

import stx.commands.agent as _cmd
from stx.commands.agent import SandboxAgentCommand
from stx.core import (
    AgentState,
    AgentStatus,
    BoxRecord,
    ManifestEntryKind,
    SandboxRecord,
    SandboxState,
    SetupState,
    SetupStatus,
    read_manifest,
)


@pytest.fixture(autouse=True)
def _box_is_running(monkeypatch):
    """Every dispatch test below assumes the box is already up — the
    not-running/missing-container cases get their own tests in
    TestBoxNotRunning."""
    monkeypatch.setattr(_cmd, "container_status", lambda container: "running")


@pytest.fixture(autouse=True)
def _setup_already_finished(monkeypatch):
    """Every dispatch test below exercises what happens after setup — the
    refusal itself gets its own tests in TestSetupNotFinished."""
    monkeypatch.setattr(
        _cmd, "read_setup_status", lambda container: SetupStatus(state=SetupState.DONE)
    )


def _make_record(slug: str = "test", *, with_app: bool = True) -> SandboxRecord:
    boxes = {}
    if with_app:
        boxes["app"] = BoxRecord(
            zone="trusted",
            user="devuser",
            branch="feat/alex/test",
            worktree=f"/tmp/wt/{slug}",
        )
    else:
        boxes["dmz"] = BoxRecord(zone="dmz", user="devuser")
    return SandboxRecord(
        slug=slug,
        created="2026-06-30T00:00:00+00:00",
        state=SandboxState.READY,
        redis_index=1 if with_app else None,
        tailnet_port=5099 if with_app else None,
        services=["postgres", "redis"] if with_app else [],
        repo="widgetshop",
        repo_root="/tmp/widgetshop",
        boxes=boxes,
    )


def _args(**kwargs) -> argparse.Namespace:
    defaults = {
        "slug": "test",
        "box": None,
        "task": "what files are in src/?",
        "task_file": None,
        "model": "sonnet",
        "wait": False,
        "force": False,
        "yes": False,
        "verbose": False,
        "dry_run": False,
        "debug": False,
    }
    return argparse.Namespace(**{**defaults, **kwargs})


class TestAddParser:
    def _parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        SandboxAgentCommand().add_parser(subparsers)
        return parser

    def test_task_positional_is_not_mistaken_for_box(self):
        # The bug this guards against: `stx agent demo "fix the bug"` used to
        # bind "fix the bug" to the `box` positional, not `task`.
        args = self._parser().parse_args(["agent", "demo", "fix the bug"])
        assert args.slug == "demo"
        assert args.task == "fix the bug"
        assert args.box is None

    def test_box_flag_sets_box_without_consuming_a_positional(self):
        args = self._parser().parse_args(
            ["agent", "demo", "fix the bug", "--box", "dmz"]
        )
        assert args.slug == "demo"
        assert args.task == "fix the bug"
        assert args.box == "dmz"

    def test_box_short_flag(self):
        args = self._parser().parse_args(["agent", "demo", "-b", "dmz"])
        assert args.box == "dmz"


class TestExecute:
    def test_unknown_slug_returns_1(self, monkeypatch, capsys):
        monkeypatch.setattr(_cmd, "load_registry", lambda: {})
        result = SandboxAgentCommand().execute(_args(slug="missing"))
        assert result == 1
        assert "missing" in capsys.readouterr().err

    def test_missing_box_returns_1(self, monkeypatch, capsys):
        record = _make_record(slug="triage", with_app=False)
        monkeypatch.setattr(_cmd, "load_registry", lambda: {"triage": record})
        result = SandboxAgentCommand().execute(_args(slug="triage", box="app"))
        assert result == 1
        assert "no 'app' box" in capsys.readouterr().err

    def test_refuses_when_agent_already_running(self, monkeypatch, capsys):
        monkeypatch.setattr(_cmd, "load_registry", lambda: {"test": _make_record()})
        monkeypatch.setattr(
            _cmd,
            "read_agent_status",
            lambda container, user: AgentStatus(state=AgentState.WORKING, pid=42),
        )
        monkeypatch.setattr(
            _cmd, "agent_pid_alive", lambda container, pid, user=None: True
        )
        dispatched = []
        monkeypatch.setattr(
            _cmd.subprocess,
            "run",
            lambda cmd, **kw: dispatched.append(cmd) or SimpleNamespace(returncode=0),
        )
        result = SandboxAgentCommand().execute(_args())
        assert result == 1
        assert "already running" in capsys.readouterr().err
        assert dispatched == []

    def test_dispatches_when_no_prior_agent(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        monkeypatch.setattr(_cmd, "load_registry", lambda: {"test": _make_record()})
        monkeypatch.setattr(_cmd, "read_agent_status", lambda container, user: None)
        dispatched = []
        monkeypatch.setattr(
            _cmd.subprocess,
            "run",
            lambda cmd, **kw: dispatched.append(cmd) or SimpleNamespace(returncode=0),
        )
        result = SandboxAgentCommand().execute(_args(task="hello"))
        assert result is None
        assert dispatched == [
            [
                "docker",
                "exec",
                "-d",
                "-u",
                "devuser",
                "stx-test--app",
                "/bin/bash",
                "/opt/stx/box/agent-wrapper.sh",
                "hello",
                "sonnet",
                "1",
            ]
        ]

    def test_dispatch_appends_manifest_start_entry(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        monkeypatch.setattr(_cmd, "load_registry", lambda: {"test": _make_record()})
        monkeypatch.setattr(_cmd, "read_agent_status", lambda container, user: None)
        monkeypatch.setattr(
            _cmd.subprocess, "run", lambda cmd, **kw: SimpleNamespace(returncode=0)
        )

        SandboxAgentCommand().execute(_args(task="hello", model="opus"))

        entries = read_manifest("test")
        assert len(entries) == 1
        assert entries[0].kind == ManifestEntryKind.START
        assert entries[0].index == 1
        assert entries[0].box == "app"
        assert entries[0].task == "hello"
        assert entries[0].model == "opus"

    def test_index_increments_across_dispatches(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        monkeypatch.setattr(_cmd, "load_registry", lambda: {"test": _make_record()})
        monkeypatch.setattr(_cmd, "read_agent_status", lambda container, user: None)
        dispatched = []
        monkeypatch.setattr(
            _cmd.subprocess,
            "run",
            lambda cmd, **kw: dispatched.append(cmd) or SimpleNamespace(returncode=0),
        )

        SandboxAgentCommand().execute(_args(task="first"))
        SandboxAgentCommand().execute(_args(task="second"))

        assert dispatched[0][-1] == "1"
        assert dispatched[1][-1] == "2"

    def test_model_flag_overrides_default(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        monkeypatch.setattr(_cmd, "load_registry", lambda: {"test": _make_record()})
        monkeypatch.setattr(_cmd, "read_agent_status", lambda container, user: None)
        dispatched = []
        monkeypatch.setattr(
            _cmd.subprocess,
            "run",
            lambda cmd, **kw: dispatched.append(cmd) or SimpleNamespace(returncode=0),
        )
        result = SandboxAgentCommand().execute(_args(model="opus"))
        assert result is None
        assert dispatched[0][-2] == "opus"

    def test_wait_flag_blocks_and_returns_agent_exit_code(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        monkeypatch.setattr(_cmd, "load_registry", lambda: {"test": _make_record()})
        monkeypatch.setattr(_cmd, "read_agent_status", lambda container, user: None)
        monkeypatch.setattr(
            _cmd.subprocess, "run", lambda cmd, **kw: SimpleNamespace(returncode=0)
        )
        monkeypatch.setattr(
            _cmd, "wait_for_agent", lambda slug, box, container, user: 0
        )

        result = SandboxAgentCommand().execute(_args(wait=True))

        assert result == 0

    def test_wait_flag_returns_1_on_nonzero_exit_code(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        monkeypatch.setattr(_cmd, "load_registry", lambda: {"test": _make_record()})
        monkeypatch.setattr(_cmd, "read_agent_status", lambda container, user: None)
        monkeypatch.setattr(
            _cmd.subprocess, "run", lambda cmd, **kw: SimpleNamespace(returncode=0)
        )
        monkeypatch.setattr(
            _cmd, "wait_for_agent", lambda slug, box, container, user: 1
        )

        result = SandboxAgentCommand().execute(_args(wait=True))

        assert result == 1

    def test_dispatches_when_prior_agent_finished(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        monkeypatch.setattr(_cmd, "load_registry", lambda: {"test": _make_record()})
        monkeypatch.setattr(
            _cmd,
            "read_agent_status",
            lambda container, user: AgentStatus(
                state=AgentState.DONE, pid=42, exit_code=0
            ),
        )
        monkeypatch.setattr(
            _cmd, "agent_pid_alive", lambda container, pid, user=None: False
        )
        dispatched = []
        monkeypatch.setattr(
            _cmd.subprocess,
            "run",
            lambda cmd, **kw: dispatched.append(cmd) or SimpleNamespace(returncode=0),
        )
        result = SandboxAgentCommand().execute(_args())
        assert result is None
        assert len(dispatched) == 1

    def test_returns_1_when_docker_exec_fails(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        monkeypatch.setattr(_cmd, "load_registry", lambda: {"test": _make_record()})
        monkeypatch.setattr(_cmd, "read_agent_status", lambda container, user: None)
        monkeypatch.setattr(
            _cmd.subprocess, "run", lambda cmd, **kw: SimpleNamespace(returncode=1)
        )
        result = SandboxAgentCommand().execute(_args())
        assert result == 1
        assert "failed to dispatch" in capsys.readouterr().err


class TestTaskFile:
    def test_dispatches_with_the_file_contents_as_the_task(
        self, monkeypatch, capsys, tmp_path
    ):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        monkeypatch.setattr(_cmd, "load_registry", lambda: {"test": _make_record()})
        monkeypatch.setattr(_cmd, "read_agent_status", lambda container, user: None)
        dispatched = []
        monkeypatch.setattr(
            _cmd.subprocess,
            "run",
            lambda cmd, **kw: dispatched.append(cmd) or SimpleNamespace(returncode=0),
        )
        task_file = tmp_path / "task.md"
        task_file.write_text("do the thing\n")

        result = SandboxAgentCommand().execute(
            _args(task=None, task_file=str(task_file))
        )

        assert result is None
        assert dispatched[0][-2] == "sonnet"
        assert dispatched[0][-3] == "do the thing\n"

    def test_refuses_both_task_and_task_file(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        task_file = tmp_path / "task.md"
        task_file.write_text("do the thing\n")

        result = SandboxAgentCommand().execute(
            _args(task="inline task", task_file=str(task_file))
        )

        assert result == 1
        assert "mutually exclusive" in capsys.readouterr().err

    def test_refuses_neither_task_nor_task_file(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))

        result = SandboxAgentCommand().execute(_args(task=None, task_file=None))

        assert result == 1
        assert "task is required" in capsys.readouterr().err

    def test_refuses_a_missing_task_file(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        missing = tmp_path / "does-not-exist.md"

        result = SandboxAgentCommand().execute(_args(task=None, task_file=str(missing)))

        assert result == 1
        assert "cannot read task file" in capsys.readouterr().err

    def test_refuses_an_empty_task_file(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        task_file = tmp_path / "empty.md"
        task_file.write_text("   \n")

        result = SandboxAgentCommand().execute(
            _args(task=None, task_file=str(task_file))
        )

        assert result == 1
        assert "is empty" in capsys.readouterr().err


class TestSetupNotFinished:
    def test_refuses_dispatch_while_setup_is_still_running(self, monkeypatch, capsys):
        monkeypatch.setattr(_cmd, "load_registry", lambda: {"test": _make_record()})
        monkeypatch.setattr(_cmd, "read_setup_status", lambda container: None)
        dispatched = []
        monkeypatch.setattr(
            _cmd.subprocess,
            "run",
            lambda cmd, **kw: dispatched.append(cmd) or SimpleNamespace(returncode=0),
        )

        result = SandboxAgentCommand().execute(_args())

        assert result == 1
        assert "setup hasn't finished" in capsys.readouterr().err
        assert dispatched == []

    def test_dispatches_once_setup_failed_rather_than_still_running(
        self, monkeypatch, tmp_path
    ):
        # A `failed` record means setup reached a known (if bad) outcome —
        # that's "finished", just unsuccessfully, so dispatch proceeds and
        # lets the human debug inside the box.
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        monkeypatch.setattr(_cmd, "load_registry", lambda: {"test": _make_record()})
        monkeypatch.setattr(
            _cmd,
            "read_setup_status",
            lambda container: SetupStatus(
                state=SetupState.FAILED, script="setup.sh", exit_code=1
            ),
        )
        monkeypatch.setattr(_cmd, "read_agent_status", lambda container, user: None)
        dispatched = []
        monkeypatch.setattr(
            _cmd.subprocess,
            "run",
            lambda cmd, **kw: dispatched.append(cmd) or SimpleNamespace(returncode=0),
        )

        result = SandboxAgentCommand().execute(_args())

        assert result is None
        assert len(dispatched) == 1


class TestBoxNotRunning:
    def test_stopped_container_prints_start_hint_before_dispatching(
        self, monkeypatch, capsys
    ):
        monkeypatch.setattr(_cmd, "load_registry", lambda: {"test": _make_record()})
        monkeypatch.setattr(_cmd, "container_status", lambda container: "exited")
        dispatched = []
        monkeypatch.setattr(
            _cmd.subprocess,
            "run",
            lambda cmd, **kw: dispatched.append(cmd) or SimpleNamespace(returncode=0),
        )

        result = SandboxAgentCommand().execute(_args())

        assert result == 1
        err = capsys.readouterr().err
        assert "box isn't running" in err
        assert "stx start test" in err
        assert dispatched == []

    def test_missing_container_points_at_new_or_refresh(self, monkeypatch, capsys):
        monkeypatch.setattr(_cmd, "load_registry", lambda: {"test": _make_record()})
        monkeypatch.setattr(_cmd, "container_status", lambda container: None)
        dispatched = []
        monkeypatch.setattr(
            _cmd.subprocess,
            "run",
            lambda cmd, **kw: dispatched.append(cmd) or SimpleNamespace(returncode=0),
        )

        result = SandboxAgentCommand().execute(_args())

        assert result == 1
        err = capsys.readouterr().err
        assert "doesn't exist" in err
        assert "stx new" in err
        assert "stx refresh" in err
        assert dispatched == []
