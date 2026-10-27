import signal
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from stx.core import ClaudeAuthMode
from stx.core.compose import ComposeVariable
from stx.forge import ForgeCredential, ForgeCredentialKind
from stx.lifecycle import provision as _prov
from stx.lifecycle.spec import SandboxSpec
from stx.manifest import (
    PROVISION_ADMIN_VARS,
    ForgeManifest,
    Manifest,
    PackageRegistryManifest,
    ProvisionManifest,
)
from stx.platform import PostgresAdminParams


def _manifest(tmp_path, **overrides) -> Manifest:
    manifest = Manifest(
        schema=1,
        name="app",
        compose="compose.yaml",
        image="widgetshop-devbox",
        user="devuser",
    )
    for key, value in overrides.items():
        object.__setattr__(manifest, key, value)
    return manifest


def _forge_manifest() -> ForgeManifest:
    return ForgeManifest(
        kind="gitlab",
        project="widgetshop/widgetshop",
        package_registry=PackageRegistryManifest(
            group="widgetshop",
            env={
                "username": "UV_INDEX_GITLAB_INTERNAL_USERNAME",
                "password": "UV_INDEX_GITLAB_INTERNAL_PASSWORD",
            },
        ),
    )


def _spec(tmp_path, *, forge=None, manifest=None, **overrides) -> SandboxSpec:
    defaults = dict(
        slug="my-slug",
        root=tmp_path,
        repo_name="repo",
        manifest=manifest or _manifest(tmp_path),
        forge=forge,
        services=[],
        with_dmz=False,
        claude_auth_mode=ClaudeAuthMode.OAUTH,
        read_only=False,
        timeout=900.0,
    )
    defaults.update(overrides)
    return SandboxSpec(**defaults)


def _push_credential() -> ForgeCredential:
    return ForgeCredential(
        kind=ForgeCredentialKind.PUSH,
        scope_id=42,
        token_id=1,
        scope="write_repository",
        secret="fake-token",
        expires_at="2026-07-09",
    )


def _package_credential() -> ForgeCredential:
    return ForgeCredential(
        kind=ForgeCredentialKind.PACKAGE,
        scope_id=43,
        token_id=2,
        scope="read_package_registry",
        secret="pkg-token",
        expires_at="2026-07-09",
        username="pkg-user",
    )


class _FakeForge:
    host = "gitlab.example.com"
    git_user = "oauth2"

    def __init__(self):
        self.revoked: list[tuple[ForgeCredentialKind, int, int]] = []

    def mint_push(self, slug, *, read_only):
        return _push_credential()

    def mint_package(self, slug):
        return _package_credential()

    def revoke(self, kind, scope_id, token_id):
        self.revoked.append((kind, scope_id, token_id))
        return True

    def username(self):
        return None


def _fake_git_refs(*, local_exists, remote_exists, origin_is_ancestor=None):
    """subprocess.run stand-in for _content_source's three possible calls:
    local ref check, origin ref check, and (only when both exist) the
    merge-base ancestor check deciding which of the two is fresher."""

    def _run(cmd, **kwargs):
        if cmd[:3] == ["git", "rev-parse", "--verify"]:
            ref = cmd[3]
            if ref.startswith("refs/remotes/origin/"):
                return MagicMock(returncode=0 if remote_exists else 1)
            assert ref.startswith("refs/heads/")
            return MagicMock(returncode=0 if local_exists else 1)
        if cmd[:2] == ["git", "merge-base"]:
            return MagicMock(returncode=0 if origin_is_ancestor else 1)
        raise AssertionError(f"unexpected command: {cmd}")

    return _run


class TestCollectHostVars:
    def _config(self, value: str) -> dict:
        return {"services": {"app": {"environment": {"A": value}}}}

    def test_refuses_a_base_env_variable(self):
        from stx.errors import StxError

        with pytest.raises(StxError, match="HOME"):
            _prov.collect_host_vars(self._config("${HOME:-}"))

    def test_refuses_a_docker_prefixed_variable(self):
        from stx.errors import StxError

        with pytest.raises(StxError, match="DOCKER_AUTH_CONFIG"):
            _prov.collect_host_vars(self._config("${DOCKER_AUTH_CONFIG:-}"))

    def test_accepts_an_ordinary_host_variable(self):
        host_vars = _prov.collect_host_vars(self._config("${DATABASE_URL}"))
        assert [v.name for v in host_vars] == ["DATABASE_URL"]


class TestContentSource:
    def test_explicit_base_always_wins(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            subprocess, "run", _fake_git_refs(local_exists=True, remote_exists=True)
        )
        assert (
            _prov._content_source(
                "feat/alex/checkout-flow", "some/explicit-ref", True, tmp_path
            )
            == "some/explicit-ref"
        )

    def test_prefers_local_when_ahead_of_origin(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            subprocess,
            "run",
            _fake_git_refs(
                local_exists=True, remote_exists=True, origin_is_ancestor=True
            ),
        )
        assert (
            _prov._content_source(
                "feat/alex/checkout-flow", "origin/dev", False, tmp_path
            )
            == "feat/alex/checkout-flow"
        )

    def test_prefers_origin_when_local_is_behind(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            subprocess,
            "run",
            _fake_git_refs(
                local_exists=True, remote_exists=True, origin_is_ancestor=False
            ),
        )
        assert (
            _prov._content_source(
                "feat/alex/checkout-flow", "origin/dev", False, tmp_path
            )
            == "origin/feat/alex/checkout-flow"
        )

    def test_uses_local_when_only_local_exists(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            subprocess, "run", _fake_git_refs(local_exists=True, remote_exists=False)
        )
        assert (
            _prov._content_source(
                "feat/alex/checkout-flow", "origin/dev", False, tmp_path
            )
            == "feat/alex/checkout-flow"
        )

    def test_uses_origin_when_only_origin_exists(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            subprocess, "run", _fake_git_refs(local_exists=False, remote_exists=True)
        )
        assert (
            _prov._content_source(
                "feat/alex/checkout-flow", "origin/dev", False, tmp_path
            )
            == "origin/feat/alex/checkout-flow"
        )

    def test_falls_back_to_base_when_neither_exists(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            subprocess, "run", _fake_git_refs(local_exists=False, remote_exists=False)
        )
        assert (
            _prov._content_source("feat/alex/brand-new", "origin/dev", False, tmp_path)
            == "origin/dev"
        )


class TestWorktreeBranchArgs:
    def test_local_branch_absent_creates_fresh_from_content_source(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: MagicMock(returncode=1))
        monkeypatch.setattr(
            _prov, "_content_source", lambda *a, **kw: "origin/feat/alex/existing"
        )

        args = _prov._worktree_branch_args(
            "stx/test", "feat/alex/existing", "origin/dev", False, tmp_path
        )
        assert args == ["-b", "stx/test", "origin/feat/alex/existing"]

    def test_local_branch_exists_is_reused_and_reconciled(self, monkeypatch, tmp_path):
        reconcile_calls = []
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: MagicMock(returncode=0))
        monkeypatch.setattr(
            _prov, "_content_source", lambda *a, **kw: "origin/feat/alex/existing"
        )
        monkeypatch.setattr(
            _prov,
            "_reconcile_stx_branch",
            lambda branch, ref, root, **kw: reconcile_calls.append((branch, ref)),
        )

        args = _prov._worktree_branch_args(
            "stx/test", "feat/alex/existing", "origin/dev", False, tmp_path
        )
        assert args == ["stx/test"]
        assert reconcile_calls == [("stx/test", "origin/feat/alex/existing")]


class TestDivergentCommits:
    def test_parses_only_plus_prefixed_lines(self, monkeypatch, tmp_path):
        cherry_output = (
            "- af9483339394ed603185a37df9bf78ec03c9921d Already applied\n"
            "+ a1ecd7aee7e5577c9e370c5977b0bafa2d3161f4 Genuinely new commit\n"
            "+ f466c3cc89946cb0d6ff8eff3a088dbe1f522da6 Another new one\n"
        )
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **kw: MagicMock(returncode=0, stdout=cherry_output),
        )
        result = _prov._divergent_commits("sand", "base", tmp_path)
        assert result == [
            ("a1ecd7aee7e5577c9e370c5977b0bafa2d3161f4", "Genuinely new commit"),
            ("f466c3cc89946cb0d6ff8eff3a088dbe1f522da6", "Another new one"),
        ]

    def test_returns_empty_when_all_already_applied(self, monkeypatch, tmp_path):
        cherry_output = "- af9483339394ed603185a37df9bf78ec03c9921d Already applied\n"
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **kw: MagicMock(returncode=0, stdout=cherry_output),
        )
        assert _prov._divergent_commits("sand", "base", tmp_path) == []

    def test_returns_none_on_git_failure(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **kw: MagicMock(returncode=1, stdout="")
        )
        assert _prov._divergent_commits("sand", "base", tmp_path) is None


class TestReconcileSandBranch:
    def test_fast_forwards_when_no_divergence(self, monkeypatch, tmp_path):
        branch_f_calls = []

        def mock_run(cmd, **kwargs):
            if cmd[:2] == ["git", "cherry"]:
                return MagicMock(returncode=0, stdout="")
            if cmd[:3] == ["git", "branch", "-f"]:
                branch_f_calls.append(cmd)
                return MagicMock(returncode=0)
            return MagicMock(returncode=0)

        monkeypatch.setattr(subprocess, "run", mock_run)
        _prov._reconcile_stx_branch("sand", "base", tmp_path, yes=False)
        assert branch_f_calls == [["git", "branch", "-f", "sand", "base"]]

    def test_leaves_branch_alone_when_check_fails(self, monkeypatch, tmp_path, capsys):
        branch_f_calls = []

        def mock_run(cmd, **kwargs):
            if cmd[:2] == ["git", "cherry"]:
                return MagicMock(returncode=1, stdout="")
            if cmd[:3] == ["git", "branch", "-f"]:
                branch_f_calls.append(cmd)
                return MagicMock(returncode=0)
            return MagicMock(returncode=0)

        monkeypatch.setattr(subprocess, "run", mock_run)
        monkeypatch.setattr(
            _prov,
            "input",
            lambda *a: (_ for _ in ()).throw(
                AssertionError("should not prompt when the check itself failed")
            ),
            raising=False,
        )
        _prov._reconcile_stx_branch("sand", "base", tmp_path, yes=False)
        assert branch_f_calls == []
        assert "could not check" in capsys.readouterr().err

    def test_yes_keeps_branch_without_prompting(self, monkeypatch, tmp_path, capsys):
        branch_f_calls = []

        def mock_run(cmd, **kwargs):
            if cmd[:2] == ["git", "cherry"]:
                return MagicMock(returncode=0, stdout="+ abc1234 Real divergent work\n")
            if cmd[:3] == ["git", "branch", "-f"]:
                branch_f_calls.append(cmd)
                return MagicMock(returncode=0)
            return MagicMock(returncode=0)

        monkeypatch.setattr(subprocess, "run", mock_run)
        monkeypatch.setattr(
            _prov,
            "input",
            lambda *a: (_ for _ in ()).throw(
                AssertionError("should not prompt when --yes")
            ),
            raising=False,
        )
        _prov._reconcile_stx_branch("sand", "base", tmp_path, yes=True)
        assert branch_f_calls == []
        assert "Real divergent work" in capsys.readouterr().out

    def test_non_interactive_keeps_branch_without_prompting(
        self, monkeypatch, tmp_path
    ):
        branch_f_calls = []

        def mock_run(cmd, **kwargs):
            if cmd[:2] == ["git", "cherry"]:
                return MagicMock(returncode=0, stdout="+ abc1234 Real divergent work\n")
            if cmd[:3] == ["git", "branch", "-f"]:
                branch_f_calls.append(cmd)
                return MagicMock(returncode=0)
            return MagicMock(returncode=0)

        monkeypatch.setattr(subprocess, "run", mock_run)
        monkeypatch.setattr(_prov.sys.stdin, "isatty", lambda: False)
        monkeypatch.setattr(
            _prov,
            "input",
            lambda *a: (_ for _ in ()).throw(
                AssertionError("should not prompt when stdin isn't a tty")
            ),
            raising=False,
        )
        _prov._reconcile_stx_branch("sand", "base", tmp_path, yes=False)
        assert branch_f_calls == []

    def test_prompts_and_keeps_on_k(self, monkeypatch, tmp_path):
        branch_f_calls = []

        def mock_run(cmd, **kwargs):
            if cmd[:2] == ["git", "cherry"]:
                return MagicMock(returncode=0, stdout="+ abc1234 Real divergent work\n")
            if cmd[:3] == ["git", "branch", "-f"]:
                branch_f_calls.append(cmd)
                return MagicMock(returncode=0)
            return MagicMock(returncode=0)

        monkeypatch.setattr(subprocess, "run", mock_run)
        monkeypatch.setattr(_prov.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(_prov, "input", lambda *a: "k", raising=False)
        _prov._reconcile_stx_branch("sand", "base", tmp_path, yes=False)
        assert branch_f_calls == []

    def test_prompts_and_discards_on_d(self, monkeypatch, tmp_path):
        branch_f_calls = []

        def mock_run(cmd, **kwargs):
            if cmd[:2] == ["git", "cherry"]:
                return MagicMock(returncode=0, stdout="+ abc1234 Real divergent work\n")
            if cmd[:3] == ["git", "branch", "-f"]:
                branch_f_calls.append(cmd)
                return MagicMock(returncode=0)
            return MagicMock(returncode=0)

        monkeypatch.setattr(subprocess, "run", mock_run)
        monkeypatch.setattr(_prov.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(_prov, "input", lambda *a: "d", raising=False)
        _prov._reconcile_stx_branch("sand", "base", tmp_path, yes=False)
        assert branch_f_calls == [["git", "branch", "-f", "sand", "base"]]

    def test_reprompts_on_invalid_input(self, monkeypatch, tmp_path):
        def mock_run(cmd, **kwargs):
            if cmd[:2] == ["git", "cherry"]:
                return MagicMock(returncode=0, stdout="+ abc1234 Real divergent work\n")
            return MagicMock(returncode=0)

        monkeypatch.setattr(subprocess, "run", mock_run)
        monkeypatch.setattr(_prov.sys.stdin, "isatty", lambda: True)
        responses = iter(["nonsense", "k"])
        monkeypatch.setattr(_prov, "input", lambda *a: next(responses), raising=False)
        _prov._reconcile_stx_branch("sand", "base", tmp_path, yes=False)
        assert next(responses, None) is None


class TestRunProvisionSteps:
    def test_runs_every_step_in_order_with_rendered_args(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        calls = []

        def mock_run(cmd, **kwargs):
            calls.append((cmd, kwargs.get("env", {})))
            return MagicMock(returncode=0)

        monkeypatch.setattr(subprocess, "run", mock_run)

        manifest = _manifest(
            tmp_path,
            provision=ProvisionManifest(
                steps=(
                    ("bash", "x.sh", "--db", "{db}"),
                    ("bash", "y.sh"),
                ),
                env={
                    "DATABASE_USER": "${PGUSER}",
                    "OTHER_SECRET": "${DATABASE_APP_PASSWORD}",
                },
            ),
        )
        # Ambient shell holds a decoy; the real value must come from secret_env.
        monkeypatch.setenv("DATABASE_APP_PASSWORD", "ambient-leak")

        result = _prov._run_provision_steps(
            manifest,
            "my-slug",
            [tmp_path / "compose.yaml"],
            tmp_path,
            redis_index=None,
            admin_vars={"PGUSER": "platform-admin"},
            host_vars=[ComposeVariable(name="DATABASE_APP_PASSWORD", required=True)],
            secret_env={"DATABASE_APP_PASSWORD": "app-secret"},
        )

        assert result is None
        compose_calls = [(cmd, env) for cmd, env in calls if cmd[0] == "docker"]
        assert len(compose_calls) == 2
        assert compose_calls[0][0][-4:] == ["bash", "x.sh", "--db", "stx_my_slug"]
        assert compose_calls[0][1]["DATABASE_USER"] == "platform-admin"
        assert compose_calls[0][1]["OTHER_SECRET"] == "app-secret"
        assert compose_calls[0][1]["PGUSER"] == "platform-admin"
        run_args = compose_calls[0][0]
        assert "STX_PROVISION_RUN=1" in run_args
        box_at = run_args.index("app", run_args.index("run"))
        assert sorted(run_args[box_at - 6 : box_at]) == [
            "-e",
            "-e",
            "-e",
            "DATABASE_USER",
            "OTHER_SECRET",
            "PGUSER",
        ]
        assert compose_calls[1][0][-2:] == ["bash", "y.sh"]

    def test_host_var_not_referenced_by_repo_compose_is_refused(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(AssertionError)
        )
        monkeypatch.setenv("DATABASE_APP_PASSWORD", "app-secret")

        manifest = _manifest(
            tmp_path,
            provision=ProvisionManifest(
                steps=(("bash", "x.sh"),),
                env={"OTHER_SECRET": "${DATABASE_APP_PASSWORD}"},
            ),
        )

        result = _prov._run_provision_steps(
            manifest,
            "my-slug",
            [tmp_path / "compose.yaml"],
            tmp_path,
            redis_index=None,
            admin_vars={},
            host_vars=[],
        )
        assert result == 1

    def test_no_steps_is_a_noop(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(AssertionError)
        )
        result = _prov._run_provision_steps(
            _manifest(tmp_path),
            "my-slug",
            [tmp_path / "compose.yaml"],
            tmp_path,
            redis_index=None,
            admin_vars={},
        )
        assert result is None

    def test_step_failure_returns_1(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: MagicMock(returncode=1))
        manifest = _manifest(
            tmp_path, provision=ProvisionManifest(steps=(("bash", "x.sh"),))
        )
        result = _prov._run_provision_steps(
            manifest,
            "my-slug",
            [tmp_path / "compose.yaml"],
            tmp_path,
            redis_index=None,
            admin_vars={},
        )
        assert result == 1
        assert "provisioning step failed" in capsys.readouterr().err


class TestProvisionAdminVars:
    def test_address_is_in_network_not_host_published(self):
        admin = PostgresAdminParams(
            host="localhost", port=15432, user="admin", password="pw"
        )

        result = _prov._provision_admin_vars(admin)

        assert result["PGHOST"] == "stx-postgres"
        assert result["PGPORT"] == "5432"
        assert result["PGUSER"] == "stx_provisioner"
        assert result["PGPASSWORD"] not in ("", "pw")
        assert result["PGDATABASE"] == "postgres"
        assert set(result) == set(PROVISION_ADMIN_VARS)


class TestRenderOverlay:
    def test_passes_the_specs_own_databases_to_overlay_render(self, tmp_path):
        spec = _spec(tmp_path, databases=("stx_my_slug", "stx_my_slug_test"))
        state = _prov.ProvisionState()
        state.wt_path = tmp_path / "wt"
        state.user = "devuser"
        render_mock = MagicMock(return_value=tmp_path / "overlay.json")

        with patch.object(_prov.overlay, "render", render_mock):
            _prov._render_overlay(spec, state)

        assert render_mock.call_args.kwargs["databases"] == (
            "stx_my_slug",
            "stx_my_slug_test",
        )

    def test_defaults_to_empty_databases(self, tmp_path):
        spec = _spec(tmp_path)
        state = _prov.ProvisionState()
        state.wt_path = tmp_path / "wt"
        state.user = "devuser"
        render_mock = MagicMock(return_value=tmp_path / "overlay.json")

        with patch.object(_prov.overlay, "render", render_mock):
            _prov._render_overlay(spec, state)

        assert render_mock.call_args.kwargs["databases"] == ()


class TestForgeMintUndo:
    def test_revokes_minted_credentials_on_unwind(self, tmp_path):
        fake_forge = _FakeForge()
        spec = _spec(
            tmp_path,
            forge=fake_forge,
            manifest=_manifest(tmp_path, forge=_forge_manifest()),
        )
        state = _prov.ProvisionState()

        _prov._mint_forge(spec, state)
        assert state.forge_credentials

        _prov._unwind(state.undo)

        assert fake_forge.revoked == [
            (ForgeCredentialKind.PUSH, 42, 1),
            (ForgeCredentialKind.PACKAGE, 43, 2),
        ]

    def test_no_forge_configured_registers_no_undo(self, tmp_path):
        spec = _spec(tmp_path, forge=None)
        state = _prov.ProvisionState()

        _prov._mint_forge(spec, state)

        assert state.undo == []


class TestComposeTeardownRegistration:
    def test_down_undo_raises_when_containers_are_not_clean(
        self, monkeypatch, tmp_path
    ):
        # A failed down must raise so rollback keeps the entry, rather than
        # count as clean and free the Redis index out from under a live box.
        from stx.lifecycle import ContainerTeardown

        spec = _spec(tmp_path, redis_index=None)
        state = _prov.ProvisionState()
        monkeypatch.setattr(
            _prov,
            "bring_down_containers",
            lambda slug: ContainerTeardown(
                down_ok=False, used_label_fallback=True, remaining=["c1"]
            ),
        )
        _prov._register_compose_teardown(spec, state)

        down = state.undo[-1]
        assert down.label == "compose down"
        with pytest.raises(_prov.StxError):
            down.fn()

    def test_flush_runs_after_the_box_is_down_on_unwind(self, monkeypatch, tmp_path):
        from stx.lifecycle import ContainerTeardown

        spec = _spec(tmp_path, redis_index=15)
        state = _prov.ProvisionState()
        order: list[str] = []
        monkeypatch.setattr(
            _prov,
            "bring_down_containers",
            lambda slug: order.append("down")
            or ContainerTeardown(down_ok=True, used_label_fallback=False, remaining=[]),
        )
        monkeypatch.setattr(
            _prov.resources, "flush_redis", lambda idx, params: order.append("flush")
        )
        monkeypatch.setattr(_prov.RedisAdminParams, "build", lambda: MagicMock())

        _prov._register_compose_teardown(spec, state)
        _prov._unwind(state.undo)

        assert order == ["down", "flush"]


class TestUnwindIsolatesFailures:
    def test_one_failing_undo_does_not_stop_the_rest(self, capsys):
        ran = []
        undo_stack = [
            _prov._Undo("first", lambda: ran.append("first")),
            _prov._Undo("second", lambda: (_ for _ in ()).throw(RuntimeError("boom"))),
            _prov._Undo("third", lambda: ran.append("third")),
        ]

        _prov._unwind(undo_stack)

        assert ran == ["third", "first"]
        assert "rollback step 'second' failed" in capsys.readouterr().err

    def test_second_interrupt_during_unwind_prints_what_was_skipped_and_reraises(
        self, capsys
    ):
        ran = []

        def _raise_interrupt():
            raise KeyboardInterrupt()

        undo_stack = [
            _prov._Undo("alpha", lambda: ran.append("alpha")),
            _prov._Undo("bravo", _raise_interrupt),
            _prov._Undo("charlie", lambda: ran.append("charlie")),
        ]

        with pytest.raises(KeyboardInterrupt):
            _prov._unwind(undo_stack)

        assert ran == ["charlie"]
        captured = capsys.readouterr()
        assert "skipped" in captured.err
        assert "bravo" in captured.err
        assert "alpha" in captured.err


class TestTerminationHandlers:
    def test_sigterm_raises_keyboardinterrupt_then_handlers_restore(self):
        original = signal.getsignal(signal.SIGTERM)
        restore = _prov._install_termination_handlers()
        try:
            with pytest.raises(KeyboardInterrupt):
                signal.raise_signal(signal.SIGTERM)
        finally:
            restore()
        assert signal.getsignal(signal.SIGTERM) == original


class TestCreateDmzSnapshot:
    def test_creates_snapshot_without_git_dir(self, monkeypatch, tmp_path):
        import stx.core as _common

        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
        (repo / "file.txt").write_text("hello")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True)
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
        ).stdout.strip()

        result = _prov._create_dmz_snapshot(repo, "my-slug", sha, "repo")

        assert result is None
        dest = _common.code_dmz_dir("my-slug", "repo")
        assert (dest / "file.txt").read_text() == "hello"
        assert not (dest / ".git").exists()

    def test_returns_1_on_bad_sha(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)

        result = _prov._create_dmz_snapshot(repo, "my-slug", "not-a-real-sha", "repo")

        assert result == 1
