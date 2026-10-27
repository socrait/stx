"""Docker-marked integration tests: verify `check_repo_compose`/`check_merged`
against REAL `docker compose ... config --format json` output, not hand-built
fixtures — the pure-function unit tests in test_overlay.py cover the decision
logic; these confirm Compose's own merge/normalization behavior is what this
module assumes it is. Docker is not available in the dev sandbox box this was
written in — run on the host with `pytest -m docker`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from stx import overlay
from stx.core.compose import base_env, compose_config
from stx.core.registry import ClaudeAuthMode
from stx.errors import StxError
from stx.manifest import Manifest

pytestmark = pytest.mark.docker

_WIDGETSHOP = Path(__file__).parent.parent / "examples" / "widgetshop"


def _dump(kind: str, files: list[Path], config: dict) -> None:
    # Set STX_DUMP_COMPOSE_DIR to capture real Compose output for use as unit-test
    # fixtures; the file name records which inputs produced it.
    if not (dump_dir := os.environ.get("STX_DUMP_COMPOSE_DIR")):
        return
    name = os.environ.get("PYTEST_CURRENT_TEST", "unknown").split(" ")[0]
    safe = name.replace("/", "_").replace("::", "__")
    inputs = "+".join(p.stem for p in files)
    out = Path(dump_dir) / f"{safe}__{kind}__{inputs}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"inputs": [p.name for p in files], "config": config}, indent=2)
    )


def _repo_config(compose_file: Path, cwd: Path) -> dict:
    config = compose_config(
        [compose_file], cwd=cwd, no_interpolate=True, env=base_env()
    )
    _dump("repo", [compose_file], config)
    return config


def _merged_config(files: list[Path], cwd: Path) -> dict:
    config = compose_config(files, cwd=cwd, env=os.environ.copy())
    _dump("merged", files, config)
    return config


class TestWidgetshopAgainstRealCompose:
    def test_allow_list_accepts_widgetshop(self):
        compose_path = _WIDGETSHOP / "compose.yaml"
        config = _repo_config(compose_path, _WIDGETSHOP)
        overlay.check_repo_compose(
            config, box_name="app", source=str(compose_path), repo_id="widgetshop"
        )

    def test_post_merge_check_accepts_widgetshop(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
        manifest = Manifest.load(_WIDGETSHOP)
        overlay_path = overlay.render(
            "demo",
            manifest,
            worktree=tmp_path / "wt",
            repo_root=_WIDGETSHOP,
            user="devuser",
            redis_index=1,
            claude_auth_mode=ClaudeAuthMode.API_KEY,
        )
        merged = _merged_config(
            [_WIDGETSHOP / "compose.yaml", overlay_path], _WIDGETSHOP
        )
        overlay_alone = _merged_config([overlay_path], _WIDGETSHOP)
        overlay.check_merged(merged, overlay_alone, source=str(overlay_path))

    def test_post_merge_check_accepts_widgetshop_with_dmz(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
        monkeypatch.setenv("STX_DMZ_ANTHROPIC_API_KEY", "dmz-fake-key")
        manifest = Manifest.load(_WIDGETSHOP)
        overlay_path = overlay.render(
            "demo",
            manifest,
            worktree=tmp_path / "wt",
            repo_root=_WIDGETSHOP,
            user="devuser",
            redis_index=1,
            claude_auth_mode=ClaudeAuthMode.API_KEY,
            dmz_image="widgetshop-devbox",
            dmz_user="devuser",
        )
        merged = _merged_config(
            [_WIDGETSHOP / "compose.yaml", overlay_path], _WIDGETSHOP
        )
        overlay_alone = _merged_config([overlay_path], _WIDGETSHOP)
        overlay.check_merged(merged, overlay_alone, source=str(overlay_path))

    def test_post_merge_check_accepts_dmz_only(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        monkeypatch.setenv("STX_DMZ_ANTHROPIC_API_KEY", "dmz-fake-key")
        overlay_path = overlay.render(
            "demo",
            None,
            dmz_image="widgetshop-devbox",
            dmz_user="devuser",
        )
        merged = _merged_config([overlay_path], tmp_path)
        overlay.check_merged(merged, merged, source=str(overlay_path))


def _mutated_repo(tmp_path: Path, mutate) -> tuple[dict, Path]:
    """Copy examples/widgetshop into tmp_path, apply `mutate` to its
    compose.yaml text, and return (normalized config, compose file path)."""
    root = tmp_path / "widgetshop"
    shutil.copytree(_WIDGETSHOP, root)
    compose_path = root / "compose.yaml"
    compose_path.write_text(mutate(compose_path.read_text()))
    return _repo_config(compose_path, root), compose_path


class TestRefusalsAgainstRealCompose:
    def test_refuses_privileged(self, tmp_path):
        config, compose_path = _mutated_repo(
            tmp_path,
            lambda text: text.replace(
                "    environment:\n",
                "    privileged: true\n    environment:\n",
                1,
            ),
        )
        with pytest.raises(StxError, match="privileged"):
            overlay.check_repo_compose(
                config, box_name="app", source=str(compose_path), repo_id="widgetshop"
            )

    def test_refuses_secrets_targeting_box_runtime(self, tmp_path):
        # The first .replace targets the service-level "    secrets:" insertion
        # point; the second must target the top-level "volumes:" section
        # specifically — "volumes:\n" alone also matches as a substring of the
        # service-level "    volumes:\n" line, which comes first in the file and
        # would otherwise swallow the inserted top-level "secrets:" stanza.
        config, compose_path = _mutated_repo(
            tmp_path,
            lambda text: text.replace(
                "    environment:\n",
                "    secrets:\n"
                "      - source: devbox_secret\n"
                "        target: /opt/stx/box/init-box.sh\n"
                "    environment:\n",
                1,
            ).replace(
                "\nvolumes:\n",
                "\nsecrets:\n  devbox_secret:\n    file: ./pyproject.toml\n\nvolumes:\n",
                1,
            ),
        )
        with pytest.raises(StxError, match="secrets"):
            overlay.check_repo_compose(
                config, box_name="app", source=str(compose_path), repo_id="widgetshop"
            )

    def test_refuses_dmz_service(self, tmp_path):
        # Insert "dmz:" as a sibling service before the top-level "volumes:"
        # section, at the same indentation as "app:" — appending at the end of
        # the file would instead nest it under the top-level volumes mapping,
        # since that's the file's last section.
        config, compose_path = _mutated_repo(
            tmp_path,
            lambda text: text.replace(
                "\nvolumes:\n",
                "\n  dmz:\n    image: widgetshop-devbox\n\nvolumes:\n",
                1,
            ),
        )
        with pytest.raises(StxError):
            overlay.check_repo_compose(
                config, box_name="app", source=str(compose_path), repo_id="widgetshop"
            )


class TestGitMountNarrowing:
    """Real Docker bind-mount enforcement of the `/git` layout `_app_service`
    renders (a read-only top, with only objects/, refs/heads/stx/,
    refs/heads/tmp/<slug>/, refs/remotes, logs/, and the box's own worktree
    admin dir reopened read-write — and that admin dir's commondir/gitdir
    pointers pinned back read-only) — the pure-function unit test in
    test_overlay.py checks the JSON shape; this confirms Docker actually
    enforces it: that the box can't create a new top-level file git would read
    for the whole repo, can't retarget its own worktree pointers, can't move
    the operator's branches (refs/heads/main) or another box's scratch
    (refs/heads/tmp/<other>/), yet ordinary commit/push/fetch and its own
    stx/<slug> + tmp/<slug>/* branch writes still work. Uses `docker run`
    directly against the same mount shape
    `overlay.render()` produces, rather than the full box runtime (firewall/
    tinyproxy/setup), which isn't what this mount layout is about. See
    docs/security.md."""

    def _make_repo_with_two_worktrees(self, tmp_path: Path) -> tuple[Path, Path, Path]:
        """main's common dir, plus two sibling worktrees ("mine", "other")
        simulating two sandboxes of the same repo — "other" stands in for
        another sandbox's own worktree admin dir, which must stay
        unwritable from "mine"'s box. The remote is configured here, on the
        host side, the same way `stx` configures a real sandbox's remote
        before its container starts — a box can't do this itself (the
        shared config is read-only from inside), so the URL has to already
        resolve inside the container by the time it starts."""
        remote = tmp_path / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)

        main = tmp_path / "main"
        main.mkdir()
        subprocess.run(["git", "init", "-q", str(main)], check=True)
        subprocess.run(
            ["git", "commit", "-q", "--allow-empty", "-m", "init"], cwd=main, check=True
        )
        subprocess.run(
            ["git", "remote", "add", "origin", str(remote)], cwd=main, check=True
        )
        subprocess.run(
            ["git", "push", "-q", "-u", "origin", "HEAD:main"], cwd=main, check=True
        )
        # Rewrite the shared config's remote URL to the path it'll resolve to
        # *inside* the container, now that we're done pushing from the host.
        subprocess.run(
            ["git", "remote", "set-url", "origin", "/remote"], cwd=main, check=True
        )

        mine = tmp_path / "mine"
        subprocess.run(
            ["git", "worktree", "add", "-q", "-b", "stx/mine", str(mine)],
            cwd=main,
            check=True,
        )
        other = tmp_path / "other"
        subprocess.run(
            ["git", "worktree", "add", "-q", "-b", "stx/other", str(other)],
            cwd=main,
            check=True,
        )
        # The rw subtrees a fresh repo may still lack — stx's own overlay
        # (`_ensure_git_rw_dirs`) makes these before `compose up`, so make them
        # here too, to bind the same shape regardless of git version. refs/
        # heads/stx/ already exists (the stx/* branches above live in it); its
        # per-slug scratch sibling refs/heads/tmp/mine/ does not yet.
        (main / ".git" / "refs" / "heads" / "tmp" / "mine").mkdir(
            parents=True, exist_ok=True
        )
        (main / ".git" / "refs" / "remotes").mkdir(parents=True, exist_ok=True)
        (main / ".git" / "logs").mkdir(parents=True, exist_ok=True)
        return remote, main / ".git", mine

    def _run_box_git(self, common_dir: Path, mine: Path, remote: Path, script: str):
        """Run `script` in alpine/git against the exact /git mount shape
        `_app_service` renders: a read-only top, the rw subtrees reopened (only
        the refs/heads/stx/ and refs/heads/tmp/<slug>/ branch namespaces, not
        all of refs/heads/), and the box's own worktree pointers pinned
        read-only. gc.auto/fetch.prune are off in the git config env, as the
        overlay sets them, so neither reaches for a packed-refs rewrite the
        read-only top couldn't take."""
        return subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "sh",
                "-e",
                "GIT_DIR=/git/worktrees/mine",
                "-e",
                "GIT_WORK_TREE=/workspace",
                "-e",
                "GIT_CONFIG_COUNT=2",
                "-e",
                "GIT_CONFIG_KEY_0=gc.auto",
                "-e",
                "GIT_CONFIG_VALUE_0=0",
                "-e",
                "GIT_CONFIG_KEY_1=fetch.prune",
                "-e",
                "GIT_CONFIG_VALUE_1=false",
                "-v",
                f"{common_dir}:/git:ro",
                "-v",
                f"{common_dir}/objects:/git/objects",
                "-v",
                f"{common_dir}/refs/heads/stx:/git/refs/heads/stx",
                "-v",
                f"{common_dir}/refs/heads/tmp/mine:/git/refs/heads/tmp/mine",
                "-v",
                f"{common_dir}/refs/remotes:/git/refs/remotes",
                "-v",
                f"{common_dir}/logs:/git/logs",
                "-v",
                f"{common_dir}/worktrees/mine:/git/worktrees/mine",
                "-v",
                f"{common_dir}/worktrees/mine/commondir:/git/worktrees/mine/commondir:ro",
                "-v",
                f"{common_dir}/worktrees/mine/gitdir:/git/worktrees/mine/gitdir:ro",
                "-v",
                f"{mine}:/workspace",
                "-v",
                f"{remote}:/remote",
                "-w",
                "/workspace",
                "alpine/git",
                "-c",
                script,
            ],
            capture_output=True,
            text=True,
            check=True,
        )

    def test_box_writes_that_must_fail_do_but_commit_and_push_work(self, tmp_path):
        remote, common_dir, mine = self._make_repo_with_two_worktrees(tmp_path)
        script = (
            "set -u; "
            "git config --global user.email t@test.invalid; "
            "git config --global user.name t; "
            # The stand-in remote is owned by the host user, not this
            # container's root (a bind mount on Linux keeps the host uid).
            "git config --global --add safe.directory /remote; "
            # The read-only top: a box can't create a new top-level file git
            # would then read for the main repo too, nor rewrite shared config.
            "if (echo bad > /git/commondir) 2>/dev/null; then echo COMMONDIR_CREATE=unexpected-success; "
            "else echo COMMONDIR_CREATE=blocked; fi; "
            "if (echo bad > /git/config.worktree) 2>/dev/null; then echo CONFIG_WORKTREE_CREATE=unexpected-success; "
            "else echo CONFIG_WORKTREE_CREATE=blocked; fi; "
            "if (echo bad > /git/config) 2>/dev/null; then echo CONFIG_WRITE=unexpected-success; "
            "else echo CONFIG_WRITE=blocked; fi; "
            # The box's own worktree pointers are pinned read-only on top of its
            # otherwise-writable admin dir.
            "if (echo bad > /git/worktrees/mine/commondir) 2>/dev/null; then echo OWN_COMMONDIR_WRITE=unexpected-success; "
            "else echo OWN_COMMONDIR_WRITE=blocked; fi; "
            "if (echo bad > /git/worktrees/other/HEAD) 2>/dev/null; then echo OTHER_WRITE=unexpected-success; "
            "else echo OTHER_WRITE=blocked; fi; "
            "if git commit -q --allow-empty -m probe; then echo COMMIT=ok; else echo COMMIT=failed; fi; "
            "if git push -q origin HEAD:refs/heads/stx/mine; then echo PUSH=ok; else echo PUSH=failed; fi; "
            "if git fetch -q origin; then echo FETCH=ok; else echo FETCH=failed; fi; "
            # Create in the box's own writable namespace, so the delete is the
            # step that fails — not the create — isolating the lock as the cause.
            "if git branch -q stx/throwaway && git branch -q -D stx/throwaway; "
            "then echo BRANCH_DELETE=ok; else echo BRANCH_DELETE=failed; fi"
        )
        result = self._run_box_git(common_dir, mine, remote, script)
        assert "COMMONDIR_CREATE=blocked" in result.stdout
        assert "CONFIG_WORKTREE_CREATE=blocked" in result.stdout
        assert "CONFIG_WRITE=blocked" in result.stdout
        assert "OWN_COMMONDIR_WRITE=blocked" in result.stdout
        assert "OTHER_WRITE=blocked" in result.stdout
        assert "COMMIT=ok" in result.stdout
        assert "PUSH=ok" in result.stdout
        assert "FETCH=ok" in result.stdout
        # Git takes the packed-refs lock for every ref delete, packed or not,
        # and the lock lives in the read-only top.
        assert "BRANCH_DELETE=failed" in result.stdout

    def test_refs_heads_is_narrowed_to_stx_and_the_boxs_own_tmp(self, tmp_path):
        """Only refs/heads/stx/ and refs/heads/tmp/<slug>/ are writable; the
        rest of refs/heads/ falls through the read-only top. So the box can
        make its own tmp/<slug>/* scratch branch, but can't plant a loose
        refs/heads/main over the operator's branch, nor write another box's
        tmp/<other>/* — the two writes the old all-of-refs/heads/ mount let it
        make under the operator's identity."""
        remote, common_dir, mine = self._make_repo_with_two_worktrees(tmp_path)
        script = (
            "set -u; "
            "git config --global user.email t@test.invalid; "
            "git config --global user.name t; "
            # Its own scratch namespace is writable.
            "if git branch -q tmp/mine/x; then echo OWN_TMP=ok; else echo OWN_TMP=failed; fi; "
            # Moving the operator's own branch to the box's commit — the attack
            # the narrowing closes — must fail: refs/heads/ top is read-only, so
            # the loose refs/heads/main (which would override the packed one)
            # can't be written.
            "if (git update-ref refs/heads/main refs/heads/stx/mine) 2>/dev/null; "
            "then echo MAIN_WRITE=unexpected-success; else echo MAIN_WRITE=blocked; fi; "
            # Another box's scratch namespace stays unwritable: refs/heads/tmp/
            # is read-only, so the box can't even create the tmp/other/ dir.
            "if (git branch -q tmp/other/x) 2>/dev/null; "
            "then echo OTHER_TMP_WRITE=unexpected-success; else echo OTHER_TMP_WRITE=blocked; fi"
        )
        result = self._run_box_git(common_dir, mine, remote, script)
        assert "OWN_TMP=ok" in result.stdout
        assert "MAIN_WRITE=blocked" in result.stdout
        assert "OTHER_TMP_WRITE=blocked" in result.stdout
