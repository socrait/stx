from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent


@pytest.mark.build
def test_wheel_contains_the_skill_and_both_box_guides(tmp_path):
    result = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    wheels = list(tmp_path.glob("*.whl"))
    assert len(wheels) == 1
    names = set(zipfile.ZipFile(wheels[0]).namelist())

    assert "stx/skill/SKILL.md" in names
    assert "stx/box/guide-trusted.md" in names
    assert "stx/box/guide-dmz.md" in names
    assert "stx/dmz/Dockerfile" in names
