"""Typed loader for sandbox.yaml, schema v1 — see docs/manifest.md."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import urllib.parse
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import yaml

from .core.box_env import reserved_env_names
from .core.names import db_name, validate_box_name
from .errors import StxError

if TYPE_CHECKING:
    from .core.registry import LinkRecord

_CompletedProcessRunner = Callable[..., "subprocess.CompletedProcess[str]"]

_SCHEMA = 1

_DNS_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_PORT_TOKEN_RE = re.compile(r"^(\*|\d+)/(tcp|udp)$")
_HOST_ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_BOX_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]*$")
_ENV_VAR_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_DB_NAME_RE = re.compile(r"^[a-z0-9_]+$")
_LINK_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_LINK_TEMPLATE_RE = re.compile(r"\{link\.([a-z][a-z0-9_]*)(?:\.internal)?\}")

#: The template grammar's entire variable vocabulary — see docs/manifest.md
#: (Template variables). Anything else inside `{...}` (a format spec, attribute
#: access, an unrecognized name) is a closed-grammar violation, not interpreted.
_TEMPLATE_VAR_RE = re.compile(
    r"^(?:slug|db|redis_index|user|link\.[a-z][a-z0-9_]*(?:\.internal)?)$"
)
_TEMPLATE_LINK_VAR_RE = re.compile(r"^link\.([a-z][a-z0-9_]*)(\.internal)?$")

_IMAGE_TAG_INVALID_RE = re.compile(r"[^a-z0-9._-]+")
_IMAGE_TAG_SEPARATOR_RUN_RE = re.compile(r"[._-]{2,}")
#: Bytes of the common-git-dir-path hash kept in the default image tag —
#: enough to make two checkouts sharing a dirname collide only by extreme
#: chance, short enough to stay readable.
_IMAGE_TAG_HASH_LEN = 8

#: Public: `init`'s own compose-file search (root, then `.devcontainer/`,
#: then `docker/`) reuses this same per-directory order.
COMPOSE_LOOKUP_ORDER = (
    "compose.yaml",
    "compose.yml",
    "docker-compose.yaml",
    "docker-compose.yml",
)

_FORGE_KINDS = ("gitlab",)

#: Names `stx` supplies to every provisioning run itself (libpq's own names
#: for the platform admin connection) — never pulled from the host secret
#: source, so a manifest referencing one needs nothing loaded for it.
PROVISION_ADMIN_VARS = ("PGHOST", "PGPORT", "PGUSER", "PGPASSWORD")

_COPY_ADD_RE = re.compile(r"^(COPY|ADD)\s+(.*)$", re.IGNORECASE)

#: Postgres's own unquoted-identifier limit.
_MAX_IDENTIFIER_BYTES = 63


@dataclass(frozen=True)
class PostgresManifest:
    databases: tuple[str, ...]


@dataclass(frozen=True)
class PlatformManifest:
    postgres: PostgresManifest | None = None
    redis: bool = False
    otel: bool = False


@dataclass(frozen=True)
class ProvisionManifest:
    steps: tuple[tuple[str, ...], ...] = ()
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class EgressManifest:
    hosts: tuple[str, ...] = ()
    ports: tuple[str, ...] = ()


@dataclass(frozen=True)
class PackageRegistryManifest:
    group: str
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ForgeManifest:
    kind: str
    project: str
    package_registry: PackageRegistryManifest | None = None


@dataclass(frozen=True)
class LinkManifest:
    route: str
    default: str = ""


class _LinkValue(str):
    """Formats as the link's browser URL (`{link.name}`); `.internal` holds
    the in-box URL (`{link.name.internal}`) — see docs/manifest.md (links)."""

    def __new__(cls, url: str, internal_url: str) -> _LinkValue:
        self = super().__new__(cls, url)
        self.internal = internal_url
        return self


def _link_namespace(
    declared: dict[str, LinkManifest], resolved: Mapping[str, LinkRecord]
) -> SimpleNamespace:
    values = {}
    for name, link in declared.items():
        r = resolved.get(name)
        if r is None:
            values[name] = _LinkValue(link.default, link.default)
        else:
            values[name] = _LinkValue(r.url, r.internal_url)
    return SimpleNamespace(**values)


@dataclass(frozen=True)
class Manifest:
    schema: int
    name: str
    compose: str
    image: str
    rebuild_on: tuple[str, ...] = ()
    setup: tuple[str, ...] = ()
    # Override only — None means "resolve from the image's own USER at use
    # time" (needs Docker, so it can't happen at load — see resolve_user()).
    user: str | None = None
    routes: dict[str, int] = field(default_factory=dict)
    publish: str | None = None
    branch: str = "sandbox/{slug}"
    platform: PlatformManifest = field(default_factory=PlatformManifest)
    provision: ProvisionManifest = field(default_factory=ProvisionManifest)
    env: dict[str, str] = field(default_factory=dict)
    egress: EgressManifest = field(default_factory=EgressManifest)
    forge: ForgeManifest | None = None
    links: dict[str, LinkManifest] = field(default_factory=dict)

    @classmethod
    def load(cls, root: Path) -> Manifest:
        """Parse, default-resolve, and validate `root / "sandbox.yaml"`, raising
        StxError on any problem.

        Every error names the file, the offending key, and the problem — a
        typo in a manifest should never need a Python traceback to diagnose.
        Defaults that need only the repo (compose lookup, the sole service,
        the Dockerfile, git) are resolved here; `user` needs a built image
        and is resolved later via `resolve_user`.
        """
        path = root / "sandbox.yaml"
        return _load_from_text(path, path.read_text(), root)

    def branch_for(
        self, slug: str, user: str, *, links: Mapping[str, LinkRecord] | None = None
    ) -> str:
        return render_template(
            self.branch, slug=slug, user=user, links=links, declared_links=self.links
        )

    def resolve_user(self, *, runner: _CompletedProcessRunner = subprocess.run) -> str:
        """The box's non-root user: the override if given, else the built
        image's own `USER` — refused when the image has none or declares
        root. Needs the image to already exist (built/rebuilt by the
        caller) — see docs/box-contract.md (Image USER)."""
        if self.user:
            return self.user
        from .core.images import image_user

        resolved = image_user(self.image, runner=runner)
        if not resolved or resolved == "root":
            raise StxError(
                f"image '{self.image}' has no non-root USER — set `user:` in "
                "sandbox.yaml, or add a USER instruction to the Dockerfile"
            )
        return resolved

    def render_env(
        self,
        *,
        slug: str,
        redis_index: int | None,
        links: Mapping[str, LinkRecord] | None = None,
    ) -> dict[str, str]:
        return {
            key: render_template(
                value,
                slug=slug,
                redis_index=redis_index,
                links=links,
                declared_links=self.links,
            )
            for key, value in self.env.items()
        }

    def render_databases(
        self, slug: str, *, links: Mapping[str, LinkRecord] | None = None
    ) -> list[str]:
        if self.platform.postgres is None:
            return []
        rendered = [
            render_template(entry, slug=slug, links=links, declared_links=self.links)
            for entry in self.platform.postgres.databases
        ]
        for name in rendered:
            if not _DB_NAME_RE.fullmatch(name):
                raise StxError(
                    f"platform.postgres.databases: rendered name {name!r} "
                    "must match [a-z0-9_]+"
                )
            if len(name.encode()) > _MAX_IDENTIFIER_BYTES:
                raise StxError(
                    f"platform.postgres.databases: rendered name {name!r} is "
                    f"{len(name.encode())} bytes, over Postgres's "
                    f"{_MAX_IDENTIFIER_BYTES}-byte identifier limit"
                )
        return rendered

    def render_provision_steps(
        self,
        *,
        slug: str,
        redis_index: int | None,
        links: Mapping[str, LinkRecord] | None = None,
    ) -> list[list[str]]:
        return [
            [
                render_template(
                    arg,
                    slug=slug,
                    redis_index=redis_index,
                    links=links,
                    declared_links=self.links,
                )
                for arg in step
            ]
            for step in self.provision.steps
        ]

    def provision_host_vars(self) -> list[str]:
        """Names provision.env's ${NAME} refs that aren't admin vars `stx`
        itself supplies (see PROVISION_ADMIN_VARS), deduped."""
        names: list[str] = []
        for value in self.provision.env.values():
            for match in _HOST_ENV_VAR_RE.finditer(value):
                name = match.group(1)
                if name not in PROVISION_ADMIN_VARS and name not in names:
                    names.append(name)
        return names

    def render_provision_env(
        self, admin_vars: dict[str, str], host_env: dict[str, str]
    ) -> dict[str, str]:
        """Raises naming the var — never rendered blank. `admin_vars` (the
        platform Postgres connection `stx` itself knows) take
        precedence over `host_env` (the loaded secret source) on a name
        collision."""
        available = {**host_env, **admin_vars}
        rendered: dict[str, str] = {}
        for key, value in self.provision.env.items():

            def _sub(m: re.Match[str]) -> str:
                name = m.group(1)
                if name not in available:
                    raise StxError(
                        f"provision.env.{key} references ${{{name}}}, which is "
                        "not set in the host environment"
                    )
                return available[name]

            rendered[key] = _HOST_ENV_VAR_RE.sub(_sub, value)
        return rendered


class _TemplateSyntaxError(Exception):
    """A template string breaks the closed grammar's brace syntax — a stray,
    unescaped `{` or `}`. Caught and re-raised as StxError by every caller
    with context (a load-time field, or the template itself) added."""


def _iter_template(template: str) -> Iterator[tuple[str, str]]:
    """Split `template` into `("text", ...)` and `("var", name)` parts.
    `{{`/`}}` are literal-brace escapes, not placeholders — nothing else
    about `str.format` (attribute access, format specs, positional/keyword
    mixing) is recognized. Raises _TemplateSyntaxError on an unescaped `{`
    or `}` (unmatched, or nested before the matching close)."""
    pos = 0
    n = len(template)
    buf: list[str] = []
    while pos < n:
        ch = template[pos]
        if ch == "{":
            if template[pos : pos + 2] == "{{":
                buf.append("{")
                pos += 2
                continue
            if buf:
                yield ("text", "".join(buf))
                buf = []
            end = template.find("}", pos + 1)
            if end == -1 or "{" in template[pos + 1 : end]:
                raise _TemplateSyntaxError(
                    f"template {template!r} has an unescaped '{{'"
                )
            yield ("var", template[pos + 1 : end])
            pos = end + 1
        elif ch == "}":
            if template[pos : pos + 2] == "}}":
                buf.append("}")
                pos += 2
                continue
            raise _TemplateSyntaxError(f"template {template!r} has an unescaped '}}'")
        else:
            buf.append(ch)
            pos += 1
    if buf:
        yield ("text", "".join(buf))


def render_template(
    template: str,
    *,
    slug: str,
    redis_index: int | None = None,
    user: str | None = None,
    links: Mapping[str, LinkRecord] | None = None,
    declared_links: dict[str, LinkManifest] | None = None,
) -> str:
    mapping = {"slug": slug, "db": db_name(slug)}
    if redis_index is not None:
        mapping["redis_index"] = str(redis_index)
    if user is not None:
        mapping["user"] = user
    link_ns = _link_namespace(declared_links, links or {}) if declared_links else None

    try:
        parts = list(_iter_template(template))
    except _TemplateSyntaxError as e:
        raise StxError(str(e)) from e

    out = []
    for kind, value in parts:
        if kind == "text":
            out.append(value)
            continue
        out.append(_resolve_template_var(value, template, mapping, link_ns))
    return "".join(out)


def _resolve_template_var(
    name: str, template: str, mapping: dict[str, str], link_ns: SimpleNamespace | None
) -> str:
    if name in mapping:
        return mapping[name]
    link_match = _TEMPLATE_LINK_VAR_RE.match(name)
    if link_match is not None and link_ns is not None:
        link_name, internal = link_match.groups()
        value = getattr(link_ns, link_name, None)
        if value is not None:
            return value.internal if internal else str(value)
    raise StxError(f"template {template!r} references unknown variable '{{{name}}}'")


def _check_template_grammar(
    value: str, path: Path, field: str, *, allow_user: bool = False
) -> None:
    """Raises naming `field` and the bad token for anything outside the
    closed template grammar — see docs/manifest.md (Template variables).
    Catches a typo'd or unsupported `{var}` at manifest load, rather than
    lazily whenever whatever command first renders this particular field.
    `{user}` resolves only for `branch`, so it's refused everywhere else
    (where it would otherwise render blank) unless `allow_user` is set."""
    try:
        parts = list(_iter_template(value))
    except _TemplateSyntaxError as e:
        raise _error(path, field, str(e)) from e
    for kind, name in parts:
        if kind != "var":
            continue
        if not _TEMPLATE_VAR_RE.match(name):
            raise _error(
                path, field, f"'{value}' references unknown variable '{{{name}}}'"
            )
        if name == "user" and not allow_user:
            raise _error(
                path,
                field,
                f"'{value}' uses {{user}}, which is only allowed in `branch`",
            )


def _check_template_fields(
    *,
    branch: str,
    env: dict[str, str],
    platform: PlatformManifest,
    provision: ProvisionManifest,
    path: Path,
) -> None:
    _check_template_grammar(branch, path, "branch", allow_user=True)
    for key, value in env.items():
        _check_template_grammar(value, path, f"env.{key}")
    if platform.postgres is not None:
        for i, entry in enumerate(platform.postgres.databases):
            _check_template_grammar(entry, path, f"platform.postgres.databases[{i}]")
    for i, step in enumerate(provision.steps):
        for j, arg in enumerate(step):
            _check_template_grammar(arg, path, f"provision.steps[{i}][{j}]")


def _error(path: Path, key: str, problem: str) -> StxError:
    return StxError(f"{path}: {key}: {problem}")


def _run_git(root: Path, *args: str) -> str | None:
    result = subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _resolve_in_repo(root: Path, value: str, key: str, path: Path) -> Path:
    """Resolve a manifest-declared repo-relative path and require it to stay
    inside `root` — both textually (no `..` component) and after following
    symlinks (a committed symlink could otherwise point outside the repo)."""
    if value.startswith("/") or ".." in Path(value).parts:
        raise _error(
            path, key, f"path '{value}' must be relative and stay inside the repo root"
        )
    resolved_root = root.resolve()
    candidate = (root / value).resolve()
    if not candidate.is_relative_to(resolved_root):
        raise _error(path, key, f"path '{value}' resolves outside the repo root")
    if not candidate.exists():
        raise _error(path, key, f"path '{value}' does not exist under {root}")
    return candidate


def _check_unknown_keys(
    data: dict, allowed: set[str], path: Path, context: str
) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise _error(path, context, f"unknown key(s) {sorted(unknown)!r}")


def _require(data: dict, key: str, path: Path, context: str) -> object:
    if key not in data:
        raise _error(path, f"{context}.{key}", "is required")
    return data[key]


def _require_str(data: dict, key: str, path: Path, context: str) -> str:
    value = _require(data, key, path, context)
    if not isinstance(value, str) or not value:
        raise _error(path, f"{context}.{key}", "must be a non-empty string")
    return value


def _str_list(data: dict, key: str, path: Path, context: str) -> tuple[str, ...]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise _error(path, f"{context}.{key}", "must be a list of strings")
    return tuple(value)


def _load_str_map(raw: object, path: Path, context: str) -> dict[str, str]:
    if not isinstance(raw, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in raw.items()
    ):
        raise _error(path, context, "must be a mapping of string to string")
    return dict(raw)


def _resolve_compose(raw: object, path: Path, root: Path) -> str:
    if raw is not None:
        value = raw
        if not isinstance(value, str) or not value:
            raise _error(path, "compose", "must be a non-empty string")
        _resolve_in_repo(root, value, "compose", path)
        return value
    for candidate in COMPOSE_LOOKUP_ORDER:
        if (root / candidate).exists():
            return candidate
    raise _error(
        path,
        "compose",
        f"no compose file found under {root} (expected one of {COMPOSE_LOOKUP_ORDER})",
    )


def _load_compose_yaml(compose_path: Path, path: Path) -> dict:
    try:
        raw = yaml.safe_load(compose_path.read_text())
    except yaml.YAMLError as e:
        raise _error(
            path, "compose", f"'{compose_path}' could not be parsed: {e}"
        ) from e
    if not isinstance(raw, dict) or not isinstance(raw.get("services"), dict):
        raise _error(
            path, "compose", f"'{compose_path}' has no top-level 'services' mapping"
        )
    return raw


def _resolve_name(raw: object, path: Path, services: dict) -> str:
    if raw is not None:
        if not isinstance(raw, str):
            raise _error(path, "name", "must be a string")
        try:
            validate_box_name(raw)
        except ValueError as e:
            raise _error(path, "name", str(e)) from e
        return raw
    if len(services) == 1:
        return next(iter(services))
    raise _error(
        path,
        "name",
        f"compose file declares {len(services)} service(s) "
        f"({sorted(services)!r}) — set `name:` to pick one",
    )


def _resolve_image(raw: object, path: Path, root: Path) -> str:
    if raw is not None:
        if not isinstance(raw, str) or not raw:
            raise _error(path, "image", "must be a non-empty string")
        return raw
    common_dir = _run_git(root, "rev-parse", "--git-common-dir")
    if common_dir is None:
        raise _error(path, "image", f"could not resolve a git repo at {root}")
    common_path = Path(common_dir)
    if not common_path.is_absolute():
        common_path = (root / common_path).resolve()
    dirname = _sanitize_image_tag_component(common_path.parent.name)
    digest = hashlib.sha256(str(common_path).encode()).hexdigest()[:_IMAGE_TAG_HASH_LEN]
    return f"stx-{dirname}-{digest}"


def _sanitize_image_tag_component(name: str) -> str:
    """`name` reduced to a valid Docker tag component: lowercase
    `[a-z0-9._-]`, no run of separators, and never starting/ending on one —
    Docker's own repository-name rules."""
    lowered = _IMAGE_TAG_INVALID_RE.sub("-", name.lower())
    collapsed = _IMAGE_TAG_SEPARATOR_RUN_RE.sub("-", lowered)
    return collapsed.strip("._-") or "repo"


def _dockerfile_copy_add_sources(dockerfile_text: str) -> list[list[str]]:
    """Each COPY/ADD instruction's source tokens (destination dropped,
    `--from=` stage copies, remote ADD URLs, and glob patterns excluded —
    a glob can't be resolved to a fixed path to hash) — one list per
    instruction, in file order."""
    lines: list[str] = []
    buf = ""
    for raw_line in dockerfile_text.splitlines():
        stripped = raw_line.rstrip()
        if stripped.endswith("\\"):
            buf += stripped[:-1] + " "
            continue
        buf += stripped
        lines.append(buf)
        buf = ""
    if buf:
        lines.append(buf)

    instructions: list[list[str]] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _COPY_ADD_RE.match(stripped)
        if not match:
            continue
        tokens = match.group(2).split()
        if any(t.startswith("--from=") for t in tokens):
            continue  # copied from another build stage, not a host path
        tokens = [t for t in tokens if not t.startswith("--")]
        if len(tokens) < 2:
            continue  # malformed/unsupported form — skip rather than guess
        sources = [
            t
            for t in tokens[:-1]
            if not t.startswith(("http://", "https://"))
            and not any(c in t for c in "*?[")
        ]
        if sources:
            instructions.append(sources)
    return instructions


def _resolve_rebuild_on(
    overrides: tuple[str, ...],
    *,
    service: dict,
    compose_path: Path,
    root: Path,
    path: Path,
) -> tuple[str, ...]:
    build = service.get("build")
    # Compose's `build: <path>` shorthand is a context with the default Dockerfile.
    if isinstance(build, str):
        build = {"context": build}
    resolved: list[str] = []
    if isinstance(build, dict):
        context = (compose_path.parent / build.get("context", ".")).resolve()
        dockerfile = context / build.get("dockerfile", "Dockerfile")
        dockerfile_rel = os.path.relpath(dockerfile, root)
        dockerfile_path = _resolve_in_repo(root, dockerfile_rel, "rebuild_on", path)
        resolved.append(dockerfile_rel)
        for sources in _dockerfile_copy_add_sources(dockerfile_path.read_text()):
            for src in sources:
                rel = os.path.relpath((context / src).resolve(), root)
                _resolve_in_repo(root, rel, "rebuild_on", path)
                if rel not in resolved:
                    resolved.append(rel)
    for entry in overrides:
        _resolve_in_repo(root, entry, "rebuild_on", path)
        if entry not in resolved:
            resolved.append(entry)
    return tuple(resolved)


def _is_git_toplevel(root: Path) -> bool:
    """Whether `root` is the top of its own checkout (not a subdirectory, and
    not outside any repo) — distinguishes a real checkout with no `origin`
    from a manifest sitting in a subdir that legitimately doesn't speak for
    the enclosing repo's remote."""
    toplevel = _run_git(root, "rev-parse", "--show-toplevel")
    return toplevel is not None and Path(toplevel).resolve() == root.resolve()


def _parse_origin(root: Path) -> tuple[str, str] | None:
    """(host, project_path) parsed from the `origin` remote, or None without one.
    None too when `root` isn't the top of its own checkout: a manifest in a
    subdirectory doesn't speak for the enclosing repo's remote."""
    toplevel = _run_git(root, "rev-parse", "--show-toplevel")
    if toplevel is None or Path(toplevel).resolve() != root.resolve():
        return None
    url = _run_git(root, "remote", "get-url", "origin")
    if not url:
        return None
    if url.startswith("git@"):
        host, _, rest = url[len("git@") :].partition(":")
    else:
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname or ""
        rest = parsed.path.lstrip("/")
    project = rest.removesuffix(".git").strip("/")
    return (host, project) if host and project else None


def _load_from_text(path: Path, text: str, root: Path) -> Manifest:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise _error(path, "<yaml>", f"could not be parsed: {e}") from e
    if not isinstance(raw, dict):
        raise _error(path, "<root>", "must be a mapping")

    _check_unknown_keys(
        raw,
        {
            "schema",
            "name",
            "compose",
            "image",
            "user",
            "rebuild_on",
            "setup",
            "routes",
            "publish",
            "branch",
            "platform",
            "provision",
            "env",
            "egress",
            "forge",
            "links",
        },
        path,
        "<root>",
    )

    schema = raw.get("schema")
    if schema != _SCHEMA:
        raise _error(path, "schema", f"must be {_SCHEMA} (found {schema!r})")

    compose = _resolve_compose(raw.get("compose"), path, root)
    compose_yaml = _load_compose_yaml(root / compose, path)
    services = compose_yaml["services"]

    name = _resolve_name(raw.get("name"), path, services)
    image = _resolve_image(raw.get("image"), path, root)

    rebuild_on_overrides = _str_list(raw, "rebuild_on", path, "<root>")
    rebuild_on = _resolve_rebuild_on(
        rebuild_on_overrides,
        service=services.get(name, {}),
        compose_path=root / compose,
        root=root,
        path=path,
    )

    user = raw.get("user")
    if user is not None:
        if not isinstance(user, str) or not _BOX_USER_RE.fullmatch(user):
            raise _error(path, "user", "must match [a-z_][a-z0-9_-]*")

    setup = _str_list(raw, "setup", path, "<root>")
    for entry in setup:
        _resolve_in_repo(root, entry, "setup", path)

    routes = _load_routes(raw.get("routes", {}), path)

    publish = raw.get("publish")
    if publish is not None:
        if not isinstance(publish, str):
            raise _error(path, "publish", "must be a string naming a route")
        if publish not in routes:
            raise _error(path, "publish", f"'{publish}' is not a route in `routes`")

    branch = raw.get("branch", "sandbox/{slug}")
    if not isinstance(branch, str):
        raise _error(path, "branch", "must be a string")

    platform = _load_platform(raw.get("platform", {}), path)
    provision = _load_provision(raw.get("provision", {}), path)
    env = _load_str_map(raw.get("env", {}), path, "env")
    reserved = reserved_env_names(env)
    if reserved:
        raise _error(
            path,
            "env",
            f"{', '.join(reserved)} reserved: stx sets these to confine the box",
        )
    egress = _load_egress(raw.get("egress", {}), path)
    forge = _load_forge(raw.get("forge"), path, root) if "forge" in raw else None
    links = _load_links(raw.get("links"), path)

    _check_template_fields(
        branch=branch, env=env, platform=platform, provision=provision, path=path
    )
    _check_redis_index_usage(
        platform=platform, env=env, provision=provision, routes_branch=branch, path=path
    )
    _check_link_usage(
        links=links,
        env=env,
        provision=provision,
        routes_branch=branch,
        platform=platform,
        path=path,
    )

    return Manifest(
        schema=schema,
        name=name,
        compose=compose,
        image=image,
        rebuild_on=rebuild_on,
        setup=setup,
        user=user,
        routes=routes,
        publish=publish,
        branch=branch,
        platform=platform,
        provision=provision,
        env=env,
        egress=egress,
        forge=forge,
        links=links,
    )


def _load_routes(raw: object, path: Path) -> dict[str, int]:
    if not isinstance(raw, dict):
        raise _error(path, "routes", "must be a mapping of name -> port")
    routes: dict[str, int] = {}
    for name, port in raw.items():
        if not isinstance(name, str) or not _DNS_LABEL_RE.fullmatch(name):
            raise _error(path, "routes", f"route name '{name}' must be a DNS label")
        if (
            not isinstance(port, int)
            or isinstance(port, bool)
            or not (1 <= port <= 65535)
        ):
            raise _error(path, f"routes.{name}", f"port {port!r} must be 1-65535")
        routes[name] = port
    return routes


def _load_platform(raw: object, path: Path) -> PlatformManifest:
    if not isinstance(raw, dict):
        raise _error(path, "platform", "must be a mapping")
    _check_unknown_keys(raw, {"postgres", "redis", "otel"}, path, "platform")
    postgres = None
    if "postgres" in raw:
        pg_raw = raw["postgres"]
        if not isinstance(pg_raw, dict):
            raise _error(path, "platform.postgres", "must be a mapping")
        _check_unknown_keys(pg_raw, {"databases"}, path, "platform.postgres")
        databases = _str_list(pg_raw, "databases", path, "platform.postgres")
        if not databases:
            raise _error(path, "platform.postgres.databases", "must be non-empty")
        for entry in databases:
            if "{db}" not in entry:
                raise _error(
                    path,
                    "platform.postgres.databases",
                    f"entry '{entry}' must contain {{db}}",
                )
        postgres = PostgresManifest(databases=databases)
    redis = raw.get("redis", False)
    if not isinstance(redis, bool):
        raise _error(path, "platform.redis", "must be a boolean")
    otel = raw.get("otel", False)
    if not isinstance(otel, bool):
        raise _error(path, "platform.otel", "must be a boolean")
    return PlatformManifest(postgres=postgres, redis=redis, otel=otel)


def _load_provision(raw: object, path: Path) -> ProvisionManifest:
    if not isinstance(raw, dict):
        raise _error(path, "provision", "must be a mapping")
    _check_unknown_keys(raw, {"steps", "env"}, path, "provision")
    steps_raw = raw.get("steps", [])
    if not isinstance(steps_raw, list):
        raise _error(path, "provision.steps", "must be a list")
    steps: list[tuple[str, ...]] = []
    for i, step_raw in enumerate(steps_raw):
        if not isinstance(step_raw, list) or not all(
            isinstance(v, str) for v in step_raw
        ):
            raise _error(path, f"provision.steps[{i}]", "must be a list of strings")
        if not step_raw:
            raise _error(path, f"provision.steps[{i}]", "must be non-empty")
        steps.append(tuple(step_raw))
    env = _load_str_map(raw.get("env", {}), path, "provision.env")
    host_var_names = {
        match.group(1)
        for value in env.values()
        for match in _HOST_ENV_VAR_RE.finditer(value)
        if match.group(1) not in PROVISION_ADMIN_VARS
    }
    reserved = reserved_env_names(host_var_names)
    if reserved:
        raise _error(
            path,
            "provision.env",
            f"{', '.join(reserved)} reserved: stx sets these to confine the box",
        )
    return ProvisionManifest(steps=tuple(steps), env=env)


def _load_egress(raw: object, path: Path) -> EgressManifest:
    if not isinstance(raw, dict):
        raise _error(path, "egress", "must be a mapping")
    _check_unknown_keys(raw, {"hosts", "ports"}, path, "egress")
    hosts = _str_list(raw, "hosts", path, "egress")
    for entry in hosts:
        pattern = entry[1:] if entry.startswith("~") else f"^{re.escape(entry)}$"
        try:
            re.compile(pattern)
        except re.error as e:
            raise _error(
                path, "egress.hosts", f"entry '{entry}' does not compile: {e}"
            ) from e
    ports = _str_list(raw, "ports", path, "egress")
    for entry in ports:
        if not _PORT_TOKEN_RE.match(entry):
            raise _error(
                path, "egress.ports", f"token '{entry}' must match (\\*|\\d+)/(tcp|udp)"
            )
        if entry == "*/tcp":
            raise _error(path, "egress.ports", "'*/tcp' would open every TCP port")
        if entry in ("80/tcp", "443/tcp"):
            raise _error(
                path,
                "egress.ports",
                f"'{entry}' would open the web directly, defeating the allowlist proxy",
            )
    return EgressManifest(hosts=hosts, ports=ports)


def _is_ancestor_group(group: str, project: str) -> bool:
    """Whether `group` names an ancestor (sub)group of `project` — a strict,
    component-wise prefix, never the project's own path."""
    group_parts = group.split("/")
    project_parts = project.split("/")
    return (
        len(group_parts) < len(project_parts)
        and project_parts[: len(group_parts)] == group_parts
    )


def _load_forge(raw: object, path: Path, root: Path) -> ForgeManifest:
    if not isinstance(raw, dict):
        raise _error(path, "forge", "must be a mapping")
    _check_unknown_keys(raw, {"kind", "project", "package_registry"}, path, "forge")

    kind = raw.get("kind")
    project = raw.get("project")
    # Always resolved, even when both are set explicitly — a minted forge
    # token is scoped to `project`, so an explicit value still has to match
    # this repo's own origin (see docs/security.md).
    origin = _parse_origin(root)
    if origin is None:
        if _is_git_toplevel(root):
            # A checkout at its own root but with no `origin` — nothing to pin
            # a scoped token against, so an explicit kind/project can't be
            # verified and a defaulted one can't be inferred. Refuse either.
            raise _error(
                path,
                "forge",
                "this repo has no `origin` remote — a forge token is scoped to "
                "the origin project, so stx won't mint one against an "
                "unverifiable kind/project",
            )
        if kind is None or project is None:
            raise _error(
                path,
                "forge",
                "no `origin` remote to default kind/project from — set them explicitly",
            )
    if origin is not None:
        origin_host, origin_project = origin
        if kind is None:
            if "gitlab" not in origin_host:
                raise _error(
                    path,
                    "forge.kind",
                    f"could not infer from origin host '{origin_host}' — set it explicitly",
                )
            kind = "gitlab"
        if project is None:
            project = origin_project
        elif project != origin_project:
            raise _error(
                path,
                "forge.project",
                f"'{project}' does not match the repo's own origin project "
                f"'{origin_project}' — a mismatch could mint a token for a "
                "project this repo isn't",
            )

    if not isinstance(kind, str) or kind not in _FORGE_KINDS:
        raise _error(path, "forge.kind", f"must be one of {_FORGE_KINDS}")
    if not isinstance(project, str) or not project:
        raise _error(path, "forge.project", "must be a non-empty string")

    package_registry = None
    if "package_registry" in raw:
        pr_raw = raw["package_registry"]
        if not isinstance(pr_raw, dict):
            raise _error(path, "forge.package_registry", "must be a mapping")
        _check_unknown_keys(pr_raw, {"group", "env"}, path, "forge.package_registry")
        group = pr_raw.get("group")
        if group is None:
            group = project.split("/")[0]
        if not isinstance(group, str) or not group:
            raise _error(
                path, "forge.package_registry.group", "must be a non-empty string"
            )
        if not _is_ancestor_group(group, project):
            raise _error(
                path,
                "forge.package_registry.group",
                f"'{group}' is not an ancestor group of forge.project '{project}'",
            )
        env_raw = _require(pr_raw, "env", path, "forge.package_registry")
        env = _load_str_map(env_raw, path, "forge.package_registry.env")
        _check_unknown_keys(
            env, {"username", "password"}, path, "forge.package_registry.env"
        )
        _require_str(env, "username", path, "forge.package_registry.env")
        _require_str(env, "password", path, "forge.package_registry.env")
        # These values name box env vars the forge passthrough injects — they
        # must be valid env-var identifiers, and never one stx reserves to
        # confine the box, or the package credential could clobber it.
        for role in ("username", "password"):
            var_name = env[role]
            if not _ENV_VAR_NAME_RE.fullmatch(var_name):
                raise _error(
                    path,
                    f"forge.package_registry.env.{role}",
                    f"'{var_name}' must be a valid environment variable name "
                    "([A-Za-z_][A-Za-z0-9_]*)",
                )
        reserved = reserved_env_names([env["username"], env["password"]])
        if reserved:
            raise _error(
                path,
                "forge.package_registry.env",
                f"{', '.join(reserved)} reserved: stx sets these to confine the box",
            )
        package_registry = PackageRegistryManifest(group=group, env=env)
    return ForgeManifest(kind=kind, project=project, package_registry=package_registry)


def _check_redis_index_usage(
    *,
    platform: PlatformManifest,
    env: dict[str, str],
    provision: ProvisionManifest,
    routes_branch: str,
    path: Path,
) -> None:
    if platform.redis:
        return
    candidates = [*env.values(), routes_branch]
    for step in provision.steps:
        candidates.extend(step)
    if platform.postgres:
        candidates.extend(platform.postgres.databases)
    for value in candidates:
        if "{redis_index}" in value:
            raise _error(
                path,
                "<root>",
                f"'{value}' references {{redis_index}} but platform.redis is not true",
            )


def _load_links(raw: object, path: Path) -> dict[str, LinkManifest]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise _error(path, "links", "must be a mapping")
    links: dict[str, LinkManifest] = {}
    for name, entry in raw.items():
        if not isinstance(name, str) or not _LINK_NAME_RE.fullmatch(name):
            raise _error(
                path, "links", f"link name {name!r} must match [a-z][a-z0-9_]*"
            )
        if not isinstance(entry, dict):
            raise _error(path, f"links.{name}", "must be a mapping")
        _check_unknown_keys(entry, {"route", "default"}, path, f"links.{name}")
        route = _require_str(entry, "route", path, f"links.{name}")
        if not _DNS_LABEL_RE.fullmatch(route):
            raise _error(path, f"links.{name}.route", f"{route!r} must be a DNS label")
        default = entry.get("default", "")
        if not isinstance(default, str):
            raise _error(path, f"links.{name}.default", "must be a string")
        links[name] = LinkManifest(route=route, default=default)
    return links


def _check_link_usage(
    *,
    links: dict[str, LinkManifest],
    env: dict[str, str],
    provision: ProvisionManifest,
    routes_branch: str,
    platform: PlatformManifest,
    path: Path,
) -> None:
    candidates = [*env.values(), routes_branch]
    for step in provision.steps:
        candidates.extend(step)
    if platform.postgres:
        candidates.extend(platform.postgres.databases)
    for value in candidates:
        for match in _LINK_TEMPLATE_RE.finditer(value):
            name = match.group(1)
            if name not in links:
                raise _error(
                    path, "<root>", f"'{value}' references undeclared link '{name}'"
                )
