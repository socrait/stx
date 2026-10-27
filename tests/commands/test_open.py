import argparse
from types import SimpleNamespace

import stx.commands.open as _cmd
from stx.commands.open import (
    SandboxOpenCommand,
    _open_url,
)
from stx.core import BoxRecord, SandboxRecord, SandboxState


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
        repo="widgetshop",
        repo_root="/tmp/widgetshop",
        boxes=boxes,
    )


def _manifest_with_routes(routes: dict) -> SimpleNamespace:
    return SimpleNamespace(routes=routes)


def _args(**kwargs) -> argparse.Namespace:
    defaults = {
        "slug": "test",
        "route": None,
        "force": False,
        "yes": False,
        "verbose": False,
        "dry_run": False,
        "debug": False,
    }
    return argparse.Namespace(**{**defaults, **kwargs})


def _ok_run(cmd, **kw):
    return SimpleNamespace(returncode=0)


def _fail_run(cmd, **kw):
    return SimpleNamespace(returncode=1)


class TestExecute:
    def test_unknown_slug_returns_1(self, monkeypatch, capsys):
        monkeypatch.setattr(_cmd, "load_registry", lambda: {})
        result = SandboxOpenCommand().execute(_args(slug="missing"))
        assert result == 1
        assert "missing" in capsys.readouterr().err

    def test_dmz_only_returns_1(self, monkeypatch, capsys):
        record = _make_record(slug="triage", with_app=False)
        monkeypatch.setattr(_cmd, "load_registry", lambda: {"triage": record})
        result = SandboxOpenCommand().execute(_args(slug="triage"))
        assert result == 1
        assert "no trusted box" in capsys.readouterr().err

    def test_opens_bare_hostname_by_default(self, monkeypatch):
        monkeypatch.setattr(_cmd, "load_registry", lambda: {"test": _make_record()})
        monkeypatch.setattr(
            _cmd.Manifest,
            "load",
            lambda root: _manifest_with_routes({"api": 8080, "admin": 8081}),
        )
        opened = []
        monkeypatch.setattr(_cmd, "_open_url", lambda url: opened.append(url) or None)
        result = SandboxOpenCommand().execute(_args(slug="test"))
        assert result is None
        assert opened == ["http://test.localhost"]

    def test_opens_named_route(self, monkeypatch):
        monkeypatch.setattr(_cmd, "load_registry", lambda: {"test": _make_record()})
        monkeypatch.setattr(
            _cmd.Manifest,
            "load",
            lambda root: _manifest_with_routes({"api": 8080, "admin": 8081}),
        )
        opened = []
        monkeypatch.setattr(_cmd, "_open_url", lambda url: opened.append(url) or None)
        result = SandboxOpenCommand().execute(_args(slug="test", route="admin"))
        assert result is None
        assert opened == ["http://admin.test.localhost"]

    def test_unknown_route_returns_1(self, monkeypatch, capsys):
        monkeypatch.setattr(_cmd, "load_registry", lambda: {"test": _make_record()})
        monkeypatch.setattr(
            _cmd.Manifest, "load", lambda root: _manifest_with_routes({"api": 8080})
        )
        result = SandboxOpenCommand().execute(_args(slug="test", route="bogus"))
        assert result == 1
        assert "unknown route" in capsys.readouterr().err

    def test_no_routes_returns_1(self, monkeypatch, capsys):
        monkeypatch.setattr(_cmd, "load_registry", lambda: {"test": _make_record()})
        monkeypatch.setattr(
            _cmd.Manifest, "load", lambda root: _manifest_with_routes({})
        )
        result = SandboxOpenCommand().execute(_args(slug="test"))
        assert result == 1
        assert "declares no routes" in capsys.readouterr().err


class TestOpenUrl:
    def test_darwin_uses_open(self, monkeypatch):
        monkeypatch.setattr(_cmd.sys, "platform", "darwin")
        calls = []
        monkeypatch.setattr(
            _cmd.subprocess,
            "run",
            lambda cmd, **kw: calls.append(cmd) or SimpleNamespace(returncode=0),
        )
        _open_url("http://test.localhost")
        assert calls[0] == ["open", "http://test.localhost"]

    def test_linux_uses_xdg_open(self, monkeypatch):
        monkeypatch.setattr(_cmd.sys, "platform", "linux")
        calls = []
        monkeypatch.setattr(
            _cmd.subprocess,
            "run",
            lambda cmd, **kw: calls.append(cmd) or SimpleNamespace(returncode=0),
        )
        _open_url("http://test.localhost")
        assert calls[0] == ["xdg-open", "http://test.localhost"]

    def test_returns_1_on_failure(self, monkeypatch, capsys):
        monkeypatch.setattr(_cmd.sys, "platform", "darwin")
        monkeypatch.setattr(_cmd.subprocess, "run", _fail_run)
        result = _open_url("http://test.localhost")
        assert result == 1
        assert "test.localhost" in capsys.readouterr().err
