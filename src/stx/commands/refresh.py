from __future__ import annotations

import signal
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, override

if TYPE_CHECKING:
    import argparse

from .. import secrets
from ..command import Command
from ..config import load_config
from ..core import (
    DEFAULT_SETUP_TIMEOUT_SECONDS,
    DMZ_BOX,
    ClaudeAuthMode,
    ForgeBoxRecord,
    ForgeSourceRecord,
    add_hosts_entry,
    allocate_route_ports,
    identity_provenance_summary,
    load_registry,
    locked_registry,
    port_bindable,
    sandbox_live_boxes,
    trusted_box_name,
)
from ..errors import StxError
from ..forge import (
    Forge,
    ForgeCredential,
    ForgeCredentialKind,
    ForgeError,
    forge_for,
    mint_all,
)
from ..forge.registry import build_forge_record, revoke_targets
from ..lifecycle import (
    SandboxSpec,
    StepAlreadyReported,
    await_box_setup,
    collect_host_vars,
    refresh_box,
    repo_compose_config,
)
from ..manifest import Manifest


def _install_termination_handlers() -> Callable[[], None]:
    """Make SIGTERM/SIGHUP raise KeyboardInterrupt so an interrupt during the
    slow recreate unwinds through the fresh-token revoke path rather than
    killing the process outright with those tokens left live on the forge —
    the same handling `new` installs. Returns a callable restoring the
    previous handlers."""

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


def _merge_forge(
    base: ForgeBoxRecord | None, incoming: ForgeBoxRecord | None
) -> ForgeBoxRecord | None:
    """Combine two forge records, preferring `incoming`'s push/package but
    keeping `base`'s where `incoming` has none — so recording one orphaned set
    onto `pending_forge` never silently drops a token an earlier refresh
    already left there to be revoked."""
    if base is None:
        return incoming
    if incoming is None:
        return base
    return ForgeBoxRecord(
        kind=incoming.kind,
        push=incoming.push or base.push,
        package=incoming.package or base.package,
    )


def _forge_without(
    record: ForgeBoxRecord | None, token_ids: set[int]
) -> ForgeBoxRecord | None:
    """`record` with any push/package whose token_id is in `token_ids` dropped
    — None if nothing is left. Used to retract tokens from `pending_forge` once
    they're either promoted onto the box or confirmed revoked."""
    if record is None:
        return None
    push = (
        record.push if record.push and record.push.token_id not in token_ids else None
    )
    package = (
        record.package
        if record.package and record.package.token_id not in token_ids
        else None
    )
    if push is None and package is None:
        return None
    return ForgeBoxRecord(kind=record.kind, push=push, package=package)


def _record_fresh_pending(
    slug: str,
    forge_kind: str,
    fresh_credentials: list[ForgeCredential],
    source: ForgeSourceRecord,
) -> None:
    """Track the just-minted fresh tokens on the entry's `pending_forge`
    immediately — before the slow recreate — so an interrupt mid-recreate
    leaves them tracked for `stx rm`/`prune` to revoke rather than leaked
    (the same thing `new` does right after minting)."""
    fresh_record = build_forge_record(forge_kind, fresh_credentials)
    with locked_registry() as reg:
        live = reg.all().get(slug)
        if live is not None:
            live.pending_forge = _merge_forge(live.pending_forge, fresh_record)
            live.pending_forge_source = source


def _rollback_fresh(
    forge: Forge, slug: str, fresh_credentials: list[ForgeCredential]
) -> None:
    """Revoke the fresh tokens a failed or interrupted refresh minted, and drop
    them from the entry's `pending_forge` — but keep any whose revoke failed on
    record so a later `stx rm` retries them rather than leaking a token still
    live on the forge."""
    if not fresh_credentials:
        return
    # A list, not a short-circuiting all(): every token must be attempted even
    # if an earlier revoke fails.
    results = [forge.revoke(c.kind, c.scope_id, c.token_id) for c in fresh_credentials]
    if all(results):
        fresh_ids = {c.token_id for c in fresh_credentials}
        with locked_registry() as reg:
            live = reg.all().get(slug)
            if live is not None:
                live.pending_forge = _forge_without(live.pending_forge, fresh_ids)
                if live.pending_forge is None:
                    live.pending_forge_source = None


class SandboxRefreshCommand(Command):
    """Re-mint a sandbox's app-box GitLab tokens and recreate its app box with
    them — mint, recreate, then revoke, in that order (see docs/reference.md,
    `refresh` row), so a failed recreate leaves the still-valid old tokens in
    place rather than stranding the box on nothing. Also re-renders the
    egress allowlist and overlay and rebuilds the image if stale, the same
    checks `new` itself runs. `--oauth`/`--api-key` optionally switch the app
    box's Claude auth mode in the same pass, since recreating is already
    required to push new env vars in. Refuses while a dispatch is live in
    the box, unless `--force` — recreation would otherwise kill it with no
    result captured.
    """

    @classmethod
    @override
    def name(cls) -> str:
        return "refresh"

    @classmethod
    @override
    def help(cls) -> str:
        return "Re-mint a sandbox's GitLab tokens and recreate its app box with them"

    @override
    def add_parser(self, subparsers) -> argparse.ArgumentParser:
        parser = subparsers.add_parser(self.name(), help=self.help())
        parser.add_argument(
            "-f",
            "--force",
            action="store_true",
            help="Recreate even while an agent dispatch is running (it is killed)",
        )
        parser.add_argument("slug", help="Sandbox slug")
        auth_group = parser.add_mutually_exclusive_group()
        auth_group.add_argument(
            "--oauth",
            action="store_true",
            help=(
                "Also switch to authenticating with your Claude.ai subscription "
                "(CLAUDE_CODE_OAUTH_TOKEN) instead of the dev-tier "
                "ANTHROPIC_API_KEY. Requires STX_CLAUDE_OAUTH_TOKEN in the "
                "secret source (mint one with `claude setup-token`)"
            ),
        )
        auth_group.add_argument(
            "--api-key",
            action="store_true",
            help=(
                "Also switch to the dev-tier ANTHROPIC_API_KEY (metered "
                "billing) instead of the Claude.ai subscription"
            ),
        )
        parser.add_argument(
            "--timeout",
            type=float,
            default=DEFAULT_SETUP_TIMEOUT_SECONDS,
            metavar="SECONDS",
            help=(
                "Give up waiting for the repo's `setup` scripts to finish after "
                f"this many seconds (default: {DEFAULT_SETUP_TIMEOUT_SECONDS:g}, 15m)"
            ),
        )
        return parser

    @override
    def _execute(self, args: argparse.Namespace) -> int | None:
        slug = args.slug
        registry = load_registry()
        if slug not in registry:
            print(f"error: no sandbox named '{slug}'", file=sys.stderr)
            return 1

        record = registry[slug]
        trusted = trusted_box_name(record.boxes)
        app = record.boxes.get(trusted) if trusted else None
        if app is None:
            print(
                f"error: sandbox '{slug}' has no app box to recreate — a dmz-only "
                "sandbox has nothing to refresh",
                file=sys.stderr,
            )
            return 1
        if not record.repo_root:
            print(
                f"error: sandbox '{slug}' registry entry has no repo root on "
                "record — recreate the sandbox with `stx rm` + `stx new` instead",
                file=sys.stderr,
            )
            return 1

        root = Path(record.repo_root)
        manifest = Manifest.load(root)

        # Refresh does two things, independently: recreate the app box (always —
        # to pick up new env, a rebuilt image, or an added route) and re-mint
        # its forge tokens (only when the repo declares a forge and the box
        # carries one). A no-forge sandbox is still refreshable; it simply has
        # no tokens to roll.
        has_forge = (
            manifest.forge is not None
            and app.forge is not None
            and app.forge.push is not None
        )

        if args.oauth:
            target_auth_mode = ClaudeAuthMode.OAUTH
        elif args.api_key:
            target_auth_mode = ClaudeAuthMode.API_KEY
        else:
            target_auth_mode = app.claude_auth_mode

        dmz_record = record.boxes.get(DMZ_BOX)
        has_dmz = dmz_record is not None

        try:
            repo_config = repo_compose_config(root, manifest, slug)
            host_vars = collect_host_vars(repo_config)
        except StxError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

        required = [
            "STX_CLAUDE_OAUTH_TOKEN"
            if target_auth_mode == ClaudeAuthMode.OAUTH
            else "ANTHROPIC_API_KEY",
            *[v.name for v in host_vars if v.required],
        ]
        if has_forge:
            required.append("STX_GITLAB_MINTER_TOKEN")
        if has_dmz:
            required.append("STX_DMZ_ANTHROPIC_API_KEY")
        try:
            secret_env = secrets.source_for(load_config()).ensure_loaded(
                required, optional=[v.name for v in host_vars if not v.required]
            )
        except secrets.SecretsError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

        forge = None
        if has_forge:
            try:
                forge = forge_for(manifest)
            except ForgeError as e:
                print(f"error: {e}", file=sys.stderr)
                return 1

        live_boxes = sandbox_live_boxes(slug, record)

        # Recreating either box replaces its PID namespace out from under a
        # running dispatch the same way — refuse the whole refresh (never a
        # partial one) when any box in play has one.
        if live_boxes:
            box_word = "box" if len(live_boxes) == 1 else "boxes"
            boxes_desc = " and ".join(live_boxes)
            if not args.force:
                print(
                    f"error: sandbox '{slug}' has an agent dispatch in progress "
                    f"in its {boxes_desc} {box_word} — recreating now would "
                    "silently kill it mid-task (no result captured, `sandbox "
                    "await` would misreport it as done). Wait for it to finish "
                    "first, or pass --force to refresh anyway.",
                    file=sys.stderr,
                )
                return 1
            print(
                f"  warning: sandbox '{slug}' has an agent dispatch in "
                f"progress in its {boxes_desc} {box_word} — --force is "
                "recreating anyway, killing it"
            )

        read_only = has_forge and app.forge.push.scope == "read_repository"
        fresh_source = (
            ForgeSourceRecord(
                kind=manifest.forge.kind,
                host=forge.host,
                project=manifest.forge.project,
            )
            if has_forge
            else None
        )
        fresh_credentials: list[ForgeCredential] = []

        # Signal handling mirrors `new`'s: a SIGTERM/SIGHUP during the slow
        # recreate becomes a KeyboardInterrupt the except-clause below turns
        # into a clean fresh-token revoke, rather than the process dying with
        # those tokens left live on the forge.
        restore_signals = _install_termination_handlers()
        try:
            try:
                if has_forge:
                    print(
                        f"  minting fresh {'ro' if read_only else 'rw'} forge "
                        "credential(s)"
                    )
                    try:
                        fresh_credentials = mint_all(forge, slug, read_only=read_only)
                    except ForgeError as e:
                        print(f"error: {e}", file=sys.stderr)
                        return 1
                    for credential in fresh_credentials:
                        print(
                            f"  minted {credential.kind.value} credential "
                            f"({credential.scope})"
                        )
                    # Track them on the entry *now*, before the slow recreate,
                    # so an interrupt mid-recreate leaves them for `rm`/`prune`.
                    _record_fresh_pending(
                        slug, manifest.forge.kind, fresh_credentials, fresh_source
                    )

                print(f"  {identity_provenance_summary(root)}")

                # Sync host-port allocations to the manifest's current routes:
                # keep the port already assigned to a surviving route, allocate
                # one for a route added since `new` (probing the host, like
                # `new`), and drop a route removed from the manifest so Traefik
                # stops serving it (and its bare `<slug>.localhost` router never
                # points at a gone route). Computed now so the recreate uses it,
                # but *not* persisted until `refresh_box` succeeds below — a
                # failed contract check or image build must leave the entry
                # matching the still-running old box, never free a dropped
                # route's port while the old box still binds it.
                route_ports = {
                    r: record.route_ports[r]
                    for r in manifest.routes
                    if r in record.route_ports
                }
                new_routes = [r for r in manifest.routes if r not in route_ports]
                if new_routes:
                    with locked_registry() as reg:
                        ports = allocate_route_ports(
                            reg.all(), len(new_routes), is_free=port_bindable
                        )
                    route_ports.update(dict(zip(new_routes, ports)))

                spec = SandboxSpec(
                    slug=slug,
                    root=root,
                    repo_name=record.repo or "repo",
                    manifest=manifest,
                    forge=forge,
                    services=record.services,
                    with_dmz=has_dmz,
                    claude_auth_mode=target_auth_mode,
                    read_only=read_only,
                    timeout=args.timeout,
                    links=app.links,
                    host_vars=host_vars,
                    secret_env=secret_env,
                    databases=record.databases,
                    redis_index=record.redis_index,
                    tailnet_port=record.tailnet_port,
                    route_ports=route_ports,
                )

                refresh_box(
                    spec,
                    repo_config=repo_config,
                    app=app,
                    dmz_record=dmz_record,
                    fresh_credentials=fresh_credentials,
                )
            except StepAlreadyReported:
                _rollback_fresh(forge, slug, fresh_credentials)
                return 1
            except StxError as e:
                print(f"error: {e}", file=sys.stderr)
                _rollback_fresh(forge, slug, fresh_credentials)
                return 1
            except BaseException:
                # Ctrl+C / SIGTERM during mint or the slow recreate, before the
                # swap below: the box still runs on the old tokens, so the fresh
                # ones are orphaned — revoke them (keeping any whose revoke
                # failed on record) and re-raise.
                _rollback_fresh(forge, slug, fresh_credentials)
                raise

            # Past here the box runs on the fresh tokens. Swap the registry over
            # to them and revoke the old ones *before* waiting for setup, so an
            # interrupt during the (long) wait can't leave the box on fresh
            # tokens with the registry still recording — and `rm` still revoking
            # — the old ones. The old forge record is read *under the lock* (not
            # from the stale top-level snapshot) so two concurrent refreshes
            # revoke the distinct generations each superseded, rather than both
            # revoking the original and leaking the other.
            old_forge: ForgeBoxRecord | None = None
            old_forge_source: ForgeSourceRecord | None = None
            orphaned = False
            with locked_registry() as reg:
                live = reg.all().get(slug)
                live_box = live.boxes.get(trusted) if live is not None else None
                if live_box is None:
                    orphaned = True
                else:
                    live_box.claude_auth_mode = target_auth_mode
                    # Keep the box's own route map in step with the manifest so
                    # a route added since `new` is resolvable for a later
                    # `--link` (and a dropped one no longer is).
                    live_box.routes = dict(manifest.routes)
                    # Persist the synced route-port set only now the box is
                    # rebuilt and bound to it — see the computation above.
                    live.route_ports = route_ports
                    if has_forge:
                        old_forge = live_box.forge
                        old_forge_source = live_box.forge_source
                        live_box.forge = build_forge_record(
                            manifest.forge.kind, fresh_credentials
                        )
                        live_box.forge_source = fresh_source
                        # Fresh now lives on the box; the old tokens become the
                        # set still to revoke. Drop the fresh from pending (now
                        # active) and merge the old in, so an interrupt before
                        # the revoke below keeps them tracked and any pending set
                        # an earlier refresh left isn't lost.
                        fresh_ids = {c.token_id for c in fresh_credentials}
                        live.pending_forge = _merge_forge(
                            _forge_without(live.pending_forge, fresh_ids), old_forge
                        )
                        live.pending_forge_source = (
                            old_forge_source or live.pending_forge_source
                        )

            if orphaned:
                print(
                    f"warning: sandbox '{slug}' was removed while refreshing — "
                    "revoking the fresh tokens just minted",
                    file=sys.stderr,
                )
                _rollback_fresh(forge, slug, fresh_credentials)
                return 1

            # Revoke the old tokens *outside* the lock — never hold the registry
            # lock across a forge HTTP call. On success, retract them from
            # pending; on failure, leave them there for `stx rm` to retry.
            if has_forge and old_forge is not None:
                targets = revoke_targets(old_forge)
                old_revoked = all(
                    [
                        forge.revoke(kind, scope_id, token_id)
                        for kind, scope_id, token_id in targets
                    ]
                )
                with locked_registry() as reg:
                    live = reg.all().get(slug)
                    if live is not None:
                        if old_revoked:
                            old_ids = {token_id for _, _, token_id in targets}
                            live.pending_forge = _forge_without(
                                live.pending_forge, old_ids
                            )
                            if live.pending_forge is None:
                                live.pending_forge_source = None
                        else:
                            print(
                                f"warning: sandbox '{slug}' old token revocation "
                                "failed — kept on record for `stx rm` to retry; "
                                "left to expire otherwise",
                                file=sys.stderr,
                            )

            # A route added since `new` needs its `<route>.<slug>.localhost`
            # name resolvable for non-browser clients — add the hosts entry the
            # same way `new` does (idempotent; a no-op for a routeless sandbox
            # or one already present), before the long setup wait so its sudo
            # prompt lands early.
            if manifest.routes:
                try:
                    add_hosts_entry(slug, manifest.routes)
                except OSError as e:
                    print(
                        f"warning: failed to add hosts entry: {e} "
                        f"(retry with `stx hosts add {slug}`)",
                        file=sys.stderr,
                    )

            # A setup failure/timeout here doesn't affect the credential swap
            # that already succeeded and is already recorded — reported after,
            # never a reason to leave the fresh tokens unrevoked-from-old or the
            # registry unsynced.
            setup_error = await_box_setup(slug, trusted, timeout=args.timeout)
        finally:
            restore_signals()

        if has_forge:
            has_package = any(
                c.kind is ForgeCredentialKind.PACKAGE for c in fresh_credentials
            )
            creds = "push + package-registry" if has_package else "push"
            print(
                f"✓ refreshed {creds} credentials for sandbox '{slug}' "
                f"(auth {target_auth_mode.value})"
            )
        else:
            print(
                f"✓ recreated sandbox '{slug}' (no forge; auth "
                f"{target_auth_mode.value})"
            )
        if setup_error:
            print(f"error: {setup_error}", file=sys.stderr)
            return 1
        return None
