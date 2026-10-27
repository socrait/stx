import argparse

import pytest

import stx.commands.await_ as _cmd
from stx.commands.await_ import SandboxAwaitCommand
from stx.core import BoxRecord, SandboxRecord, SandboxState
from stx.errors import StxError


def _make_record(slug: str = "test") -> SandboxRecord:
    return SandboxRecord(
        slug=slug,
        created="2026-06-30T00:00:00+00:00",
        state=SandboxState.READY,
        redis_index=1,
        tailnet_port=5099,
        services=["postgres", "redis"],
        repo="widgetshop",
        repo_root="/tmp/widgetshop",
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
        "timeout": None,
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
        SandboxAwaitCommand().add_parser(subparsers)
        return parser

    def test_box_flag_not_positional(self):
        args = self._parser().parse_args(["await", "demo", "--box", "dmz"])
        assert args.slug == "demo"
        assert args.box == "dmz"

    def test_box_short_flag(self):
        args = self._parser().parse_args(["await", "demo", "-b", "dmz"])
        assert args.box == "dmz"

    def test_bare_word_after_slug_is_not_a_box_positional(self):
        with pytest.raises(SystemExit):
            self._parser().parse_args(["await", "demo", "dmz"])


class TestExecute:
    def test_unknown_slug_returns_1(self, monkeypatch, capsys):
        monkeypatch.setattr(_cmd, "load_registry", lambda: {})
        result = SandboxAwaitCommand().execute(_args(slug="missing"))
        assert result == 1
        assert "missing" in capsys.readouterr().err

    def test_no_agent_dispatched_returns_1(self, monkeypatch, capsys):
        monkeypatch.setattr(_cmd, "load_registry", lambda: {"test": _make_record()})
        monkeypatch.setattr(
            _cmd,
            "wait_for_agent",
            lambda slug, box, container, user=None, timeout=None: None,
        )
        result = SandboxAwaitCommand().execute(_args())
        assert result == 1
        assert "no agent has been dispatched" in capsys.readouterr().err

    def test_returns_0_on_zero_exit_code(self, monkeypatch):
        monkeypatch.setattr(_cmd, "load_registry", lambda: {"test": _make_record()})
        monkeypatch.setattr(
            _cmd,
            "wait_for_agent",
            lambda slug, box, container, user=None, timeout=None: 0,
        )

        result = SandboxAwaitCommand().execute(_args())

        assert result == 0

    def test_nonzero_exit_code_returns_1(self, monkeypatch):
        monkeypatch.setattr(_cmd, "load_registry", lambda: {"test": _make_record()})
        monkeypatch.setattr(
            _cmd,
            "wait_for_agent",
            lambda slug, box, container, user=None, timeout=None: 1,
        )

        result = SandboxAwaitCommand().execute(_args())

        assert result == 1

    def test_passes_resolved_slug_box_and_container_to_wait_for_agent(
        self, monkeypatch
    ):
        monkeypatch.setattr(_cmd, "load_registry", lambda: {"test": _make_record()})
        seen = []
        monkeypatch.setattr(
            _cmd,
            "wait_for_agent",
            lambda slug, box, container, user=None, timeout=None: seen.append(
                (slug, box, container)
            )
            or 0,
        )

        SandboxAwaitCommand().execute(_args())

        assert seen == [("test", "app", "stx-test--app")]

    def test_passes_timeout_through_to_wait_for_agent(self, monkeypatch):
        monkeypatch.setattr(_cmd, "load_registry", lambda: {"test": _make_record()})
        seen = []
        monkeypatch.setattr(
            _cmd,
            "wait_for_agent",
            lambda slug, box, container, user=None, timeout=None: seen.append(timeout)
            or 0,
        )

        SandboxAwaitCommand().execute(_args(timeout=30.0))

        assert seen == [30.0]

    def test_timeout_error_from_wait_for_agent_returns_1(self, monkeypatch, capsys):
        monkeypatch.setattr(_cmd, "load_registry", lambda: {"test": _make_record()})

        def _raise(slug, box, container, user=None, timeout=None):
            raise StxError(f"timed out after {timeout:g}s waiting for the agent")

        monkeypatch.setattr(_cmd, "wait_for_agent", _raise)

        result = SandboxAwaitCommand().execute(_args(timeout=5.0))

        assert result == 1
        assert "timed out" in capsys.readouterr().err
