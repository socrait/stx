import argparse
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from stx.commands.rebuild import SandboxRebuildCommand, build_image

_MOD = "stx.commands.rebuild"


def _args(*, dmz: bool = False, repo: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(dmz=dmz, repo=repo)


class TestDmzFlag:
    def test_builds_the_dmz_image_and_prints_its_tag(self, capsys):
        with (
            patch(f"{_MOD}.dmz.build", return_value=None) as build,
            patch(f"{_MOD}.dmz.image_tag", return_value="stx-dmz:0.0.1"),
        ):
            result = SandboxRebuildCommand().execute(_args(dmz=True))

        assert result is None
        build.assert_called_once_with()
        assert "stx-dmz:0.0.1" in capsys.readouterr().out

    def test_propagates_a_build_failure(self):
        with patch(f"{_MOD}.dmz.build", return_value=1):
            result = SandboxRebuildCommand().execute(_args(dmz=True))

        assert result == 1

    def test_does_not_resolve_a_repo_root(self):
        with (
            patch(f"{_MOD}.dmz.build", return_value=None),
            patch(f"{_MOD}.dmz.image_tag", return_value="stx-dmz:0.0.1"),
            patch(f"{_MOD}.resolve_repo_root") as resolve_repo_root,
        ):
            SandboxRebuildCommand().execute(_args(dmz=True))

        resolve_repo_root.assert_not_called()


class TestBuildImage:
    def test_dockerfile_resolves_against_the_build_context_not_the_repo_root(self):
        """Compose itself resolves `build.dockerfile` against `build.context`,
        never the repo root — the same rule manifest.py's own `rebuild_on`
        resolution already applies. A context other than "." must still
        find the right Dockerfile."""
        root = Path("/repo")
        manifest = SimpleNamespace(
            name="app", compose="compose.yaml", image="widgetshop-devbox"
        )
        build = {"context": "docker", "dockerfile": "Dockerfile.dev"}
        with (
            patch(f"{_MOD}._repo_build_spec", return_value=build),
            patch(f"{_MOD}.subprocess.run") as run,
        ):
            run.return_value = SimpleNamespace(returncode=0)
            build_image(root, manifest, label_hash="deadbeef")

        (docker_args,), _ = run.call_args
        assert "-f" in docker_args
        dockerfile_arg = docker_args[docker_args.index("-f") + 1]
        assert dockerfile_arg == str(root / "docker" / "Dockerfile.dev")
