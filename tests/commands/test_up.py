import argparse
from unittest.mock import patch

from stx.commands.up import SandboxUpCommand
from stx.platform import ALL_SERVICES

_MOD = "stx.commands.up"


def _args(*, services=None, recreate=False, yes=False) -> argparse.Namespace:
    return argparse.Namespace(services=services or [], recreate=recreate, yes=yes)


class TestAddParser:
    def _parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        SandboxUpCommand().add_parser(subparsers)
        return parser

    def test_yes_flag_parses(self):
        args = self._parser().parse_args(["up", "--recreate", "--yes"])
        assert args.yes is True

    def test_yes_short_flag(self):
        args = self._parser().parse_args(["up", "-y"])
        assert args.yes is True

    def test_yes_defaults_false(self):
        args = self._parser().parse_args(["up"])
        assert args.yes is False


class TestDefaultBehavior:
    def test_no_services_defaults_to_all_four(self):
        with patch(f"{_MOD}.stx_platform.run_platform_up", return_value=None) as up:
            result = SandboxUpCommand().execute(_args())

        assert result is None
        up.assert_called_once_with(list(ALL_SERVICES))

    def test_named_services_are_passed_through(self):
        with patch(f"{_MOD}.stx_platform.run_platform_up", return_value=None) as up:
            SandboxUpCommand().execute(_args(services=["postgres", "redis"]))

        up.assert_called_once_with(["postgres", "redis"])

    def test_propagates_a_failure(self):
        with patch(f"{_MOD}.stx_platform.run_platform_up", return_value=1):
            result = SandboxUpCommand().execute(_args())

        assert result == 1


class TestRecreate:
    def test_recreate_calls_run_platform_recreate_not_run_platform_up(self):
        with (
            patch(f"{_MOD}.stx_platform.run_platform_up") as up,
            patch(
                f"{_MOD}.stx_platform.run_platform_recreate", return_value=None
            ) as recreate,
        ):
            result = SandboxUpCommand().execute(_args(recreate=True))

        assert result is None
        up.assert_not_called()
        recreate.assert_called_once_with(list(ALL_SERVICES), yes=False)

    def test_recreate_defaults_yes_to_false(self):
        with patch(
            f"{_MOD}.stx_platform.run_platform_recreate", return_value=None
        ) as recreate:
            SandboxUpCommand().execute(_args(recreate=True, services=["postgres"]))

        recreate.assert_called_once_with(["postgres"], yes=False)

    def test_recreate_with_yes_passes_it_through(self):
        with patch(
            f"{_MOD}.stx_platform.run_platform_recreate", return_value=None
        ) as recreate:
            SandboxUpCommand().execute(
                _args(recreate=True, yes=True, services=["postgres"])
            )

        recreate.assert_called_once_with(["postgres"], yes=True)

    def test_propagates_a_recreate_failure(self):
        with patch(f"{_MOD}.stx_platform.run_platform_recreate", return_value=1):
            result = SandboxUpCommand().execute(_args(recreate=True))

        assert result == 1
