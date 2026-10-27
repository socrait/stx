import subprocess
from pathlib import Path
from unittest.mock import patch

import stx.commands.doctor as _cmd
from stx.commands.doctor import SandboxDoctorCommand
from stx.errors import StxError

_MOD = "stx.commands.doctor"


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "alex@mycompany.com"], cwd=path, check=True
    )
    subprocess.run(["git", "config", "user.name", "Alex Doe"], cwd=path, check=True)


def test_refuses_when_in_sandbox_box(monkeypatch, capsys):
    monkeypatch.setenv("STX_ZONE", "trusted")
    with patch(f"{_MOD}.git_toplevel", side_effect=AssertionError("should not run")):
        result = SandboxDoctorCommand().execute(None)

    assert result == 1
    assert "host-only" in capsys.readouterr().err


class TestFindHazardousConfig:
    def test_empty_when_nothing_set(self, tmp_path):
        _init_repo(tmp_path)
        assert _cmd._find_hazardous_config(tmp_path) == []

    def test_finds_core_worktree(self, tmp_path):
        # core.worktree must point at a real, existing dir — an invalid path
        # makes every subsequent `git config` call in this repo fail outright
        # ("fatal: Invalid path"), which is itself exactly why this key is
        # hazardous, but not what this test is exercising.
        _init_repo(tmp_path)
        subprocess.run(
            ["git", "config", "core.worktree", str(tmp_path)],
            cwd=tmp_path,
            check=True,
        )
        assert _cmd._find_hazardous_config(tmp_path) == [
            ("core.worktree", str(tmp_path))
        ]

    def test_finds_core_checkstat(self, tmp_path):
        _init_repo(tmp_path)
        subprocess.run(
            ["git", "config", "core.checkstat", "minimal"], cwd=tmp_path, check=True
        )
        assert _cmd._find_hazardous_config(tmp_path) == [("core.checkstat", "minimal")]

    def test_finds_both_when_both_set(self, tmp_path):
        _init_repo(tmp_path)
        subprocess.run(
            ["git", "config", "core.worktree", str(tmp_path)], cwd=tmp_path, check=True
        )
        subprocess.run(
            ["git", "config", "core.checkstat", "minimal"], cwd=tmp_path, check=True
        )
        found = dict(_cmd._find_hazardous_config(tmp_path))
        assert found == {"core.worktree": str(tmp_path), "core.checkstat": "minimal"}

    def test_ignores_unrelated_config(self, tmp_path):
        _init_repo(tmp_path)
        subprocess.run(
            ["git", "config", "commit.gpgsign", "true"], cwd=tmp_path, check=True
        )
        assert _cmd._find_hazardous_config(tmp_path) == []


class TestReportImageRequirements:
    def test_not_a_repo_skips_quietly(self, monkeypatch, capsys):
        monkeypatch.setattr(
            _cmd,
            "resolve_repo_root",
            lambda explicit, cwd: (_ for _ in ()).throw(StxError("not a repo")),
        )

        assert _cmd._report_image_requirements() is True
        assert "not run inside a repo with a manifest — skipped" in (
            capsys.readouterr().out
        )

    def test_manifest_load_error_is_reported_not_swallowed(
        self, monkeypatch, tmp_path, capsys
    ):
        monkeypatch.setattr(_cmd, "resolve_repo_root", lambda explicit, cwd: tmp_path)
        monkeypatch.setattr(
            _cmd.Manifest,
            "load",
            lambda root: (_ for _ in ()).throw(
                StxError("sandbox.yaml: schema: must be 1 (found 2)")
            ),
        )

        result = _cmd._report_image_requirements()

        assert result is False
        out = capsys.readouterr().out
        assert "manifest failed to load" in out
        assert "schema: must be 1 (found 2)" in out
        assert "skipped" not in out


class TestExecute:
    def _stub_new_checks(self, monkeypatch, tmp_path) -> None:
        """Make the newer compose/image/dotenv checks pass (or no-op) so pre-existing
        tests keep asserting only on the hazard/identity checks they were written for."""
        monkeypatch.setattr(_cmd, "compose_version", lambda: (2, 20, 0))
        monkeypatch.setattr(
            _cmd,
            "resolve_repo_root",
            lambda explicit, cwd: (_ for _ in ()).throw(StxError("not a repo")),
        )
        monkeypatch.setattr(
            _cmd, "default_dotenv_path", lambda: tmp_path / "does-not-exist" / ".env"
        )

    def test_clean_repo_returns_none(self, tmp_path, capsys, monkeypatch):
        monkeypatch.delenv("STX_ZONE", raising=False)
        _init_repo(tmp_path)
        self._stub_new_checks(monkeypatch, tmp_path)
        with patch(f"{_MOD}.git_toplevel", return_value=tmp_path):
            result = SandboxDoctorCommand().execute(None)

        assert result is None
        out = capsys.readouterr().out
        assert "no hazardous universal config keys found" in out
        assert "warning" not in out

    def test_hazardous_config_returns_1(self, tmp_path, capsys, monkeypatch):
        monkeypatch.delenv("STX_ZONE", raising=False)
        _init_repo(tmp_path)
        subprocess.run(
            ["git", "config", "core.worktree", str(tmp_path)], cwd=tmp_path, check=True
        )
        self._stub_new_checks(monkeypatch, tmp_path)
        with patch(f"{_MOD}.git_toplevel", return_value=tmp_path):
            result = SandboxDoctorCommand().execute(None)

        assert result == 1
        out = capsys.readouterr().out
        assert f"core.worktree = {tmp_path}" in out

    def test_suspicious_identity_returns_1(self, tmp_path, capsys, monkeypatch):
        monkeypatch.delenv("STX_ZONE", raising=False)
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"], cwd=tmp_path, check=True
        )
        subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)
        self._stub_new_checks(monkeypatch, tmp_path)
        with patch(f"{_MOD}.git_toplevel", return_value=tmp_path):
            result = SandboxDoctorCommand().execute(None)

        assert result == 1
        assert "warning" in capsys.readouterr().out
