import argparse
from unittest.mock import MagicMock, patch

from stx.commands.down import SandboxDownCommand
from stx.core import BoxRecord, SandboxRecord, SandboxState

_MOD = "stx.commands.down"


def _args(*, force: bool = False) -> argparse.Namespace:
    return argparse.Namespace(force=force)


def _make_record(slug: str, *, trusted: bool) -> SandboxRecord:
    boxes = (
        {"app": BoxRecord(zone="trusted", user="devuser")}
        if trusted
        else {"dmz": BoxRecord(zone="dmz", user="devuser")}
    )
    return SandboxRecord(
        slug=slug,
        created="2026-06-26T00:00:00+00:00",
        state=SandboxState.READY,
        redis_index=1 if trusted else None,
        tailnet_port=5099 if trusted else None,
        boxes=boxes,
    )


class TestHelpText:
    def test_does_not_understate_the_platform_to_two_services(self):
        # The platform is traefik+postgres+redis+otel (see ALL_SERVICES in
        # stx.platform) — down used to describe it as just "postgres + redis".
        assert "postgres + redis" not in SandboxDownCommand.help()


class TestAddParser:
    def _parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        SandboxDownCommand().add_parser(subparsers)
        return parser

    def test_force_flag_parses(self):
        args = self._parser().parse_args(["down", "--force"])
        assert args.force is True

    def test_force_short_flag(self):
        args = self._parser().parse_args(["down", "-f"])
        assert args.force is True

    def test_force_defaults_false(self):
        args = self._parser().parse_args(["down"])
        assert args.force is False


class TestExecute:
    def test_refuses_with_an_active_trusted_box_and_no_force(self, capsys):
        with patch(
            f"{_MOD}.load_registry",
            return_value={"demo": _make_record("demo", trusted=True)},
        ):
            result = SandboxDownCommand().execute(_args())

        assert result == 1
        err = capsys.readouterr().err
        assert "demo" in err
        assert "--force" in err

    def test_force_stops_despite_an_active_trusted_box(self):
        with (
            patch(
                f"{_MOD}.load_registry",
                return_value={"demo": _make_record("demo", trusted=True)},
            ),
            patch(f"{_MOD}.stx_platform.compose_env", return_value={}),
            patch(f"{_MOD}.subprocess.run", return_value=MagicMock(returncode=0)),
        ):
            result = SandboxDownCommand().execute(_args(force=True))

        assert result is None

    def test_dmz_only_sandboxes_dont_block_without_force(self):
        with (
            patch(
                f"{_MOD}.load_registry",
                return_value={"demo": _make_record("demo", trusted=False)},
            ),
            patch(f"{_MOD}.stx_platform.compose_env", return_value={}),
            patch(f"{_MOD}.subprocess.run", return_value=MagicMock(returncode=0)),
        ):
            result = SandboxDownCommand().execute(_args())

        assert result is None

    def test_compose_failure_returns_1(self, capsys):
        with (
            patch(f"{_MOD}.load_registry", return_value={}),
            patch(f"{_MOD}.stx_platform.compose_env", return_value={}),
            patch(f"{_MOD}.subprocess.run", return_value=MagicMock(returncode=1)),
        ):
            result = SandboxDownCommand().execute(_args())

        assert result == 1
        assert "failed to stop" in capsys.readouterr().err

    def test_success_prints_confirmation(self, capsys):
        with (
            patch(f"{_MOD}.load_registry", return_value={}),
            patch(f"{_MOD}.stx_platform.compose_env", return_value={}),
            patch(f"{_MOD}.subprocess.run", return_value=MagicMock(returncode=0)),
        ):
            result = SandboxDownCommand().execute(_args())

        assert result is None
        assert "platform down" in capsys.readouterr().out
