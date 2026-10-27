from unittest.mock import MagicMock

import pytest

import stx.dmz as _dmz
from stx.errors import StxError


def _runner(*, inspect_returncode: int, build_returncode: int = 0):
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        returncode = inspect_returncode if "inspect" in cmd else build_returncode
        return MagicMock(returncode=returncode)

    return runner, calls


class TestImageTag:
    def test_includes_the_stx_version(self, monkeypatch):
        monkeypatch.setattr(_dmz.metadata, "version", lambda name: "9.9.9")
        assert _dmz.image_tag() == "stx-dmz:9.9.9"


class TestDockerfileDir:
    def test_ships_a_dockerfile(self):
        assert (_dmz.dockerfile_dir() / "Dockerfile").is_file()


class TestEnsureBuilt:
    def test_skips_build_when_image_already_exists(self):
        runner, calls = _runner(inspect_returncode=0)

        image = _dmz.ensure_built(runner=runner)

        assert image == _dmz.image_tag()
        assert not any("build" in cmd for cmd in calls)

    def test_builds_when_image_missing(self):
        runner, calls = _runner(inspect_returncode=1, build_returncode=0)

        image = _dmz.ensure_built(runner=runner)

        assert image == _dmz.image_tag()
        build_calls = [cmd for cmd in calls if "build" in cmd]
        assert len(build_calls) == 1
        assert "-t" in build_calls[0]
        assert _dmz.image_tag() in build_calls[0]

    def test_raises_when_build_fails(self):
        runner, _ = _runner(inspect_returncode=1, build_returncode=1)

        with pytest.raises(StxError, match=_dmz.image_tag()):
            _dmz.ensure_built(runner=runner)
