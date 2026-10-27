from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from ..errors import StxError

CompletedProcessRunner = Callable[..., "subprocess.CompletedProcess[str]"]

#: A wedged docker/git invocation must fail the same way a real one does,
#: not block every other command behind it forever — see docs/reference.md.
DEFAULT_COMMAND_TIMEOUT_SECONDS = 30

#: The floor every `docker compose` invocation is built from — see
#: docs/secrets.md (What reaches a box).
_BASE_ENV_KEYS = ("PATH", "HOME")
_BASE_ENV_PREFIXES = ("DOCKER_",)

#: `stx`'s own credentials — a compose file referencing one of these could
#: pull it into the box, or from the operator's shell outside the secret
#: source entirely. Refused by name.
STX_CREDENTIAL_NAMES = (
    "STX_GITLAB_MINTER_TOKEN",
    "STX_CLAUDE_OAUTH_TOKEN",
    "STX_DMZ_ANTHROPIC_API_KEY",
    "STX_POSTGRES_PASSWORD",
    "STX_REDIS_PASSWORD",
)

# Compose's own interpolation grammar, scanned from the *uninterpolated*
# config so a modifier suffix survives to tell required apart from optional
# (`config --variables` reports both the same way — see docs/secrets.md):
# bare `$NAME` or braced `${NAME}` with no modifier (required); `${NAME:-x}`/
# `${NAME-x}` (optional, default on unset/empty); `${NAME:+x}`/`${NAME+x}`
# (optional, alt-value only when set); `${NAME:?e}`/`${NAME?e}` (required,
# errors with `e` when unset). `$$` is Compose's own escape for a literal
# `$` and never a variable reference — matched first so it's never
# misparsed as the start of one.
_COMPOSE_VAR_RE = re.compile(
    r"\$\$"
    r"|\$\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)(?P<modifier>:?[-+?][^}]*)?\}"
    r"|\$(?P<bare>[A-Za-z_][A-Za-z0-9_]*)"
)

#: A variable reference inside a `${NAME<modifier>default}` default — `${B}`
#: or `$B`, but never the `$$` literal-dollar escape. stx resolves each
#: variable from the loaded secret source in a single pass, so a default that
#: itself interpolates another variable (`${A:-${B}}`) would silently not
#: expand; refuse it rather than mislead.
_NESTED_INTERP_RE = re.compile(r"\$\{|\$[A-Za-z_]")


def run_bounded(
    args: list[str],
    *,
    runner: CompletedProcessRunner = subprocess.run,
    timeout: float = DEFAULT_COMMAND_TIMEOUT_SECONDS,
    **kwargs,
) -> subprocess.CompletedProcess[str]:
    """Run a non-interactive docker/git invocation bounded by `timeout`,
    every such call in stx's own core modules goes through this. On a
    timeout, synthesizes a failed result (returncode 124, the shell
    convention) instead of letting `subprocess.TimeoutExpired` escape — so
    every existing `if result.returncode != 0` branch already handles a
    wedged daemon or lock exactly the way it handles any other failure,
    with no caller-side changes needed. Not for a command expected to run
    long while showing its own progress (an image build, a `compose up`
    that pulls/starts containers) — only for metadata reads and quick
    signals. `runner` is a seam for tests to inject a fake subprocess
    result, which never actually blocks, so `timeout` is inert there.
    """
    try:
        return runner(args, timeout=timeout, **kwargs)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args,
            124,
            stdout="",
            stderr=f"timed out after {timeout:g}s: {' '.join(args)}",
        )


def _compose_var_required(modifier: str | None) -> bool:
    """Whether a `${NAME<modifier>}` reference is required: the `?` forms
    error on unset (required); the `-`/`+` forms don't (optional); no
    modifier at all is required."""
    if modifier is None:
        return True
    op = modifier[1] if modifier[0] == ":" else modifier[0]
    return op == "?"


#: `mkstemp`'s own default (owner-only) would silently tighten every
#: caller's file below whatever a bare `write_text`/`write_bytes` produces
#: under its process umask (typically 644) — these files are read from
#: inside a container as a uid that never matches the host writer's own, so
#: losing "other read" would break that read with no caller-visible change.
_DEFAULT_ATOMIC_WRITE_MODE = 0o644


def atomic_write_bytes(
    path: Path, content: bytes, *, mode: int = _DEFAULT_ATOMIC_WRITE_MODE
) -> None:
    """Write `content` to `path` so neither a concurrent reader nor a crash
    mid-write ever sees a torn file: a unique temp file in the same
    directory (a fixed `<name>.tmp` can still collide between two
    concurrent writers, or point a reader at a crashed writer's half-done
    content), fsynced, then `os.replace()` into place — atomic on a single
    filesystem, POSIX and Windows both, unlike a direct write. `mode` is
    applied to the temp file before the rename, since `os.replace` keeps
    the replaced file's own mode, not the old target's."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def atomic_write_text(
    path: Path, content: str, *, mode: int = _DEFAULT_ATOMIC_WRITE_MODE
) -> None:
    """`atomic_write_bytes`, encoding `content` as UTF-8 text."""
    atomic_write_bytes(path, content.encode(), mode=mode)


def resolve_dockerfile_path(context: Path, build: Mapping) -> Path:
    """Where Compose itself resolves a service's `build.dockerfile`: against
    the build context, never the repo root or the compose file's own
    directory — the same rule `manifest.py`'s own `rebuild_on` resolution
    already applies to `dockerfile`, shared here so a second, diverging
    implementation doesn't resolve it differently for the actual `docker
    build` invocation (`commands/rebuild.py`)."""
    return context / build.get("dockerfile", "Dockerfile")


def compose_command(
    compose_files: list[Path], *args: str, project: str | None = None
) -> list[str]:
    """Build a `docker compose` invocation over one or more `-f` files, reading
    no env file — secrets reach the process only via the trimmed environment
    the caller builds (`base_env`/`resolve_compose_env`, see docs/secrets.md),
    never this process's own full environment. Without an explicit
    --env-file, docker compose auto-loads the project directory's .env,
    interpolating it into the sandbox stack and warning on every `$` inside
    it. `project` sets `-p`, pinning the compose project name — so a repo
    compose file's own `name:` (or the directory basename) can't decide it,
    which is what the derived `<project>_<volume>` default volume names hang
    off.
    """
    cmd = ["docker", "compose", "--env-file", "/dev/null"]
    if project is not None:
        cmd += ["-p", project]
    for f in compose_files:
        cmd += ["-f", str(f)]
    cmd += list(args)
    return cmd


def base_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """`PATH`, `HOME`, and every `DOCKER_*` variable from `environ` — the floor
    every `docker compose` invocation is built from; see docs/secrets.md.
    Callers add whatever secrets/tool variables that invocation needs on top.
    """
    environ = environ if environ is not None else os.environ
    return {
        k: v
        for k, v in environ.items()
        if k in _BASE_ENV_KEYS or k.startswith(_BASE_ENV_PREFIXES)
    }


@dataclass(frozen=True)
class ComposeVariable:
    name: str
    required: bool  # False when every reference to it carries a `:-`/`-` default


def compose_variables(config: Mapping) -> list[ComposeVariable]:
    """Every `$NAME`/`${NAME...}` Compose would interpolate, scanned from
    `config` (an uninterpolated `compose_config()` result) — required
    unless every reference to a name is one of the optional forms. Order
    follows first appearance."""
    required_by_name: dict[str, bool] = {}
    order: list[str] = []

    def _scan(value: object) -> None:
        if isinstance(value, str):
            for match in _COMPOSE_VAR_RE.finditer(value):
                name = match.group("braced") or match.group("bare")
                if name is None:
                    continue  # the `$$` escape — not a variable reference
                modifier = match.group("modifier")
                if modifier is not None and _NESTED_INTERP_RE.search(
                    modifier.replace("$$", "")
                ):
                    raise StxError(
                        f"compose file uses nested interpolation in ${{{name}...}}"
                        f" ({value!r}) — stx resolves each variable once from the "
                        "secret source and cannot expand a reference inside a "
                        "default; inline the value instead"
                    )
                required = (
                    True
                    if match.group("bare")
                    else _compose_var_required(match.group("modifier"))
                )
                if name not in required_by_name:
                    order.append(name)
                    required_by_name[name] = required
                elif required:
                    required_by_name[name] = True
        elif isinstance(value, dict):
            for v in value.values():
                _scan(v)
        elif isinstance(value, list):
            for v in value:
                _scan(v)

    _scan(config)
    return [ComposeVariable(name=n, required=required_by_name[n]) for n in order]


def refuse_stx_credential_variables(variables: list[ComposeVariable]) -> None:
    """Raises naming the first of `stx`'s own credentials a compose file
    references — see STX_CREDENTIAL_NAMES."""
    for variable in variables:
        if variable.name in STX_CREDENTIAL_NAMES:
            raise StxError(
                f"compose file references ${{{variable.name}}}, one of stx's own "
                "credentials — remove it; the box never sees this value"
            )


def refuse_base_env_variables(variables: list[ComposeVariable]) -> None:
    """Raises naming the first compose variable `base_env` would carry into the
    `docker compose` process straight from the operator's shell — `PATH`,
    `HOME`, or any `DOCKER_*`. An optional such reference (`${HOME:-}`) is left
    out of the resolved secret env but would still interpolate from that floor,
    so it must be refused outright rather than silently filled from the shell.
    """
    for variable in variables:
        name = variable.name
        if name in _BASE_ENV_KEYS or name.startswith(_BASE_ENV_PREFIXES):
            raise StxError(
                f"compose file references ${{{name}}}, which stx passes to docker "
                "compose from the host environment (PATH, HOME, DOCKER_*) — it "
                "would resolve from the operator's shell, not the secret source; "
                "remove it"
            )


def resolve_compose_env(
    variables: list[ComposeVariable], secret_env: Mapping[str, str]
) -> dict[str, str]:
    """Each compose variable's value from `secret_env` — the loaded secret
    source's own mapping (see docs/secrets.md, and `SecretSource.ensure_loaded`),
    never this process's ambient environment, so a repo's compose can't pull an
    operator's real shell variable. Required and unset/empty raises naming it;
    optional and unset/empty is omitted, never passed blank."""
    values: dict[str, str] = {}
    for variable in variables:
        value = secret_env.get(variable.name)
        if not value:
            if variable.required:
                raise StxError(
                    f"compose file references ${{{variable.name}}}, which is "
                    "not set in the loaded secret source"
                )
            continue
        values[variable.name] = value
    return values


def compose_variable_placeholders(variables: list[ComposeVariable]) -> dict[str, str]:
    """Every compose variable bound to an empty string, for a teardown:
    `compose down` must resolve each name but never uses its value."""
    return {variable.name: "" for variable in variables}


def teardown_env(
    compose_files: list[Path],
    *,
    cwd: Path,
    runner: CompletedProcessRunner = subprocess.run,
) -> dict[str, str]:
    """The trimmed `docker compose down` environment: `base_env()` plus every
    `${NAME}` any of `compose_files` references (the repo file and the
    rendered overlay alike — its own harness/forge/package-registry
    pass-throughs included) bound to `""`. Teardown never uses a secret's
    value, only resolves its name."""
    env = base_env()
    config = compose_config(
        compose_files, cwd=cwd, no_interpolate=True, env=env, runner=runner
    )
    env.update(compose_variable_placeholders(compose_variables(config)))
    return env


def compose_version(
    *, runner: CompletedProcessRunner = subprocess.run
) -> tuple[int, int, int] | None:
    """The installed `docker compose` version as a (major, minor, patch) tuple,
    or None if it can't be determined (docker missing, unexpected output).
    `runner` is a seam for tests to inject a fake subprocess result."""
    result = run_bounded(
        ["docker", "compose", "version", "--short"],
        runner=runner,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    parts = result.stdout.strip().lstrip("v").split(".")
    if len(parts) < 3 or not all(p.isdigit() for p in parts[:3]):
        return None
    major, minor, patch = (int(p) for p in parts[:3])
    return (major, minor, patch)


def container_status(
    container: str, *, runner: CompletedProcessRunner = subprocess.run
) -> str | None:
    """A container's `.State.Status` (e.g. "running", "exited"), or None if
    it doesn't exist. `runner` is a seam for tests to inject a fake
    subprocess result."""
    result = run_bounded(
        ["docker", "inspect", "--format", "{{.State.Status}}", container],
        runner=runner,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def container_started_at(
    container: str, *, runner: CompletedProcessRunner = subprocess.run
) -> str | None:
    """A container's `.State.StartedAt` (RFC3339Nano, UTC) — the instant
    Docker most recently started it, which changes across a stop/start even
    though the container id doesn't. Used to tell a still-running process
    apart from the kernel having reused its pid for something else after a
    restart. None if the container doesn't exist. `runner` is a seam for
    tests to inject a fake subprocess result."""
    result = run_bounded(
        ["docker", "inspect", "--format", "{{.State.StartedAt}}", container],
        runner=runner,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def container_recent_logs(
    container: str, *, lines: int = 20, runner: CompletedProcessRunner = subprocess.run
) -> str:
    """A container's last `lines` lines of stdout+stderr via `docker logs
    --tail`, trimmed — used to explain why a box died rather than reached
    the outcome a caller was waiting on. Empty string if the container
    doesn't exist or has logged nothing. `runner` is a seam for tests to
    inject a fake subprocess result."""
    result = run_bounded(
        ["docker", "logs", "--tail", str(lines), container],
        runner=runner,
        capture_output=True,
        text=True,
    )
    return ((result.stdout or "") + (result.stderr or "")).strip()


def container_health(
    container: str, *, runner: CompletedProcessRunner = subprocess.run
) -> str | None:
    """A running container's `.State.Health.Status` ("healthy"/"unhealthy"/
    "starting"), or None if it declares no healthcheck (or doesn't exist).
    `runner` is a seam for tests to inject a fake subprocess result."""
    result = run_bounded(
        ["docker", "inspect", "--format", "{{.State.Health.Status}}", container],
        runner=runner,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value if value and value != "<no value>" else None


def container_label(
    container: str, label: str, *, runner: CompletedProcessRunner = subprocess.run
) -> str | None:
    """A running container's own value for `label`, or None if absent or the
    container doesn't exist. `runner` is a seam for tests to inject a fake
    subprocess result."""
    result = run_bounded(
        [
            "docker",
            "inspect",
            "--format",
            f'{{{{index .Config.Labels "{label}"}}}}',
            container,
        ],
        runner=runner,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value if value and value != "<no value>" else None


def container_image_id(
    container: str, *, runner: CompletedProcessRunner = subprocess.run
) -> str | None:
    """A running container's actual image id (`.Image`), or None if it
    doesn't exist. `runner` is a seam for tests to inject a fake subprocess
    result."""
    result = run_bounded(
        ["docker", "inspect", "--format", "{{.Image}}", container],
        runner=runner,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def compose_config_hash(
    compose_files: list[Path],
    service: str,
    *,
    cwd: Path,
    env: Mapping[str, str],
    runner: CompletedProcessRunner = subprocess.run,
) -> str | None:
    """`docker compose config --hash <service>`'s hash for one service, or
    None if the command fails (e.g. the service isn't defined in
    `compose_files`). `runner` is a seam for tests to inject a fake
    subprocess result."""
    result = run_bounded(
        compose_command(compose_files, "config", "--hash", service),
        runner=runner,
        cwd=str(cwd),
        env=dict(env),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    parts = result.stdout.strip().split()
    return parts[1] if len(parts) == 2 and parts[0] == service else None


def compose_config(
    compose_files: list[Path],
    *,
    cwd: Path,
    env: Mapping[str, str],
    no_interpolate: bool = False,
    project: str | None = None,
    runner: CompletedProcessRunner = subprocess.run,
) -> dict:
    """Run `docker compose ... config --format json` and return the parsed result.

    `env` is always explicit, never this process's own full environment —
    build it with `base_env`/`resolve_compose_env` (see docs/secrets.md) plus
    whatever secrets this specific check needs. `no_interpolate` matches the
    repo allow-list check's own need to normalize a repo file without
    resolving `${VAR}` references against the calling environment —
    `overlay.check_repo_compose`'s only caller. `project` pins the compose
    project name (`-p`), so the repo file's own `name:` can't drive the
    derived default volume names. `runner` is a seam for tests to inject a
    fake subprocess result without invoking Docker.
    """
    args = ["config", "--format", "json"]
    if no_interpolate:
        args.append("--no-interpolate")
    result = run_bounded(
        compose_command(compose_files, *args, project=project),
        runner=runner,
        cwd=str(cwd),
        env=dict(env),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise StxError(f"docker compose config failed: {result.stderr.strip()}")
    return json.loads(result.stdout)
