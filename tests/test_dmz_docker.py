"""Docker-marked integration test: builds stx's own dmz image for real and
checks it against the box contract's image requirements. Docker is not
available in the dev sandbox box this was written in — run on the host with
`pytest -m docker`.
"""

from __future__ import annotations

import subprocess

import pytest

from stx import dmz
from stx.core.images import image_has_command, image_has_user, image_user

pytestmark = pytest.mark.docker

_REQUIRED_COMMANDS = (
    "bash",
    "iptables",
    "ip6tables",
    "ss",
    "tinyproxy",
    "ulogd",
    "jq",
    "git",
    "su",
)


def test_builds_and_meets_the_image_requirements():
    # A fresh build, so an image left over from before a Dockerfile change
    # can't stand in for the current one.
    assert dmz.build() is None
    image = dmz.image_tag()

    for binary in _REQUIRED_COMMANDS:
        assert image_has_command(image, binary), f"missing {binary}"

    user = image_user(image)
    assert user is not None
    assert user != "root"
    assert image_has_user(image, user)
    assert image_has_command(image, "claude", user=user)


def test_workspace_is_not_writable_by_the_box_user():
    image = dmz.ensure_built()
    result = subprocess.run(
        ["docker", "run", "--rm", image, "sh", "-c", "touch /workspace/probe"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
