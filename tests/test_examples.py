"""Exercises the synthetic example repo (examples/widgetshop) against the
real manifest loader — the one place the test suite loads a `sandbox.yaml`
straight off disk end to end, rather than constructing a Manifest in memory."""

from pathlib import Path

from stx.manifest import Manifest

_WIDGETSHOP = Path(__file__).parent.parent / "examples" / "widgetshop"


class TestWidgetshopManifest:
    def test_loads_without_error(self):
        Manifest.load(_WIDGETSHOP)

    def test_defaults_resolved_from_the_repo(self):
        manifest = Manifest.load(_WIDGETSHOP)
        assert manifest.name == "app"
        assert manifest.compose == "compose.yaml"
        assert manifest.user is None  # defaults to the Dockerfile's `USER devuser`

    def test_rebuild_on_merges_override_with_dockerfile_defaults(self):
        manifest = Manifest.load(_WIDGETSHOP)
        assert ".devcontainer/Dockerfile" in manifest.rebuild_on
        assert "pyproject.toml" in manifest.rebuild_on  # from the Dockerfile's COPY
        assert (
            "uv.lock" in manifest.rebuild_on
        )  # explicit override (glob in the Dockerfile)

    def test_publish_names_the_route(self):
        manifest = Manifest.load(_WIDGETSHOP)
        assert manifest.publish == "api"

    def test_does_not_need_stx_postgres_user(self):
        # provisioning gets PGUSER/PGPASSWORD from stx itself.
        manifest = Manifest.load(_WIDGETSHOP)
        assert "STX_POSTGRES_USER" not in manifest.provision_host_vars()

    def test_routes(self):
        manifest = Manifest.load(_WIDGETSHOP)
        assert manifest.routes == {"api": 8080}

    def test_platform(self):
        manifest = Manifest.load(_WIDGETSHOP)
        assert manifest.platform.redis is True
        assert manifest.platform.otel is True
        assert manifest.platform.postgres.databases == ("{db}", "{db}_test")

    def test_forge_has_fictional_values(self):
        manifest = Manifest.load(_WIDGETSHOP)
        assert manifest.forge.project == "widgetshop/widgetshop"
        assert manifest.forge.package_registry.group == "widgetshop"

    def test_render_databases(self):
        manifest = Manifest.load(_WIDGETSHOP)
        assert manifest.render_databases("fix-447") == [
            "stx_fix_447",
            "stx_fix_447_test",
        ]

    def test_render_provision_steps(self):
        manifest = Manifest.load(_WIDGETSHOP)
        steps = manifest.render_provision_steps(slug="fix-447", redis_index=3)
        assert steps[0] == ["bash", "scripts/box-provision.sh"]
        assert "stx_fix_447" in steps[1]

    def test_branch_for(self):
        manifest = Manifest.load(_WIDGETSHOP)
        assert manifest.branch_for("fix-447", "alex") == "feat/alex/fix-447"

    def test_referenced_scripts_exist_and_are_executable(self):
        for rel in ("scripts/box-init.sh", "scripts/box-provision.sh"):
            path = _WIDGETSHOP / rel
            assert path.is_file()
            assert path.stat().st_mode & 0o111
