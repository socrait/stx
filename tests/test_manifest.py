import copy
import re
import subprocess

import pytest
import yaml

from stx.core.registry import LinkRecord
from stx.errors import StxError
from stx.manifest import LinkManifest, Manifest, render_template

_BASE: dict = {
    "schema": 1,
    "name": "app",
    "compose": "compose.yaml",
    "image": "widgetshop-devbox",
}


def _write(root, data: dict, *, compose: str = "services:\n  app: {}\n") -> None:
    (root / "compose.yaml").write_text(compose)
    (root / "sandbox.yaml").write_text(yaml.safe_dump(data))


def _manifest(root, overrides: dict | None = None) -> dict:
    data = copy.deepcopy(_BASE)
    if overrides:
        data.update(overrides)
    return data


def _git(root, *args) -> None:
    subprocess.run(["git", *args], cwd=str(root), check=True, capture_output=True)


def _init_repo(root, *, origin: str | None = None) -> None:
    _git(root, "init", "-q")
    if origin:
        _git(root, "remote", "add", "origin", origin)


class TestSchema:
    def test_missing_schema_rejected(self, tmp_path):
        data = _manifest(tmp_path)
        del data["schema"]
        _write(tmp_path, data)
        with pytest.raises(StxError, match="schema"):
            Manifest.load(tmp_path)

    def test_wrong_schema_rejected(self, tmp_path):
        data = _manifest(tmp_path, {"schema": 2})
        _write(tmp_path, data)
        with pytest.raises(StxError, match="schema"):
            Manifest.load(tmp_path)

    def test_valid_schema_loads(self, tmp_path):
        _write(tmp_path, _manifest(tmp_path))
        assert Manifest.load(tmp_path).schema == 1


class TestRootShape:
    def test_non_mapping_root_rejected(self, tmp_path):
        (tmp_path / "sandbox.yaml").write_text("- 1\n- 2\n")
        with pytest.raises(StxError, match="mapping"):
            Manifest.load(tmp_path)

    def test_invalid_yaml_rejected(self, tmp_path):
        (tmp_path / "sandbox.yaml").write_text("name: [unterminated\n")
        with pytest.raises(StxError, match="could not be parsed"):
            Manifest.load(tmp_path)

    def test_unknown_top_level_key_rejected(self, tmp_path):
        data = _manifest(tmp_path, {"bogus": True})
        _write(tmp_path, data)
        with pytest.raises(StxError, match="unknown key"):
            Manifest.load(tmp_path)

    def test_box_section_rejected(self, tmp_path):
        # the `box:` section is gone — every key is top-level now.
        data = _manifest(tmp_path)
        del data["name"], data["compose"], data["image"]
        data["box"] = {"name": "app", "compose": "compose.yaml", "image": "x"}
        _write(tmp_path, data)
        with pytest.raises(StxError, match="unknown key"):
            Manifest.load(tmp_path)

    def test_host_env_key_rejected(self, tmp_path):
        # `host_env:` is gone — provision.env's ${NAME} refs resolve from the
        # host secret source directly.
        data = _manifest(tmp_path, {"host_env": {"X": "y"}})
        _write(tmp_path, data)
        with pytest.raises(StxError, match="unknown key"):
            Manifest.load(tmp_path)

    def test_init_key_rejected(self, tmp_path):
        # `init:` is gone — `setup:` is the only spelling.
        data = _manifest(tmp_path, {"init": ["box-init.sh"]})
        _write(tmp_path, data)
        with pytest.raises(StxError, match="unknown key"):
            Manifest.load(tmp_path)

    def test_dmz_section_rejected(self, tmp_path):
        # the dmz box has no manifest config of its own — it's `stx`'s own
        # built-in image, not something a repo's manifest declares.
        data = _manifest(tmp_path, {"dmz": {"image": "custom-dmz"}})
        _write(tmp_path, data)
        with pytest.raises(StxError, match="unknown key"):
            Manifest.load(tmp_path)


class TestName:
    def test_name_must_match_slug_pattern(self, tmp_path):
        data = _manifest(tmp_path, {"name": "App Box"})
        _write(tmp_path, data)
        with pytest.raises(StxError, match="box name"):
            Manifest.load(tmp_path)

    def test_name_must_not_be_dmz(self, tmp_path):
        data = _manifest(tmp_path, {"name": "dmz"})
        _write(tmp_path, data)
        with pytest.raises(StxError, match="dmz"):
            Manifest.load(tmp_path)

    def test_name_defaults_to_sole_compose_service(self, tmp_path):
        data = _manifest(tmp_path)
        del data["name"]
        _write(tmp_path, data, compose="services:\n  widget: {}\n")
        assert Manifest.load(tmp_path).name == "widget"

    def test_ambiguous_services_without_override_rejected(self, tmp_path):
        data = _manifest(tmp_path)
        del data["name"]
        _write(tmp_path, data, compose="services:\n  a: {}\n  b: {}\n")
        with pytest.raises(StxError, match="2 service"):
            Manifest.load(tmp_path)


class TestCompose:
    def test_compose_path_must_exist(self, tmp_path):
        data = _manifest(tmp_path, {"compose": "missing.yaml"})
        _write(tmp_path, data)
        with pytest.raises(StxError, match="does not exist"):
            Manifest.load(tmp_path)

    def test_compose_dotdot_escape_rejected(self, tmp_path):
        data = _manifest(tmp_path, {"compose": "../compose.yaml"})
        _write(tmp_path, data)
        (tmp_path.parent / "compose.yaml").write_text("services: {app: {}}\n")
        with pytest.raises(StxError, match="repo root"):
            Manifest.load(tmp_path)

    def test_compose_defaults_via_lookup_order(self, tmp_path):
        data = _manifest(tmp_path)
        del data["compose"]
        (tmp_path / "sandbox.yaml").write_text(yaml.safe_dump(data))
        (tmp_path / "compose.yml").write_text("services:\n  app: {}\n")
        assert Manifest.load(tmp_path).compose == "compose.yml"

    def test_no_compose_file_found_rejected(self, tmp_path):
        data = _manifest(tmp_path)
        del data["compose"]
        (tmp_path / "sandbox.yaml").write_text(yaml.safe_dump(data))
        with pytest.raises(StxError, match="no compose file found"):
            Manifest.load(tmp_path)

    def test_compose_with_no_services_mapping_rejected(self, tmp_path):
        data = _manifest(tmp_path)
        _write(tmp_path, data, compose="not-a-services-doc: true\n")
        with pytest.raises(StxError, match="services"):
            Manifest.load(tmp_path)


_DOCKER_TAG_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")


class TestImage:
    def test_image_defaults_to_repo_dir_name(self, tmp_path):
        data = _manifest(tmp_path)
        del data["image"]
        _write(tmp_path, data)
        _init_repo(tmp_path)
        image = Manifest.load(tmp_path).image
        assert image.startswith(f"stx-{tmp_path.name}-")
        assert _DOCKER_TAG_RE.match(image)

    def test_image_default_is_a_valid_lowercase_docker_tag(self, tmp_path, monkeypatch):
        # Docker rejects uppercase repository names outright — a dirname
        # like "MyApp" used to flow straight into the tag unlowercased.
        repo = tmp_path / "MyApp Repo!"
        repo.mkdir()
        _init_repo(repo)
        data = _manifest(repo)
        del data["image"]
        _write(repo, data)
        image = Manifest.load(repo).image
        assert _DOCKER_TAG_RE.match(image), image
        assert image == image.lower()

    def test_image_default_shared_across_worktrees(self, tmp_path, monkeypatch):
        # A linked worktree's own (randomly named) directory must not change
        # the image tag — the default comes from the parent of the git
        # common dir, stable across worktrees of the same repo.
        main = tmp_path / "main-repo"
        main.mkdir()
        _init_repo(main)
        (main / "README.md").write_text("x")
        _git(main, "add", "README.md")
        _git(main, "commit", "-q", "-m", "init")

        wt = tmp_path / "some-random-worktree-dir"
        subprocess.run(
            ["git", "worktree", "add", "-b", "wt-branch", str(wt)],
            cwd=str(main),
            check=True,
            capture_output=True,
        )
        data = _manifest(wt)
        del data["image"]
        _write(wt, data)
        assert Manifest.load(wt).image.startswith("stx-main-repo-")

    def test_image_default_differs_across_checkouts_sharing_a_dirname(self, tmp_path):
        # Two independent checkouts named identically (e.g. two sandboxes of
        # the same repo name checked out side by side) must not rebuild over
        # each other's image.
        first = tmp_path / "a" / "widgetshop"
        second = tmp_path / "b" / "widgetshop"
        for repo in (first, second):
            repo.mkdir(parents=True)
            _init_repo(repo)
            data = _manifest(repo)
            del data["image"]
            _write(repo, data)

        first_image = Manifest.load(first).image
        second_image = Manifest.load(second).image
        assert first_image != second_image
        assert first_image.startswith("stx-widgetshop-")
        assert second_image.startswith("stx-widgetshop-")

    def test_image_override_used_as_is(self, tmp_path):
        data = _manifest(tmp_path, {"image": "custom-tag"})
        _write(tmp_path, data)
        assert Manifest.load(tmp_path).image == "custom-tag"


class TestUser:
    def test_user_must_match_user_pattern(self, tmp_path):
        data = _manifest(tmp_path, {"user": "Dev User"})
        _write(tmp_path, data)
        with pytest.raises(StxError, match="user"):
            Manifest.load(tmp_path)

    def test_valid_user_loads(self, tmp_path):
        data = _manifest(tmp_path, {"user": "dev-user_1"})
        _write(tmp_path, data)
        assert Manifest.load(tmp_path).user == "dev-user_1"

    def test_no_override_leaves_user_none(self, tmp_path):
        _write(tmp_path, _manifest(tmp_path))
        assert Manifest.load(tmp_path).user is None


class TestResolveUser:
    def test_override_short_circuits_image_inspect(self, tmp_path):
        data = _manifest(tmp_path, {"user": "devuser"})
        _write(tmp_path, data)
        manifest = Manifest.load(tmp_path)
        runner = lambda *a, **kw: (_ for _ in ()).throw(  # noqa: E731
            AssertionError("should not inspect the image")
        )
        assert manifest.resolve_user(runner=runner) == "devuser"

    def test_resolves_from_image_label(self, tmp_path):
        _write(tmp_path, _manifest(tmp_path))
        manifest = Manifest.load(tmp_path)
        runner = lambda *a, **kw: subprocess.CompletedProcess(  # noqa: E731
            [], 0, stdout="devuser\n", stderr=""
        )
        assert manifest.resolve_user(runner=runner) == "devuser"

    def test_refuses_root_user(self, tmp_path):
        _write(tmp_path, _manifest(tmp_path))
        manifest = Manifest.load(tmp_path)
        runner = lambda *a, **kw: subprocess.CompletedProcess(  # noqa: E731
            [], 0, stdout="root\n", stderr=""
        )
        with pytest.raises(StxError, match="no non-root USER"):
            manifest.resolve_user(runner=runner)

    def test_refuses_unset_user(self, tmp_path):
        _write(tmp_path, _manifest(tmp_path))
        manifest = Manifest.load(tmp_path)
        runner = lambda *a, **kw: subprocess.CompletedProcess(  # noqa: E731
            [], 1, stdout="", stderr="no such image"
        )
        with pytest.raises(StxError, match="no non-root USER"):
            manifest.resolve_user(runner=runner)


class TestRebuildOn:
    def test_dockerfile_and_copy_add_sources_are_defaults(self, tmp_path):
        (tmp_path / "Dockerfile").write_text(
            "FROM python:3.12-slim\n"
            "COPY pyproject.toml uv.lock ./\n"
            "ADD extra/ /app/extra\n"
            "USER devuser\n"
        )
        (tmp_path / "pyproject.toml").write_text("")
        (tmp_path / "uv.lock").write_text("")
        (tmp_path / "extra").mkdir()
        (tmp_path / "extra" / "file.txt").write_text("")
        data = _manifest(tmp_path)
        _write(
            tmp_path,
            data,
            compose="services:\n  app:\n    build:\n      context: .\n",
        )
        rebuild_on = Manifest.load(tmp_path).rebuild_on
        assert "Dockerfile" in rebuild_on
        assert "pyproject.toml" in rebuild_on
        assert "uv.lock" in rebuild_on
        assert "extra" in rebuild_on

    def test_build_string_shorthand_resolves_the_default_dockerfile(self, tmp_path):
        (tmp_path / "Dockerfile").write_text(
            "FROM python:3.12-slim\nCOPY uv.lock ./\nUSER devuser\n"
        )
        (tmp_path / "uv.lock").write_text("")
        data = _manifest(tmp_path)
        _write(tmp_path, data, compose="services:\n  app:\n    build: .\n")
        assert Manifest.load(tmp_path).rebuild_on == ("Dockerfile", "uv.lock")

    def test_explicit_rebuild_on_merges_with_dockerfile_defaults(self, tmp_path):
        (tmp_path / "Dockerfile").write_text("FROM python:3.12-slim\nUSER devuser\n")
        (tmp_path / "uv.lock").write_text("")
        data = _manifest(tmp_path, {"rebuild_on": ["uv.lock"]})
        _write(
            tmp_path,
            data,
            compose="services:\n  app:\n    build:\n      context: .\n",
        )
        rebuild_on = Manifest.load(tmp_path).rebuild_on
        assert rebuild_on == ("Dockerfile", "uv.lock")

    def test_copy_from_build_stage_is_excluded(self, tmp_path):
        (tmp_path / "Dockerfile").write_text(
            "FROM python:3.12-slim AS builder\n"
            "FROM python:3.12-slim\n"
            "COPY --from=builder /app /app\n"
            "USER devuser\n"
        )
        data = _manifest(tmp_path)
        _write(
            tmp_path,
            data,
            compose="services:\n  app:\n    build:\n      context: .\n",
        )
        assert Manifest.load(tmp_path).rebuild_on == ("Dockerfile",)

    def test_rebuild_on_dotdot_escape_rejected(self, tmp_path):
        (tmp_path.parent / "outside.lock").write_text("")
        data = _manifest(tmp_path, {"rebuild_on": ["../outside.lock"]})
        _write(tmp_path, data)
        with pytest.raises(StxError, match="repo root"):
            Manifest.load(tmp_path)

    def test_rebuild_on_missing_path_rejected(self, tmp_path):
        data = _manifest(tmp_path, {"rebuild_on": ["missing.lock"]})
        _write(tmp_path, data)
        with pytest.raises(StxError, match="does not exist"):
            Manifest.load(tmp_path)

    def test_glob_copy_source_is_excluded(self, tmp_path):
        (tmp_path / "Dockerfile").write_text(
            "FROM python:3.12-slim\nCOPY pyproject.toml uv.lock* ./\nUSER devuser\n"
        )
        (tmp_path / "pyproject.toml").write_text("")
        data = _manifest(tmp_path)
        _write(
            tmp_path,
            data,
            compose="services:\n  app:\n    build:\n      context: .\n",
        )
        rebuild_on = Manifest.load(tmp_path).rebuild_on
        assert rebuild_on == ("Dockerfile", "pyproject.toml")

    def test_no_build_section_means_no_dockerfile_defaults(self, tmp_path):
        data = _manifest(tmp_path)
        _write(tmp_path, data, compose="services:\n  app:\n    image: prebuilt\n")
        assert Manifest.load(tmp_path).rebuild_on == ()


class TestSetup:
    def test_setup_path_must_exist(self, tmp_path):
        data = _manifest(tmp_path, {"setup": ["missing.sh"]})
        _write(tmp_path, data)
        with pytest.raises(StxError, match="does not exist"):
            Manifest.load(tmp_path)

    def test_setup_existing_path_passes(self, tmp_path):
        (tmp_path / "box-init.sh").write_text("")
        data = _manifest(tmp_path, {"setup": ["box-init.sh"]})
        _write(tmp_path, data)
        assert Manifest.load(tmp_path).setup == ("box-init.sh",)

    def test_setup_symlink_escape_rejected(self, tmp_path):
        outside = tmp_path.parent / "outside-hook.sh"
        outside.write_text("")
        (tmp_path / "hook.sh").symlink_to(outside)
        data = _manifest(tmp_path, {"setup": ["hook.sh"]})
        _write(tmp_path, data)
        with pytest.raises(StxError, match="repo root"):
            Manifest.load(tmp_path)


class TestRoutesAndPublish:
    def test_port_out_of_range_rejected(self, tmp_path):
        data = _manifest(tmp_path, {"routes": {"api": 70000}})
        _write(tmp_path, data)
        with pytest.raises(StxError, match="1-65535"):
            Manifest.load(tmp_path)

    def test_name_must_be_dns_label(self, tmp_path):
        data = _manifest(tmp_path, {"routes": {"Not_A_Label!": 8080}})
        _write(tmp_path, data)
        with pytest.raises(StxError, match="DNS label"):
            Manifest.load(tmp_path)

    def test_name_cannot_contain_a_dot(self, tmp_path):
        # A route name and a sandbox slug (validate_slug, [a-z0-9-]+) are both
        # single, dot-free DNS labels — so a bare `<slug>.localhost` is always
        # 2 labels while a route-prefixed `<route>.<slug>.localhost` is always
        # 3, and the two can never collide regardless of the names chosen.
        data = _manifest(tmp_path, {"routes": {"foo.bar": 8080}})
        _write(tmp_path, data)
        with pytest.raises(StxError, match="DNS label"):
            Manifest.load(tmp_path)

    def test_valid_route_loads(self, tmp_path):
        data = _manifest(tmp_path, {"routes": {"api": 8080}})
        _write(tmp_path, data)
        assert Manifest.load(tmp_path).routes == {"api": 8080}

    def test_empty_routes_is_bare_box(self, tmp_path):
        _write(tmp_path, _manifest(tmp_path))
        assert Manifest.load(tmp_path).routes == {}

    def test_publish_must_name_a_route(self, tmp_path):
        data = _manifest(tmp_path, {"routes": {"api": 8080}, "publish": "bogus"})
        _write(tmp_path, data)
        with pytest.raises(StxError, match="not a route"):
            Manifest.load(tmp_path)

    def test_publish_names_a_route(self, tmp_path):
        data = _manifest(tmp_path, {"routes": {"api": 8080}, "publish": "api"})
        _write(tmp_path, data)
        assert Manifest.load(tmp_path).publish == "api"

    def test_no_publish_means_nothing_published(self, tmp_path):
        data = _manifest(tmp_path, {"routes": {"api": 8080}})
        _write(tmp_path, data)
        assert Manifest.load(tmp_path).publish is None


class TestBranch:
    def test_branch_defaults_to_sandbox_slug(self, tmp_path):
        _write(tmp_path, _manifest(tmp_path))
        assert Manifest.load(tmp_path).branch == "sandbox/{slug}"

    def test_branch_override_used_as_is(self, tmp_path):
        data = _manifest(tmp_path, {"branch": "feat/{user}/{slug}"})
        _write(tmp_path, data)
        assert Manifest.load(tmp_path).branch == "feat/{user}/{slug}"


class TestPlatform:
    def test_unknown_platform_key_rejected(self, tmp_path):
        data = _manifest(tmp_path, {"platform": {"bogus": True}})
        _write(tmp_path, data)
        with pytest.raises(StxError, match="unknown key"):
            Manifest.load(tmp_path)

    def test_postgres_databases_must_contain_db_placeholder(self, tmp_path):
        data = _manifest(
            tmp_path, {"platform": {"postgres": {"databases": ["literal_name"]}}}
        )
        _write(tmp_path, data)
        with pytest.raises(StxError, match=r"\{db\}"):
            Manifest.load(tmp_path)

    def test_valid_postgres_loads(self, tmp_path):
        data = _manifest(
            tmp_path,
            {
                "platform": {
                    "postgres": {"databases": ["{db}", "{db}_test"]},
                    "redis": True,
                }
            },
        )
        _write(tmp_path, data)
        m = Manifest.load(tmp_path)
        assert m.platform.postgres.databases == ("{db}", "{db}_test")

    def test_no_platform_key_means_nothing_starts(self, tmp_path):
        _write(tmp_path, _manifest(tmp_path))
        m = Manifest.load(tmp_path)
        assert m.platform.postgres is None
        assert m.platform.redis is False
        assert m.platform.otel is False


class TestProvision:
    def test_unknown_provision_key_rejected(self, tmp_path):
        data = _manifest(tmp_path, {"provision": {"bogus": []}})
        _write(tmp_path, data)
        with pytest.raises(StxError, match="unknown key"):
            Manifest.load(tmp_path)

    def test_step_must_be_list_of_strings(self, tmp_path):
        data = _manifest(tmp_path, {"provision": {"steps": [{"run": ["x"]}]}})
        _write(tmp_path, data)
        with pytest.raises(StxError, match="list of strings"):
            Manifest.load(tmp_path)

    def test_step_must_be_non_empty(self, tmp_path):
        data = _manifest(tmp_path, {"provision": {"steps": [[]]}})
        _write(tmp_path, data)
        with pytest.raises(StxError, match="non-empty"):
            Manifest.load(tmp_path)

    def test_env_must_be_string_map(self, tmp_path):
        data = _manifest(tmp_path, {"provision": {"env": {"X": 1}}})
        _write(tmp_path, data)
        with pytest.raises(StxError, match="string to string"):
            Manifest.load(tmp_path)

    def test_valid_provision_loads_bare_argv_steps(self, tmp_path):
        data = _manifest(
            tmp_path,
            {
                "provision": {
                    "steps": [["bash", "x.sh"]],
                    "env": {"DATABASE_USER": "${PGUSER}"},
                }
            },
        )
        _write(tmp_path, data)
        m = Manifest.load(tmp_path)
        assert m.provision.steps == (("bash", "x.sh"),)
        assert m.provision.env == {"DATABASE_USER": "${PGUSER}"}

    def test_admin_var_references_are_never_reserved(self, tmp_path):
        # PGUSER/PGPASSWORD aren't reserved — they're stx's own admin
        # connection vars, not a host var reference at all.
        data = _manifest(
            tmp_path,
            {
                "provision": {
                    "env": {
                        "DATABASE_USER": "${PGUSER}",
                        "DATABASE_PASSWORD": "${PGPASSWORD}",
                    }
                }
            },
        )
        _write(tmp_path, data)
        assert Manifest.load(tmp_path) is not None

    @pytest.mark.parametrize(
        "name",
        [
            "STX_POSTGRES_PASSWORD",
            "STX_GITLAB_MINTER_TOKEN",
            "STX_FORGE_TOKEN",
            "GIT_CONFIG_KEY_0",
        ],
    )
    def test_reserved_host_var_reference_rejected_at_load(self, tmp_path, name):
        data = _manifest(tmp_path, {"provision": {"env": {"X": f"${{{name}}}"}}})
        _write(tmp_path, data)
        with pytest.raises(StxError, match=f"{name} reserved"):
            Manifest.load(tmp_path)


class TestEnv:
    def test_env_must_be_string_map(self, tmp_path):
        data = _manifest(tmp_path, {"env": {"X": 1}})
        _write(tmp_path, data)
        with pytest.raises(StxError, match="string to string"):
            Manifest.load(tmp_path)

    def test_valid_env_loads(self, tmp_path):
        data = _manifest(tmp_path, {"env": {"DATABASE_DB": "{db}"}})
        _write(tmp_path, data)
        assert Manifest.load(tmp_path).env == {"DATABASE_DB": "{db}"}

    @pytest.mark.parametrize(
        "name",
        [
            "STX_ZONE",
            "STX_EGRESS_PORTS",
            "GIT_DIR",
            "https_proxy",
            "NO_PROXY",
            "LD_PRELOAD",
            "BASH_ENV",
            "PYTHONSTARTUP",
            "NODE_OPTIONS",
        ],
    )
    def test_names_that_confine_the_box_are_refused(self, tmp_path, name):
        data = _manifest(tmp_path, {"env": {name: "x"}})
        _write(tmp_path, data)
        with pytest.raises(StxError, match=f"{name} reserved"):
            Manifest.load(tmp_path)


class TestEgress:
    def test_unknown_egress_key_rejected(self, tmp_path):
        data = _manifest(tmp_path, {"egress": {"bogus": []}})
        _write(tmp_path, data)
        with pytest.raises(StxError, match="unknown key"):
            Manifest.load(tmp_path)

    def test_hosts_regex_must_compile(self, tmp_path):
        data = _manifest(tmp_path, {"egress": {"hosts": ["~("]}})
        _write(tmp_path, data)
        with pytest.raises(StxError, match="does not compile"):
            Manifest.load(tmp_path)

    def test_plain_hostname_loads(self, tmp_path):
        data = _manifest(tmp_path, {"egress": {"hosts": ["pypi.org"]}})
        _write(tmp_path, data)
        assert Manifest.load(tmp_path).egress.hosts == ("pypi.org",)

    def test_port_token_must_match_docker_notation(self, tmp_path):
        data = _manifest(tmp_path, {"egress": {"ports": ["bogus"]}})
        _write(tmp_path, data)
        with pytest.raises(StxError, match=r"tcp\|udp"):
            Manifest.load(tmp_path)

    def test_port_tokens_load(self, tmp_path):
        data = _manifest(tmp_path, {"egress": {"ports": ["7881/tcp", "*/udp"]}})
        _write(tmp_path, data)
        assert Manifest.load(tmp_path).egress.ports == ("7881/tcp", "*/udp")

    def test_wildcard_tcp_is_refused(self, tmp_path):
        data = _manifest(tmp_path, {"egress": {"ports": ["*/tcp"]}})
        _write(tmp_path, data)
        with pytest.raises(StxError, match=r"\*/tcp"):
            Manifest.load(tmp_path)

    @pytest.mark.parametrize("entry", ["80/tcp", "443/tcp"])
    def test_web_port_bypass_is_refused(self, tmp_path, entry):
        # Direct 80/443 would bypass the allowlist proxy entirely — the box
        # user could reach any host on the open web, not just `hosts`.
        data = _manifest(tmp_path, {"egress": {"ports": [entry]}})
        _write(tmp_path, data)
        with pytest.raises(StxError, match="allowlist proxy"):
            Manifest.load(tmp_path)

    def test_old_direct_token_shape_rejected(self, tmp_path):
        data = _manifest(tmp_path, {"egress": {"ports": ["tcp:443"]}})
        _write(tmp_path, data)
        with pytest.raises(StxError, match=r"tcp\|udp"):
            Manifest.load(tmp_path)

    def test_direct_key_rejected(self, tmp_path):
        # `egress.direct:` is gone — `egress.hosts`/`egress.ports` are the only keys.
        data = _manifest(tmp_path, {"egress": {"direct": ["443/tcp"]}})
        _write(tmp_path, data)
        with pytest.raises(StxError, match="unknown key"):
            Manifest.load(tmp_path)


class TestForge:
    def test_unknown_forge_key_rejected(self, tmp_path):
        data = _manifest(
            tmp_path, {"forge": {"kind": "gitlab", "project": "a/b", "bogus": 1}}
        )
        _write(tmp_path, data)
        with pytest.raises(StxError, match="unknown key"):
            Manifest.load(tmp_path)

    def test_kind_must_be_gitlab(self, tmp_path):
        data = _manifest(tmp_path, {"forge": {"kind": "github", "project": "a/b"}})
        _write(tmp_path, data)
        with pytest.raises(StxError, match="gitlab"):
            Manifest.load(tmp_path)

    def test_explicit_kind_and_project_loads(self, tmp_path):
        data = _manifest(
            tmp_path, {"forge": {"kind": "gitlab", "project": "widgetshop/widgetshop"}}
        )
        _write(tmp_path, data)
        m = Manifest.load(tmp_path)
        assert m.forge.kind == "gitlab"
        assert m.forge.project == "widgetshop/widgetshop"

    def test_kind_and_project_default_from_origin(self, tmp_path):
        data = _manifest(tmp_path, {"forge": {}})
        _write(tmp_path, data)
        _init_repo(tmp_path, origin="git@gitlab.com:widgetshop/widgetshop.git")
        m = Manifest.load(tmp_path)
        assert m.forge.kind == "gitlab"
        assert m.forge.project == "widgetshop/widgetshop"

    def test_https_origin_also_defaults(self, tmp_path):
        data = _manifest(tmp_path, {"forge": {}})
        _write(tmp_path, data)
        _init_repo(tmp_path, origin="https://gitlab.com/widgetshop/widgetshop.git")
        m = Manifest.load(tmp_path)
        assert m.forge.project == "widgetshop/widgetshop"

    def test_no_origin_without_override_rejected(self, tmp_path):
        data = _manifest(tmp_path, {"forge": {}})
        _write(tmp_path, data)
        _init_repo(tmp_path)
        with pytest.raises(StxError, match="origin"):
            Manifest.load(tmp_path)

    def test_no_origin_with_explicit_kind_and_project_rejected(self, tmp_path):
        # A checkout at its own root but with no `origin` — a forge token is
        # scoped to the origin project, so there's nothing to pin an explicit
        # kind/project against; refuse rather than mint for an unverifiable one.
        data = _manifest(tmp_path, {"forge": {"kind": "gitlab", "project": "a/b"}})
        _write(tmp_path, data)
        _init_repo(tmp_path)
        with pytest.raises(StxError, match="origin"):
            Manifest.load(tmp_path)

    def test_non_gitlab_origin_without_kind_override_rejected(self, tmp_path):
        data = _manifest(tmp_path, {"forge": {}})
        _write(tmp_path, data)
        _init_repo(tmp_path, origin="git@github.com:widgetshop/widgetshop.git")
        with pytest.raises(StxError, match="infer"):
            Manifest.load(tmp_path)

    def test_package_registry_requires_env(self, tmp_path):
        data = _manifest(
            tmp_path,
            {
                "forge": {
                    "kind": "gitlab",
                    "project": "a/b",
                    "package_registry": {},
                }
            },
        )
        _write(tmp_path, data)
        with pytest.raises(StxError, match="env"):
            Manifest.load(tmp_path)

    def test_package_registry_group_defaults_from_project(self, tmp_path):
        data = _manifest(
            tmp_path,
            {
                "forge": {
                    "kind": "gitlab",
                    "project": "widgetshop/widgetshop",
                    "package_registry": {
                        "env": {"username": "U", "password": "P"},
                    },
                }
            },
        )
        _write(tmp_path, data)
        m = Manifest.load(tmp_path)
        assert m.forge.package_registry.group == "widgetshop"

    def test_package_registry_env_requires_username_and_password(self, tmp_path):
        data = _manifest(
            tmp_path,
            {
                "forge": {
                    "kind": "gitlab",
                    "project": "a/b",
                    "package_registry": {"env": {"username": "U"}},
                }
            },
        )
        _write(tmp_path, data)
        with pytest.raises(StxError, match="password"):
            Manifest.load(tmp_path)

    @pytest.mark.parametrize("name", ["1BAD", "has-dash", "has space", ""])
    def test_package_registry_env_name_must_be_an_identifier(self, tmp_path, name):
        data = _manifest(
            tmp_path,
            {
                "forge": {
                    "kind": "gitlab",
                    "project": "a/b",
                    "package_registry": {"env": {"username": name, "password": "P"}},
                }
            },
        )
        _write(tmp_path, data)
        with pytest.raises(StxError, match="package_registry.env"):
            Manifest.load(tmp_path)

    @pytest.mark.parametrize("name", ["PATH", "GIT_DIR", "STX_ZONE", "HTTPS_PROXY"])
    def test_package_registry_env_name_cannot_be_reserved(self, tmp_path, name):
        # The forge passthrough injects these names into the box environment;
        # a reserved one would let the package credential clobber a key stx
        # sets to confine the box.
        data = _manifest(
            tmp_path,
            {
                "forge": {
                    "kind": "gitlab",
                    "project": "a/b",
                    "package_registry": {"env": {"username": "U", "password": name}},
                }
            },
        )
        _write(tmp_path, data)
        with pytest.raises(StxError, match="reserved"):
            Manifest.load(tmp_path)

    def test_explicit_project_matching_origin_loads(self, tmp_path):
        data = _manifest(
            tmp_path, {"forge": {"kind": "gitlab", "project": "widgetshop/widgetshop"}}
        )
        _write(tmp_path, data)
        _init_repo(tmp_path, origin="git@gitlab.com:widgetshop/widgetshop.git")
        m = Manifest.load(tmp_path)
        assert m.forge.project == "widgetshop/widgetshop"

    def test_explicit_project_not_matching_origin_rejected(self, tmp_path):
        # A malicious manifest naming a project other than this repo's own
        # origin could get a Developer token minted for it — see
        # docs/security.md.
        data = _manifest(
            tmp_path, {"forge": {"kind": "gitlab", "project": "other-org/other-repo"}}
        )
        _write(tmp_path, data)
        _init_repo(tmp_path, origin="git@gitlab.com:widgetshop/widgetshop.git")
        with pytest.raises(
            StxError, match="other-org/other-repo.*widgetshop/widgetshop"
        ):
            Manifest.load(tmp_path)

    def test_enclosing_repos_origin_is_not_this_manifests(self, tmp_path):
        # A manifest in a subdirectory (an example inside another project)
        # doesn't speak for the enclosing checkout's remote.
        _init_repo(tmp_path, origin="git@github.com:someone/enclosing.git")
        nested = tmp_path / "examples" / "shop"
        nested.mkdir(parents=True)
        data = _manifest(
            nested, {"forge": {"kind": "gitlab", "project": "widgetshop/widgetshop"}}
        )
        _write(nested, data)
        assert Manifest.load(nested).forge.project == "widgetshop/widgetshop"

    def test_package_registry_ancestor_group_of_project_accepted(self, tmp_path):
        data = _manifest(
            tmp_path,
            {
                "forge": {
                    "kind": "gitlab",
                    "project": "widgetshop/platform/widgetshop",
                    "package_registry": {
                        "group": "widgetshop/platform",
                        "env": {"username": "U", "password": "P"},
                    },
                }
            },
        )
        _write(tmp_path, data)
        m = Manifest.load(tmp_path)
        assert m.forge.package_registry.group == "widgetshop/platform"

    def test_package_registry_group_equal_to_project_rejected(self, tmp_path):
        data = _manifest(
            tmp_path,
            {
                "forge": {
                    "kind": "gitlab",
                    "project": "widgetshop/widgetshop",
                    "package_registry": {
                        "group": "widgetshop/widgetshop",
                        "env": {"username": "U", "password": "P"},
                    },
                }
            },
        )
        _write(tmp_path, data)
        with pytest.raises(StxError, match="ancestor group"):
            Manifest.load(tmp_path)

    def test_package_registry_unrelated_group_rejected(self, tmp_path):
        data = _manifest(
            tmp_path,
            {
                "forge": {
                    "kind": "gitlab",
                    "project": "widgetshop/widgetshop",
                    "package_registry": {
                        "group": "someone-elses-group",
                        "env": {"username": "U", "password": "P"},
                    },
                }
            },
        )
        _write(tmp_path, data)
        with pytest.raises(StxError, match="ancestor group"):
            Manifest.load(tmp_path)

    def test_package_registry_sibling_subgroup_rejected(self, tmp_path):
        # Shares a top-level group with the project but isn't actually an
        # ancestor of it — a sibling subgroup, not a containing one.
        data = _manifest(
            tmp_path,
            {
                "forge": {
                    "kind": "gitlab",
                    "project": "widgetshop/platform/widgetshop",
                    "package_registry": {
                        "group": "widgetshop/other-platform",
                        "env": {"username": "U", "password": "P"},
                    },
                }
            },
        )
        _write(tmp_path, data)
        with pytest.raises(StxError, match="ancestor group"):
            Manifest.load(tmp_path)


class TestRedisIndexUsage:
    def test_redis_index_in_env_without_redis_true_rejected(self, tmp_path):
        data = _manifest(tmp_path, {"env": {"REDIS_DB": "{redis_index}"}})
        _write(tmp_path, data)
        with pytest.raises(StxError, match="redis_index"):
            Manifest.load(tmp_path)

    def test_redis_index_in_branch_without_redis_true_rejected(self, tmp_path):
        data = _manifest(tmp_path, {"branch": "feat/{user}/{redis_index}"})
        _write(tmp_path, data)
        with pytest.raises(StxError, match="redis_index"):
            Manifest.load(tmp_path)

    def test_redis_index_in_provision_without_redis_true_rejected(self, tmp_path):
        data = _manifest(
            tmp_path, {"provision": {"steps": [["echo", "{redis_index}"]]}}
        )
        _write(tmp_path, data)
        with pytest.raises(StxError, match="redis_index"):
            Manifest.load(tmp_path)

    def test_redis_index_in_databases_without_redis_true_rejected(self, tmp_path):
        data = _manifest(
            tmp_path,
            {"platform": {"postgres": {"databases": ["{db}_{redis_index}"]}}},
        )
        _write(tmp_path, data)
        with pytest.raises(StxError, match="redis_index"):
            Manifest.load(tmp_path)

    def test_redis_index_allowed_when_redis_true(self, tmp_path):
        data = _manifest(
            tmp_path,
            {"platform": {"redis": True}, "env": {"REDIS_DB": "{redis_index}"}},
        )
        _write(tmp_path, data)
        assert Manifest.load(tmp_path).env == {"REDIS_DB": "{redis_index}"}


class TestTemplateGrammarAtLoad:
    """The closed substituter catches a bad template at `Manifest.load()`,
    before any command renders it — unlike `str.format`, which would only
    fail (or silently render something wrong) whenever something downstream
    first calls render_template on that specific field."""

    def test_unknown_variable_in_env_rejected_at_load(self, tmp_path):
        data = _manifest(tmp_path, {"env": {"FOO": "{slgu}"}})
        _write(tmp_path, data)
        with pytest.raises(StxError, match=r"env\.FOO.*unknown variable"):
            Manifest.load(tmp_path)

    def test_unknown_variable_in_branch_rejected_at_load(self, tmp_path):
        data = _manifest(tmp_path, {"branch": "feat/{slgu}"})
        _write(tmp_path, data)
        with pytest.raises(StxError, match=r"branch.*unknown variable"):
            Manifest.load(tmp_path)

    def test_user_var_in_env_rejected_at_load(self, tmp_path):
        # {user} is only resolvable for the branch; anywhere else it would
        # render blank at use time, so it's refused at load.
        data = _manifest(tmp_path, {"env": {"FOO": "u-{user}"}})
        _write(tmp_path, data)
        with pytest.raises(StxError, match=r"env\.FOO.*user"):
            Manifest.load(tmp_path)

    def test_user_var_in_provision_step_rejected_at_load(self, tmp_path):
        data = _manifest(tmp_path, {"provision": {"steps": [["echo", "{user}"]]}})
        _write(tmp_path, data)
        with pytest.raises(StxError, match=r"provision\.steps\[0\]\[1\].*user"):
            Manifest.load(tmp_path)

    def test_user_var_allowed_in_branch(self, tmp_path):
        data = _manifest(tmp_path, {"branch": "feat/{user}/{slug}"})
        _write(tmp_path, data)
        assert Manifest.load(tmp_path).branch == "feat/{user}/{slug}"

    def test_unknown_variable_in_provision_step_rejected_at_load(self, tmp_path):
        data = _manifest(tmp_path, {"provision": {"steps": [["echo", "{slgu}"]]}})
        _write(tmp_path, data)
        with pytest.raises(StxError, match=r"provision\.steps\[0\]\[1\]"):
            Manifest.load(tmp_path)

    def test_unknown_variable_in_database_rejected_at_load(self, tmp_path):
        data = _manifest(
            tmp_path, {"platform": {"postgres": {"databases": ["{db}_{slgu}"]}}}
        )
        _write(tmp_path, data)
        with pytest.raises(StxError, match=r"platform\.postgres\.databases\[0\]"):
            Manifest.load(tmp_path)

    def test_format_spec_rejected_at_load(self, tmp_path):
        # str.format would silently right-pad this; the closed grammar
        # doesn't interpret anything after the variable name.
        data = _manifest(tmp_path, {"env": {"FOO": "{slug:>12}"}})
        _write(tmp_path, data)
        with pytest.raises(StxError, match="unknown variable"):
            Manifest.load(tmp_path)

    def test_attribute_access_rejected_at_load(self, tmp_path):
        data = _manifest(tmp_path, {"env": {"FOO": "{slug.__class__.__name__}"}})
        _write(tmp_path, data)
        with pytest.raises(StxError, match="unknown variable"):
            Manifest.load(tmp_path)

    def test_link_attribute_beyond_internal_rejected_at_load(self, tmp_path):
        data = _manifest(
            tmp_path,
            {"links": {"api": {"route": "api"}}, "env": {"FOO": "{link.api.foo}"}},
        )
        _write(tmp_path, data)
        with pytest.raises(StxError, match="unknown variable"):
            Manifest.load(tmp_path)

    def test_stray_closing_brace_rejected_at_load(self, tmp_path):
        data = _manifest(tmp_path, {"env": {"FOO": "a}b"}})
        _write(tmp_path, data)
        with pytest.raises(StxError, match="unescaped"):
            Manifest.load(tmp_path)

    def test_stray_opening_brace_rejected_at_load(self, tmp_path):
        data = _manifest(tmp_path, {"env": {"FOO": "a{b"}})
        _write(tmp_path, data)
        with pytest.raises(StxError, match="unescaped"):
            Manifest.load(tmp_path)

    def test_escaped_braces_load_fine(self, tmp_path):
        data = _manifest(tmp_path, {"env": {"FOO": "{{not a var}}"}})
        _write(tmp_path, data)
        assert Manifest.load(tmp_path).env == {"FOO": "{{not a var}}"}


class TestRenderTemplate:
    def test_renders_slug_and_db(self):
        assert render_template("{slug}-{db}", slug="fix-447") == "fix-447-stx_fix_447"

    def test_renders_redis_index(self):
        assert render_template("db{redis_index}", slug="x", redis_index=3) == "db3"

    def test_renders_user(self):
        assert render_template("feat/{user}/{slug}", slug="x", user="alex") == (
            "feat/alex/x"
        )

    def test_unknown_variable_raises(self):
        with pytest.raises(StxError, match="unknown variable"):
            render_template("{bogus}", slug="x")

    def test_escaped_braces_render_literally(self):
        assert render_template("{{{slug}}}", slug="x") == "{x}"
        assert render_template("{{not a var}}", slug="x") == "{not a var}"

    def test_format_spec_is_not_interpreted(self):
        with pytest.raises(StxError, match="unknown variable"):
            render_template("{slug:>12}", slug="x")

    def test_attribute_access_is_not_interpreted(self):
        with pytest.raises(StxError, match="unknown variable"):
            render_template("{slug.__class__.__name__}", slug="x")

    def test_stray_unescaped_brace_raises_stx_error_not_a_bare_value_error(self):
        with pytest.raises(StxError, match="unescaped"):
            render_template("a}b", slug="x")

    def test_link_attribute_beyond_internal_raises_stx_error(self, tmp_path):
        declared = {"api": LinkManifest(route="api")}
        with pytest.raises(StxError, match="unknown variable"):
            render_template(
                "{link.api.foo}", slug="x", declared_links=declared, links={}
            )


class TestManifestRendering:
    def test_branch_for(self, tmp_path):
        data = _manifest(tmp_path, {"branch": "feat/{user}/{slug}"})
        _write(tmp_path, data)
        m = Manifest.load(tmp_path)
        assert m.branch_for("fix-447", "alex") == "feat/alex/fix-447"

    def test_render_env(self, tmp_path):
        data = _manifest(
            tmp_path,
            {"platform": {"redis": True}, "env": {"REDIS_DB": "{redis_index}"}},
        )
        _write(tmp_path, data)
        m = Manifest.load(tmp_path)
        assert m.render_env(slug="fix-447", redis_index=3) == {"REDIS_DB": "3"}

    def test_render_databases(self, tmp_path):
        data = _manifest(
            tmp_path, {"platform": {"postgres": {"databases": ["{db}", "{db}_test"]}}}
        )
        _write(tmp_path, data)
        m = Manifest.load(tmp_path)
        assert m.render_databases("fix-447") == ["stx_fix_447", "stx_fix_447_test"]

    def test_render_databases_empty_without_postgres(self, tmp_path):
        _write(tmp_path, _manifest(tmp_path))
        assert Manifest.load(tmp_path).render_databases("fix-447") == []

    def test_render_databases_rejects_unsafe_rendered_name(self, tmp_path):
        data = _manifest(
            tmp_path,
            {"platform": {"postgres": {"databases": ['{db}"; DROP SCHEMA x']}}},
        )
        _write(tmp_path, data)
        m = Manifest.load(tmp_path)
        with pytest.raises(StxError, match=r"\[a-z0-9_\]\+"):
            m.render_databases("fix-447")

    def test_render_databases_rejects_a_name_over_63_bytes(self, tmp_path):
        data = _manifest(tmp_path, {"platform": {"postgres": {"databases": ["{db}"]}}})
        _write(tmp_path, data)
        m = Manifest.load(tmp_path)
        # db_name() prefixes "stx_" (4 bytes) — a 60-char slug pushes the
        # rendered name to 64 bytes, over Postgres's identifier limit.
        with pytest.raises(StxError, match="63-byte"):
            m.render_databases("a" * 60)

    def test_render_provision_steps(self, tmp_path):
        data = _manifest(
            tmp_path,
            {"provision": {"steps": [["python", "-m", "x", "--db", "{db}"]]}},
        )
        _write(tmp_path, data)
        m = Manifest.load(tmp_path)
        assert m.render_provision_steps(slug="fix-447", redis_index=None) == [
            ["python", "-m", "x", "--db", "stx_fix_447"]
        ]

    def test_provision_host_vars_excludes_admin_vars(self, tmp_path):
        data = _manifest(
            tmp_path,
            {
                "provision": {
                    "env": {
                        "DATABASE_USER": "${PGUSER}",
                        "DATABASE_PASSWORD": "${PGPASSWORD}",
                        "DATABASE_APP_PASSWORD": "${DATABASE_APP_PASSWORD}",
                    }
                }
            },
        )
        _write(tmp_path, data)
        m = Manifest.load(tmp_path)
        assert m.provision_host_vars() == ["DATABASE_APP_PASSWORD"]

    def test_render_provision_env_substitutes_admin_and_host_vars(self, tmp_path):
        data = _manifest(
            tmp_path,
            {
                "provision": {
                    "env": {
                        "DATABASE_USER": "${PGUSER}",
                        "DATABASE_APP_PASSWORD": "${DATABASE_APP_PASSWORD}",
                    }
                }
            },
        )
        _write(tmp_path, data)
        m = Manifest.load(tmp_path)
        rendered = m.render_provision_env(
            {
                "PGHOST": "stx-postgres",
                "PGPORT": "5432",
                "PGUSER": "admin",
                "PGPASSWORD": "x",
            },
            {"DATABASE_APP_PASSWORD": "secret"},
        )
        assert rendered == {
            "DATABASE_USER": "admin",
            "DATABASE_APP_PASSWORD": "secret",
        }

    def test_render_provision_env_raises_on_missing_var(self, tmp_path):
        data = _manifest(
            tmp_path,
            {
                "provision": {
                    "env": {"DATABASE_APP_PASSWORD": "${DATABASE_APP_PASSWORD}"}
                }
            },
        )
        _write(tmp_path, data)
        m = Manifest.load(tmp_path)
        with pytest.raises(StxError, match="DATABASE_APP_PASSWORD"):
            m.render_provision_env({}, {})


class TestLinks:
    def test_loads_route_and_default(self, tmp_path):
        data = _manifest(
            tmp_path, {"links": {"api": {"route": "api", "default": "mock"}}}
        )
        _write(tmp_path, data)
        m = Manifest.load(tmp_path)
        assert m.links == {"api": LinkManifest(route="api", default="mock")}

    def test_default_defaults_to_empty_string(self, tmp_path):
        data = _manifest(tmp_path, {"links": {"api": {"route": "api"}}})
        _write(tmp_path, data)
        m = Manifest.load(tmp_path)
        assert m.links["api"].default == ""

    def test_no_links_key_is_empty(self, tmp_path):
        _write(tmp_path, _manifest(tmp_path))
        assert Manifest.load(tmp_path).links == {}

    def test_bad_link_name_rejected(self, tmp_path):
        data = _manifest(tmp_path, {"links": {"My-Api": {"route": "api"}}})
        _write(tmp_path, data)
        with pytest.raises(StxError, match="links"):
            Manifest.load(tmp_path)

    def test_bad_route_rejected(self, tmp_path):
        data = _manifest(tmp_path, {"links": {"api": {"route": "Not_A_Label"}}})
        _write(tmp_path, data)
        with pytest.raises(StxError, match="route"):
            Manifest.load(tmp_path)

    def test_unknown_link_key_rejected(self, tmp_path):
        data = _manifest(tmp_path, {"links": {"api": {"route": "api", "bogus": 1}}})
        _write(tmp_path, data)
        with pytest.raises(StxError, match="links.api"):
            Manifest.load(tmp_path)

    def test_missing_route_rejected(self, tmp_path):
        data = _manifest(tmp_path, {"links": {"api": {"default": "mock"}}})
        _write(tmp_path, data)
        with pytest.raises(StxError, match="route"):
            Manifest.load(tmp_path)


class TestLinkUsage:
    def test_undeclared_link_in_env_rejected(self, tmp_path):
        data = _manifest(tmp_path, {"env": {"API_URL": "{link.api}"}})
        _write(tmp_path, data)
        with pytest.raises(StxError, match="undeclared link"):
            Manifest.load(tmp_path)

    def test_declared_link_in_env_accepted(self, tmp_path):
        data = _manifest(
            tmp_path,
            {
                "links": {"api": {"route": "api"}},
                "env": {"API_URL": "{link.api}"},
            },
        )
        _write(tmp_path, data)
        Manifest.load(tmp_path)  # does not raise

    def test_undeclared_link_internal_form_rejected(self, tmp_path):
        data = _manifest(tmp_path, {"branch": "{link.api.internal}"})
        _write(tmp_path, data)
        with pytest.raises(StxError, match="undeclared link"):
            Manifest.load(tmp_path)


class TestLinkRendering:
    def test_unlinked_declared_name_renders_default(self, tmp_path):
        data = _manifest(
            tmp_path,
            {
                "links": {"api": {"route": "api", "default": "mock"}},
                "env": {
                    "API_URL": "{link.api}",
                    "API_INTERNAL": "{link.api.internal}",
                },
            },
        )
        _write(tmp_path, data)
        m = Manifest.load(tmp_path)
        assert m.render_env(slug="web-1", redis_index=None) == {
            "API_URL": "mock",
            "API_INTERNAL": "mock",
        }

    def test_linked_name_renders_url_and_internal_url(self, tmp_path):
        data = _manifest(
            tmp_path,
            {
                "links": {"api": {"route": "api", "default": "mock"}},
                "env": {
                    "API_URL": "{link.api}",
                    "API_INTERNAL": "{link.api.internal}",
                },
            },
        )
        _write(tmp_path, data)
        m = Manifest.load(tmp_path)
        resolved = {
            "api": LinkRecord(
                target="be-1", route="api", port=8080, container="stx-be-1--app"
            )
        }
        assert m.render_env(slug="web-1", redis_index=None, links=resolved) == {
            "API_URL": "http://api.be-1.localhost",
            "API_INTERNAL": "http://stx-be-1--app:8080",
        }

    def test_branch_for_renders_link(self, tmp_path):
        data = _manifest(
            tmp_path,
            {
                "links": {"api": {"route": "api"}},
                "branch": "feat/{user}/{slug}-{link.api}",
            },
        )
        _write(tmp_path, data)
        m = Manifest.load(tmp_path)
        resolved = {
            "api": LinkRecord(
                target="be-1", route="api", port=8080, container="stx-be-1--app"
            )
        }
        assert (
            m.branch_for("web-1", "alex", links=resolved)
            == "feat/alex/web-1-http://api.be-1.localhost"
        )
