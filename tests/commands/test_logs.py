import argparse
import json

import pytest

import stx.commands.logs as _cmd
from stx.commands.logs import (
    SandboxLogsCommand,
    _format_duration,
    _render_event,
    _render_tool_call,
    _task_snippet,
    _truncate,
)
from stx.core import (
    AgentState,
    AgentStatus,
    BoxRecord,
    ManifestEntry,
    ManifestEntryKind,
    SandboxRecord,
    SandboxState,
    append_manifest_entry,
    logs_dir,
    read_manifest,
)


def _make_record(slug: str = "test") -> SandboxRecord:
    return SandboxRecord(
        slug=slug,
        created="2026-06-30T00:00:00+00:00",
        state=SandboxState.READY,
        redis_index=1,
        tailnet_port=5099,
        boxes={
            "app": BoxRecord(
                zone="trusted",
                user="devuser",
                branch="feat/alex/test",
                worktree=f"/tmp/wt/{slug}",
            )
        },
    )


def _args(**kwargs) -> argparse.Namespace:
    defaults = {
        "slug": "test",
        "box": None,
        "follow": False,
        "list": False,
        "raw": False,
        "force": False,
        "yes": False,
        "verbose": False,
        "dry_run": False,
        "debug": False,
    }
    return argparse.Namespace(**{**defaults, **kwargs})


def _write_capture(slug: str, box: str, index: int, envelopes: list[dict]) -> None:
    path = logs_dir(slug, box) / f"{index}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in envelopes) + "\n")


class TestAddParser:
    def _parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        SandboxLogsCommand().add_parser(subparsers)
        return parser

    def test_list_and_raw_together_is_a_parse_error(self):
        with pytest.raises(SystemExit):
            self._parser().parse_args(["logs", "test", "--list", "--raw"])

    def test_follow_and_list_together_is_a_parse_error(self):
        with pytest.raises(SystemExit):
            self._parser().parse_args(["logs", "test", "--follow", "--list"])

    def test_single_mode_flag_parses_fine(self):
        args = self._parser().parse_args(["logs", "test", "--list"])
        assert args.list is True
        assert args.follow is False
        assert args.raw is False

    def test_box_flag_not_positional(self):
        args = self._parser().parse_args(["logs", "test", "--box", "dmz"])
        assert args.slug == "test"
        assert args.box == "dmz"

    def test_box_short_flag(self):
        args = self._parser().parse_args(["logs", "test", "-b", "dmz"])
        assert args.box == "dmz"

    def test_bare_word_after_slug_is_not_a_box_positional(self):
        with pytest.raises(SystemExit):
            self._parser().parse_args(["logs", "test", "dmz"])

    def test_verbose_flag_parses(self):
        args = self._parser().parse_args(["logs", "test", "--verbose"])
        assert args.verbose is True

    def test_verbose_short_flag(self):
        args = self._parser().parse_args(["logs", "test", "-v"])
        assert args.verbose is True

    def test_verbose_defaults_false(self):
        args = self._parser().parse_args(["logs", "test"])
        assert args.verbose is False


class TestExecute:
    def test_unknown_slug_returns_1(self, monkeypatch, capsys):
        monkeypatch.setattr(_cmd, "load_registry", lambda: {})
        result = SandboxLogsCommand().execute(_args(slug="missing"))
        assert result == 1
        assert "missing" in capsys.readouterr().err

    def test_missing_box_returns_1(self, monkeypatch, capsys):
        record = SandboxRecord(
            slug="triage",
            created="2026-06-30T00:00:00+00:00",
            state=SandboxState.READY,
            redis_index=None,
            tailnet_port=None,
            boxes={"dmz": BoxRecord(zone="dmz", user="devuser")},
        )
        monkeypatch.setattr(_cmd, "load_registry", lambda: {"triage": record})
        result = SandboxLogsCommand().execute(_args(slug="triage", box="app"))
        assert result == 1
        assert "no 'app' box" in capsys.readouterr().err

    def test_no_dispatches_recorded_returns_1(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        monkeypatch.setattr(_cmd, "load_registry", lambda: {"test": _make_record()})
        monkeypatch.setattr(_cmd, "read_agent_status", lambda container, user: None)

        result = SandboxLogsCommand().execute(_args())

        assert result == 1
        assert "no dispatches recorded" in capsys.readouterr().err

    def test_default_prints_header_and_footer(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        monkeypatch.setattr(_cmd, "load_registry", lambda: {"test": _make_record()})
        monkeypatch.setattr(_cmd, "read_agent_status", lambda container, user: None)
        append_manifest_entry(
            "test",
            ManifestEntry(
                kind=ManifestEntryKind.START,
                index=1,
                box="app",
                task="do the thing",
                model="sonnet",
                started_at="2026-07-07T00:00:00Z",
            ),
        )
        append_manifest_entry(
            "test",
            ManifestEntry(
                kind=ManifestEntryKind.FINISH,
                index=1,
                box="app",
                exit_code=0,
                finished_at="2026-07-07T00:01:00Z",
                session_id="sess-1",
                total_cost_usd=0.42,
            ),
        )
        _write_capture(
            "test",
            "app",
            1,
            [
                {
                    "t": "2026-07-07T00:00:01Z",
                    "e": {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "on it"}]},
                    },
                }
            ],
        )

        result = SandboxLogsCommand().execute(_args())

        assert result is None
        out = capsys.readouterr().out
        assert "dispatch #1" in out
        assert "do the thing" in out
        assert "✎ on it" in out
        assert "state=done  duration=1m00s  cost=$0.42" in out

    def test_box_output_control_chars_are_stripped(self, monkeypatch, capsys, tmp_path):
        # Box output is attacker-influenced; terminal control sequences in it
        # must not reach the host terminal. Newlines and tabs are kept.
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        monkeypatch.setattr(_cmd, "load_registry", lambda: {"test": _make_record()})
        monkeypatch.setattr(_cmd, "read_agent_status", lambda container, user: None)
        append_manifest_entry(
            "test",
            ManifestEntry(
                kind=ManifestEntryKind.START,
                index=1,
                box="app",
                task="t",
                model="sonnet",
                started_at="2026-07-07T00:00:00Z",
            ),
        )
        _write_capture(
            "test",
            "app",
            1,
            [
                {
                    "t": "2026-07-07T00:00:01Z",
                    "e": {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {"type": "text", "text": "a\x1b[31mred\x1b[0m\x07\tb"}
                            ]
                        },
                    },
                }
            ],
        )

        assert SandboxLogsCommand().execute(_args()) is None
        out = capsys.readouterr().out
        assert "\x1b" not in out
        assert "\x07" not in out
        assert "ared\tb" in out  # tab kept, escape + bell gone

    def test_zero_cost_dispatch_shows_dollar_zero_not_dash(
        self, monkeypatch, capsys, tmp_path
    ):
        # A genuinely free dispatch (total_cost_usd == 0.0) must not render the
        # same as "cost not recorded" (None) — a falsy-zero check would collapse
        # the two.
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        monkeypatch.setattr(_cmd, "load_registry", lambda: {"test": _make_record()})
        monkeypatch.setattr(_cmd, "read_agent_status", lambda container, user: None)
        append_manifest_entry(
            "test",
            ManifestEntry(
                kind=ManifestEntryKind.START,
                index=1,
                box="app",
                started_at="2026-07-07T00:00:00Z",
            ),
        )
        append_manifest_entry(
            "test",
            ManifestEntry(
                kind=ManifestEntryKind.FINISH,
                index=1,
                box="app",
                exit_code=0,
                finished_at="2026-07-07T00:00:05Z",
                total_cost_usd=0.0,
            ),
        )

        SandboxLogsCommand().execute(_args())

        out = capsys.readouterr().out
        assert "cost=$0.00" in out

    def test_raw_emits_envelope_jsonl(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        monkeypatch.setattr(_cmd, "load_registry", lambda: {"test": _make_record()})
        monkeypatch.setattr(_cmd, "read_agent_status", lambda container, user: None)
        append_manifest_entry(
            "test",
            ManifestEntry(kind=ManifestEntryKind.START, index=1, box="app"),
        )
        _write_capture(
            "test", "app", 1, [{"t": "t0", "e": {"type": "system", "subtype": "init"}}]
        )

        result = SandboxLogsCommand().execute(_args(raw=True))

        assert result is None
        out = capsys.readouterr().out
        assert json.loads(out.strip()) == {
            "t": "t0",
            "e": {"type": "system", "subtype": "init"},
        }

    def test_list_shows_dispatches_and_total_cost(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        monkeypatch.setattr(_cmd, "load_registry", lambda: {"test": _make_record()})
        monkeypatch.setattr(_cmd, "read_agent_status", lambda container, user: None)
        append_manifest_entry(
            "test",
            ManifestEntry(
                kind=ManifestEntryKind.START,
                index=1,
                box="app",
                task="first task",
                started_at="2026-07-07T00:00:00Z",
            ),
        )
        append_manifest_entry(
            "test",
            ManifestEntry(
                kind=ManifestEntryKind.FINISH,
                index=1,
                box="app",
                exit_code=0,
                finished_at="2026-07-07T00:00:10Z",
                total_cost_usd=1.5,
            ),
        )

        result = SandboxLogsCommand().execute(_args(list=True))

        assert result is None
        out = capsys.readouterr().out
        assert "first task" in out
        assert "total cost: $1.50" in out

    def test_list_zero_cost_dispatch_shows_dollar_zero_not_dash(
        self, monkeypatch, capsys, tmp_path
    ):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        monkeypatch.setattr(_cmd, "load_registry", lambda: {"test": _make_record()})
        monkeypatch.setattr(_cmd, "read_agent_status", lambda container, user: None)
        append_manifest_entry(
            "test",
            ManifestEntry(
                kind=ManifestEntryKind.START,
                index=1,
                box="app",
                started_at="2026-07-07T00:00:00Z",
            ),
        )
        append_manifest_entry(
            "test",
            ManifestEntry(
                kind=ManifestEntryKind.FINISH,
                index=1,
                box="app",
                exit_code=0,
                finished_at="2026-07-07T00:00:05Z",
                total_cost_usd=0.0,
            ),
        )

        SandboxLogsCommand().execute(_args(list=True))

        out = capsys.readouterr().out
        assert "$0.00" in out
        assert "total cost: $0.00" in out

    def test_list_shows_full_session_id_not_truncated(
        self, monkeypatch, capsys, tmp_path
    ):
        # `claude --resume` needs the exact id — a truncated prefix falls through
        # to the interactive picker's fuzzy search and matches nothing.
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        monkeypatch.setattr(_cmd, "load_registry", lambda: {"test": _make_record()})
        monkeypatch.setattr(_cmd, "read_agent_status", lambda container, user: None)
        full_session_id = "371207fa-1234-5678-9abc-def012345678"
        append_manifest_entry(
            "test",
            ManifestEntry(kind=ManifestEntryKind.START, index=1, box="app"),
        )
        append_manifest_entry(
            "test",
            ManifestEntry(
                kind=ManifestEntryKind.FINISH,
                index=1,
                box="app",
                exit_code=0,
                finished_at="2026-07-07T00:00:10Z",
                session_id=full_session_id,
            ),
        )

        SandboxLogsCommand().execute(_args(list=True))

        out = capsys.readouterr().out
        assert full_session_id in out

    def test_list_lazily_records_finish_for_unwaited_dispatch(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        monkeypatch.setattr(_cmd, "load_registry", lambda: {"test": _make_record()})
        monkeypatch.setattr(
            _cmd,
            "read_agent_status",
            lambda container, user: AgentStatus(
                state=AgentState.DONE,
                pid=1,
                index=1,
                exit_code=0,
                finished_at="2026-07-07T00:00:10Z",
            ),
        )
        append_manifest_entry(
            "test",
            ManifestEntry(kind=ManifestEntryKind.START, index=1, box="app"),
        )

        SandboxLogsCommand().execute(_args(list=True))

        finishes = [
            e for e in read_manifest("test") if e.kind == ManifestEntryKind.FINISH
        ]
        assert len(finishes) == 1


class TestFollowExitsOnCompletion:
    def test_already_finished_dispatch_exits_immediately(
        self, monkeypatch, capsys, tmp_path
    ):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        monkeypatch.setattr(_cmd, "load_registry", lambda: {"test": _make_record()})
        monkeypatch.setattr(_cmd, "read_agent_status", lambda container, user: None)
        append_manifest_entry(
            "test",
            ManifestEntry(
                kind=ManifestEntryKind.START,
                index=1,
                box="app",
                started_at="2026-07-07T00:00:00Z",
            ),
        )
        append_manifest_entry(
            "test",
            ManifestEntry(
                kind=ManifestEntryKind.FINISH,
                index=1,
                box="app",
                exit_code=0,
                finished_at="2026-07-07T00:00:05Z",
            ),
        )
        sleeps = []
        monkeypatch.setattr(_cmd.time, "sleep", lambda s: sleeps.append(s))

        result = SandboxLogsCommand().execute(_args(follow=True))

        assert result is None
        assert sleeps == []  # loop body ran zero times — exited on the first pass
        out = capsys.readouterr().out
        assert "state=done  duration=5s" in out

    def test_dispatch_finishing_mid_follow_exits_after_detecting_it(
        self, monkeypatch, capsys, tmp_path
    ):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        monkeypatch.setattr(_cmd, "load_registry", lambda: {"test": _make_record()})
        append_manifest_entry(
            "test",
            ManifestEntry(
                kind=ManifestEntryKind.START,
                index=1,
                box="app",
                started_at="2026-07-07T00:00:00Z",
            ),
        )
        statuses = iter(
            [
                AgentStatus(state=AgentState.WORKING, pid=1, index=1),
                AgentStatus(state=AgentState.WORKING, pid=1, index=1),
                AgentStatus(
                    state=AgentState.DONE,
                    pid=1,
                    index=1,
                    exit_code=0,
                    finished_at="2026-07-07T00:00:05Z",
                ),
            ]
        )
        monkeypatch.setattr(
            _cmd, "read_agent_status", lambda container, user: next(statuses)
        )
        monkeypatch.setattr(_cmd.time, "sleep", lambda s: None)

        result = SandboxLogsCommand().execute(_args(follow=True))

        assert result is None
        out = capsys.readouterr().out
        assert "state=done  duration=5s" in out


class TestFormatDuration:
    def test_under_a_minute(self):
        assert _format_duration("2026-07-07T00:00:00", "2026-07-07T00:00:45") == "45s"

    def test_over_a_minute(self):
        assert _format_duration("2026-07-07T00:00:00", "2026-07-07T00:01:05") == "1m05s"

    def test_invalid_timestamps_return_dash(self):
        assert _format_duration("not-a-time", "also-not") == "-"


class TestTaskSnippet:
    def test_truncates_long_task(self):
        long_task = "x" * 100
        snippet = _task_snippet(long_task)
        assert len(snippet) == 60
        assert snippet.endswith("…")

    def test_collapses_newlines(self):
        assert _task_snippet("line one\nline two") == "line one line two"


class TestTruncate:
    def test_short_text_unchanged(self):
        assert _truncate("hi", 10) == "hi"

    def test_long_text_truncated_with_ellipsis(self):
        assert _truncate("hello world", 5) == "hell…"


class TestRenderToolCall:
    def test_bash_shows_command(self):
        block = {"name": "Bash", "input": {"command": "ls -la"}}
        assert _render_tool_call(block) == "Bash: ls -la"

    def test_read_shows_file_path(self):
        block = {"name": "Read", "input": {"file_path": "/foo/bar.py"}}
        assert _render_tool_call(block) == "Read: /foo/bar.py"

    def test_task_shows_description(self):
        block = {"name": "Task", "input": {"description": "explore the codebase"}}
        assert _render_tool_call(block) == "Task: explore the codebase"

    def test_unknown_tool_shows_compacted_input(self):
        block = {"name": "Weird", "input": {"x": 1}}
        assert _render_tool_call(block) == 'Weird: {"x": 1}'


class TestRenderEvent:
    def test_assistant_text_becomes_snippet(self):
        envelope = {
            "t": "t0",
            "e": {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "hello there"}]},
            },
        }
        assert _render_event(envelope, verbose=False) == "t0  ✎ hello there"

    def test_assistant_tool_use_renders_call(self):
        envelope = {
            "t": "t0",
            "e": {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {"command": "pwd"},
                        }
                    ]
                },
            },
        }
        assert _render_event(envelope, verbose=False) == "t0  → Bash: pwd"

    def test_successful_tool_result_hidden_by_default(self):
        envelope = {
            "t": "t0",
            "e": {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "is_error": False, "content": "ok"}
                    ]
                },
            },
        }
        assert _render_event(envelope, verbose=False) is None

    def test_successful_tool_result_shown_when_verbose(self):
        envelope = {
            "t": "t0",
            "e": {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "is_error": False, "content": "ok"}
                    ]
                },
            },
        }
        assert _render_event(envelope, verbose=True) == "t0  ✓ ok"

    def test_error_tool_result_always_shown(self):
        envelope = {
            "t": "t0",
            "e": {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "is_error": True,
                            "content": "permission denied",
                        }
                    ]
                },
            },
        }
        assert _render_event(envelope, verbose=False) == "t0  ✗ permission denied"

    def test_unknown_event_type_returns_none(self):
        envelope = {"t": "t0", "e": {"type": "system", "subtype": "init"}}
        assert _render_event(envelope, verbose=False) is None
