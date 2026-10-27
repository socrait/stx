import itertools
import json
import time
from unittest.mock import MagicMock

import pytest

from stx.core import SETUP_STATUS_PATH, SetupState, SetupStatus, read_setup_status
from stx.core.box_env import SYSTEM_PATH
from stx.core.setup_status import wait_for_setup
from stx.errors import StxError


class TestReadSetupStatus:
    def test_parses_done_status(self):
        raw = json.dumps({"state": "done"})
        status = read_setup_status(
            "stx-test--app",
            runner=lambda cmd, **kw: MagicMock(returncode=0, stdout=raw),
        )
        assert status == SetupStatus(state=SetupState.DONE)

    def test_parses_failed_status(self):
        raw = json.dumps({"state": "failed", "script": "setup.sh", "exit_code": 7})
        status = read_setup_status(
            "stx-test--app",
            runner=lambda cmd, **kw: MagicMock(returncode=0, stdout=raw),
        )
        assert status == SetupStatus(
            state=SetupState.FAILED, script="setup.sh", exit_code=7
        )

    def test_returns_none_when_file_absent(self):
        status = read_setup_status(
            "stx-test--app", runner=lambda cmd, **kw: MagicMock(returncode=1, stdout="")
        )
        assert status is None

    def test_returns_none_on_corrupt_json(self):
        status = read_setup_status(
            "stx-test--app",
            runner=lambda cmd, **kw: MagicMock(returncode=0, stdout="not json"),
        )
        assert status is None

    def test_reads_root_owned_record_with_a_pinned_path(self):
        calls = []

        def _run(cmd, **kw):
            calls.append(cmd)
            return MagicMock(returncode=0, stdout=json.dumps({"state": "done"}))

        read_setup_status("stx-test--app", runner=_run)

        assert calls == [
            [
                "docker",
                "exec",
                "--env",
                f"PATH={SYSTEM_PATH}",
                "stx-test--app",
                "cat",
                SETUP_STATUS_PATH,
            ]
        ]


def _setup_runner(exec_results, *, running=True, logs=""):
    """A fake subprocess runner that dispatches on the docker verb: `exec`
    (the setup-status reads, one per `exec_results` entry, repeating the
    last once exhausted), `inspect` (container liveness), and `logs` (the
    dead-container diagnostic)."""
    results = list(exec_results)

    def _run(cmd, **kw):
        verb = cmd[1]
        if verb == "exec":
            result = results.pop(0) if len(results) > 1 else results[0]
            return result
        if verb == "inspect":
            return MagicMock(
                returncode=0, stdout=("running" if running else "exited") + "\n"
            )
        if verb == "logs":
            return MagicMock(returncode=0, stdout=logs, stderr="")
        raise AssertionError(f"unexpected docker verb: {verb}")

    return _run


class TestWaitForSetup:
    def test_returns_as_soon_as_a_record_appears(self, monkeypatch):
        monkeypatch.setattr(time, "sleep", lambda seconds: None)
        runner = _setup_runner(
            [
                MagicMock(returncode=1, stdout=""),
                MagicMock(returncode=0, stdout=json.dumps({"state": "done"})),
            ]
        )

        status = wait_for_setup("stx-test--app", "app", runner=runner)

        assert status == SetupStatus(state=SetupState.DONE)

    def test_returns_failed_record_without_raising(self, monkeypatch):
        monkeypatch.setattr(time, "sleep", lambda seconds: None)
        raw = json.dumps({"state": "failed", "script": "setup.sh", "exit_code": 7})
        runner = _setup_runner([MagicMock(returncode=0, stdout=raw)])

        status = wait_for_setup("stx-test--app", "app", runner=runner)

        assert status == SetupStatus(
            state=SetupState.FAILED, script="setup.sh", exit_code=7
        )

    def test_raises_after_timeout(self, monkeypatch):
        monkeypatch.setattr(time, "sleep", lambda seconds: None)
        clock = itertools.count(0, 10)
        monkeypatch.setattr(time, "monotonic", lambda: next(clock))
        runner = _setup_runner([MagicMock(returncode=1, stdout="")])

        with pytest.raises(StxError, match="timed out"):
            wait_for_setup("stx-test--app", "app", timeout=1.0, runner=runner)

    def test_prints_a_heartbeat_while_waiting(self, monkeypatch, capsys):
        monkeypatch.setattr(time, "sleep", lambda seconds: None)
        clock = itertools.count(0, 10)
        monkeypatch.setattr(time, "monotonic", lambda: next(clock))
        runner = _setup_runner([MagicMock(returncode=1, stdout="")])

        with pytest.raises(StxError):
            wait_for_setup("stx-test--app", "app", timeout=65.0, runner=runner)

        assert "still waiting for setup in 'app'" in capsys.readouterr().err

    def test_raises_fast_when_container_already_stopped(self, monkeypatch):
        """A dead box must not wait out the full timeout before being
        reported — the entrypoint can exit long before setup would."""
        slept = []
        monkeypatch.setattr(time, "sleep", lambda seconds: slept.append(seconds))
        runner = _setup_runner(
            [MagicMock(returncode=1, stdout="")],
            running=False,
            logs="tinyproxy: bind failed\n",
        )

        with pytest.raises(StxError, match="exited before setup finished"):
            wait_for_setup("stx-test--app", "app", timeout=900.0, runner=runner)

        assert not slept

    def test_dead_container_error_includes_last_log_lines(self, monkeypatch):
        monkeypatch.setattr(time, "sleep", lambda seconds: None)
        runner = _setup_runner(
            [MagicMock(returncode=1, stdout="")],
            running=False,
            logs="firewall setup failed: iptables: command not found\n",
        )

        with pytest.raises(StxError, match="iptables: command not found"):
            wait_for_setup("stx-test--app", "app", runner=runner)
