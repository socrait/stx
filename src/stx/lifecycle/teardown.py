"""Tearing a sandbox down, shared by `rm`, `prune`'s zombie/stuck paths and
`new`'s rollback.

`teardown_sandbox` is the one mechanical teardown — containers, overlay,
egress, network, databases, Redis index, Traefik route, hosts entry, worktree
and handoff/log dirs — driven entirely by the registry record and the
containers' own `stx.slug` labels. No overlay file and no manifest are read, so
a broken or missing `sandbox.yaml` can never block it; a `TeardownPolicy`
captures the (few) places `rm` and `prune` legitimately differ, and the
`TeardownResult` reports what it could not remove rather than claiming success.

The container bring-down is deliberately manifest-free: it addresses the stack
by its compose project name (`stx-<slug>`, the overlay's own top-level
`name:`) and, failing that, by the `stx.slug` label every sandbox container
carries. `new`'s rollback reuses `bring_down_containers` directly (its other
resources unwind through their own granular undo closures).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .. import egress, overlay
from .. import network as stx_network
from .. import platform as stx_platform
from ..core import (
    LABEL_SLUG,
    SandboxRecord,
    code_dmz_root,
    common_dir_for,
    dir_has_files,
    drop_databases,
    drop_dir,
    drop_dmz_dir,
    flush_redis,
    has_uncommitted_changes,
    has_unpushed_commits,
    list_sandbox_databases,
    logs_root,
    remove_hosts_entry,
    remove_locked_worktree,
    remove_traefik_route,
    sandbox_compose_project,
    sandbox_has_own_network,
    sandbox_network_name,
)
from ..core.compose import CompletedProcessRunner, run_bounded
from ..errors import StxError
from ..platform import PostgresAdminParams, RedisAdminParams

#: A wedged docker daemon must fail teardown, not hang every `rm`/`prune`
#: behind it forever — every docker call here is bounded by this.
_DOCKER_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class ContainerTeardown:
    """What `bring_down_containers` managed to do.

    `remaining` is the set of sandbox containers still present after both the
    compose-project down and the label-based fallback — a non-empty list means
    teardown could not finish and the caller must keep the registry entry (and
    overlay) so a later `rm`/`prune` can retry, rather than orphan zombies no
    command would ever find again. `queried` is False when `docker ps` itself
    couldn't answer: teardown then has no idea what survived, which is never
    "clean" — the caller must keep the entry just as it would for a known
    survivor, rather than claim success on a daemon it couldn't even reach.
    """

    down_ok: bool  # `docker compose down` itself exited 0
    used_label_fallback: bool  # containers survived the compose down, removed by label
    remaining: list[str] = field(default_factory=list)  # container ids still present
    queried: bool = True  # the final `docker ps` check actually answered

    @property
    def clean(self) -> bool:
        return self.queried and not self.remaining


def container_ids_for_slug(
    slug: str, *, runner: CompletedProcessRunner = subprocess.run
) -> list[str] | None:
    """Every container (running or not) carrying this sandbox's `stx.slug`
    label — the ground truth teardown checks against, independent of any
    compose file or registry entry. None (not []) when the `docker ps` query
    itself failed: "couldn't ask" is not "nothing there", and a caller that
    conflated the two would free a live sandbox's resources on a daemon
    hiccup."""
    result = run_bounded(
        ["docker", "ps", "-aq", "--filter", f"label={LABEL_SLUG}={slug}"],
        runner=runner,
        capture_output=True,
        text=True,
        timeout=_DOCKER_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        return None
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def bring_down_containers(
    slug: str, *, runner: CompletedProcessRunner = subprocess.run
) -> ContainerTeardown:
    """Stop and remove every container belonging to `slug`.

    Tries `docker compose -p <project> down` first (no compose file needed —
    the project name alone identifies the stack). If anything the compose down
    missed or failed to remove is still labelled with this slug, falls back to
    `docker rm -f` on those ids by label. Every docker call is bounded so a
    wedged daemon fails teardown rather than hanging it. Returns what happened;
    the caller decides whether an unclean result aborts (keep the entry) or is
    tolerated (`--force`). A `docker ps` that can't answer leaves `queried`
    False, which `clean` treats as not-clean — unknown is never success."""
    project = sandbox_compose_project(slug)
    down = run_bounded(
        ["docker", "compose", "--env-file", "/dev/null", "-p", project, "down"],
        runner=runner,
        capture_output=True,
        text=True,
        timeout=_DOCKER_TIMEOUT_SECONDS,
    )
    down_ok = down.returncode == 0

    ids = container_ids_for_slug(slug, runner=runner)
    if ids is None:
        return ContainerTeardown(
            down_ok=down_ok, used_label_fallback=False, remaining=[], queried=False
        )
    used_fallback = False
    if ids:
        used_fallback = True
        run_bounded(
            ["docker", "rm", "-f", *ids],
            runner=runner,
            capture_output=True,
            text=True,
            timeout=_DOCKER_TIMEOUT_SECONDS,
        )
        ids = container_ids_for_slug(slug, runner=runner)
        if ids is None:
            return ContainerTeardown(
                down_ok=down_ok,
                used_label_fallback=used_fallback,
                remaining=[],
                queried=False,
            )

    return ContainerTeardown(
        down_ok=down_ok, used_label_fallback=used_fallback, remaining=ids
    )


# --------------------------------------------------------------------------- #
# The one full teardown — shared by `rm` and `prune`
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TeardownPolicy:
    """Where `rm` and `prune` legitimately differ in tearing a sandbox down.
    Everything else is identical and lives in `teardown_sandbox`."""

    force: bool = False
    keep_db: bool = False
    # Continue past surviving containers to tear down the rest. `rm` only does
    # when forced — otherwise it bails with everything (overlay included) kept,
    # so its registry entry isn't half-removed around a zombie no command would
    # ever find again; `prune` always does (a zombie has no entry to protect).
    continue_when_unclean: bool = False
    # Remove the overlay/egress files even when containers survived the down.
    # `rm --force` does (the user asked to drop the entry regardless); `prune`
    # never does, so a later retry can still redo the stack from them.
    drop_overlay_when_unclean: bool = False
    # Guard each worktree/handoff dir for unsaved work before removing it.
    # `prune` does (a zombie may hold live, unsaved work the registry lost
    # track of); `rm` vetted them up front, so it removes outright.
    guard_unsaved: bool = False
    # rmtree a worktree outright when there's no repo checkout to unregister it
    # from (`git worktree remove` needs one). `rm` does; `prune` always resolves
    # a checkout to work from.
    rmtree_worktree_without_repo: bool = False


@dataclass
class TeardownResult:
    """What `teardown_sandbox` managed. `clean` is the single signal a caller
    needs to decide whether to drop its registry entry; `failures` and
    `left_behind` say what it couldn't reclaim so the caller can report it."""

    container: ContainerTeardown
    # Teardown stopped after the container attempt: they survived and the
    # policy didn't allow continuing. The caller keeps its entry (and overlay)
    # so the zombies aren't orphaned.
    stopped_unclean: bool = False
    # (step, message) for a DB/Redis step that was attempted and errored —
    # the caller decides whether that's fatal (`rm` keeps its entry) or a
    # best-effort warning (`prune`).
    failures: list[tuple[str, str]] = field(default_factory=list)
    # Human-readable resources deliberately left in place for manual recovery
    # or a later retry (unsaved worktree, dirty handoff dir, kept overlay).
    left_behind: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return self.container.clean and not self.stopped_unclean and not self.failures


def teardown_sandbox(
    record: SandboxRecord,
    *,
    policy: TeardownPolicy,
    root: Path | None,
    wt_path: Path | None,
    databases: tuple[str, ...] | None,
    db_params: PostgresAdminParams | None,
    redis_params: RedisAdminParams | None,
    runner: CompletedProcessRunner = subprocess.run,
) -> TeardownResult:
    """Tear a sandbox down from its registry `record` and its containers' own
    labels — containers, overlay, egress, network, databases, Redis index,
    Traefik route, hosts entry, worktree and handoff/log dirs. Reads no overlay
    file and no manifest.

    `databases` is the names to drop (from the record or recovered from
    labels); `None` means "unknown" — never guess, list what exists and leave
    it. `db_params`/`redis_params` are the already-resolved admin credentials
    (`None` when the caller's preflight found them unavailable, so that step is
    skipped). The caller keeps its registry entry iff the returned result isn't
    `clean`."""
    slug = record.slug
    result = TeardownResult(container=bring_down_containers(slug, runner=runner))

    if not result.container.clean and not policy.continue_when_unclean:
        # Containers survived and the caller won't tolerate it — keep the
        # overlay (and everything else) so a retry can find the stack.
        result.stopped_unclean = True
        return result

    if result.container.clean or policy.drop_overlay_when_unclean:
        overlay.remove(slug)
        egress.remove(slug)
    else:
        result.left_behind.append("overlay and egress allowlist")

    if sandbox_has_own_network(record):
        net = sandbox_network_name(slug)
        for service in record.services:
            stx_network.disconnect(net, stx_platform.container_name(service))
        stx_network.remove_network(net)

    _teardown_databases(slug, databases, policy, db_params, result)
    _teardown_redis(record.redis_index, redis_params, result)

    remove_traefik_route(slug)
    try:
        remove_hosts_entry(slug)
    except OSError as e:
        print(
            f"  warning: failed to remove hosts entry for '{slug}': {e}",
            file=sys.stderr,
        )

    _teardown_worktree(slug, root, wt_path, policy, result)
    _teardown_dirs(slug, policy, result)
    return result


def _teardown_databases(
    slug: str,
    databases: tuple[str, ...] | None,
    policy: TeardownPolicy,
    db_params: PostgresAdminParams | None,
    result: TeardownResult,
) -> None:
    if policy.keep_db or db_params is None:
        return
    if databases is None:
        # No recorded/labeled names — never guess (a '{db}_test' template means
        # the pair can't be reconstructed from the slug). List and leave.
        try:
            existing = list_sandbox_databases(slug, db_params)
        except StxError as e:
            print(f"  warning: {e}", file=sys.stderr)
            return
        if existing:
            print(
                f"  warning: '{slug}' has no recorded database names — found "
                f"{', '.join(existing)}; drop them by hand if no longer needed",
                file=sys.stderr,
            )
            result.left_behind.append(f"database(s) {', '.join(existing)}")
        return
    if not databases:
        return
    try:
        drop_databases(list(databases), db_params)
    except StxError as e:
        result.failures.append(("databases", str(e)))


def _teardown_redis(
    redis_index: int | None,
    redis_params: RedisAdminParams | None,
    result: TeardownResult,
) -> None:
    # The index is freed the instant the registry entry is dropped (the
    # allocator only looks at live entries), so it must be flushed first or the
    # next sandbox to reuse it inherits this one's keys. db 0 is never a
    # sandbox's own index.
    if not (redis_index and redis_index > 0):
        return
    if redis_params is None:
        result.failures.append(
            ("redis", f"cannot flush redis db {redis_index} (secrets unavailable)")
        )
        return
    try:
        flush_redis(redis_index, redis_params)
    except StxError as e:
        result.failures.append(("redis", str(e)))


def _unsaved_worktree_reason(slug: str, root: Path | None, wt_path: Path) -> str | None:
    """Why a worktree isn't safe to remove — or None when it is. A failed check
    counts as "unsaved", never as confirmed-clean."""
    common_dir = common_dir_for(root) if root is not None else None
    if common_dir is None:
        return (
            "could not be checked for unsaved work (no repo checkout to verify "
            "its worktree against)"
        )
    try:
        if has_uncommitted_changes(slug, wt_path, common_dir) or has_unpushed_commits(
            slug, wt_path, common_dir
        ):
            return "has uncommitted or unpushed work"
    except StxError as e:
        return f"could not be checked for unsaved work ({e})"
    return None


def _teardown_worktree(
    slug: str,
    root: Path | None,
    wt_path: Path | None,
    policy: TeardownPolicy,
    result: TeardownResult,
) -> None:
    if not (wt_path and wt_path.exists()):
        return
    if policy.guard_unsaved and not policy.force:
        reason = _unsaved_worktree_reason(slug, root, wt_path)
        if reason is not None:
            print(
                f"  warning: '{slug}' {reason} — leaving its worktree at "
                f"{wt_path} for manual recovery (rerun with --force to remove "
                "it anyway)",
                file=sys.stderr,
            )
            result.left_behind.append(f"worktree {wt_path}")
            return
    if root is None:
        if policy.rmtree_worktree_without_repo:
            # No checkout to run `git worktree remove` from — the directory is
            # all that's left to clean up.
            shutil.rmtree(wt_path, ignore_errors=True)
            print(
                f"  warning: sandbox '{slug}' has no repo checkout to "
                f"unregister its worktree from — removed {wt_path} directly",
                file=sys.stderr,
            )
        return
    # See remove_locked_worktree for why --force is doubled; by here it's
    # already vetted safe (independent of the CLI's own --force).
    if not remove_locked_worktree(wt_path, cwd=root):
        print(
            f"  warning: git worktree remove failed for '{slug}' (continuing)",
            file=sys.stderr,
        )


def _teardown_dirs(slug: str, policy: TeardownPolicy, result: TeardownResult) -> None:
    for dir_path in (drop_dir(slug), drop_dmz_dir(slug)):
        if policy.guard_unsaved and not policy.force and dir_has_files(dir_path):
            print(
                f"  warning: '{slug}' has undelivered handoff-dir artifacts at "
                f"{dir_path} — leaving it in place (rerun with --force to "
                "remove it anyway)",
                file=sys.stderr,
            )
            result.left_behind.append(f"handoff dir {dir_path}")
        else:
            shutil.rmtree(dir_path, ignore_errors=True)
    shutil.rmtree(code_dmz_root(slug), ignore_errors=True)
    shutil.rmtree(logs_root(slug), ignore_errors=True)
