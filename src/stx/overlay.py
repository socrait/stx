"""The box contract — see docs/box-contract.md. (JSON, not YAML — merged via
docker compose -f ... -f ...)"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from . import network
from . import platform as stx_platform
from .core.box_env import reserved_env_names
from .core.names import (
    DMZ_BOX,
    container_name_for_box,
    dmz_container_name,
    sandbox_compose_project,
)
from .core.paths import (
    LABEL_BOX,
    LABEL_DATABASES,
    LABEL_REDIS_INDEX,
    LABEL_REPO_ROOT,
    LABEL_SLUG,
    allowlist_path,
    compose_overlay_path,
    drop_dir,
    drop_dmz_dir,
    host_git_identity,
    logs_dir,
    repo_git_dir,
)
from .core.registry import ClaudeAuthMode
from .errors import StxError

#: Owner-only — see docs/secrets.md.
_COMPOSE_DIR_MODE = 0o700
_COMPOSE_FILE_MODE = 0o600

if TYPE_CHECKING:
    from .core.registry import LinkRecord
    from .manifest import Manifest

_DMZ_NETWORK = "dmz-net"
_PROXY_URL = "http://127.0.0.1:3128"


def _no_proxy(links: Mapping[str, LinkRecord] | None) -> str:
    """Platform service names plus every linked target's own box container —
    see docs/manifest.md (links)."""
    names = [stx_platform.container_name(s) for s in ("postgres", "redis", "otel")]
    names += [link.container for link in (links or {}).values()]
    return "localhost,127.0.0.1," + ",".join(dict.fromkeys(names)) + ",.localhost"


# The box runtime's entrypoint applies egress rules and then drops to the box
# user itself, so it must start as root whatever `USER` the image declares.
_RUNTIME_USER = "0:0"

# Reserved mount paths — see docs/box-contract.md.
_TOOL_RESERVED_MOUNTS = (
    "/workspace",
    "/git",
    "/drop",
    "/drop-dmz",
    "/logs",
    "/opt/stx",
    "/etc/tinyproxy",
    "/run/stx",
)

# Public: `init`'s own best-effort, no-docker scan reuses these same sets —
# see docs/box-contract.md (the repo compose allow-list).
TOP_LEVEL_ALLOWED = {"services", "volumes", "networks"}
SERVICE_ALLOWED = {"build", "environment", "volumes", "labels", "shm_size", "networks"}
BUILD_ALLOWED = {"context", "dockerfile", "args", "target"}

#: Public, reused by `init`'s own best-effort scan. The base of the only
#: prefix an explicit top-level `volumes.<name>.name:` may use. The full
#: prefix is per-repo (`shared_cache_prefix`): a shared cache across every
#: sandbox of *one* repo, never a way to name (and so mount) the platform's
#: own volume, another sandbox's, or another repo's cache.
SHARED_CACHE_VOLUME_PREFIX = "stx-cache-"


def shared_cache_prefix(repo_id: str) -> str:
    """The one prefix an explicit shared-cache volume `name:` may use, scoped
    to `repo_id` — the repo's own directory name (what the registry records as
    a sandbox's `repo`). Two sandboxes of the same repo share a cache named
    `stx-cache-<repo-id>--*`; an unrelated repo, with a different directory
    name, gets a different prefix and so can't name its way onto that volume.

    The id and the rest are split by a double dash so one repo's prefix can't
    be a prefix of another's: repo `app`'s `stx-cache-app--` is not a prefix of
    repo `app-web`'s `stx-cache-app-web--node`. That only holds while the id
    itself carries no `--` and no trailing `-` — see `repo_cache_id_problem`."""
    return f"{SHARED_CACHE_VOLUME_PREFIX}{repo_id}--"


def repo_cache_id_problem(repo_id: str) -> str | None:
    """Why `repo_id` can't scope shared-cache volume names, or None if it can.
    An id that ends with `-` or already contains `--` would blur the `--` that
    `shared_cache_prefix` puts between the id and the cache name, so one repo's
    prefix could again match another's volume — refuse it and say to rename."""
    if repo_id.endswith("-") or "--" in repo_id:
        return (
            f"repo directory name {repo_id!r} can't scope its shared-cache "
            "volumes: it must not end with '-' or contain '--' (that is the "
            f"delimiter in {SHARED_CACHE_VOLUME_PREFIX}<repo>--<name>); rename "
            "the directory"
        )
    return None


def refuse_repo_project_name(compose_path: Path, *, source: str) -> None:
    """Refuse a repo compose file that sets its own top-level `name:`. stx
    forces the compose project to `stx-<slug>` (via `-p`, see
    lifecycle.provision), which is what keeps a box's default-named volumes
    (`<project>_<vol>`) inside its own sandbox's namespace; a repo-authored
    project name would just be overridden, so refusing it is clearer than
    silently ignoring it — and shuts the door on it mattering at all."""
    try:
        raw = yaml.safe_load(compose_path.read_text())
    except (OSError, yaml.YAMLError):
        return  # a bad/unreadable file is the real `compose config`'s to report
    if isinstance(raw, dict) and "name" in raw:
        raise _refuse(
            source, "<root>", "name", detail="stx sets the compose project name itself"
        )


# Compose's own synthesized shape.
_SYNTHESIZED_SERVICE_NETWORKS = {"default": None}

# Keys the post-merge check asserts equal the overlay's own values.
_EQUALITY_KEYS = (
    "cap_add",
    "cap_drop",
    "security_opt",
    "networks",
    "ports",
    "user",
    "entrypoint",
    "command",
)

# Refusal vocabulary also checked post-merge, as the belt to the allow-list's braces.
_REFUSED_IF_UNSET_BY_OVERLAY = (
    "privileged",
    "network_mode",
    "pid",
    "ipc",
    "devices",
    "extra_hosts",
    "dns",
    "sysctls",
    "tmpfs",
    "secrets",
    "configs",
    "links",
    "profiles",
)

_DMZ_ALLOWED_ENV_KEYS = frozenset(
    {"STX_SLUG", "STX_ZONE", "ENVIRONMENT", "ANTHROPIC_API_KEY", "STX_BOX_USER"}
)


def box_runtime_dir() -> Path:
    """Host path to the box runtime, mounted read-only at /opt/stx/box.

    See docs/box-contract.md.
    """
    return Path(__file__).resolve().parent / "box"


def _volume(source: str, target: str, *, read_only: bool = False) -> dict:
    return {"type": "bind", "source": source, "target": target, "read_only": read_only}


def _ensure_git_rw_dirs(git_dir: Path, slug: str) -> None:
    """Create the read-write subtrees the /git mounts bind before `compose up`
    runs. objects/ and worktrees/<slug>/ always exist already, but the two
    branch namespaces the box may write — refs/heads/stx/ (its own stx/<slug>
    branch) and refs/heads/tmp/<slug>/ (its cross-branch scratch, see
    docs/reference.md, Branches) — may not yet, and neither may refs/remotes/
    (no remotes yet) or logs/ (ref logging off). Docker would bind-mount a
    missing dir as a root-owned empty one the box user then can't write, so stx
    makes them itself first. git updates a ref by lock-and-rename within the
    ref's own directory, so each namespace's *directory* has to be the mount."""
    subdirs = (
        "objects",
        "refs/heads/stx",
        f"refs/heads/tmp/{slug}",
        "refs/remotes",
        "logs",
        f"worktrees/{slug}",
    )
    for sub in subdirs:
        (git_dir / sub).mkdir(parents=True, exist_ok=True)


def _harness_passthrough(claude_auth_mode: ClaudeAuthMode) -> dict[str, str]:
    """Exactly one key, never both — see docs/secrets.md."""
    if claude_auth_mode == ClaudeAuthMode.OAUTH:
        return {"CLAUDE_CODE_OAUTH_TOKEN": "${STX_CLAUDE_OAUTH_TOKEN}"}
    return {"ANTHROPIC_API_KEY": "${ANTHROPIC_API_KEY}"}


def _forge_passthrough(manifest: Manifest) -> dict[str, str]:
    """Present only when the manifest declares a forge — see docs/secrets.md."""
    if manifest.forge is None:
        return {}
    env = {
        "STX_FORGE_TOKEN": "${STX_FORGE_TOKEN}",
        "STX_FORGE_HOST": "${STX_FORGE_HOST}",
        "STX_FORGE_GIT_USER": "${STX_FORGE_GIT_USER}",
    }
    package_registry = manifest.forge.package_registry
    if package_registry is not None:
        for var_name in (
            package_registry.env["username"],
            package_registry.env["password"],
        ):
            env[var_name] = f"${{{var_name}}}"
    return env


def box_secret_env(
    *,
    manifest: Manifest | None,
    claude_auth_mode: ClaudeAuthMode,
    with_dmz: bool,
    forge_env: dict[str, str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The harness/forge secret values for the box contract's pass-through
    entries — see docs/secrets.md."""
    environ = environ if environ is not None else os.environ
    env: dict[str, str] = {}
    if manifest is not None:
        if claude_auth_mode == ClaudeAuthMode.OAUTH:
            token = environ.get("STX_CLAUDE_OAUTH_TOKEN")
            if token:
                env["STX_CLAUDE_OAUTH_TOKEN"] = token
        else:
            key = environ.get("ANTHROPIC_API_KEY")
            if key:
                env["ANTHROPIC_API_KEY"] = key
        if forge_env:
            env.update(forge_env)
    if with_dmz:
        dmz_key = environ.get("STX_DMZ_ANTHROPIC_API_KEY")
        if dmz_key:
            env["STX_DMZ_ANTHROPIC_API_KEY"] = dmz_key
    return env


def _postgres_env(manifest: Manifest, databases: tuple[str, ...]) -> dict[str, str]:
    """libpq's own connection variables for a repo using the platform
    Postgres — where it is and which database is the box's own; the role and
    password stay the repo's. See docs/box-contract.md (Platform services)."""
    if manifest.platform.postgres is None:
        return {}
    env = {
        "PGHOST": stx_platform.container_name("postgres"),
        "PGPORT": str(stx_platform.POSTGRES_CONTAINER_PORT),
    }
    if databases:
        env["PGDATABASE"] = databases[0]
    return env


def _app_service(
    slug: str,
    manifest: Manifest,
    *,
    user: str,
    worktree: Path,
    repo_root: Path,
    redis_index: int | None,
    tailnet_port: int | None,
    tailscale_ip: str | None,
    claude_auth_mode: ClaudeAuthMode,
    networks: list[str],
    links: Mapping[str, LinkRecord] | None = None,
    route_ports: Mapping[str, int] | None = None,
    route_bind_addr: str | None = None,
    databases: tuple[str, ...] = (),
) -> dict:
    name, email = host_git_identity(repo_root)
    no_proxy = _no_proxy(links)

    owned: dict[str, str] = {
        "STX_SLUG": slug,
        "STX_ZONE": "trusted",
        "GIT_DIR": f"/git/worktrees/{slug}",
        "GIT_WORK_TREE": "/workspace",
        "GIT_CONFIG_COUNT": "6",
        "GIT_CONFIG_KEY_0": "commit.gpgsign",
        "GIT_CONFIG_VALUE_0": "false",
        "GIT_CONFIG_KEY_1": "user.name",
        "GIT_CONFIG_VALUE_1": f"{name} (sandbox)",
        "GIT_CONFIG_KEY_2": "user.email",
        "GIT_CONFIG_VALUE_2": email,
        "GIT_CONFIG_KEY_3": "push.default",
        "GIT_CONFIG_VALUE_3": "upstream",
        # The top of /git is read-only now (see the mounts below), so git must
        # never try to repack or prune packed-refs — those rewrite the
        # top-level packed-refs file, which it can no longer open for writing.
        # Auto-gc would fire on ordinary commits; fetch.prune would fire on
        # every fetch.
        "GIT_CONFIG_KEY_4": "gc.auto",
        "GIT_CONFIG_VALUE_4": "0",
        "GIT_CONFIG_KEY_5": "fetch.prune",
        "GIT_CONFIG_VALUE_5": "false",
        "HTTPS_PROXY": _PROXY_URL,
        "HTTP_PROXY": _PROXY_URL,
        "https_proxy": _PROXY_URL,
        "http_proxy": _PROXY_URL,
        "NO_PROXY": no_proxy,
        "no_proxy": no_proxy,
        "STX_BOX_USER": user,
        "STX_BOX_SETUP": " ".join(manifest.setup),
        "STX_EGRESS_PORTS": " ".join(manifest.egress.ports),
        "STX_SETUP_STATUS_FILE": "/run/stx/setup-status.json",
        "STX_SETUP_LOG_FILE": "/logs/setup.log",
        **_harness_passthrough(claude_auth_mode),
    }
    # `package_registry.env` names ride in via the forge passthrough and are
    # repo-controlled — the loader already refuses reserved names, but a
    # non-reserved key the overlay still owns (the harness var) would clobber
    # it here. Checked before the merge so it can't win by being spread last.
    forge_passthrough = _forge_passthrough(manifest)
    forge_overrides = sorted(set(forge_passthrough) & set(owned))
    if forge_overrides:
        raise StxError(
            f"forge package_registry.env sets {', '.join(forge_overrides)}, which "
            "stx sets itself to confine the box; rename it in package_registry.env"
        )
    owned.update(forge_passthrough)
    repo_env = manifest.render_env(slug=slug, redis_index=redis_index, links=links)
    overridden = sorted(set(repo_env) & set(owned))
    if overridden:
        raise StxError(
            f"manifest `env` sets {', '.join(overridden)}, which stx sets itself "
            "to confine the box; remove them from `env`"
        )
    env = {**_postgres_env(manifest, databases), **repo_env, **owned}

    git_dir = repo_git_dir(repo_root)
    _ensure_git_rw_dirs(git_dir, slug)
    volumes = [
        _volume(str(worktree), "/workspace"),
        # The top of /git is read-only, so a box can't create a *new*
        # top-level file git would then read for the whole repo — a
        # `commondir`/`config.worktree` the box chose would otherwise redirect
        # the operator's own `git status` (and their IDE, and stx) into a
        # config the box wrote: code execution on the host. config/hooks/info,
        # refs/tags, modules/, packed-refs, and the whole worktrees/ dir are
        # all read-only by falling through this one mount — no mount of their
        # own needed. See docs/security.md.
        _volume(str(git_dir), "/git", read_only=True),
        # Reopened read-write — the subtrees git writes during ordinary
        # commit/fetch/push: loose objects, remote-tracking refs, and reflogs.
        # Per-worktree files git writes (HEAD, FETCH_HEAD, ORIG_HEAD, the
        # index) live under worktrees/<slug> below, not here.
        _volume(f"{git_dir}/objects", "/git/objects"),
        # Only the two local-branch namespaces a box legitimately writes, not
        # all of refs/heads/. The rest of refs/heads/ falls through the
        # read-only top, so a box can't write a loose refs/heads/main (a loose
        # ref overrides the packed one) nor move whatever branch the operator
        # has checked out — a box-made commit can no longer ride into the
        # operator's identity on their next pull/commit/push. stx/ is the dir
        # holding the box's own stx/<slug> branch; tmp/<slug>/ is its
        # cross-branch scratch namespace (see docs/reference.md, Branches).
        # tmp/ itself stays read-only, so a box can't write another box's
        # tmp/<other>/ — only its own slug's. (A box can still move another
        # sandbox's stx/* branch: stx/ is shared, not per-slug — see
        # docs/security.md.)
        _volume(f"{git_dir}/refs/heads/stx", "/git/refs/heads/stx"),
        _volume(f"{git_dir}/refs/heads/tmp/{slug}", f"/git/refs/heads/tmp/{slug}"),
        _volume(f"{git_dir}/refs/remotes", "/git/refs/remotes"),
        _volume(f"{git_dir}/logs", "/git/logs"),
        # This box's own worktree admin dir, read-write for its HEAD/index/
        # reflog — but its two pointer files pinned read-only on top, so the
        # box can't retarget its own commondir/gitdir to make an
        # administrative lookup read somewhere it chose. (Host git refuses an
        # altered pointer too — see core.hostgit — this stops the alteration
        # at the mount, not just the read.)
        _volume(f"{git_dir}/worktrees/{slug}", f"/git/worktrees/{slug}"),
        _volume(
            f"{git_dir}/worktrees/{slug}/commondir",
            f"/git/worktrees/{slug}/commondir",
            read_only=True,
        ),
        _volume(
            f"{git_dir}/worktrees/{slug}/gitdir",
            f"/git/worktrees/{slug}/gitdir",
            read_only=True,
        ),
    ]
    volumes += [
        _volume(str(drop_dir(slug)), "/drop"),
        _volume(str(drop_dmz_dir(slug)), "/drop-dmz", read_only=True),
        _volume(str(logs_dir(slug, manifest.name)), "/logs"),
        _volume(str(box_runtime_dir()), "/opt/stx/box", read_only=True),
        _volume(str(allowlist_path(slug)), "/etc/tinyproxy/allowlist", read_only=True),
    ]

    service = {
        "image": manifest.image,
        "user": _RUNTIME_USER,
        # `-p` keeps bash from importing a function definition from the
        # environment (`BASH_FUNC_name%%=() {...}`) into this root NET_ADMIN
        # entrypoint — defense in depth under the env-name reservation that
        # already refuses such a name (see core/box_env.py).
        "entrypoint": ["/bin/bash", "-p", "/opt/stx/box/init-box.sh"],
        "command": ["sleep", "infinity"],
        "environment": env,
        "volumes": volumes,
        "labels": {
            LABEL_SLUG: slug,
            LABEL_BOX: manifest.name,
            LABEL_REDIS_INDEX: str(redis_index) if redis_index is not None else "",
            LABEL_REPO_ROOT: str(repo_root),
            LABEL_DATABASES: ",".join(databases),
            "devcontainer.metadata": json.dumps([{"remoteUser": user}]),
        },
        "networks": networks,
        "init": True,
        "cap_drop": ["ALL"],
        "cap_add": ["NET_ADMIN", "SETUID", "SETGID"],
        "security_opt": ["no-new-privileges:true"],
        "container_name": container_name_for_box(manifest.name, slug),
        "hostname": slug,
        "working_dir": "/workspace",
    }
    ports = []
    for name, container_port in manifest.routes.items():
        host_port = (route_ports or {}).get(name)
        if host_port is None:
            continue
        ports.append(
            {
                "target": container_port,
                "published": str(host_port),
                # Never 0.0.0.0 — see docs/reference.md (Routing).
                "host_ip": route_bind_addr or "127.0.0.1",
            }
        )
    if manifest.publish and tailnet_port is not None:
        published_port = manifest.routes[manifest.publish]
        # Never 0.0.0.0 — see docs/reference.md (Tailnet publishing).
        bind_addr = tailscale_ip if tailscale_ip else "127.0.0.1"
        ports.append(
            {
                "target": published_port,
                "published": str(tailnet_port),
                "host_ip": bind_addr,
            }
        )
    if ports:
        service["ports"] = ports
    return service


def _dmz_service(
    slug: str, *, image: str, user: str, snapshot: Path | None = None
) -> dict:
    volumes = [
        _volume(str(drop_dmz_dir(slug)), "/drop"),
        _volume(str(logs_dir(slug, DMZ_BOX)), "/logs"),
        _volume(str(box_runtime_dir()), "/opt/stx/box", read_only=True),
    ]
    # Absent for a --repo none sandbox, which has no repo to snapshot.
    if snapshot is not None:
        volumes.append(_volume(str(snapshot), "/workspace", read_only=True))
    return {
        "image": image,
        "user": _RUNTIME_USER,
        # `-p` keeps bash from importing a function definition from the
        # environment (`BASH_FUNC_name%%=() {...}`) into this root NET_ADMIN
        # entrypoint — defense in depth under the env-name reservation that
        # already refuses such a name (see core/box_env.py).
        "entrypoint": ["/bin/bash", "-p", "/opt/stx/box/init-box.sh"],
        "command": ["sleep", "infinity"],
        "environment": {
            "STX_SLUG": slug,
            "STX_ZONE": "dmz",
            "ENVIRONMENT": "local",
            "ANTHROPIC_API_KEY": "${STX_DMZ_ANTHROPIC_API_KEY}",
            "STX_BOX_USER": user,
        },
        "volumes": volumes,
        "labels": {
            LABEL_SLUG: slug,
            LABEL_BOX: DMZ_BOX,
            "devcontainer.metadata": json.dumps([{"remoteUser": user}]),
        },
        "networks": [_DMZ_NETWORK],
        "init": True,
        "cap_drop": ["ALL"],
        "cap_add": ["NET_ADMIN", "SETUID", "SETGID"],
        "security_opt": ["no-new-privileges:true"],
        "container_name": dmz_container_name(slug),
        "hostname": f"{slug}-dmz",
        "working_dir": "/workspace",
    }


def render(
    slug: str,
    manifest: Manifest | None,
    *,
    user: str | None = None,
    worktree: Path | None = None,
    repo_root: Path | None = None,
    redis_index: int | None = None,
    tailnet_port: int | None = None,
    tailscale_ip: str | None = None,
    claude_auth_mode: ClaudeAuthMode = ClaudeAuthMode.API_KEY,
    dmz_image: str | None = None,
    dmz_user: str | None = None,
    dmz_snapshot: Path | None = None,
    links: Mapping[str, LinkRecord] | None = None,
    route_ports: Mapping[str, int] | None = None,
    route_bind_addr: str | None = None,
    databases: tuple[str, ...] = (),
) -> Path:
    if manifest is None and dmz_image is None:
        raise StxError("overlay.render: nothing to render (no manifest, no dmz image)")

    services: dict[str, dict] = {}
    networks: dict[str, dict] = {}
    if manifest is not None:
        if worktree is None or repo_root is None or user is None:
            raise StxError(
                "overlay.render: manifest given but worktree/repo_root/user missing"
            )
        sandbox_net = network.sandbox_network_name(slug)
        service_networks = [sandbox_net]
        networks[sandbox_net] = {"external": True}
        for link in (links or {}).values():
            target_net = network.sandbox_network_name(link.target)
            if target_net not in networks:
                networks[target_net] = {"external": True}
            if target_net not in service_networks:
                service_networks.append(target_net)
        services[manifest.name] = _app_service(
            slug,
            manifest,
            user=user,
            worktree=worktree,
            repo_root=repo_root,
            redis_index=redis_index,
            tailnet_port=tailnet_port,
            tailscale_ip=tailscale_ip,
            claude_auth_mode=claude_auth_mode,
            networks=service_networks,
            links=links,
            route_ports=route_ports,
            route_bind_addr=route_bind_addr,
            databases=databases,
        )
    if dmz_image is not None:
        if dmz_user is None:
            raise StxError("overlay.render: dmz_image given but dmz_user missing")
        services[DMZ_BOX] = _dmz_service(
            slug, image=dmz_image, user=dmz_user, snapshot=dmz_snapshot
        )
        networks[_DMZ_NETWORK] = {}

    doc = {
        "name": sandbox_compose_project(slug),
        "services": services,
        "networks": networks,
    }
    path = compose_overlay_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(_COMPOSE_DIR_MODE)
    path.write_text(json.dumps(doc, indent=2) + "\n")
    path.chmod(_COMPOSE_FILE_MODE)
    return path


def remove(slug: str) -> None:
    """Delete the rendered overlay file for `slug`, if any — `rm`, `prune`, and
    `new`'s rollback all call this so a torn-down or failed sandbox leaves no
    stale compose file behind."""
    compose_overlay_path(slug).unlink(missing_ok=True)


def _refuse(
    source: str, service: str, key: str, *, detail: str | None = None
) -> StxError:
    where = "top level" if service == "<root>" else f"service '{service}'"
    message = f"{source}: {where}: key '{key}' is not allowed"
    if detail:
        message += f" ({detail})"
    return StxError(message)


def _is_tool_reserved(target: str) -> bool:
    return any(target == p or target.startswith(p + "/") for p in _TOOL_RESERVED_MOUNTS)


def check_repo_compose(
    config: dict, *, box_name: str, source: str, repo_id: str
) -> None:
    """The repo compose allow-list — see docs/box-contract.md. Raises StxError
    naming the file, service, and key on the first violation found. `repo_id`
    scopes which explicit shared-cache volume name is allowed (see
    `shared_cache_prefix`); the config must be the one computed with the
    sandbox's own `-p stx-<slug>` project (see lifecycle.provision), so the
    `<project>_<vol>` default names below are the ones Compose will actually
    create at `up`."""
    problem = repo_cache_id_problem(repo_id)
    if problem:
        raise StxError(f"{source}: {problem}")

    for key in config:
        if key == "name":
            continue  # compose's own synthesized project name — never repo-authored
        if key in TOP_LEVEL_ALLOWED or key.startswith("x-"):
            continue
        raise _refuse(source, "<root>", key)

    networks = config.get("networks")
    if networks is not None and (
        set(networks) != {"default"} or not isinstance(networks["default"], dict)
    ):
        raise _refuse(source, "<root>", "networks")

    services = config.get("services", {})
    if set(services) != {box_name}:
        raise _refuse(
            source,
            "<root>",
            "services",
            detail=f"expected exactly {{'{box_name}'}}, found {sorted(services)}",
        )
    service = services[box_name]
    for key in service:
        if key not in SERVICE_ALLOWED:
            raise _refuse(source, box_name, key)
    environment = service.get("environment") or {}
    names = (
        environment
        if isinstance(environment, dict)
        else [entry.split("=", 1)[0] for entry in environment]
    )
    reserved = reserved_env_names(names)
    if reserved:
        raise _refuse(
            source,
            box_name,
            "environment",
            detail=f"{', '.join(reserved)} reserved: stx sets these to confine the box",
        )
    if "networks" in service and service["networks"] != _SYNTHESIZED_SERVICE_NETWORKS:
        raise _refuse(source, box_name, "networks")

    build = service.get("build")
    if isinstance(build, dict):
        for key in build:
            if key not in BUILD_ALLOWED:
                raise _refuse(source, box_name, f"build.{key}")

    # Compose always fills in a computed default (`<project>_<volume-key>`)
    # for a volume with no explicit `name:` of its own — `config["name"]` is
    # the project name it resolved, "never repo-authored" per the loop
    # above, so comparing against it is how an explicit override is told
    # apart from Compose's own default.
    project = config.get("name")
    declared_volumes = config.get("volumes", {}) or {}
    for vol_name, vol_def in declared_volumes.items():
        vol_def = vol_def or {}
        if vol_def.get("external"):
            raise _refuse(source, "<root>", f"volumes.{vol_name}.external")
        if vol_def.get("driver_opts"):
            raise _refuse(source, "<root>", f"volumes.{vol_name}.driver_opts")
        if vol_def.get("labels"):
            raise _refuse(source, "<root>", f"volumes.{vol_name}.labels")
        driver = vol_def.get("driver")
        if driver is not None and driver != "local":
            raise _refuse(source, "<root>", f"volumes.{vol_name}.driver")
        name = vol_def.get("name")
        default_name = f"{project}_{vol_name}" if project else None
        cache_prefix = shared_cache_prefix(repo_id)
        if (
            name is not None
            and name != default_name
            and not name.startswith(cache_prefix)
        ):
            raise _refuse(
                source,
                "<root>",
                f"volumes.{vol_name}.name",
                detail=f"'{name}' must start with '{cache_prefix}'",
            )

    for entry in service.get("volumes", []) or []:
        _check_repo_volume_entry(
            entry, declared_volumes, source=source, box_name=box_name
        )


def _check_repo_volume_entry(
    entry, declared_volumes: dict, *, source: str, box_name: str
) -> None:
    if not isinstance(entry, dict):
        raise _refuse(
            source, box_name, "volumes", detail=f"unrecognized entry {entry!r}"
        )
    vol_type = entry.get("type")
    vol_source = entry.get("source")
    target = entry.get("target", "")
    if vol_type != "volume" or not vol_source:
        raise _refuse(source, box_name, "volumes", detail=f"bind mount at '{target}'")
    if vol_source not in declared_volumes:
        raise _refuse(
            source,
            box_name,
            "volumes",
            detail=f"undeclared named volume '{vol_source}'",
        )
    if _is_tool_reserved(target):
        raise _refuse(
            source, box_name, "volumes", detail=f"mounted at reserved path '{target}'"
        )


#: A scheme prefix or `git@` login means Compose/BuildKit treats `context`
#: as a remote git or URL context, never a local path — see
#: docs/box-contract.md.
_URL_CONTEXT_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]*://|git@)")


def check_build_context(
    config: dict, *, box_name: str, root: Path, source: str
) -> None:
    """Refuse a `build.context` that isn't a local path inside `root` — see
    docs/box-contract.md. A no-op when the service declares no `build` or no
    `context` (image-only services need neither)."""
    build = config.get("services", {}).get(box_name, {}).get("build")
    if not isinstance(build, dict):
        return
    context = build.get("context")
    if not isinstance(context, str) or not context:
        return
    if _URL_CONTEXT_RE.match(context):
        raise _refuse(
            source,
            box_name,
            "build.context",
            detail=f"'{context}' is a URL/git context, not a local path",
        )
    candidate = Path(context)
    if not candidate.is_absolute():
        candidate = root / candidate
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise _refuse(
            source,
            box_name,
            "build.context",
            detail=f"'{context}' resolves outside the repo root",
        )


def _volumes_by_target(volumes: list[dict]) -> dict[str, dict]:
    return {entry["target"]: entry for entry in volumes if "target" in entry}


def check_merged(merged: dict, overlay_alone: dict, *, source: str) -> None:
    """The post-merge check — see docs/box-contract.md. Raises StxError naming
    the file, service, and key on the first mismatch."""
    merged_services = merged.get("services", {})
    for name, overlay_service in overlay_alone.get("services", {}).items():
        merged_service = merged_services.get(name)
        if merged_service is None:
            raise StxError(
                f"{source}: service '{name}' is missing from the merged config"
            )

        for key in _EQUALITY_KEYS:
            if key not in overlay_service:
                continue
            if merged_service.get(key) != overlay_service[key]:
                raise _refuse(source, name, key)

        for key in _REFUSED_IF_UNSET_BY_OVERLAY:
            if key in overlay_service:
                continue
            if key in merged_service:
                raise _refuse(source, name, key)

        merged_volumes = _volumes_by_target(merged_service.get("volumes", []))
        for entry in overlay_service.get("volumes", []):
            target = entry.get("target")
            merged_entry = merged_volumes.get(target)
            if merged_entry is None or (
                merged_entry.get("source") != entry.get("source")
                or bool(merged_entry.get("read_only")) != bool(entry.get("read_only"))
            ):
                raise _refuse(
                    source,
                    name,
                    "volumes",
                    detail=f"missing reserved mount '{target}'",
                )

        if name == DMZ_BOX:
            extra = set(merged_service.get("environment", {})) - _DMZ_ALLOWED_ENV_KEYS
            if extra:
                raise _refuse(
                    source,
                    name,
                    "environment",
                    detail=f"unexpected key(s) {sorted(extra)}",
                )
