import subprocess
from pathlib import Path

import yaml

from stx.commands.init import SandboxInitCommand, _repo_compose_violations


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, capture_output=True, check=True)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


class TestSandboxInitCommand:
    def test_writes_starter_manifest_at_checkout_root(self, monkeypatch, tmp_path):
        _init_repo(tmp_path)
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        monkeypatch.chdir(nested)

        result = SandboxInitCommand().execute(None)

        assert result is None
        manifest = tmp_path / "sandbox.yaml"
        assert manifest.exists()
        assert "schema: 1" in manifest.read_text()

    def test_refuses_to_overwrite_existing_manifest(
        self, monkeypatch, tmp_path, capsys
    ):
        _init_repo(tmp_path)
        (tmp_path / "sandbox.yaml").write_text("schema: 1\ncustom: true\n")
        monkeypatch.chdir(tmp_path)

        result = SandboxInitCommand().execute(None)

        assert result == 1
        assert "already exists" in capsys.readouterr().err
        assert "custom: true" in (tmp_path / "sandbox.yaml").read_text()

    def test_errors_outside_a_git_checkout(self, monkeypatch, tmp_path, capsys):
        monkeypatch.chdir(tmp_path)

        result = SandboxInitCommand().execute(None)

        assert result == 1
        assert "not in a git checkout" in capsys.readouterr().err

    def test_never_restates_the_branch_default(self, monkeypatch, tmp_path):
        # manifest.md's own documented default is "sandbox/{slug}" — the old
        # starter template wrote "feat/{user}/{slug}" instead, contradicting it.
        _init_repo(tmp_path)
        monkeypatch.chdir(tmp_path)

        SandboxInitCommand().execute(None)

        assert "branch:" not in (tmp_path / "sandbox.yaml").read_text()

    def test_bare_repo_output_still_parses_as_valid_manifest_yaml(
        self, monkeypatch, tmp_path
    ):
        _init_repo(tmp_path)
        monkeypatch.chdir(tmp_path)

        SandboxInitCommand().execute(None)

        doc = yaml.safe_load((tmp_path / "sandbox.yaml").read_text())
        assert doc == {"schema": 1, "setup": []}

    def test_warns_when_the_repo_directory_name_cant_scope_a_cache(
        self, monkeypatch, tmp_path, capsys
    ):
        repo = tmp_path / "app--web"
        repo.mkdir()
        _init_repo(repo)
        monkeypatch.chdir(repo)

        SandboxInitCommand().execute(None)

        out = capsys.readouterr().out
        assert "app--web" in out
        assert "rename the directory" in out

    def test_in_sandbox_box_guard(self, monkeypatch, tmp_path, capsys):
        _init_repo(tmp_path)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("STX_ZONE", "trusted")

        result = SandboxInitCommand().execute(None)

        assert result == 1
        assert "host-only" in capsys.readouterr().err
        assert not (tmp_path / "sandbox.yaml").exists()


def _python_backend_repo(root: Path) -> None:
    """A Flask-ish repo: root-level compliant compose, single-stage
    Dockerfile with a USER and EXPOSE, a uv-managed pyproject.toml naming a
    GitLab package index with a ${TOKEN}."""
    _write(
        root / "compose.yaml",
        """\
services:
  app:
    build:
      context: .
      dockerfile: Dockerfile
    environment:
      APP_ENV: local
""",
    )
    _write(
        root / "Dockerfile",
        """\
FROM python:3.12-slim
RUN useradd -m appuser
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN pip install uv && uv sync --frozen
COPY . .
USER appuser
EXPOSE 8000
CMD ["python", "app.py"]
""",
    )
    _write(
        root / "pyproject.toml",
        """\
[project]
name = "backend"

[[tool.uv.index]]
name = "internal"
url = "https://gitlab.example.com/api/v4/projects/123/packages/pypi/simple"
# auth: ${UV_INDEX_INTERNAL_PASSWORD}
""",
    )
    _write(root / "uv.lock", "")


class TestPythonBackendShape:
    def _run(self, monkeypatch, tmp_path, capsys):
        _init_repo(tmp_path)
        _python_backend_repo(tmp_path)
        monkeypatch.chdir(tmp_path)

        result = SandboxInitCommand().execute(None)

        manifest_text = (tmp_path / "sandbox.yaml").read_text()
        out = capsys.readouterr().out
        return result, manifest_text, out

    def test_root_compose_is_the_default_location_no_key_written(
        self, monkeypatch, tmp_path, capsys
    ):
        result, manifest_text, out = self._run(monkeypatch, tmp_path, capsys)

        assert result is None
        assert "compose:" not in manifest_text
        assert "default location" in out

    def test_egress_hosts_inferred_from_pypi_ecosystem_files(
        self, monkeypatch, tmp_path, capsys
    ):
        _, manifest_text, out = self._run(monkeypatch, tmp_path, capsys)

        doc = yaml.safe_load(manifest_text)
        assert sorted(doc["egress"]["hosts"]) == ["files.pythonhosted.org", "pypi.org"]
        assert "pypi.org" in out

    def test_package_registry_inferred_from_pyproject_toml(
        self, monkeypatch, tmp_path, capsys
    ):
        _, manifest_text, out = self._run(monkeypatch, tmp_path, capsys)

        doc = yaml.safe_load(manifest_text)
        env = doc["forge"]["package_registry"]["env"]
        assert env["password"] == "UV_INDEX_INTERNAL_PASSWORD"
        assert env["username"] == "UV_INDEX_INTERNAL_USERNAME"
        assert "pyproject.toml" in out

    def test_setup_reminder_mentions_runtime_only_token(
        self, monkeypatch, tmp_path, capsys
    ):
        _, manifest_text, _ = self._run(monkeypatch, tmp_path, capsys)

        assert "exists only at runtime" in manifest_text
        assert "setup: []" in manifest_text

    def test_dockerfile_user_present_gets_no_warning_note(
        self, monkeypatch, tmp_path, capsys
    ):
        _, _, out = self._run(monkeypatch, tmp_path, capsys)

        assert "no USER instruction" not in out
        assert "USER root" not in out

    def test_image_requirements_line_always_printed(
        self, monkeypatch, tmp_path, capsys
    ):
        _, _, out = self._run(monkeypatch, tmp_path, capsys)

        assert "image requirements:" in out
        assert "docs/box-contract.md" in out

    def test_single_stage_dockerfile_gets_no_multi_purpose_suggestion(
        self, monkeypatch, tmp_path, capsys
    ):
        _, _, out = self._run(monkeypatch, tmp_path, capsys)

        assert "dedicated sandbox stage" not in out

    def test_route_suggestion_commented_from_expose(
        self, monkeypatch, tmp_path, capsys
    ):
        _, manifest_text, _ = self._run(monkeypatch, tmp_path, capsys)

        assert "# routes: {app: 8000}" in manifest_text
        doc = yaml.safe_load(manifest_text)
        assert "routes" not in doc  # commented, not a live key

    def test_route_suggestion_shows_bare_hostname(self, monkeypatch, tmp_path, capsys):
        _, manifest_text, _ = self._run(monkeypatch, tmp_path, capsys)

        assert "# <slug>.localhost -> app:8000" in manifest_text
        assert "app.<slug>.localhost" not in manifest_text

    def test_next_steps_printed(self, monkeypatch, tmp_path, capsys):
        _, _, out = self._run(monkeypatch, tmp_path, capsys)

        assert "next steps:" in out
        assert "stx new <slug>" in out


def _node_frontend_repo(root: Path) -> None:
    """A frontend devcontainer trial shape: compose lives under docker/ (not
    the default root lookup) and declares `ports:`, which the allow-list
    refuses outright; the Dockerfile is multi-stage with no USER at all;
    package.json plus a GitLab .npmrc with a ${TOKEN}."""
    _write(
        root / "docker" / "compose.yaml",
        """\
services:
  web:
    build:
      context: ..
      dockerfile: docker/Dockerfile
    ports:
      - "3000:3000"
    environment:
      NODE_ENV: development
""",
    )
    _write(
        root / "docker" / "Dockerfile",
        """\
FROM node:20 AS build
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM node:20
WORKDIR /app
COPY --from=build /app ./
EXPOSE 3000
CMD ["npm", "start"]
""",
    )
    _write(root / "package.json", '{"name": "frontend"}\n')
    _write(
        root / ".npmrc",
        "//gitlab.example.com/api/v4/packages/npm/:_authToken=${GITLAB_TOKEN}\n"
        "@myorg:registry=https://gitlab.example.com/api/v4/packages/npm/\n",
    )


class TestNodeFrontendShape:
    def _run(self, monkeypatch, tmp_path, capsys):
        _init_repo(tmp_path)
        _node_frontend_repo(tmp_path)
        monkeypatch.chdir(tmp_path)

        result = SandboxInitCommand().execute(None)

        manifest_text = (tmp_path / "sandbox.yaml").read_text()
        out = capsys.readouterr().out
        return result, manifest_text, out

    def test_finds_compose_under_docker_dir(self, monkeypatch, tmp_path, capsys):
        result, _, out = self._run(monkeypatch, tmp_path, capsys)

        assert result is None
        assert "docker/compose.yaml" in out

    def test_ports_violation_refuses_to_write_compose_key(
        self, monkeypatch, tmp_path, capsys
    ):
        _, manifest_text, out = self._run(monkeypatch, tmp_path, capsys)

        assert "compose:" not in manifest_text
        assert "can't be the sandbox compose file as it stands" in out
        assert "web.ports" in out

    def test_violation_report_suggests_a_minimal_compose_shape(
        self, monkeypatch, tmp_path, capsys
    ):
        _, _, out = self._run(monkeypatch, tmp_path, capsys)

        assert "minimal one (build and environment only)" in out
        assert "context: .." in out
        assert "dockerfile: docker/Dockerfile" in out
        assert "environment:" in out

    def test_multi_stage_dockerfile_without_user_gets_both_notes(
        self, monkeypatch, tmp_path, capsys
    ):
        _, _, out = self._run(monkeypatch, tmp_path, capsys)

        assert "no USER instruction" in out
        assert "dedicated sandbox stage" in out

    def test_npm_egress_host_inferred(self, monkeypatch, tmp_path, capsys):
        _, manifest_text, out = self._run(monkeypatch, tmp_path, capsys)

        doc = yaml.safe_load(manifest_text)
        assert doc["egress"]["hosts"] == ["registry.npmjs.org"]
        assert "registry.npmjs.org" in out

    def test_package_registry_inferred_from_npmrc(self, monkeypatch, tmp_path, capsys):
        _, manifest_text, out = self._run(monkeypatch, tmp_path, capsys)

        doc = yaml.safe_load(manifest_text)
        env = doc["forge"]["package_registry"]["env"]
        assert env["password"] == "GITLAB_TOKEN"
        assert env["username"] == "GITLAB_USERNAME"
        assert ".npmrc" in out

    def test_route_suggestion_from_dockerfile_expose_since_compose_was_refused(
        self, monkeypatch, tmp_path, capsys
    ):
        _, manifest_text, _ = self._run(monkeypatch, tmp_path, capsys)

        assert "# routes: {web: 3000}" in manifest_text


class TestRepoComposeViolationsVolumes:
    """`_repo_compose_violations` scans the raw repo-authored YAML (never a
    `docker compose config` capture), so an unnamed volume never carries a
    `name:` key here — unlike `overlay.check_repo_compose`'s normalized
    input, there's no Compose-filled default to tell apart from an explicit
    override."""

    def _doc(self, volume_def: dict) -> dict:
        return {
            "services": {"app": {"build": {"context": "."}}},
            "volumes": {"cache": volume_def},
        }

    def _violations(self, doc: dict) -> list[str]:
        return _repo_compose_violations(doc, repo_id="myrepo")

    def test_no_violations_for_an_unnamed_volume(self):
        assert self._violations(self._doc({})) == []

    def test_explicit_shared_cache_name_is_accepted(self):
        assert self._violations(self._doc({"name": "stx-cache-myrepo--npm"})) == []

    def test_explicit_name_without_the_repo_scoped_prefix_is_a_violation(self):
        # The bare `stx-cache-` base isn't enough — it must carry this repo's id.
        assert "volumes.cache.name" in self._violations(
            self._doc({"name": "stx-cache-npm"})
        )

    def test_a_prefix_repos_cache_name_is_a_violation_for_a_longer_named_repo(self):
        # Repo `app` may not name repo `app-web`'s cache: `stx-cache-app--` is
        # not a prefix of `stx-cache-app-web--node`.
        assert "volumes.cache.name" in _repo_compose_violations(
            self._doc({"name": "stx-cache-app-web--node"}), repo_id="app"
        )

    def test_explicit_name_without_prefix_is_a_violation(self):
        assert "volumes.cache.name" in self._violations(
            self._doc({"name": "stx-postgres-data"})
        )

    def test_labels_is_a_violation(self):
        assert "volumes.cache.labels" in self._violations(
            self._doc({"labels": {"foo": "bar"}})
        )

    def test_non_local_driver_is_a_violation(self):
        assert "volumes.cache.driver" in self._violations(self._doc({"driver": "nfs"}))

    def test_explicit_local_driver_is_accepted(self):
        assert self._violations(self._doc({"driver": "local"})) == []

    def test_repo_authored_top_level_name_is_a_violation(self):
        assert "name" in self._violations(
            {"name": "stx-redis", "services": {"app": {"build": {"context": "."}}}}
        )
