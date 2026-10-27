from unittest.mock import MagicMock

import pytest

from stx.core.images import (
    image_exists,
    image_has_command,
    image_has_user,
    image_label,
    image_user,
)


def _runner(returncode: int, *, stdout: str = ""):
    return lambda *a, **kw: MagicMock(returncode=returncode, stdout=stdout)


class TestImageLabel:
    def test_returns_label_value(self):
        assert (
            image_label("widgetshop-devbox", "x", runner=_runner(0, stdout="devuser\n"))
            == "devuser"
        )

    def test_returns_none_when_label_unset(self):
        assert (
            image_label(
                "widgetshop-devbox", "x", runner=_runner(0, stdout="<no value>\n")
            )
            is None
        )

    def test_returns_none_when_image_missing(self):
        assert image_label("missing", "x", runner=_runner(1)) is None


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        ("devuser\n", "devuser"),
        ("devuser:staff\n", "devuser"),
        ("0\n", "root"),
        ("0:0\n", "root"),
        ("\n", None),
    ],
)
def test_image_user_reads_the_dockerfile_user(stdout, expected):
    assert image_user("widgetshop-devbox", runner=_runner(0, stdout=stdout)) == expected


def test_image_user_is_none_when_image_missing():
    assert image_user("missing", runner=_runner(1)) is None


class TestImageHasCommand:
    def test_true_when_command_found(self):
        assert image_has_command("widgetshop-devbox", "claude", runner=_runner(0))

    def test_false_when_command_missing(self):
        assert not image_has_command("widgetshop-devbox", "claude", runner=_runner(1))

    def test_passes_user_flag_when_given(self):
        calls = []

        def runner(cmd, **kw):
            calls.append(cmd)
            return MagicMock(returncode=0)

        image_has_command("widgetshop-devbox", "claude", user="devuser", runner=runner)
        assert "-u" in calls[0] and "devuser" in calls[0]


class TestImageHasUser:
    def test_true_when_user_exists(self):
        assert image_has_user("widgetshop-devbox", "devuser", runner=_runner(0))

    def test_false_when_user_missing(self):
        assert not image_has_user("widgetshop-devbox", "devuser", runner=_runner(1))


class TestImageExists:
    def test_true_when_found(self):
        assert image_exists("widgetshop-devbox", runner=_runner(0))

    def test_false_when_missing(self):
        assert not image_exists("missing", runner=_runner(1))
