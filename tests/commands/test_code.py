import argparse
from pathlib import Path

import pytest

import stx.commands.code as _cmd
from stx.commands.code import (
    _open_attached,
    _open_host_folder,
)
from stx.core import BoxRecord, SandboxRecord, SandboxState
from stx.errors import StxError


def _make_record(slug: str = "test", *, repo_root: str = "/tmp/repo") -> SandboxRecord:
    return SandboxRecord(
        slug=slug,
        created="2026-06-30T00:00:00+00:00",
        state=SandboxState.READY,
        redis_index=1,
        tailnet_port=5099,
        repo_root=repo_root,
        boxes={
            "app": BoxRecord(
                zone="trusted",
                user="devuser",
                branch="feat/alex/test",
                worktree=f"/tmp/wt/{slug}",
            )
        },
    )


def _allow_worktree_metadata(
    monkeypatch, *, common_dir: Path | None = Path("/tmp/common")
):
    monkeypatch.setattr(_cmd, "common_dir_for", lambda root: common_dir)
    monkeypatch.setattr(_cmd, "verify_worktree_metadata", lambda *a, **kw: None)


def _args(**kwargs) -> argparse.Namespace:
    defaults = {
        "slug": "test",
        "box": None,
        "attach": False,
        "ide": "auto",
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
        _cmd.SandboxCodeCommand().add_parser(subparsers)
        return parser

    def test_box_flag_not_positional(self):
        args = self._parser().parse_args(["code", "demo", "--box", "dmz"])
        assert args.slug == "demo"
        assert args.box == "dmz"

    def test_box_short_flag(self):
        args = self._parser().parse_args(["code", "demo", "-b", "dmz"])
        assert args.box == "dmz"

    def test_bare_word_after_slug_is_not_a_box_positional(self):
        with pytest.raises(SystemExit):
            self._parser().parse_args(["code", "demo", "dmz"])


class TestExecute:
    def test_unknown_slug_returns_1(self, monkeypatch, capsys):
        monkeypatch.setattr(_cmd, "load_registry", lambda: {})
        result = _cmd.SandboxCodeCommand().execute(_args(slug="missing"))
        assert result == 1
        assert "missing" in capsys.readouterr().err

    def test_attach_routes_to_open_attached(self, monkeypatch):
        monkeypatch.setattr(_cmd, "load_registry", lambda: {"test": _make_record()})
        monkeypatch.setattr(_cmd, "container_status", lambda container: "running")
        calls = []
        monkeypatch.setattr(
            _cmd,
            "_open_attached",
            lambda slug, *, box, ide: calls.append((slug, box, ide)) or None,
        )
        _cmd.SandboxCodeCommand().execute(_args(attach=True, ide="cursor"))
        assert calls == [("test", "app", "cursor")]

    def test_attach_with_stopped_container_prints_start_hint(self, monkeypatch, capsys):
        monkeypatch.setattr(_cmd, "load_registry", lambda: {"test": _make_record()})
        monkeypatch.setattr(_cmd, "container_status", lambda container: "exited")
        calls = []
        monkeypatch.setattr(
            _cmd, "_open_attached", lambda *a, **kw: calls.append(a) or None
        )

        result = _cmd.SandboxCodeCommand().execute(_args(attach=True))

        assert result == 1
        err = capsys.readouterr().err
        assert "box isn't running" in err
        assert "stx start test" in err
        assert calls == []

    def test_attach_with_missing_container_points_at_new_or_refresh(
        self, monkeypatch, capsys
    ):
        monkeypatch.setattr(_cmd, "load_registry", lambda: {"test": _make_record()})
        monkeypatch.setattr(_cmd, "container_status", lambda container: None)
        calls = []
        monkeypatch.setattr(
            _cmd, "_open_attached", lambda *a, **kw: calls.append(a) or None
        )

        result = _cmd.SandboxCodeCommand().execute(_args(attach=True))

        assert result == 1
        err = capsys.readouterr().err
        assert "doesn't exist" in err
        assert "stx new" in err
        assert "stx refresh" in err
        assert calls == []

    def test_non_attach_never_checks_container_status(self, monkeypatch):
        monkeypatch.setattr(_cmd, "load_registry", lambda: {"test": _make_record()})
        _allow_worktree_metadata(monkeypatch)
        monkeypatch.setattr(
            _cmd,
            "container_status",
            lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should not run")),
        )
        monkeypatch.setattr(_cmd, "_open_host_folder", lambda *a, **kw: None)

        result = _cmd.SandboxCodeCommand().execute(_args())

        assert result is None

    def test_non_attach_warns_the_folder_is_box_written(self, monkeypatch, capsys):
        monkeypatch.setattr(_cmd, "load_registry", lambda: {"test": _make_record()})
        _allow_worktree_metadata(monkeypatch)
        monkeypatch.setattr(_cmd, "_open_host_folder", lambda *a, **kw: None)

        _cmd.SandboxCodeCommand().execute(_args())

        err = capsys.readouterr().err
        assert "box" in err.lower()
        assert "trust" in err.lower()

    def test_host_folder_routes_with_ide(self, monkeypatch):
        monkeypatch.setattr(_cmd, "load_registry", lambda: {"test": _make_record()})
        _allow_worktree_metadata(monkeypatch)
        calls = []

        def _mock(worktree, *, ide="auto"):
            calls.append((worktree, ide))
            return None

        monkeypatch.setattr(_cmd, "_open_host_folder", _mock)
        _cmd.SandboxCodeCommand().execute(_args(ide="cursor"))
        assert calls == [("/tmp/wt/test", "cursor")]

    def test_refuses_when_worktree_metadata_was_altered(self, monkeypatch, capsys):
        monkeypatch.setattr(_cmd, "load_registry", lambda: {"test": _make_record()})
        monkeypatch.setattr(_cmd, "common_dir_for", lambda root: Path("/tmp/common"))

        def _raise(*a, **kw):
            raise StxError("sandbox 'test's worktree gitfile was altered")

        monkeypatch.setattr(_cmd, "verify_worktree_metadata", _raise)
        opened = []
        monkeypatch.setattr(
            _cmd, "_open_host_folder", lambda *a, **kw: opened.append(a) or None
        )

        result = _cmd.SandboxCodeCommand().execute(_args())

        assert result == 1
        assert "altered" in capsys.readouterr().err
        assert not opened

    def test_refuses_when_no_repo_checkout_to_verify_against(self, monkeypatch, capsys):
        monkeypatch.setattr(
            _cmd, "load_registry", lambda: {"test": _make_record(repo_root="")}
        )
        opened = []
        monkeypatch.setattr(
            _cmd, "_open_host_folder", lambda *a, **kw: opened.append(a) or None
        )

        result = _cmd.SandboxCodeCommand().execute(_args())

        assert result == 1
        assert "could not verify" in capsys.readouterr().err
        assert not opened

    def test_dmz_box_never_checks_worktree_metadata(self, monkeypatch):
        record = _make_record()
        record.boxes["dmz"] = BoxRecord(zone="dmz", user="devuser")
        monkeypatch.setattr(_cmd, "load_registry", lambda: {"test": record})
        monkeypatch.setattr(
            _cmd,
            "verify_worktree_metadata",
            lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should not run")),
        )
        calls = []
        monkeypatch.setattr(
            _cmd, "_open_host_folder", lambda *a, **kw: calls.append(a) or None
        )

        result = _cmd.SandboxCodeCommand().execute(_args(box="dmz"))

        assert result is None
        assert calls


class TestOpenHostFolder:
    def test_auto_tries_cursor_first(self, monkeypatch):
        launched = []
        monkeypatch.setattr(
            _cmd, "_launch_editor", lambda b, a, w: (launched.append(b), True)[1]
        )
        assert _open_host_folder("/wt", ide="auto") is None
        assert launched[0] == "cursor"

    def test_auto_falls_back_to_vscode(self, monkeypatch):
        launched = []

        def _mock(binary, app_name, worktree):
            launched.append(binary)
            return binary == "code"

        monkeypatch.setattr(_cmd, "_launch_editor", _mock)
        assert _open_host_folder("/wt", ide="auto") is None
        assert launched == ["cursor", "code"]

    def test_explicit_ide_not_found_returns_1(self, monkeypatch, capsys):
        monkeypatch.setattr(_cmd, "_launch_editor", lambda b, a, w: False)
        result = _open_host_folder("/wt", ide="cursor")
        assert result == 1
        assert "cursor" in capsys.readouterr().err

    def test_auto_nothing_found_returns_1(self, monkeypatch, capsys):
        monkeypatch.setattr(_cmd, "_launch_editor", lambda b, a, w: False)
        result = _open_host_folder("/wt", ide="auto")
        assert result == 1
        assert "Cursor" in capsys.readouterr().err


class TestOpenAttached:
    def test_auto_tries_cursor_first(self, monkeypatch):
        tried = []
        monkeypatch.setattr(
            _cmd, "_cursor_binary", lambda: tried.append("cursor") or "/bin/cursor"
        )
        monkeypatch.setattr(
            _cmd, "_vscode_binary", lambda: tried.append("vscode") or "/bin/code"
        )
        monkeypatch.setattr(_cmd.subprocess, "run", lambda cmd, **kw: None)
        _open_attached("test", box="app")
        assert tried == ["cursor"]

    def test_auto_falls_back_to_vscode_when_cursor_missing(self, monkeypatch):
        monkeypatch.setattr(_cmd, "_cursor_binary", lambda: None)
        monkeypatch.setattr(_cmd, "_vscode_binary", lambda: "/bin/code")
        calls = []
        monkeypatch.setattr(_cmd.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
        result = _open_attached("test", box="app")
        assert result is None
        assert calls[0][0] == "/bin/code"

    def test_explicit_ide_only_tries_that_one(self, monkeypatch):
        monkeypatch.setattr(
            _cmd, "_cursor_binary", lambda: (_ for _ in ()).throw(AssertionError)
        )
        monkeypatch.setattr(_cmd, "_vscode_binary", lambda: "/bin/code")
        calls = []
        monkeypatch.setattr(_cmd.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
        result = _open_attached("test", box="app", ide="vscode")
        assert result is None
        assert calls[0][0] == "/bin/code"

    def test_neither_found_returns_1(self, monkeypatch, capsys):
        monkeypatch.setattr(_cmd, "_cursor_binary", lambda: None)
        monkeypatch.setattr(_cmd, "_vscode_binary", lambda: None)
        result = _open_attached("test", box="app")
        assert result == 1
        assert "Cursor or VS Code" in capsys.readouterr().err

    def test_explicit_ide_not_found_names_it(self, monkeypatch, capsys):
        monkeypatch.setattr(_cmd, "_vscode_binary", lambda: None)
        result = _open_attached("test", box="app", ide="vscode")
        assert result == 1
        assert "Visual Studio Code" in capsys.readouterr().err

    def test_hex_encodes_container_name(self, monkeypatch):
        monkeypatch.setattr(_cmd, "_cursor_binary", lambda: "/usr/bin/cursor")
        calls = []
        monkeypatch.setattr(_cmd.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
        _open_attached("my-sandbox", box="app")
        assert len(calls) == 1
        uri = calls[0][2]
        expected_hex = "stx-my-sandbox--app".encode().hex()
        assert f"attached-container+{expected_hex}" in uri
