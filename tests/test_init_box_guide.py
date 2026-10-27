from __future__ import annotations

import os
import subprocess
from pathlib import Path

_INIT_BOX = Path(__file__).parent.parent / "src" / "stx" / "box" / "init-box.sh"

_FAKE_LOOKUP = '_home_dir_for() {{ echo "{home}"; }}\nsu() {{ bash -c "$4"; }}\n'


def _run(
    script: str, *, home: Path, env: dict[str, str]
) -> subprocess.CompletedProcess:
    full_script = _FAKE_LOOKUP.format(home=home) + script
    return subprocess.run(
        ["bash", "-c", f'source "{_INIT_BOX}"\n{full_script}'],
        capture_output=True,
        text=True,
        env={**os.environ, **env},
    )


class TestInstallBoxGuide:
    def test_symlinks_run_dir_agents_md_to_the_zone_guide(self, tmp_path):
        box_dir = tmp_path / "box"
        box_dir.mkdir()
        (box_dir / "guide-trusted.md").write_text("trusted guide\n")
        run_dir = tmp_path / "run"
        home = tmp_path / "home"
        home.mkdir()

        result = _run(
            f'install_box_guide "{box_dir}" "{run_dir}"',
            home=home,
            env={"STX_ZONE": "trusted", "STX_BOX_USER": "devuser"},
        )

        assert result.returncode == 0, result.stderr
        link = run_dir / "AGENTS.md"
        assert link.is_symlink()
        assert link.resolve() == (box_dir / "guide-trusted.md").resolve()

    def test_picks_the_dmz_guide_in_the_dmz_zone(self, tmp_path):
        box_dir = tmp_path / "box"
        box_dir.mkdir()
        (box_dir / "guide-dmz.md").write_text("dmz guide\n")
        run_dir = tmp_path / "run"
        home = tmp_path / "home"
        home.mkdir()

        result = _run(
            f'install_box_guide "{box_dir}" "{run_dir}"',
            home=home,
            env={"STX_ZONE": "dmz", "STX_BOX_USER": "devuser"},
        )

        assert result.returncode == 0, result.stderr
        assert (run_dir / "AGENTS.md").resolve() == (box_dir / "guide-dmz.md").resolve()

    def test_installs_claude_md_when_absent(self, tmp_path):
        box_dir = tmp_path / "box"
        box_dir.mkdir()
        (box_dir / "guide-trusted.md").write_text("trusted guide\n")
        run_dir = tmp_path / "run"
        home = tmp_path / "home"
        home.mkdir()

        result = _run(
            f'install_box_guide "{box_dir}" "{run_dir}"',
            home=home,
            env={"STX_ZONE": "trusted", "STX_BOX_USER": "devuser"},
        )

        assert result.returncode == 0, result.stderr
        assert (home / ".claude" / "CLAUDE.md").read_text() == "trusted guide\n"

    def test_leaves_an_existing_claude_md_untouched(self, tmp_path):
        box_dir = tmp_path / "box"
        box_dir.mkdir()
        (box_dir / "guide-trusted.md").write_text("new guide\n")
        run_dir = tmp_path / "run"
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "CLAUDE.md").write_text("pre-existing\n")

        result = _run(
            f'install_box_guide "{box_dir}" "{run_dir}"',
            home=home,
            env={"STX_ZONE": "trusted", "STX_BOX_USER": "devuser"},
        )

        assert result.returncode == 0, result.stderr
        assert (home / ".claude" / "CLAUDE.md").read_text() == "pre-existing\n"

    def test_defaults_to_the_trusted_zone_when_unset(self, tmp_path):
        box_dir = tmp_path / "box"
        box_dir.mkdir()
        (box_dir / "guide-trusted.md").write_text("trusted guide\n")
        run_dir = tmp_path / "run"
        home = tmp_path / "home"
        home.mkdir()

        result = _run(
            f'install_box_guide "{box_dir}" "{run_dir}"',
            home=home,
            env={"STX_BOX_USER": "devuser"},
        )

        assert result.returncode == 0, result.stderr
        assert (run_dir / "AGENTS.md").resolve() == (
            box_dir / "guide-trusted.md"
        ).resolve()
