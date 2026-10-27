from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, override

if TYPE_CHECKING:
    import argparse

import yaml

from .. import overlay
from ..command import Command
from ..core.paths import repo_cache_id
from ..manifest import COMPOSE_LOOKUP_ORDER
from ..repo import git_toplevel

#: Compose's own per-directory lookup order, tried at the root first, then
#: the two conventional devcontainer homes.
_COMPOSE_SEARCH_DIRS = ("", ".devcontainer", "docker")

_IMAGE_REQUIREMENTS_LINE = (
    "image requirements: bash, iptables, ip6tables, ss, tinyproxy, ulogd, jq, "
    "git, su, a non-root user with a home dir, and the harness binary on "
    "that user's PATH — see docs/box-contract.md"
)

#: File whose presence implies these egress hosts — see docs/manifest.md (egress).
_ECOSYSTEM_EGRESS_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("package.json", ("registry.npmjs.org",)),
    ("uv.lock", ("pypi.org", "files.pythonhosted.org")),
    ("pyproject.toml", ("pypi.org", "files.pythonhosted.org")),
)

#: Checked in this order; the first with both a GitLab reference and an
#: `${ENV}` token wins.
_PRIVATE_REGISTRY_FILES = (".npmrc", "pyproject.toml")

_FROM_RE = re.compile(r"^\s*FROM\s+\S+", re.IGNORECASE | re.MULTILINE)
_USER_RE = re.compile(r"^\s*USER\s+(\S+)", re.IGNORECASE | re.MULTILINE)
_EXPOSE_RE = re.compile(r"^\s*EXPOSE\s+(\d+)", re.IGNORECASE | re.MULTILINE)
_ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::?-[^}]*)?\}")


@dataclass
class _ComposeFindings:
    path: Path | None = None
    rel_path: str | None = None
    is_default_location: bool = True
    box_name: str | None = None
    service: dict = field(default_factory=dict)
    violations: list[str] = field(default_factory=list)


@dataclass
class _DockerfileFindings:
    path: Path | None = None
    user: str | None = None
    stage_count: int = 0
    exposed_port: int | None = None


@dataclass
class _PackageRegistryFindings:
    source: str
    password_var: str
    username_var: str


class SandboxInitCommand(Command):
    """Write a `sandbox.yaml` starter inferred from the repo: the compose
    file Compose would find, the Dockerfile it builds, ecosystem egress
    hints, and a private-registry guess — see docs/manifest.md. Every
    inferred key still wants a human read before `stx new` runs for real."""

    @classmethod
    @override
    def name(cls) -> str:
        return "init"

    @classmethod
    @override
    def help(cls) -> str:
        return "Write a sandbox.yaml starter inferred from the repo"

    @override
    def add_parser(self, subparsers) -> argparse.ArgumentParser:
        return subparsers.add_parser(self.name(), help=self.help())

    @override
    def _execute(self, args: argparse.Namespace) -> int | None:
        root = git_toplevel(Path.cwd())
        if root is None:
            print("error: not in a git checkout", file=sys.stderr)
            return 1

        manifest_path = root / "sandbox.yaml"
        if manifest_path.exists():
            print(
                f"error: {manifest_path} already exists — remove it first if you "
                "want to start over",
                file=sys.stderr,
            )
            return 1

        compose = _find_compose(root)
        dockerfile = _find_dockerfile(compose)
        egress_hosts = _ecosystem_egress_hosts(root)
        package_registry = _find_package_registry(root)
        route_port = _route_port_suggestion(compose, dockerfile)

        manifest_path.write_text(
            _render_manifest(
                compose=compose,
                egress_hosts=egress_hosts,
                package_registry=package_registry,
                route_port=route_port,
            )
        )
        print(f"wrote {manifest_path}")
        _print_report(
            compose=compose,
            dockerfile=dockerfile,
            egress_hosts=egress_hosts,
            package_registry=package_registry,
            route_port=route_port,
        )
        cache_id_problem = overlay.repo_cache_id_problem(repo_cache_id(root))
        if cache_id_problem:
            print(f"\nwarning: {cache_id_problem} before a shared-cache volume works")
        return None


def _find_compose(root: Path) -> _ComposeFindings:
    for subdir in _COMPOSE_SEARCH_DIRS:
        directory = root / subdir if subdir else root
        for filename in COMPOSE_LOOKUP_ORDER:
            path = directory / filename
            if path.exists():
                return _load_compose(root, path, is_default_location=subdir == "")
    return _ComposeFindings()


def _load_compose(
    root: Path, path: Path, *, is_default_location: bool
) -> _ComposeFindings:
    rel_path = str(path.relative_to(root))
    try:
        doc = yaml.safe_load(path.read_text())
    except yaml.YAMLError:
        doc = None
    if not isinstance(doc, dict):
        return _ComposeFindings(
            path=path, rel_path=rel_path, is_default_location=is_default_location
        )

    services = doc.get("services")
    box_name: str | None = None
    service: dict = {}
    if isinstance(services, dict) and len(services) == 1:
        ((box_name, service),) = services.items()
        service = service or {}

    return _ComposeFindings(
        path=path,
        rel_path=rel_path,
        is_default_location=is_default_location,
        box_name=box_name,
        service=service,
        violations=_repo_compose_violations(doc, repo_id=repo_cache_id(root)),
    )


def _repo_compose_violations(doc: dict, *, repo_id: str) -> list[str]:
    """Best-effort, no-docker approximation of the real allow-list check
    (see docs/box-contract.md) — good enough to warn at `init` time, before
    `new`'s own check (which normalizes the file through `docker compose
    config` first) runs for real. Lists every refused key, unlike that
    check's fail-on-first raise, since `init` wants to report them all at
    once."""
    violations: list[str] = []
    for key in doc:
        if key in overlay.TOP_LEVEL_ALLOWED or key.startswith("x-"):
            continue
        # A repo-authored top-level `name:` is refused: stx pins the compose
        # project itself (see lifecycle.provision).
        violations.append(key)

    services = doc.get("services")
    if not isinstance(services, dict) or len(services) != 1:
        violations.append(
            f"services (expected exactly one, found {sorted(services or {})})"
        )
        return violations

    ((name, service),) = services.items()
    service = service or {}
    for key in service:
        if key not in overlay.SERVICE_ALLOWED:
            violations.append(f"{name}.{key}")

    build = service.get("build")
    if isinstance(build, dict):
        for key in build:
            if key not in overlay.BUILD_ALLOWED:
                violations.append(f"{name}.build.{key}")

    for vol_name, vol_def in (doc.get("volumes") or {}).items():
        vol_def = vol_def or {}
        if vol_def.get("external"):
            violations.append(f"volumes.{vol_name}.external")
        if vol_def.get("driver_opts"):
            violations.append(f"volumes.{vol_name}.driver_opts")
        if vol_def.get("labels"):
            violations.append(f"volumes.{vol_name}.labels")
        driver = vol_def.get("driver")
        if driver is not None and driver != "local":
            violations.append(f"volumes.{vol_name}.driver")
        name = vol_def.get("name")
        if name is not None and not name.startswith(
            overlay.shared_cache_prefix(repo_id)
        ):
            violations.append(f"volumes.{vol_name}.name")

    return violations


def _find_dockerfile(compose: _ComposeFindings) -> _DockerfileFindings:
    if compose.path is None:
        return _DockerfileFindings()
    build = compose.service.get("build")
    if isinstance(build, str):
        build = {"context": build}
    if not isinstance(build, dict):
        return _DockerfileFindings()

    context = (compose.path.parent / build.get("context", ".")).resolve()
    dockerfile = context / build.get("dockerfile", "Dockerfile")
    if not dockerfile.exists():
        return _DockerfileFindings()

    text = dockerfile.read_text()
    users = _USER_RE.findall(text)
    expose_match = _EXPOSE_RE.search(text)
    return _DockerfileFindings(
        path=dockerfile,
        user=users[-1] if users else None,
        stage_count=len(_FROM_RE.findall(text)),
        exposed_port=int(expose_match.group(1)) if expose_match else None,
    )


def _ecosystem_egress_hosts(root: Path) -> list[str]:
    hosts: list[str] = []
    for filename, hostnames in _ECOSYSTEM_EGRESS_HINTS:
        if not (root / filename).exists():
            continue
        for host in hostnames:
            if host not in hosts:
                hosts.append(host)
    return hosts


def _find_package_registry(root: Path) -> _PackageRegistryFindings | None:
    for filename in _PRIVATE_REGISTRY_FILES:
        path = root / filename
        if not path.exists():
            continue
        text = path.read_text()
        if "gitlab" not in text.lower():
            continue
        match = _ENV_VAR_RE.search(text)
        if not match:
            continue
        password_var = match.group(1)
        return _PackageRegistryFindings(
            source=filename,
            password_var=password_var,
            username_var=_username_var_for(password_var),
        )
    return None


def _username_var_for(password_var: str) -> str:
    for suffix in ("_PASSWORD", "_TOKEN"):
        if password_var.endswith(suffix):
            return password_var[: -len(suffix)] + "_USERNAME"
    return f"{password_var}_USERNAME"


def _route_port_suggestion(
    compose: _ComposeFindings, dockerfile: _DockerfileFindings
) -> int | None:
    port = _compose_port(compose.service)
    return port if port is not None else dockerfile.exposed_port


def _compose_port(service: dict) -> int | None:
    for entry in service.get("ports") or []:
        if isinstance(entry, dict) and "target" in entry:
            try:
                return int(entry["target"])
            except (TypeError, ValueError):
                continue
        elif isinstance(entry, int):
            return entry
        elif isinstance(entry, str):
            tail = entry.split(":")[-1].split("/")[0]
            if tail.isdigit():
                return int(tail)
    return None


def _minimal_compose_shape(box_name: str, service: dict) -> list[str]:
    # Paths in the existing build block are relative to that file, and the minimal file
    # goes next to it, so they carry over unchanged.
    build = service.get("build")
    if isinstance(build, str):
        build = {"context": build}
    if not isinstance(build, dict):
        build = {"context": "."}
    build_lines = [
        f"      {key}: {build[key]}"
        for key in ("context", "dockerfile", "target")
        if build.get(key) is not None
    ]
    return [
        "services:",
        f"  {box_name}:",
        "    build:",
        *build_lines,
        "    environment:",
        "      KEY: value",
    ]


def _render_manifest(
    *,
    compose: _ComposeFindings,
    egress_hosts: list[str],
    package_registry: _PackageRegistryFindings | None,
    route_port: int | None,
) -> str:
    lines = [
        "schema: 1",
        "# name, image, and rebuild_on default from the repo (the compose file's",
        "# sole service, <repo>-box, the Dockerfile + what it COPY/ADDs) — override",
        "# any of them here only if the default is wrong for this repo.",
    ]
    if compose.rel_path and not compose.is_default_location and not compose.violations:
        lines.append(f"compose: {compose.rel_path}")
    lines.append("")

    if package_registry is not None:
        lines.append("# the package-registry credential below exists only at runtime —")
        lines.append(
            "# setup must install dependencies (e.g. npm ci / uv sync), not the"
        )
        lines.append("# Dockerfile build")
    lines.append("setup: []       # repo-relative scripts, run in-box from /workspace")

    if route_port is not None:
        box = compose.box_name or "app"
        lines.append(
            f"# routes: {{{box}: {route_port}}}  # <slug>.localhost -> {box}:{route_port}"
        )
        lines.append(
            f"# publish: {box}  # names the route published on the tailnet; omit for none"
        )
    else:
        lines.append(
            "# publish: <route>  # names the route published on the tailnet; omit for none"
        )
    lines.append("")

    if egress_hosts:
        hosts_yaml = ", ".join(egress_hosts)
        lines.append("egress:")
        lines.append(f"  hosts: [{hosts_yaml}]")
        lines.append("")

    if package_registry is not None:
        lines.append("forge:")
        lines.append("  package_registry:")
        lines.append(
            f"    env: {{username: {package_registry.username_var}, "
            f"password: {package_registry.password_var}}}"
        )
        lines.append("")

    lines.append(
        "# see docs/manifest.md for the full field list (platform, provision, "
        "env, publish, forge)"
    )
    return "\n".join(lines).rstrip() + "\n"


def _print_report(
    *,
    compose: _ComposeFindings,
    dockerfile: _DockerfileFindings,
    egress_hosts: list[str],
    package_registry: _PackageRegistryFindings | None,
    route_port: int | None,
) -> None:
    if compose.path is None:
        print("compose: none found — nothing to inspect for a Dockerfile or routes")
    elif compose.violations:
        print(
            f"{compose.rel_path} can't be the sandbox compose file as it stands — "
            f"refused key(s): {', '.join(compose.violations)}"
        )
        print("  make a minimal one (build and environment only) next to it, e.g.:")
        for line in _minimal_compose_shape(compose.box_name or "app", compose.service):
            print(f"    {line}")
    elif compose.is_default_location:
        print(f"compose: {compose.rel_path} (default location, passes the allow-list)")
    else:
        print(f"compose: {compose.rel_path} (passes the allow-list)")

    if dockerfile.path is not None:
        print(f"Dockerfile: {dockerfile.path}")
        if dockerfile.user is None:
            print(
                "  note: no USER instruction — stx needs a non-root user; "
                "see docs/box-contract.md"
            )
        elif dockerfile.user == "root":
            print(
                "  note: USER root — stx needs a non-root user; "
                "see docs/box-contract.md"
            )
        print(f"  {_IMAGE_REQUIREMENTS_LINE}")
        if dockerfile.stage_count > 1:
            print(
                f"  suggestion: this Dockerfile has {dockerfile.stage_count} build "
                "stages — consider a dedicated sandbox stage (box tools + harness) "
                "rather than reusing one of the app's own stages"
            )

    if egress_hosts:
        print(f"egress.hosts inferred: {', '.join(egress_hosts)}")

    if package_registry is not None:
        print(
            f"forge.package_registry inferred from {package_registry.source} "
            f"(password: {package_registry.password_var})"
        )

    print()
    print("next steps:")
    print("  - review the inferred keys in sandbox.yaml")
    if compose.violations:
        print("  - write the minimal compose file suggested above, then set `compose:`")
    if route_port is not None:
        print("  - uncomment routes/publish if this app serves HTTP")
    print("  - run `stx new <slug>` to provision a sandbox")
