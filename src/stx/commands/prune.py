from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import override

from .. import platform as stx_platform
from .. import secrets
from ..command import Command
from ..config import load_config
from ..core import (
    LABEL_DATABASES,
    LABEL_REDIS_INDEX,
    LABEL_REPO_ROOT,
    LABEL_SLUG,
    SandboxRecord,
    SandboxState,
    branch_is_fully_pushed,
    branch_name,
    find_tmp_branches,
    load_registry_with_unparsed,
    locked_registry,
    worktree_path,
)
from ..errors import StxError
from ..lifecycle import TeardownPolicy, owner_alive, teardown_sandbox
from ..platform import PostgresAdminParams, RedisAdminParams
from ..repo import git_toplevel

_STX_BRANCH_CONFIG_RE = re.compile(
    r"^branch\.stx/(?P<slug>[a-z0-9-]+)\.stx (?P<value>\S+)$"
)
_TMP_BRANCH_RE = re.compile(r"^tmp/(?P<slug>[a-z0-9-]+)/")


class SandboxPruneCommand(Command):
    """Tear down zombie sandboxes (containers with no registry entry),
    unparseable registry entries (torn down the same way, then deleted
    outright — never repaired), and sweep orphaned `stx/*` and `tmp/*`
    branch state — see docs/reference.md (Teardown guards, Branches). Does
    not revoke the sandbox's GitLab token; relies on its own short lifetime
    as the backstop, same as any other orphaned token."""

    @classmethod
    @override
    def name(cls) -> str:
        return "prune"

    @classmethod
    @override
    def help(cls) -> str:
        return (
            "Tear down zombie sandboxes and unparseable registry entries, "
            "and sweep orphaned stx/* branch config"
        )

    @override
    def add_parser(self, subparsers) -> argparse.ArgumentParser:
        parser = subparsers.add_parser(self.name(), help=self.help())
        parser.add_argument(
            "-y", "--yes", action="store_true", help="Prune without asking first"
        )
        parser.add_argument(
            "-f",
            "--force",
            action="store_true",
            help=(
                "Also remove what prune otherwise leaves for manual recovery: "
                "worktrees and branches holding work that exists on no remote"
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List what would be pruned without removing anything",
        )
        return parser

    @override
    def _execute(self, args: argparse.Namespace) -> int | None:
        try:
            registry, unparsed = load_registry_with_unparsed()
        except StxError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        root = git_toplevel(Path.cwd()) or Path.cwd()
        # An unparseable entry already has a registry entry — just a broken
        # one — so it's excluded here too, and handled by its own dedicated
        # path below instead of being rediscovered as a plain container
        # zombie.
        zombies = _find_zombie_slugs({**registry, **unparsed})
        stuck = _find_stuck_provisioning(registry)
        orphaned_branches = _find_orphaned_stx_branches(root, registry)
        orphaned_tmp_owners = _find_orphaned_tmp_branch_owners(root, registry)

        if (
            not zombies
            and not unparsed
            and not stuck
            and not orphaned_branches
            and not orphaned_tmp_owners
        ):
            print("no zombie sandboxes or orphaned branch config found")
            return None

        if zombies:
            print(
                f"found {len(zombies)} zombie sandbox(es): {', '.join(sorted(zombies))}"
            )
        if unparsed:
            plural = "y" if len(unparsed) == 1 else "ies"
            print(
                f"found {len(unparsed)} unparseable registry entr{plural}: "
                f"{', '.join(sorted(unparsed))}"
            )
            for slug in sorted(unparsed):
                _raw, err = unparsed[slug]
                print(f"  '{slug}' is unparseable: {err}")
        if stuck:
            print(
                f"found {len(stuck)} sandbox(es) stuck mid-provisioning: "
                f"{', '.join(sorted(stuck))}"
            )
        if orphaned_branches:
            plural = "y" if len(orphaned_branches) == 1 else "ies"
            print(
                f"found {len(orphaned_branches)} orphaned stx/* branch config "
                f"entr{plural}: {', '.join(sorted(orphaned_branches))}"
            )
        if orphaned_tmp_owners:
            plural = "" if len(orphaned_tmp_owners) == 1 else "s"
            print(
                f"found orphaned tmp/* scratch branches from "
                f"{len(orphaned_tmp_owners)} slug{plural}: "
                f"{', '.join(sorted(orphaned_tmp_owners))}"
            )

        if args.dry_run:
            print("(dry run — nothing removed)")
            return None

        if not args.yes:
            if not sys.stdin.isatty():
                print(
                    "error: refusing to prune without confirmation in a "
                    "non-interactive session. Rerun with --yes.",
                    file=sys.stderr,
                )
                return 1
            answer = input("Proceed with removal? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                print("aborted: nothing removed")
                return None

        # DB/Redis admin credentials, if available. Unavailable secrets skip
        # only the DB/Redis half of zombie teardown — the stuck-entry and
        # branch sweeps below don't need them and must still run (the user
        # already confirmed).
        db_params: PostgresAdminParams | None = None
        redis_params: RedisAdminParams | None = None
        if zombies or unparsed:
            try:
                secrets.source_for(load_config()).ensure_loaded(
                    stx_platform.required_secrets(["postgres", "redis"])
                )
                db_params = PostgresAdminParams.build()
                redis_params = RedisAdminParams.build()
            except secrets.SecretsError as e:
                print(
                    f"warning: {e} — skipping DB/Redis cleanup (stuck-entry and "
                    "branch cleanup still run)",
                    file=sys.stderr,
                )
                db_params = redis_params = None

        for slug in sorted(zombies):
            _prune_one(slug, zombies[slug], db_params, redis_params, force=args.force)

        for slug in sorted(unparsed):
            raw, _err = unparsed[slug]
            _prune_one(
                slug,
                _zombie_info_from_raw(raw),
                db_params,
                redis_params,
                force=args.force,
            )
            # Never rewritten/repaired — torn down the same as a zombie,
            # then its raw entry is dropped outright.
            with locked_registry() as reg:
                reg.remove(slug)
            print(f"  ✓ removed unparseable registry entry for '{slug}'")

        for slug in sorted(stuck):
            _prune_stuck(stuck[slug], db_params, redis_params, force=args.force)

        for slug in sorted(orphaned_branches):
            _prune_orphaned_stx_branch(root, slug, force=args.force)

        for slug in sorted(orphaned_tmp_owners):
            _prune_tmp_branches(root, slug, force=args.force)

        return None


def _find_stuck_provisioning(
    registry: dict[str, SandboxRecord],
) -> dict[str, SandboxRecord]:
    """Registry entries a *crashed* `stx new` left in `state=provisioning` —
    no BoxRecord was ever written for them, so `_find_zombie_slugs` (which
    only looks at container labels and explicitly skips anything already in
    the registry) never discovers them on its own. An entry whose owning
    `new` process is still alive is skipped: it isn't stuck, it's in flight,
    and tearing it down would pull the ground out from under a running
    provision (see docs/reference.md, Teardown guards)."""
    return {
        slug: record
        for slug, record in registry.items()
        if record.state == SandboxState.PROVISIONING
        and not owner_alive(record.owner_pid, record.owner_started)
    }


@dataclass
class _ZombieInfo:
    redis_index: int | None
    repo_root: Path | None
    # None means "no recorded names" (a container predating database
    # labeling, or — for an unparseable entry — raw JSON with no `databases`
    # key) — the caller must list what actually exists rather than guess; see
    # docs/reference.md (Teardown guards). `()` is a legitimate "this
    # manifest declares no databases," distinct from "unknown."
    databases: tuple[str, ...] | None = None


def _zombie_info_from_raw(raw: dict) -> _ZombieInfo:
    """The same info `_prune_one` needs for a container-only zombie, read
    straight off an unparseable entry's own raw JSON instead of container
    labels — it already carries these fields (whatever a prior schema
    lacked is exactly what made it fail to parse), so there's no need to
    fall back to label-scanning its container."""
    redis_index = raw.get("redis_index")
    repo_root_raw = raw.get("repo_root")
    databases = raw.get("databases")
    return _ZombieInfo(
        redis_index=redis_index if isinstance(redis_index, int) else None,
        repo_root=Path(repo_root_raw) if repo_root_raw else None,
        databases=tuple(databases) if databases is not None else None,
    )


def _find_zombie_slugs(registry: dict) -> dict[str, _ZombieInfo]:
    """Slugs with an `stx.slug`-labeled container but no registry entry (parsed
    or not — a slug with only an unparseable entry is handled by its own
    dedicated path, not re-discovered here) — see docs/reference.md (Teardown
    guards) for why labels, not parsing the container name, recover the Redis
    index, repo root, and database names."""
    result = subprocess.run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            f"label={LABEL_SLUG}",
            "--format",
            f'{{{{index .Labels "{LABEL_SLUG}"}}}}\t'
            f'{{{{index .Labels "{LABEL_REDIS_INDEX}"}}}}\t'
            f'{{{{index .Labels "{LABEL_REPO_ROOT}"}}}}\t'
            f'{{{{index .Labels "{LABEL_DATABASES}"}}}}',
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return {}
    zombies: dict[str, _ZombieInfo] = {}
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        slug, redis_index_raw, repo_root_raw, databases_raw = (p.strip() for p in parts)
        if not slug or slug in registry:
            continue
        redis_index = int(redis_index_raw) if redis_index_raw.isdigit() else None
        repo_root = Path(repo_root_raw) if repo_root_raw else None
        databases = tuple(databases_raw.split(",")) if databases_raw else None
        existing = zombies.get(slug)
        if existing is None:
            zombies[slug] = _ZombieInfo(
                redis_index=redis_index, repo_root=repo_root, databases=databases
            )
        else:
            # A slug's app and dmz containers both carry stx.slug, but only
            # the app one carries redis_index/repo_root/databases — merge
            # rather than let whichever row docker lists second blank out
            # the other's.
            zombies[slug] = _ZombieInfo(
                redis_index=existing.redis_index or redis_index,
                repo_root=existing.repo_root or repo_root,
                databases=existing.databases or databases,
            )
    return zombies


def _find_orphaned_stx_branches(root: Path, registry: dict) -> set[str]:
    """Slugs with a `stx`-marked `branch.stx/<slug>.stx true` tracking-config
    entry but no registry entry. Only `stx new`-created branch config carries
    the marker — a `stx/<slug>` branch another tool set up (sharing this
    repo's namespace) has no `.stx` key at all, so it's never even considered
    here, let alone pruned — see docs/reference.md (Branches)."""
    result = subprocess.run(
        ["git", "config", "--get-regexp", r"^branch\.stx/.*\.stx$"],
        cwd=str(root),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return set()
    slugs = set()
    for line in result.stdout.splitlines():
        match = _STX_BRANCH_CONFIG_RE.match(line)
        if match and match.group("value") == "true":
            slugs.add(match.group("slug"))
    return slugs - set(registry)


def _prune_orphaned_stx_branch(root: Path, slug: str, *, force: bool) -> None:
    branch = branch_name(slug)
    branch_exists = (
        subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=str(root),
        ).returncode
        == 0
    )

    if branch_exists:
        delete_flag = "-D" if force else "-d"
        result = subprocess.run(
            ["git", "branch", delete_flag, branch],
            cwd=str(root),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(
                f"  warning: leftover branch '{branch}' has unmerged commits — "
                "leaving it and its tracking config in place for manual recovery "
                "(rerun with --force to delete it anyway)",
                file=sys.stderr,
            )
            return

    subprocess.run(
        ["git", "config", "--remove-section", f"branch.{branch}"],
        cwd=str(root),
        capture_output=True,
    )
    print(f"  removed orphaned branch config for '{slug}' ({branch})")


def _find_orphaned_tmp_branch_owners(root: Path, registry: dict) -> set[str]:
    """Slugs with tmp/<slug>/* scratch branches but no registry entry."""
    result = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/tmp/"],
        cwd=str(root),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return set()
    slugs = set()
    for line in result.stdout.splitlines():
        match = _TMP_BRANCH_RE.match(line)
        if match:
            slugs.add(match.group("slug"))
    return slugs - set(registry)


def _prune_tmp_branches(root: Path, slug: str, *, force: bool) -> None:
    for branch in find_tmp_branches(root, slug):
        if not force and not branch_is_fully_pushed(root, branch):
            print(
                f"  warning: '{branch}' has commits not reachable from any "
                "remote — leaving it in place for manual recovery "
                "(rerun with --force to remove it anyway)",
                file=sys.stderr,
            )
            continue
        result = subprocess.run(
            ["git", "branch", "-D", branch], cwd=str(root), capture_output=True
        )
        if result.returncode != 0:
            print(
                f"  warning: failed to remove '{branch}' (continuing)",
                file=sys.stderr,
            )
        else:
            print(f"  removed cross-branch scratch branch '{branch}'")


def _zombie_record(slug: str, zombie: _ZombieInfo) -> SandboxRecord:
    """A minimal registry record synthesized from a zombie's container labels,
    so `teardown_sandbox` can work from a record even though a zombie has none.
    `box_names=("app",)` so it's treated as having its own network to remove;
    `databases` is passed to teardown separately since it may be "unknown"."""
    return SandboxRecord(
        slug=slug,
        created="",
        state=SandboxState.READY,
        redis_index=zombie.redis_index,
        tailnet_port=None,
        services=[],
        repo_root=str(zombie.repo_root) if zombie.repo_root else "",
        box_names=("app",),
        boxes={},
    )


# Prune is best-effort and never revokes forge tokens (relies on their short
# lifetime) — it tears down through the shared engine, continuing past
# surviving containers (there's no entry to protect) but keeping the overlay
# when the down fails, and guarding each worktree/handoff dir for unsaved work.
def _prune_policy(*, force: bool) -> TeardownPolicy:
    return TeardownPolicy(force=force, continue_when_unclean=True, guard_unsaved=True)


def _warn_failures(result) -> None:
    for _step, message in result.failures:
        print(f"  warning: {message}", file=sys.stderr)


def _reclaimable_resources(
    redis_index: int | None, databases: tuple[str, ...] | None
) -> tuple[int | None, tuple[str, ...] | None, list[str]]:
    """Withhold any of a zombie's Redis index or database names that a *live*
    registry entry now holds, read under the registry lock. `rm --force` drops
    a registry entry while its containers survive, freeing that entry's Redis
    index and database names for a brand-new sandbox to claim; the survivors
    then read as a zombie whose labels still name those now-reused resources.
    Flushing the index or dropping the databases would destroy the new
    sandbox's data — so anything a current entry holds is withheld (and
    reported) rather than reclaimed. See docs/reference.md (Teardown guards).

    `databases is None` ("unknown") stays None — teardown lists-and-leaves
    those rather than dropping anything, so there's nothing to withhold."""
    notes: list[str] = []
    with locked_registry() as reg:
        live = reg.all().values()
        live_indices = {r.redis_index for r in live if r.redis_index}
        live_databases: set[str] = set()
        for record in live:
            live_databases.update(record.databases)
    safe_index = redis_index
    if redis_index is not None and redis_index in live_indices:
        notes.append(f"redis index {redis_index} (now held by a live sandbox)")
        safe_index = None
    safe_databases = databases
    if databases:
        reused = [name for name in databases if name in live_databases]
        if reused:
            notes.append(
                f"database(s) {', '.join(reused)} (now held by a live sandbox)"
            )
            safe_databases = tuple(n for n in databases if n not in live_databases)
    return safe_index, safe_databases, notes


def _prune_one(
    slug: str,
    zombie: _ZombieInfo,
    db_params: PostgresAdminParams | None,
    redis_params: RedisAdminParams | None,
    *,
    force: bool,
) -> None:
    print(f"  pruning '{slug}'")
    root = zombie.repo_root or git_toplevel(Path.cwd()) or Path.cwd()
    redis_index, databases, withheld = _reclaimable_resources(
        zombie.redis_index, zombie.databases
    )
    for note in withheld:
        print(f"  skipping {note} — not this zombie's to reclaim")
    record = _zombie_record(slug, zombie)
    record.redis_index = redis_index
    result = teardown_sandbox(
        record,
        policy=_prune_policy(force=force),
        root=root,
        wt_path=worktree_path(slug),
        databases=databases,
        db_params=db_params,
        redis_params=redis_params,
    )
    _warn_failures(result)
    print(f"  ✓ pruned '{slug}' (GitLab token relies on its own expiry, not revoked)")


def _prune_stuck(
    record: SandboxRecord,
    db_params: PostgresAdminParams | None,
    redis_params: RedisAdminParams | None,
    *,
    force: bool,
) -> None:
    slug = record.slug
    print(f"  cleaning up '{slug}' (stuck mid-provisioning)")
    root = Path(record.repo_root) if record.repo_root else None
    result = teardown_sandbox(
        record,
        policy=_prune_policy(force=force),
        root=root,
        wt_path=worktree_path(slug),
        databases=record.databases,
        db_params=db_params,
        redis_params=redis_params,
    )
    _warn_failures(result)
    if not result.clean:
        print(
            f"  warning: could not fully clean up '{slug}' — left in the "
            "registry for a retry",
            file=sys.stderr,
        )
        return
    with locked_registry() as reg:
        reg.remove(slug)
    print(f"  ✓ cleaned up '{slug}'")
