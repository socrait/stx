"""The one provision path `new` and `refresh` share.

A step either raises (nothing it created survives) or records its effect on the
mutable `ProvisionState` and registers an undo for whatever it created. `new`
runs the full sequence and, on any failure, unwinds the undo stack — container
teardown there goes through `lifecycle.teardown`, the same path `rm`/`prune`
use. `refresh` re-runs only the box half (contract checks, image, then egress,
overlay, env, containers, setup wait) over a sandbox `new` already built; it
never unwinds. A failure before the writes (a non-compliant repo, a failed
image build) leaves the old allowlist, overlay, image, and running box
untouched; a `compose up` or route-write failure can leave the box partially
recreated on the fresh files. Either way it revokes only the fresh tokens it
minted, never the old box's own.

Steps print their own progress; failures surface as `StxError` (or
`StepAlreadyReported`, when a helper already printed) for the command layer to
report. See docs/reference.md (Provisioning, Branches, Teardown guards).
"""

from __future__ import annotations

import shutil
import signal
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .. import dmz, egress, overlay
from .. import network as stx_network
from .. import platform as stx_platform
from ..commands import rebuild
from ..core import (
    DMZ_BOX,
    BoxRecord,
    ForgeSourceRecord,
    LinkRecord,
    SandboxRecord,
    SetupState,
    Zone,
    base_env,
    branch_name,
    code_dmz_dir,
    code_dmz_root,
    compose,
    container_name_for_box,
    drop_dir,
    drop_dmz_dir,
    identity_provenance_summary,
    images,
    locked_registry,
    logs_dir,
    logs_root,
    remove_locked_worktree,
    resources,
    routing,
    sandbox_compose_project,
    setup_status,
    worktree,
    worktree_path,
)
from ..core.paths import repo_cache_id
from ..errors import StxError
from ..forge import ForgeCredential, mint_all
from ..forge.env import render_box_env
from ..forge.registry import build_forge_record
from ..manifest import Manifest
from ..platform import PostgresAdminParams, RedisAdminParams
from .spec import SandboxSpec
from .teardown import bring_down_containers


class StepAlreadyReported(StxError):
    """Raised by a step whose own helper already printed its error — the
    command must return 1 without printing again."""

    def __init__(self) -> None:
        super().__init__("")


class RollbackIncomplete(StxError):
    """`new`'s rollback could not reclaim everything, so the registry entry is
    kept in `provisioning` (carrying its redis index, database names, and any
    minted token ids) for `stx rm` to retry, rather than dropped."""

    def __init__(self, slug: str, failed: list[str]) -> None:
        self.slug = slug
        self.failed = failed
        super().__init__(
            f"sandbox '{slug}' left registered mid-provisioning — rollback "
            f"could not finish: {', '.join(failed)}. Run `stx rm {slug}` to "
            "retry teardown."
        )


@dataclass
class _Undo:
    label: str
    fn: Callable[[], None]


@dataclass
class ProvisionState:
    """Mutable scratch threaded through the steps — what provisioning creates,
    plus the undo stack `new` unwinds on failure."""

    undo: list[_Undo] = field(default_factory=list)
    boxes: dict[str, BoxRecord] = field(default_factory=dict)
    wt_path: Path | None = None
    base_sha: str = ""
    user: str | None = None
    db_params: PostgresAdminParams | None = None
    forge_credentials: list[ForgeCredential] = field(default_factory=list)
    dmz_image: str | None = None
    dmz_user: str | None = None
    dmz_snapshot: Path | None = None
    tailscale_fqdn: str | None = None
    tailscale_ip: str | None = None
    route_bind_addr: str | None = None
    overlay_path: Path | None = None
    compose_env: dict[str, str] = field(default_factory=dict)
    setup_error: str | None = None


@dataclass
class ProvisionOutcome:
    """What `provision_new` leaves for the command layer to report on."""

    boxes: dict[str, BoxRecord]
    setup_error: str | None
    tailscale_fqdn: str | None


# --------------------------------------------------------------------------- #
# Rollback machinery
# --------------------------------------------------------------------------- #


def _install_termination_handlers() -> Callable[[], None]:
    """Make SIGTERM/SIGHUP raise KeyboardInterrupt so an orchestration in
    flight unwinds through the normal rollback path rather than the process
    dying outright. Returns a callable that restores the previous handlers."""

    def _raise(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    signals = [signal.SIGTERM]
    if hasattr(signal, "SIGHUP"):
        signals.append(signal.SIGHUP)
    previous = {sig: signal.signal(sig, _raise) for sig in signals}

    def _restore() -> None:
        for sig, handler in previous.items():
            signal.signal(sig, handler)

    return _restore


def _unwind(undo_stack: list[_Undo]) -> list[str]:
    """Run every undo in reverse registration order, isolating failures so one
    doesn't stop the rest — see docs/reference.md (Branches, rollback
    paragraph). A second Ctrl+C here interrupts the unwind itself; the
    current step and everything still queued behind it are named as skipped
    before re-raising.

    Returns the labels of steps that failed. A non-empty result means rollback
    left resources behind: the caller must keep the registry entry so `stx rm`
    can retry, rather than drop it and lose track of what's still there."""
    failed: list[str] = []
    remaining = list(reversed(undo_stack))
    for i, step in enumerate(remaining):
        try:
            step.fn()
        except KeyboardInterrupt:
            skipped = ", ".join(s.label for s in remaining[i:])
            print(
                f"  warning: rollback interrupted — skipped: {skipped}", file=sys.stderr
            )
            raise
        except Exception as e:  # noqa: BLE001 — best-effort cleanup, never propagate
            failed.append(step.label)
            print(
                f"  warning: rollback step '{step.label}' failed: {e}", file=sys.stderr
            )
    return failed


def _cleanup_ephemeral_dirs(slug: str) -> None:
    """Idempotent cleanup for directories that are always safe to remove
    outright, regardless of which step failed — mirrors `stx rm`'s own tail."""
    shutil.rmtree(drop_dir(slug), ignore_errors=True)
    shutil.rmtree(drop_dmz_dir(slug), ignore_errors=True)
    shutil.rmtree(code_dmz_root(slug), ignore_errors=True)
    shutil.rmtree(logs_root(slug), ignore_errors=True)


def _unreserve(slug: str) -> None:
    with locked_registry() as reg:
        reg.unreserve(slug)


# --------------------------------------------------------------------------- #
# Up-front repo-compose contract (command layer, before any change)
# --------------------------------------------------------------------------- #


def repo_compose_config(root: Path, manifest: Manifest, slug: str) -> dict:
    """The repo's own compose file parsed with no interpolation — the input to
    both the allow-list checks and the host-variable scan. Pinned to this
    sandbox's own `-p stx-<slug>` project so the derived default volume names
    are the ones Compose will actually create at `up`, not ones a repo-authored
    top-level `name:` (or the checkout's directory basename) would decide."""
    repo_compose = root / manifest.compose
    return compose.compose_config(
        [repo_compose],
        cwd=root,
        no_interpolate=True,
        env=base_env(),
        project=sandbox_compose_project(slug),
    )


def collect_host_vars(repo_config: dict) -> list:
    """The repo compose file's own referenced variables, refusing any that name
    an stx credential or that `base_env` would fill from the operator's shell
    (see docs/secrets.md)."""
    host_vars = compose.compose_variables(repo_config)
    compose.refuse_stx_credential_variables(host_vars)
    compose.refuse_base_env_variables(host_vars)
    return host_vars


def check_repo_compose_contract(
    repo_config: dict, *, manifest: Manifest, root: Path
) -> None:
    """The box-contract allow-list and build-context checks on the repo file
    alone — runs before anything stateful so a non-compliant repo is refused
    before it is touched."""
    repo_compose = root / manifest.compose
    overlay.refuse_repo_project_name(repo_compose, source=str(repo_compose))
    overlay.check_repo_compose(
        repo_config,
        box_name=manifest.name,
        source=str(repo_compose),
        repo_id=repo_cache_id(root),
    )
    overlay.check_build_context(
        repo_config, box_name=manifest.name, root=root, source=str(repo_compose)
    )


# --------------------------------------------------------------------------- #
# Shared box-half steps — used by both `new` and `refresh`
# --------------------------------------------------------------------------- #


def _render_egress(spec: SandboxSpec, state: ProvisionState) -> None:
    egress.render(
        spec.manifest,
        spec.slug,
        forge_host=spec.forge.host if spec.forge is not None else None,
    )
    slug = spec.slug
    state.undo.append(_Undo("remove egress allowlist", lambda: egress.remove(slug)))


def _render_overlay(spec: SandboxSpec, state: ProvisionState) -> None:
    state.overlay_path = overlay.render(
        spec.slug,
        spec.manifest,
        user=state.user,
        worktree=state.wt_path,
        repo_root=spec.root,
        redis_index=spec.redis_index,
        tailnet_port=spec.tailnet_port,
        tailscale_ip=state.tailscale_ip,
        claude_auth_mode=spec.claude_auth_mode,
        dmz_image=state.dmz_image if spec.with_dmz else None,
        dmz_user=state.dmz_user if spec.with_dmz else None,
        dmz_snapshot=state.dmz_snapshot if spec.with_dmz else None,
        links=spec.links,
        route_ports=spec.route_ports,
        route_bind_addr=state.route_bind_addr,
        databases=spec.databases,
    )
    slug = spec.slug
    state.undo.append(_Undo("remove overlay file", lambda: overlay.remove(slug)))


def _build_compose_env(spec: SandboxSpec, state: ProvisionState) -> None:
    """The trimmed `docker compose` process environment — base vars, the repo
    compose file's own referenced variables, and the harness/forge secret
    values the overlay's bare pass-through entries reference (see
    docs/secrets.md) — never this process's own full environment."""
    env = base_env()
    env.update(compose.resolve_compose_env(spec.host_vars, spec.secret_env))
    env.update(
        overlay.box_secret_env(
            manifest=spec.manifest,
            claude_auth_mode=spec.claude_auth_mode,
            with_dmz=spec.with_dmz,
            forge_env=(
                render_box_env(spec.forge, state.forge_credentials, spec.manifest)
                if spec.forge is not None
                else None
            ),
        )
    )
    state.compose_env = env


def _check_merged(spec: SandboxSpec, state: ProvisionState) -> None:
    """The post-merge box-contract check — the overlay merged with the repo
    file, and the overlay alone for comparison. A dmz-only sandbox has no repo
    file, so the merge is the overlay alone (one normalization covers both
    arguments)."""
    overlay_path = state.overlay_path
    if spec.manifest is not None:
        repo_compose = spec.root / spec.manifest.compose
        merged = compose.compose_config(
            [repo_compose, overlay_path], cwd=spec.root, env=state.compose_env
        )
        overlay_alone = compose.compose_config(
            [overlay_path], cwd=spec.root, env=state.compose_env
        )
    else:
        overlay_alone = compose.compose_config(
            [overlay_path], cwd=spec.root or Path.cwd(), env=state.compose_env
        )
        merged = overlay_alone
    overlay.check_merged(merged, overlay_alone, source=str(overlay_path))


def await_box_setup(slug: str, box_name: str, *, timeout: float) -> str | None:
    """Block until a box's `setup` scripts finish, returning an error string
    on failure/timeout rather than raising — a setup failure is a normal (if
    disappointing) outcome the sandbox should survive, left running for the
    human to debug (see docs/box-contract.md, step 8), never a reason to unwind
    everything just provisioned. Returns None on success."""
    container = container_name_for_box(box_name, slug)
    print(f"  waiting for setup in '{box_name}' (timeout {timeout:g}s)")
    try:
        status = setup_status.wait_for_setup(container, box_name, timeout=timeout)
    except StxError as e:
        return str(e)
    if status.state == SetupState.FAILED:
        log_path = logs_dir(slug, box_name) / "setup.log"
        return (
            f"setup script '{status.script}' failed with exit code "
            f"{status.exit_code} in '{box_name}' — see {log_path}"
        )
    print("  setup done")
    return None


def _run_compose_up(
    spec: SandboxSpec, state: ProvisionState, up_services: list[str]
) -> int:
    compose_files = _compose_files(spec, state)
    result = subprocess.run(
        compose.compose_command(compose_files, "up", "-d", *up_services),
        cwd=str(spec.root) if spec.root else None,
        env=state.compose_env,
    )
    return result.returncode


def _compose_files(spec: SandboxSpec, state: ProvisionState) -> list[Path]:
    manifest = spec.manifest
    if manifest is not None:
        return [spec.root / manifest.compose, state.overlay_path]
    return [state.overlay_path]


# --------------------------------------------------------------------------- #
# `new`-only steps
# --------------------------------------------------------------------------- #


def _ensure_sandbox_network(spec: SandboxSpec, state: ProvisionState) -> None:
    """Create this sandbox's own network and connect the platform services it
    uses, then register the undo. The ensure-and-connect itself is the shared
    `connect_services_to_sandbox_network` that `stx start` also reuses, so the
    two never drift. See docs/security.md (Isolation)."""
    slug = spec.slug
    stx_platform.connect_services_to_sandbox_network(slug, spec.services)
    services = [s for s in spec.services if s != "traefik"]
    state.undo.append(
        _Undo(
            "disconnect sandbox network",
            lambda: _teardown_sandbox_network(slug, services),
        )
    )


def _teardown_sandbox_network(slug: str, services: list[str]) -> None:
    net = stx_network.sandbox_network_name(slug)
    for service in services:
        stx_network.disconnect(net, stx_platform.container_name(service))
    stx_network.remove_network(net)


def _create_worktree(spec: SandboxSpec, state: ProvisionState, *, yes: bool) -> None:
    root = spec.root
    branch = spec.branch
    local_branch = branch_name(spec.slug)
    wt_path = worktree_path(spec.slug)

    print(f"  creating worktree {wt_path} on {local_branch} (pushes to {branch})")
    # local_branch is unique per slug by construction (registry-enforced),
    # so it can never collide with another live worktree the way a
    # target-named local branch could — no collision fallback needed.
    worktree_args = _worktree_branch_args(
        local_branch, branch, spec.base, spec.base_explicit, root, yes=yes
    )
    result = subprocess.run(
        [
            "git",
            "worktree",
            "add",
            # --lock: an in-box `git worktree prune` can't unregister this —
            # see docs/reference.md (Branches).
            "--lock",
            "--reason",
            "sandbox worktree — remove via `stx rm`/`stx prune`, "
            "never `git worktree prune`",
            str(wt_path),
            *worktree_args,
        ],
        cwd=str(root),
        capture_output=True,
        text=True,
    )
    if result.stdout:
        print(result.stdout, end="")
    # Only a freshly created local_branch gets deleted on rollback — one
    # this `new` call reused (worktree_args == [local_branch]) may already
    # carry history from a prior sandbox on this slug.
    created_branch = local_branch if worktree_args[0] == "-b" else None
    if result.returncode != 0:
        # `worktree add -b` creates the branch before it checks the path, so
        # a failed add can still leave a fresh branch behind.
        # A "branch already exists" failure means it isn't ours to delete.
        if created_branch is not None and "a branch named" not in result.stderr:
            subprocess.run(
                ["git", "branch", "-D", created_branch],
                cwd=str(root),
                capture_output=True,
            )
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
        if "already checked out at" in result.stderr:
            print(
                "  hint: remove the stale worktree with "
                "`git worktree remove --force --force <path>` (doubled — "
                "it's locked) then retry",
                file=sys.stderr,
            )
        raise StxError("git worktree add failed")

    state.wt_path = wt_path
    state.undo.append(
        _Undo(
            "remove worktree",
            lambda: _cleanup_worktree(root, wt_path, created_branch),
        )
    )

    # Rewritten unconditionally — never trusted from a prior state, since a
    # reused local_branch may already be tracking something stale.
    tracking_config = {
        f"branch.{local_branch}.remote": "origin",
        f"branch.{local_branch}.merge": f"refs/heads/{branch}",
    }
    for key, value in tracking_config.items():
        tracking_result = subprocess.run(
            ["git", "config", key, value],
            cwd=str(root),
            capture_output=True,
            text=True,
        )
        if tracking_result.returncode != 0:
            print(
                f"  warning: could not set {local_branch}'s tracking to "
                f"{branch} ({tracking_result.stderr.strip()}) — a plain "
                "`git push` inside the box may not land where expected; "
                f"fix with `git config branch.{local_branch}.merge "
                f"refs/heads/{branch}` in the worktree",
                file=sys.stderr,
            )
            break

    # Marks this branch config as stx's own, so `stx prune`'s orphan sweep
    # only ever touches config it created itself — never a `stx/<slug>`
    # branch another tool set up sharing this repo (see docs/reference.md,
    # Branches).
    subprocess.run(
        ["git", "config", f"branch.{local_branch}.stx", "true"],
        cwd=str(root),
        capture_output=True,
    )

    sha_result = subprocess.run(
        ["git", "-C", str(wt_path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
    )
    state.base_sha = sha_result.stdout.strip() if sha_result.returncode == 0 else ""


def _prepare_handoff_dirs(spec: SandboxSpec) -> None:
    # Pre-created so docker never auto-creates the bind source root-owned
    # (see docs/reference.md, State layout).
    drop_dir(spec.slug).mkdir(parents=True, exist_ok=True)
    drop_dmz_dir(spec.slug).mkdir(parents=True, exist_ok=True)
    logs_dir(spec.slug, spec.manifest.name).mkdir(parents=True, exist_ok=True)


def _check_databases(
    spec: SandboxSpec, state: ProvisionState, registry: dict[str, SandboxRecord]
) -> None:
    databases = spec.manifest.render_databases(spec.slug, links=spec.links)
    existing = resources.existing_databases(databases, state.db_params)
    if existing:
        raise StxError(_leftover_databases_error(spec.slug, existing, registry))
    db_params = state.db_params
    state.undo.append(
        _Undo("drop databases", lambda: resources.drop_databases(databases, db_params))
    )


def _leftover_databases_error(
    slug: str, existing: list[str], registry: dict[str, SandboxRecord]
) -> str:
    """Names what's left behind, and the fix — `stx rm <owner>` if a
    registered sandbox still claims them, else how to drop them by hand.
    Never adopted automatically: see docs/reference.md (`new` row)."""
    names = ", ".join(existing)
    owner = next(
        (
            other_slug
            for other_slug, record in registry.items()
            if other_slug != slug and set(existing) & set(record.databases)
        ),
        None,
    )
    if owner is not None:
        return (
            f"database(s) {names} already exist, left from sandbox '{owner}'. "
            f"Run `stx rm {owner}` to clean them up first."
        )
    return (
        f"database(s) {names} already exist, left from an earlier sandbox "
        "that's no longer registered. Drop them by hand (e.g. `DROP DATABASE "
        f"{existing[0]}` via psql against the platform Postgres) before "
        "retrying."
    )


def _mint_forge(spec: SandboxSpec, state: ProvisionState) -> None:
    if spec.forge is None:
        return
    forge = spec.forge
    credentials = mint_all(forge, spec.slug, read_only=spec.read_only)
    for credential in credentials:
        print(f"  minted {credential.kind.value} credential ({credential.scope})")
    state.forge_credentials = credentials

    def _revoke() -> None:
        # Attempt every credential, then raise if any revoke failed: a token
        # left live on the forge is a leaked resource, so the unwind must
        # report this step as failed and keep the provisioning entry (with its
        # recorded ids) for `stx rm` to retry — never drop it and lose track.
        results = [
            forge.revoke(credential.kind, credential.scope_id, credential.token_id)
            for credential in credentials
        ]
        if not all(results):
            raise StxError("one or more forge token revocations failed")

    state.undo.append(_Undo("revoke forge credentials", _revoke))


def _record_pending_forge(spec: SandboxSpec, state: ProvisionState) -> None:
    """Persist the just-minted forge token ids onto the (still provisioning)
    registry entry immediately, before any later step that could crash the
    process outright and leave them untracked — `rm`/`prune` read them back to
    revoke what a crash left behind. Overwritten by `finish` on success."""
    if spec.forge is None:
        return
    manifest = spec.manifest
    forge_record = build_forge_record(manifest.forge.kind, state.forge_credentials)
    source = ForgeSourceRecord(
        kind=manifest.forge.kind,
        host=spec.forge.host,
        project=manifest.forge.project,
    )
    with locked_registry() as reg:
        reg.record_pending_forge(spec.slug, forge_record, source)


def _ensure_image_and_resolve_user(spec: SandboxSpec, state: ProvisionState) -> None:
    """Builds/rebuilds the image if stale, then resolves `user` — the override,
    or the now-guaranteed-to-exist image's own `USER` (see docs/box-contract.md,
    Image USER)."""
    manifest = spec.manifest
    ret = rebuild.ensure_image_fresh(spec.root, manifest)
    if ret:
        raise StepAlreadyReported()
    try:
        state.user = manifest.resolve_user()
    except StxError as e:
        print(f"error: {e}", file=sys.stderr)
        raise StepAlreadyReported() from e


def _build_box_record(spec: SandboxSpec, state: ProvisionState) -> None:
    manifest = spec.manifest
    state.boxes[manifest.name] = BoxRecord(
        zone=Zone.TRUSTED,
        user=state.user,
        branch=spec.branch,
        worktree=str(state.wt_path),
        base_sha=state.base_sha,
        image=manifest.image,
        claude_auth_mode=spec.claude_auth_mode,
        forge=(
            build_forge_record(manifest.forge.kind, state.forge_credentials)
            if spec.forge is not None
            else None
        ),
        forge_source=(
            ForgeSourceRecord(
                kind=manifest.forge.kind,
                host=spec.forge.host,
                project=manifest.forge.project,
            )
            if spec.forge is not None
            else None
        ),
        # Stored unconditionally — a sandbox linked to later needs its own
        # routes resolvable from the registry alone (see docs/manifest.md).
        routes=dict(manifest.routes),
        links=dict(spec.links),
    )


def _prepare_dmz(spec: SandboxSpec, state: ProvisionState) -> None:
    root = spec.root
    dmz_image = dmz.ensure_built()
    dmz_user = images.image_user(dmz_image)
    if not dmz_user or dmz_user == "root":
        raise StxError(f"no dmz box user: image '{dmz_image}' has no non-root USER")
    drop_dmz_dir(spec.slug).mkdir(parents=True, exist_ok=True)
    code_dmz_dir(spec.slug, spec.repo_name or "repo").mkdir(parents=True, exist_ok=True)
    logs_dir(spec.slug, DMZ_BOX).mkdir(parents=True, exist_ok=True)

    snapshot_sha = state.base_sha or (
        _resolve_ref(spec.base or worktree.resolve_default_base_branch(root), root)
        if root
        else ""
    )
    dmz_snapshot: Path | None = None
    if root:
        ret = _create_dmz_snapshot(
            root, spec.slug, snapshot_sha, spec.repo_name or "repo"
        )
        if ret:
            raise StepAlreadyReported()
        dmz_snapshot = code_dmz_dir(spec.slug, spec.repo_name or "repo")
        slug = spec.slug
        state.undo.append(
            _Undo(
                "remove dmz snapshot",
                lambda: shutil.rmtree(code_dmz_root(slug), ignore_errors=True),
            )
        )

    state.dmz_image = dmz_image
    state.dmz_user = dmz_user
    state.dmz_snapshot = dmz_snapshot
    state.boxes[DMZ_BOX] = BoxRecord(
        zone=Zone.DMZ, user=dmz_user, base_sha=snapshot_sha, image=dmz_image
    )


def _register_redis_flush(spec: SandboxSpec, state: ProvisionState) -> None:
    """Register the rollback flush of this sandbox's own Redis db-index.

    The index is freed the instant the registry reservation is dropped (the
    allocator only looks at live entries), so if the box came up and wrote to
    it before the failure, the next sandbox to reuse that index would inherit
    this one's leftover keys. Flushing on rollback — before the reservation is
    freed — is what keeps that from happening. Registered alongside the
    compose-down undo (only once the box could actually have written), so an
    early failure doesn't pointlessly try — and fail — to flush an index
    nothing ever touched."""
    redis_index = spec.redis_index
    if redis_index is None:
        return

    def _flush() -> None:
        resources.flush_redis(redis_index, RedisAdminParams.build())

    state.undo.append(_Undo("flush redis index", _flush))


def _register_compose_teardown(spec: SandboxSpec, state: ProvisionState) -> None:
    """Register the undos for anything a `compose run` or `compose up` leaves —
    the container bring-down and the Redis flush — *before* the first compose
    invocation, so an interrupted provision step (its own `compose run`) is
    unwound too, not just a failed `up`.

    The flush is registered first and the down second, so on rollback (reverse
    order) the down runs first and the flush second: the index is freed only
    once the box that could write to it is gone. The down goes through the
    shared `bring_down_containers` (manifest-free, bounded) and *raises* when
    it isn't clean, so a failed down keeps the provisioning entry (via
    `RollbackIncomplete`) rather than counting as success and freeing a Redis
    index out from under a box still running."""
    _register_redis_flush(spec, state)
    slug = spec.slug

    def _down() -> None:
        result = bring_down_containers(slug)
        if not result.clean:
            raise StxError(
                f"sandbox '{slug}' containers could not be brought down during rollback"
            )

    state.undo.append(_Undo("compose down", _down))


def _compose_up_new(spec: SandboxSpec, state: ProvisionState) -> None:
    # The compose-down and redis-flush undos were registered by
    # `_register_compose_teardown` before any compose invocation, so a partial
    # `up` (one service starts, another doesn't) is already covered here.
    manifest = spec.manifest
    up_services = [manifest.name] if manifest else []
    if spec.with_dmz:
        up_services.append("dmz")
        print(
            "  starting dmz sidecar (spend-capped harness key only, open "
            "egress, own network)"
        )
    if manifest is not None:
        if spec.redis_index is not None:
            print(f"  starting '{manifest.name}' box (redis db {spec.redis_index})")
        else:
            print(f"  starting '{manifest.name}' box")

    if _run_compose_up(spec, state, up_services) != 0:
        raise StxError("sandbox containers failed to start")


def _provision_steps_step(spec: SandboxSpec, state: ProvisionState) -> None:
    manifest = spec.manifest
    repo_compose = spec.root / manifest.compose
    # A repo's own provisioning steps connect as the non-superuser
    # `stx_provisioner` role, never the platform admin — see
    # docs/secrets.md (Provisioning credentials).
    provisioner = stx_platform.provisioner_params(state.db_params)
    admin_vars = {
        "PGHOST": provisioner.host,
        "PGPORT": str(provisioner.port),
        "PGUSER": provisioner.user,
        "PGPASSWORD": provisioner.password,
    }
    ret = _run_provision_steps(
        manifest,
        spec.slug,
        [repo_compose, state.overlay_path],
        spec.root,
        redis_index=spec.redis_index,
        admin_vars=admin_vars,
        compose_env=state.compose_env,
        links=spec.links,
        host_vars=spec.host_vars,
        secret_env=spec.secret_env,
    )
    if ret:
        raise StepAlreadyReported()


def _write_route_new(spec: SandboxSpec, state: ProvisionState) -> None:
    try:
        routing.write_traefik_route(spec.slug, spec.route_ports)
    except OSError as e:
        raise StxError(f"failed to write Traefik route: {e}") from e
    slug = spec.slug
    state.undo.append(
        _Undo("remove traefik route", lambda: routing.remove_traefik_route(slug))
    )

    warning = routing.traefik_routes_warning(stx_platform.container_name("traefik"))
    if warning:
        print(f"  {warning}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Git / worktree helpers
# --------------------------------------------------------------------------- #


def _content_source(
    target_branch: str, base: str | None, base_explicit: bool, root: Path
) -> str:
    """The ref a box's content — or a reused local_branch's divergence
    baseline — should be judged against. An explicit --base always wins;
    absent that, prefers target_branch's own local ref when it isn't behind
    origin (letting a continuation box pick up not-yet-pushed work), else
    origin/<target_branch>, else `base` (or, left unset, the resolved
    default base branch) when the target has no history anywhere yet — see
    docs/reference.md (`new`, `--base`).
    """
    if base_explicit:
        return base

    local_target = subprocess.run(
        ["git", "rev-parse", "--verify", f"refs/heads/{target_branch}"],
        cwd=str(root),
        capture_output=True,
    )
    remote_target = subprocess.run(
        ["git", "rev-parse", "--verify", f"refs/remotes/origin/{target_branch}"],
        cwd=str(root),
        capture_output=True,
    )

    if local_target.returncode == 0:
        if remote_target.returncode != 0:
            return target_branch
        ahead = subprocess.run(
            [
                "git",
                "merge-base",
                "--is-ancestor",
                f"origin/{target_branch}",
                target_branch,
            ],
            cwd=str(root),
            capture_output=True,
        )
        return target_branch if ahead.returncode == 0 else f"origin/{target_branch}"

    return (
        f"origin/{target_branch}"
        if remote_target.returncode == 0
        else (base or worktree.resolve_default_base_branch(root))
    )


def _divergent_commits(
    branch: str, comparison_ref: str, root: Path
) -> list[tuple[str, str]] | None:
    """Commits on `branch` with no patch-equivalent in `comparison_ref`'s
    history (`git cherry`, which compares by patch content, not SHA — robust
    to a rebased `comparison_ref`). Returns None, never [], when the check
    itself couldn't run; the caller must not treat that as confirmed-clean.
    """
    result = subprocess.run(
        ["git", "cherry", "-v", comparison_ref, branch],
        cwd=str(root),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    divergent = []
    for line in result.stdout.splitlines():
        if line.startswith("+ "):
            sha, _, subject = line[2:].partition(" ")
            divergent.append((sha, subject))
    return divergent


def _reconcile_stx_branch(
    branch: str, comparison_ref: str, root: Path, *, yes: bool
) -> None:
    """Fast-forward `branch` to `comparison_ref` if `_divergent_commits` finds
    no real divergence (safe: nothing would be lost); otherwise never
    auto-resolves — prints what it found and asks, defaulting to keeping
    `branch` as-is on `--yes` or non-interactive stdin — see docs/reference.md
    (Branches). A failed divergence check is treated the same as "found
    something", never as "confirmed safe".
    """
    divergent = _divergent_commits(branch, comparison_ref, root)
    if divergent == []:
        result = subprocess.run(
            ["git", "branch", "-f", branch, comparison_ref],
            cwd=str(root),
            capture_output=True,
        )
        if result.returncode != 0:
            print(
                f"  warning: could not fast-forward {branch} to {comparison_ref} "
                "(branch may still be checked out elsewhere — reusing current tip)",
                file=sys.stderr,
            )
        return

    if divergent is None:
        print(
            f"  warning: could not check {branch} for divergent commits against "
            f"{comparison_ref} — leaving {branch} as-is",
            file=sys.stderr,
        )
        return

    print(f"  {branch} has {len(divergent)} commit(s) not present in {comparison_ref}:")
    for sha, subject in divergent:
        print(f"    {sha[:8]} {subject}")

    if yes:
        print(
            f"  --yes: keeping {branch}'s own commits (not resetting to {comparison_ref})"
        )
        return
    if not sys.stdin.isatty():
        print(
            f"  non-interactive: keeping {branch}'s own commits "
            f"(not resetting to {comparison_ref})"
        )
        return

    while True:
        choice = (
            input(
                f"  Keep {branch}'s own commits [k], or discard them and reset "
                f"to {comparison_ref} [d]? [k/d] "
            )
            .strip()
            .lower()
        )
        if choice in ("", "k", "keep"):
            return
        if choice in ("d", "discard"):
            subprocess.run(
                ["git", "branch", "-f", branch, comparison_ref], cwd=str(root)
            )
            return
        print("  Please enter 'k' or 'd'.")


def _worktree_branch_args(
    local_branch: str,
    target_branch: str,
    base: str | None,
    base_explicit: bool,
    root: Path,
    *,
    yes: bool = False,
) -> list[str]:
    """Return the trailing args for `git worktree add <path> ...`. Always
    selects/creates `local_branch` (never `target_branch` itself — see
    docs/reference.md, Branches); content comes from `_content_source` either
    way. Tracking is configured by the caller after `git worktree add`
    succeeds, unconditionally, so it's re-synced even when `local_branch`
    already existed with stale tracking from an earlier sandbox on this slug.
    When `local_branch` already exists, reconciles it via
    `_reconcile_stx_branch` before reuse.
    """
    content_source = _content_source(target_branch, base, base_explicit, root)

    local = subprocess.run(
        ["git", "rev-parse", "--verify", f"refs/heads/{local_branch}"],
        cwd=str(root),
        capture_output=True,
    )
    if local.returncode == 0:
        _reconcile_stx_branch(local_branch, content_source, root, yes=yes)
        return [local_branch]

    return ["-b", local_branch, content_source]


def _resolve_ref(ref: str, root: Path) -> str:
    """Resolve a ref to a commit SHA. Used for the dmz snapshot when there's no worktree."""
    result = subprocess.run(
        ["git", "rev-parse", ref], cwd=str(root), capture_output=True, text=True
    )
    return result.stdout.strip() if result.returncode == 0 else ref


def _create_dmz_snapshot(root: Path, slug: str, sha: str, repo: str) -> int | None:
    """Populate the dmz snapshot dir with a git-archive checkout of sha (no
    .git dir) — see docs/security.md on why a snapshot, never a worktree."""
    dest = code_dmz_dir(slug, repo)
    dest.mkdir(parents=True, exist_ok=True)
    print(f"  creating dmz snapshot at {sha[:12] if sha else sha}")
    archive = subprocess.run(
        ["git", "archive", sha], cwd=str(root), capture_output=True
    )
    if archive.returncode != 0:
        print(
            f"error: git archive failed for {sha}: "
            f"{archive.stderr.decode(errors='replace')}",
            file=sys.stderr,
        )
        return 1
    extract = subprocess.run(
        ["tar", "-x", "-C", str(dest)], input=archive.stdout, capture_output=True
    )
    if extract.returncode != 0:
        print(
            f"error: snapshot extraction failed: "
            f"{extract.stderr.decode(errors='replace')}",
            file=sys.stderr,
        )
        return 1
    return None


def _run_provision_steps(
    manifest: Manifest,
    slug: str,
    compose_files: list[Path],
    root: Path,
    *,
    redis_index: int | None,
    admin_vars: dict[str, str],
    compose_env: dict[str, str] | None = None,
    links: dict[str, LinkRecord] | None = None,
    host_vars: list | None = None,
    secret_env: dict[str, str] | None = None,
) -> int | None:
    """Run every `provision.steps` entry as a one-shot `compose run --rm <box>`,
    in order, over the repo file merged with the rendered overlay — see
    docs/manifest.md (`provision`) for the idempotency contract this relies
    on to run unconditionally on every `stx new`. `compose_env` is the base
    process environment (see docs/secrets.md; defaults to `base_env()` for
    callers outside `new`'s own flow) — `provision.env`'s rendered overrides
    ride on top of it. `admin_vars` is the platform Postgres connection
    `stx` itself supplies (PGHOST/PGPORT/PGUSER/PGPASSWORD). `host_vars` is
    the repo's own compose file's referenced variables (see
    `compose_variables`) — `provision.env` may only resolve a host var
    already in this set, and its value comes from `secret_env` (the loaded
    secret source), never this process's own ambient environment (see
    docs/secrets.md, Provisioning credentials).
    """
    if not manifest.provision.steps:
        return None
    try:
        host_env = compose.resolve_compose_env(host_vars or [], secret_env or {})
        provision_env = manifest.render_provision_env(admin_vars, host_env)
    except StxError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    base = dict(compose_env) if compose_env is not None else base_env()
    # `compose run` passes a process variable into the container only when named.
    pass_through = [arg for name in provision_env for arg in ("-e", name)]
    for argv in manifest.render_provision_steps(
        slug=slug, redis_index=redis_index, links=links
    ):
        print(f"  provisioning: {' '.join(argv)}")
        result = subprocess.run(
            compose.compose_command(
                compose_files,
                "run",
                "--rm",
                "-e",
                "STX_PROVISION_RUN=1",
                *pass_through,
                manifest.name,
                *argv,
            ),
            env={**base, **provision_env},
            cwd=str(root),
        )
        if result.returncode != 0:
            print(f"error: provisioning step failed: {' '.join(argv)}", file=sys.stderr)
            return 1
    return None


def _cleanup_worktree(
    root: Path, wt_path: Path, created_branch: str | None = None
) -> None:
    remove_locked_worktree(wt_path, cwd=root)
    if created_branch is None:
        return
    result = subprocess.run(
        ["git", "branch", "-D", created_branch],
        cwd=str(root),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(
            f"  warning: could not delete branch '{created_branch}' "
            f"({result.stderr.strip()}) — left in place, not lost",
            file=sys.stderr,
        )


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def provision_new(
    spec: SandboxSpec, *, registry: dict[str, SandboxRecord], yes: bool
) -> ProvisionOutcome:
    """Provision a freshly reserved sandbox. On any failure, unwinds the undo
    stack (container teardown there goes through `lifecycle.teardown`), cleans
    the ephemeral dirs, and either drops the reservation (clean unwind) or keeps
    it in `provisioning` for `stx rm` to retry (unwind left resources behind).

    A SIGTERM/SIGHUP arriving mid-provisioning unwinds like Ctrl+C, not killing
    the process with its worktree/DBs/tokens/containers behind — the handlers
    stay installed through the rollback (restored in the finally) so a second
    signal there is handled too. See docs/reference.md (Branches, rollback).
    """
    state = ProvisionState()
    restore_signals = _install_termination_handlers()
    try:
        if spec.manifest is not None:
            _ensure_sandbox_network(spec, state)
            _create_worktree(spec, state, yes=yes)
            state.db_params = PostgresAdminParams.build()
            _prepare_handoff_dirs(spec)
            _check_databases(spec, state, registry)
            _render_egress(spec, state)
            _mint_forge(spec, state)
            _record_pending_forge(spec, state)
            _ensure_image_and_resolve_user(spec, state)
            _build_box_record(spec, state)
            state.tailscale_fqdn = routing.get_tailscale_hostname()
            state.tailscale_ip = routing.get_tailscale_ipv4()
            if spec.manifest.routes:
                state.route_bind_addr = stx_network.route_bind_address()

        if spec.with_dmz:
            _prepare_dmz(spec, state)

        if spec.manifest is not None or spec.with_dmz:
            _render_overlay(spec, state)
            _build_compose_env(spec, state)

        if spec.manifest is not None:
            _check_merged(spec, state)
            print(f"  {identity_provenance_summary(spec.root)}")
        elif state.overlay_path is not None:
            _check_merged(spec, state)

        # Register the container bring-down and Redis flush undos before the
        # first compose invocation — the provision steps' own `compose run`
        # included — so an interrupted step unwinds its containers and flushes
        # its index (see `_register_compose_teardown`).
        if state.overlay_path is not None:
            _register_compose_teardown(spec, state)

        if spec.manifest is not None:
            _provision_steps_step(spec, state)

        if state.overlay_path is not None:
            _compose_up_new(spec, state)

        if spec.manifest is not None:
            state.setup_error = await_box_setup(
                spec.slug, spec.manifest.name, timeout=spec.timeout
            )

        if spec.manifest is not None and spec.manifest.routes:
            _write_route_new(spec, state)

        with locked_registry() as reg:
            reg.finish(spec.slug, state.boxes)
    except BaseException as e:
        # BaseException, not Exception: Ctrl+C (KeyboardInterrupt) must unwind
        # too, not skip straight past rollback and leave the worktree/DBs/
        # tokens/containers/registry entry all behind.
        failed = _unwind(state.undo)
        _cleanup_ephemeral_dirs(spec.slug)
        if failed:
            # Rollback couldn't reclaim everything — keep the entry in
            # `provisioning` (it still carries the redis index, database names,
            # and any minted token ids) so `stx rm` can retry.
            if isinstance(e, KeyboardInterrupt):
                raise
            raise RollbackIncomplete(spec.slug, failed) from e
        _unreserve(spec.slug)
        if isinstance(e, (OSError, KeyError)):
            # The command layer only reports StxError cleanly; surface an
            # unexpected OS or registry-key error from provisioning as one
            # rather than letting it reach the CLI as a bare traceback.
            raise StxError(f"provisioning '{spec.slug}' failed: {e}") from e
        raise
    finally:
        restore_signals()

    return ProvisionOutcome(
        boxes=state.boxes,
        setup_error=state.setup_error,
        tailscale_fqdn=state.tailscale_fqdn,
    )


def refresh_box(
    spec: SandboxSpec,
    *,
    repo_config: dict,
    app: BoxRecord,
    dmz_record: BoxRecord | None,
    fresh_credentials: list[ForgeCredential],
) -> None:
    """Re-run the box half over an existing sandbox: validate (box-contract
    checks, then the image build), and only once those pass rewrite the egress
    allowlist and overlay, recreate the container(s), and sync the Traefik
    route. Raises on failure.

    Deliberately never unwinds and keeps no undo stack: a failure *before* the
    writes (a non-compliant repo, a failed image build) leaves the old
    allowlist, overlay, image, and running box exactly as they were. A failure
    at `compose up` or the route write can leave the box partially recreated on
    the fresh files, but the caller still revokes only the fresh tokens it
    minted and never the old box's own. See docs/reference.md (refresh row)."""
    state = ProvisionState()
    state.user = app.user
    state.wt_path = Path(app.worktree)
    state.forge_credentials = fresh_credentials
    state.tailscale_ip = routing.get_tailscale_ipv4()
    if spec.manifest.routes:
        state.route_bind_addr = stx_network.route_bind_address()
    if dmz_record is not None:
        state.dmz_image = dmz_record.image
        state.dmz_user = dmz_record.user
        state.dmz_snapshot = code_dmz_dir(spec.slug, spec.repo_name or "repo")

    # Validate first, before touching anything on disk — a non-compliant repo
    # or a failed image build must leave the old box's files intact.
    check_repo_compose_contract(repo_config, manifest=spec.manifest, root=spec.root)
    ret = rebuild.ensure_image_fresh(spec.root, spec.manifest)
    if ret:
        raise StepAlreadyReported()

    # Validation passed — now (and only now) rewrite the allowlist and overlay.
    # Rendered directly rather than through `new`'s undo-registering wrappers:
    # refresh never unwinds, so an undo stack here would only ever be dead.
    egress.render(
        spec.manifest,
        spec.slug,
        forge_host=spec.forge.host if spec.forge is not None else None,
    )
    state.overlay_path = overlay.render(
        spec.slug,
        spec.manifest,
        user=state.user,
        worktree=state.wt_path,
        repo_root=spec.root,
        redis_index=spec.redis_index,
        tailnet_port=spec.tailnet_port,
        tailscale_ip=state.tailscale_ip,
        claude_auth_mode=spec.claude_auth_mode,
        dmz_image=state.dmz_image if spec.with_dmz else None,
        dmz_user=state.dmz_user if spec.with_dmz else None,
        dmz_snapshot=state.dmz_snapshot if spec.with_dmz else None,
        links=spec.links,
        route_ports=spec.route_ports,
        route_bind_addr=state.route_bind_addr,
        databases=spec.databases,
    )
    _build_compose_env(spec, state)
    _check_merged(spec, state)

    if spec.claude_auth_mode != app.claude_auth_mode:
        print(f"  switching Claude auth to {spec.claude_auth_mode.value}")
    detail = " with fresh credentials" if fresh_credentials else ""
    print(
        f"  recreating app box{detail}"
        + (" (and dmz sidecar)" if spec.with_dmz else "")
    )
    up_services = [spec.manifest.name, *([DMZ_BOX] if spec.with_dmz else [])]
    if _run_compose_up(spec, state, up_services) != 0:
        raise StxError(
            "failed to recreate app box — the old tokens (if any) were left "
            "intact; any fresh ones are being revoked"
        )

    # Serve exactly the manifest's current routes. A route dropped since `new`
    # is already absent from spec.route_ports, so rewriting here stops Traefik
    # serving it; with none left, remove the file rather than leave a stale
    # one. Wrapped: an unwrapped OSError here would crash the process with the
    # fresh tokens recorded but never revoked.
    try:
        if spec.route_ports:
            routing.write_traefik_route(spec.slug, spec.route_ports)
        else:
            routing.remove_traefik_route(spec.slug)
    except OSError as e:
        raise StxError(f"failed to write Traefik route: {e}") from e
