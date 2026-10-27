from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, override

if TYPE_CHECKING:
    import argparse

from .. import network as stx_network
from .. import platform as stx_platform
from .. import secrets
from ..command import Command
from ..config import load_config
from ..core import (
    SandboxState,
    branch_is_fully_pushed,
    branch_name,
    common_dir_for,
    container_name_for_box,
    describe_commits,
    dir_has_files,
    drop_dir,
    drop_dmz_dir,
    find_tmp_branches,
    has_uncommitted_changes,
    linkers_of,
    load_registry_with_unparsed,
    locked_registry,
    sandbox_network_name,
    trusted_box_name,
    unpushed_commit_shas,
    worktree_path,
)
from ..errors import StxError
from ..forge import ForgeError, forge_for_source
from ..forge.registry import revoke_targets
from ..lifecycle import TeardownPolicy, owner_alive, teardown_sandbox
from ..platform import PostgresAdminParams, RedisAdminParams


def _format_at_risk_commits(
    slug: str, wt_path: Path, common_dir: Path, shas: list[str]
) -> str:
    """Up to 5 "<sha> <subject>" lines naming at-risk commits, then
    "...and N more" past that. Falls back to bare SHAs if the subject lookup
    itself fails — the commits are still at risk either way."""
    shown = shas[:5]
    try:
        described = describe_commits(slug, wt_path, common_dir, shown)
    except StxError:
        described = shown
    lines = [f"    {line}" for line in described]
    if len(shas) > 5:
        lines.append(f"    ...and {len(shas) - 5} more")
    return "\n".join(lines)


class SandboxRmCommand(Command):
    """Tear down a sandbox: boxes, DBs, Redis index, worktree, handoff dirs."""

    @classmethod
    @override
    def name(cls) -> str:
        return "rm"

    @classmethod
    @override
    def help(cls) -> str:
        return (
            "Tear down a sandbox (boxes + DB + Redis index + worktree + handoff dirs)"
        )

    @override
    def add_parser(self, subparsers) -> argparse.ArgumentParser:
        parser = subparsers.add_parser(self.name(), help=self.help())
        parser.add_argument(
            "-f",
            "--force",
            action="store_true",
            help=(
                "Remove even when a guard refuses (unpushed commits, uncommitted "
                "changes, a sandbox linking to this one, cleanup that can't finish); "
                "what's left behind is printed"
            ),
        )
        parser.add_argument("slug", help="Sandbox slug")
        parser.add_argument(
            "--rm-branch",
            action="store_true",
            help=(
                "Also delete the sandbox's own local branch (stx/<slug>) — never "
                "the branch it targets, which is never checked out by any box"
            ),
        )
        parser.add_argument(
            "--keep-db",
            action="store_true",
            help="Preserve the Postgres databases (stx_<slug> and stx_<slug>_test) "
            "for manual inspection; stx new with the same slug will refuse to "
            "run until they're dropped — there's no automatic reuse",
        )
        return parser

    @override
    def _execute(self, args: argparse.Namespace) -> int | None:
        slug = args.slug
        rm_branch = args.rm_branch
        keep_db = args.keep_db
        force = args.force
        cleanup_failed = False
        # Set when a cleanup step (database drop, forge revoke) is never
        # even attempted — secrets unavailable, or nothing recorded to
        # rebuild a forge client from. Unlike cleanup_failed (an attempt
        # that ran and failed), --force lets a skip through — it's already
        # been warned about by the time this is checked, below.
        cleanup_skipped = False

        registry, unparsed = load_registry_with_unparsed()
        if slug not in registry:
            if slug in unparsed:
                _raw, err = unparsed[slug]
                print(
                    f"error: sandbox '{slug}' has an unparseable registry "
                    f"entry ({err}) — run `stx prune` to clean it up",
                    file=sys.stderr,
                )
                return 1
            print(f"error: no sandbox named '{slug}'", file=sys.stderr)
            return 1

        record = registry[slug]

        linkers = linkers_of(slug, registry)
        if linkers:
            if not force:
                print(
                    f"error: sandbox '{slug}' is linked to by "
                    f"{', '.join(sorted(linkers))}. Remove them first, or use "
                    "--force to disconnect them.",
                    file=sys.stderr,
                )
                return 1
            print(
                f"  warning: disconnecting linker(s) {', '.join(sorted(linkers))} "
                f"from '{slug}'s network"
            )

        root = Path(record.repo_root) if record.repo_root else None
        common_dir = common_dir_for(root) if root is not None else None
        trusted = trusted_box_name(record.boxes)
        app = record.boxes.get(trusted) if trusted else None
        # A crashed `new` can leave a sandbox stuck in `provisioning` with no
        # BoxRecord — recover what's recoverable at its conventional,
        # slug-derived location instead. But an entry whose owning `new` is
        # still alive isn't stuck, it's in flight: tearing it down would pull
        # the ground out from under a running provision, so refuse unless
        # --force (see docs/reference.md, Teardown guards).
        stuck = record.state == SandboxState.PROVISIONING
        if (
            record.state == SandboxState.PROVISIONING
            and owner_alive(record.owner_pid, record.owner_started)
            and not force
        ):
            print(
                f"error: sandbox '{slug}' is still being provisioned by a live "
                f"`stx new` (pid {record.owner_pid}). Wait for it to finish, or "
                "use --force to tear it down anyway.",
                file=sys.stderr,
            )
            return 1
        wt_path: Path | None = None
        if app:
            wt_path = (
                Path(app.worktree)
                if os.path.isabs(app.worktree)
                else (root / app.worktree if root else worktree_path(slug))
            )
        elif stuck:
            wt_path = worktree_path(slug)

        # Guard against data loss unless --force. A failed check — including
        # no common_dir to verify the worktree's own metadata against — is
        # treated as dirty, not clean; it proves nothing about whether work
        # would be lost.
        if not force and wt_path and wt_path.exists():
            if common_dir is None:
                print(
                    f"error: could not check sandbox '{slug}' for uncommitted "
                    "changes (no repo checkout to verify its worktree "
                    "against). Use --force to proceed anyway.",
                    file=sys.stderr,
                )
                return 1
            try:
                dirty = has_uncommitted_changes(slug, wt_path, common_dir)
            except StxError as e:
                print(
                    f"error: could not check sandbox '{slug}' for uncommitted "
                    f"changes ({e}). Use --force to proceed anyway.",
                    file=sys.stderr,
                )
                return 1
            if dirty:
                print(
                    f"error: sandbox '{slug}' has uncommitted changes. "
                    "Commit or stash them, or use --force.",
                    file=sys.stderr,
                )
                return 1

        # Checked unconditionally (not just when not force) so --force can warn
        # instead of silently proceeding — once the registry entry below is
        # deleted, this is the last point anything records what was on this
        # worktree or that it existed at all. A failed check is treated as
        # dirty too: refuse without --force, warn and proceed with it.
        unpushed: list[str] = []
        if wt_path and wt_path.exists():
            if common_dir is None:
                if not force:
                    print(
                        f"error: could not determine whether sandbox '{slug}' "
                        "has unpushed commits (no repo checkout to verify its "
                        "worktree against). Use --force to proceed anyway.",
                        file=sys.stderr,
                    )
                    return 1
                print(
                    f"warning: could not determine whether sandbox '{slug}' "
                    "has unpushed commits (no repo checkout to verify its "
                    "worktree against) — proceeding due to --force",
                    file=sys.stderr,
                )
            else:
                try:
                    unpushed = unpushed_commit_shas(slug, wt_path, common_dir)
                except StxError as e:
                    if not force:
                        print(
                            f"error: could not determine whether sandbox '{slug}' "
                            f"has unpushed commits ({e}). Use --force to proceed "
                            "anyway.",
                            file=sys.stderr,
                        )
                        return 1
                    print(
                        f"warning: could not determine whether sandbox '{slug}' "
                        f"has unpushed commits ({e}) — proceeding due to --force",
                        file=sys.stderr,
                    )
        if unpushed:
            # common_dir can't be None here: unpushed is only ever populated
            # in the branch above that already required it.
            listing = _format_at_risk_commits(slug, wt_path, common_dir, unpushed)
            if not force:
                print(
                    f"error: sandbox '{slug}' has unpushed commits:\n{listing}\n"
                    "Push them, or use --force.",
                    file=sys.stderr,
                )
                return 1
            local_branch = branch_name(slug)
            print(
                f"warning: sandbox '{slug}' has {len(unpushed)} unpushed commit(s) "
                "that --force does not delete — only --rm-branch removes the "
                f"branch itself:\n{listing}\nRecover with `git log {local_branch}` "
                f"in {root}.",
                file=sys.stderr,
            )

        # Handoff dirs exist independent of the app box — guard regardless of wt_path
        if not force:
            for dir_path in (drop_dir(slug), drop_dmz_dir(slug)):
                if dir_has_files(dir_path):
                    print(
                        f"error: sandbox '{slug}' has files in its handoff dir "
                        f"at {dir_path} that were never committed to git. "
                        "Clear them if they're no longer needed, or use --force.",
                        file=sys.stderr,
                    )
                    return 1

        # The registry entry is the source of truth for what to tear down,
        # never the checkout's manifest — it may have switched branches, lost
        # sandbox.yaml, or been deleted since this sandbox was provisioned (see
        # docs/reference.md, Teardown guards).
        databases = list(record.databases)

        # Preflight: resolve everything cleanup needs — secrets, a revocable
        # forge client, DB/Redis admin params — *before* touching anything
        # irreversible. If we can't finish and --force wasn't given, abort now
        # with the entry and all its resources fully intact, rather than tear
        # the containers down and then discover we can't revoke or drop,
        # leaving a half-removed entry behind.
        forge = None
        # Both the box's own current tokens and any left on record for
        # revocation (minted by a crashed `new`, or an old set a `refresh`
        # couldn't revoke) must be torn down — see docs/reference.md (Teardown
        # guards).
        forge_records = [
            r
            for r in (
                app.forge if app is not None else None,
                record.pending_forge,
            )
            if r is not None
        ]
        forge_source = (
            app.forge_source if (app is not None and app.forge is not None) else None
        ) or record.pending_forge_source
        db_params = None
        redis_params = None
        if app is not None or stuck:
            required = stx_platform.required_secrets(record.services)
            if forge_records:
                required = [*required, "STX_GITLAB_MINTER_TOKEN"]
            secrets_loaded = True
            if required:
                try:
                    secrets.source_for(load_config()).ensure_loaded(required)
                except secrets.SecretsError as e:
                    print(
                        f"warning: {e} — skipping forge/DB/Redis cleanup",
                        file=sys.stderr,
                    )
                    secrets_loaded = False
                    cleanup_skipped = True

            if secrets_loaded and forge_records:
                if forge_source is None:
                    print(
                        f"warning: sandbox '{slug}' has minted tokens but no forge "
                        "source recorded to rebuild its forge from — tokens left "
                        "to their own expiry, not revoked",
                        file=sys.stderr,
                    )
                    cleanup_skipped = True
                else:
                    try:
                        forge = forge_for_source(forge_source)
                    except ForgeError as e:
                        print(f"warning: {e} — tokens not revoked", file=sys.stderr)
                        cleanup_skipped = True

            if secrets_loaded:
                try:
                    if databases:
                        db_params = PostgresAdminParams.build()
                    if record.redis_index and record.redis_index > 0:
                        redis_params = RedisAdminParams.build()
                except secrets.SecretsError as e:
                    print(f"warning: {e} — skipping DB/Redis cleanup", file=sys.stderr)
                    db_params = None
                    redis_params = None
                    if databases:
                        cleanup_skipped = True
                        print(
                            f"  left behind: database(s) {', '.join(databases)}",
                            file=sys.stderr,
                        )

        if cleanup_skipped and not force:
            print(
                f"error: sandbox '{slug}' cannot be fully torn down right now "
                "(see the warning above) — kept in the registry with everything "
                f"intact. Retry `stx rm {slug}` once it's available, or pass "
                "--force to remove it regardless.",
                file=sys.stderr,
            )
            return 1

        # Disconnect any (force-permitted) linkers from this sandbox's network
        # before teardown removes it out from under them — the guard above
        # already refused unless --force.
        if linkers:
            net = sandbox_network_name(slug)
            for linker_slug in linkers:
                linker_box = trusted_box_name(registry[linker_slug].boxes)
                if linker_box is not None:
                    stx_network.disconnect(
                        net, container_name_for_box(linker_box, linker_slug)
                    )

        # The one shared mechanical teardown — containers, overlay, egress,
        # network, DB, Redis, route, hosts, worktree, dirs. It self-gates on
        # the record (a dmz-only sandbox gets no network/DB/Redis/worktree),
        # keeps the overlay when containers survive, and reports what it
        # couldn't remove rather than claiming success. --force is what lets rm
        # continue past surviving containers and drop the overlay anyway; the
        # data-loss guards above already vetted the worktree/handoff dirs, so
        # teardown removes them outright.
        result = teardown_sandbox(
            record,
            policy=TeardownPolicy(
                force=force,
                keep_db=keep_db,
                continue_when_unclean=force,
                drop_overlay_when_unclean=True,
                rmtree_worktree_without_repo=True,
            ),
            root=root,
            wt_path=wt_path,
            databases=tuple(databases),
            db_params=db_params,
            redis_params=redis_params,
        )
        if result.stopped_unclean:
            print(
                f"error: sandbox '{slug}' has {len(result.container.remaining)} "
                "container(s) that could not be removed — the registry entry and "
                f"overlay are kept so they aren't orphaned. Retry `stx rm {slug}`, "
                "or pass --force to remove the entry regardless.",
                file=sys.stderr,
            )
            return 1

        if keep_db and databases and db_params:
            print(f"  kept databases {', '.join(databases)}")

        # A DB/Redis step that was attempted and failed keeps the entry so a
        # retry can finish it, rather than freeing a Redis index that was never
        # flushed or dropping the entry with its databases still live.
        for step, message in result.failures:
            cleanup_failed = True
            hint = (
                "or pass --keep-db to skip dropping databases"
                if step == "databases"
                else "once secrets/connectivity are restored"
            )
            print(
                f"error: {message} — sandbox '{slug}' kept in the registry; "
                f"retry `stx rm {slug}` {hint}",
                file=sys.stderr,
            )

        if app is not None or stuck:
            if forge is not None and forge_records:
                # List, not a short-circuiting all(): every token must be
                # attempted even if an earlier one fails.
                results = [
                    forge.revoke(kind, scope_id, token_id)
                    for forge_record in forge_records
                    for kind, scope_id, token_id in revoke_targets(forge_record)
                ]
                if not all(results):
                    # The tokens are still live on the forge — keep the entry so
                    # a retry can revoke them, rather than drop it and lose the
                    # ids. --force accepts leaving them to their own expiry.
                    if not force:
                        cleanup_failed = True
                        print(
                            f"error: sandbox '{slug}' token revocation failed — kept "
                            f"in the registry; retry `stx rm {slug}`, or pass --force "
                            "to remove it and leave the tokens to expire",
                            file=sys.stderr,
                        )
                    else:
                        print(
                            f"warning: sandbox '{slug}' token revocation failed — "
                            "removed anyway (--force); tokens left to their own expiry",
                            file=sys.stderr,
                        )

            if cleanup_skipped:
                # cleanup_skipped without --force already returned above, so
                # reaching here means --force removed it regardless.
                print(
                    f"warning: '{slug}' removed from the registry despite the "
                    "skipped cleanup above (--force) — nothing further will "
                    "retry it",
                    file=sys.stderr,
                )

            # tmp/<slug>/<task> scratch branches (see docs/reference.md,
            # Branches) may outlive a dispatch that never reached its own
            # death-rule cleanup — swept here too. None for a --repo none
            # sandbox: there's no repo checkout to sweep branches in.
            for tmp_branch in find_tmp_branches(root, slug) if root else []:
                if force or branch_is_fully_pushed(root, tmp_branch):
                    branch_result = subprocess.run(
                        ["git", "branch", "-D", tmp_branch],
                        cwd=str(root),
                        capture_output=True,
                        text=True,
                    )
                    if branch_result.returncode != 0:
                        print(
                            f"warning: failed to remove '{tmp_branch}' (continuing)",
                            file=sys.stderr,
                        )
                else:
                    print(
                        f"warning: '{tmp_branch}' has commits not reachable from "
                        "any remote — left in place for manual recovery "
                        "(rerun with --force to remove it anyway).",
                        file=sys.stderr,
                    )

            if rm_branch and root is not None:
                # local_branch is stx/<slug> (branch_name), never
                # app.branch — see docs/reference.md (Branches).
                local_branch = branch_name(slug)
                branch_result = subprocess.run(
                    ["git", "branch", "-d", local_branch],
                    cwd=str(root),
                    capture_output=True,
                    text=True,
                )
                if branch_result.returncode != 0:
                    # git itself refuses -d (safe delete) on unmerged commits —
                    # this is that refusal surfacing, not a new destructive step.
                    print(
                        f"warning: could not delete branch '{local_branch}' "
                        f"({branch_result.stderr.strip()}) — left in place, not lost",
                        file=sys.stderr,
                    )

        if cleanup_failed:
            print(
                f"✗ sandbox '{slug}' not fully removed (DB/Redis cleanup failed) "
                "— registry entry kept",
                file=sys.stderr,
            )
            return 1

        with locked_registry() as reg:
            reg.remove(slug)

        print(f"✓ sandbox '{slug}' removed")
        return None
