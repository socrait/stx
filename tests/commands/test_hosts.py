import argparse
from unittest.mock import patch

from stx.commands.hosts import SandboxHostsCommand
from stx.core import BoxRecord, SandboxRecord, SandboxState

_MOD = "stx.commands.hosts"


def _record(slug: str = "my-slug", *, routes: dict | None = None) -> SandboxRecord:
    boxes = {}
    if routes is not None:
        boxes["app"] = BoxRecord(zone="trusted", user="devuser", routes=routes)
    return SandboxRecord(
        slug=slug,
        created="2026-06-30T00:00:00+00:00",
        state=SandboxState.READY,
        redis_index=None,
        tailnet_port=None,
        boxes=boxes,
    )


def _args(action: str, slug: str = "my-slug") -> argparse.Namespace:
    return argparse.Namespace(hosts_action=action, slug=slug)


class TestAdd:
    def test_unknown_slug_errors(self, capsys):
        with patch(f"{_MOD}.load_registry", return_value={}):
            result = SandboxHostsCommand().execute(_args("add", slug="nope"))

        assert result == 1
        assert "no sandbox named 'nope'" in capsys.readouterr().err

    def test_no_routes_says_so_and_does_not_add(self, capsys):
        calls = []
        with (
            patch(
                f"{_MOD}.load_registry",
                return_value={"my-slug": _record(routes={})},
            ),
            patch(f"{_MOD}.add_hosts_entry", side_effect=lambda *a: calls.append(a)),
        ):
            result = SandboxHostsCommand().execute(_args("add"))

        assert result is None
        assert calls == []
        assert "declares no routes" in capsys.readouterr().out

    def test_no_trusted_box_says_no_routes(self, capsys):
        with patch(
            f"{_MOD}.load_registry",
            return_value={"my-slug": _record(routes=None)},
        ):
            result = SandboxHostsCommand().execute(_args("add"))

        assert result is None
        assert "declares no routes" in capsys.readouterr().out

    def test_adds_bare_and_route_hostnames_from_registry(self, capsys):
        calls = []
        with (
            patch(
                f"{_MOD}.load_registry",
                return_value={"my-slug": _record(routes={"api": 8080, "admin": 8081})},
            ),
            patch(f"{_MOD}.hosts_entry_exists", return_value=False),
            patch(
                f"{_MOD}.add_hosts_entry",
                side_effect=lambda s, r: calls.append((s, r)),
            ),
        ):
            result = SandboxHostsCommand().execute(_args("add"))

        assert result is None
        assert calls == [("my-slug", {"api": 8080, "admin": 8081})]
        out = capsys.readouterr().out
        assert "added:" in out
        assert "my-slug.localhost" in out
        assert "api.my-slug.localhost" in out
        assert "admin.my-slug.localhost" in out

    def test_already_present_skips_add(self, capsys):
        calls = []
        with (
            patch(
                f"{_MOD}.load_registry",
                return_value={"my-slug": _record(routes={"api": 8080})},
            ),
            patch(f"{_MOD}.hosts_entry_exists", return_value=True),
            patch(
                f"{_MOD}.add_hosts_entry",
                side_effect=lambda s, r: calls.append((s, r)),
            ),
        ):
            result = SandboxHostsCommand().execute(_args("add"))

        assert result is None
        assert calls == []
        out = capsys.readouterr().out
        assert "already present" in out
        assert "api.my-slug.localhost" in out

    def test_sudo_failure_returns_1(self, capsys):
        def _raise(slug, routes):
            raise OSError("sudo failed while updating the hosts file")

        with (
            patch(
                f"{_MOD}.load_registry",
                return_value={"my-slug": _record(routes={"api": 8080})},
            ),
            patch(f"{_MOD}.hosts_entry_exists", return_value=False),
            patch(f"{_MOD}.add_hosts_entry", side_effect=_raise),
        ):
            result = SandboxHostsCommand().execute(_args("add"))

        assert result == 1
        assert "sudo failed" in capsys.readouterr().err


class TestRemove:
    def test_unknown_slug_errors(self, capsys):
        with patch(f"{_MOD}.load_registry", return_value={}):
            result = SandboxHostsCommand().execute(_args("remove", slug="nope"))

        assert result == 1
        assert "no sandbox named 'nope'" in capsys.readouterr().err

    def test_removes_entry_lists_names_removed(self, capsys):
        calls = []
        with (
            patch(
                f"{_MOD}.load_registry",
                return_value={"my-slug": _record(routes={"api": 8080})},
            ),
            patch(
                f"{_MOD}.remove_hosts_entry",
                side_effect=lambda s: calls.append(s)
                or ["my-slug.localhost", "api.my-slug.localhost"],
            ),
        ):
            result = SandboxHostsCommand().execute(_args("remove"))

        assert result is None
        assert calls == ["my-slug"]
        out = capsys.readouterr().out
        assert "removed:" in out
        assert "my-slug.localhost" in out
        assert "api.my-slug.localhost" in out

    def test_already_absent_skips_remove(self, capsys):
        with (
            patch(
                f"{_MOD}.load_registry",
                return_value={"my-slug": _record(routes={"api": 8080})},
            ),
            patch(f"{_MOD}.remove_hosts_entry", return_value=[]),
        ):
            result = SandboxHostsCommand().execute(_args("remove"))

        assert result is None
        assert "already absent" in capsys.readouterr().out

    def test_sudo_failure_returns_1(self, capsys):
        def _raise(slug):
            raise OSError("sudo failed while updating the hosts file")

        with (
            patch(
                f"{_MOD}.load_registry",
                return_value={"my-slug": _record(routes={"api": 8080})},
            ),
            patch(f"{_MOD}.remove_hosts_entry", side_effect=_raise),
        ):
            result = SandboxHostsCommand().execute(_args("remove"))

        assert result == 1
        assert "sudo failed" in capsys.readouterr().err
