from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import override

from .. import linking, secrets
from .. import platform as stx_platform
from ..command import Command
from ..config import load_config, resolve_branch_username
from ..core import (
    DEFAULT_SETUP_TIMEOUT_SECONDS,
    DMZ_BOX,
    BoxRecord,
    ClaudeAuthMode,
    add_hosts_entry,
    allocate_redis_index,
    allocate_route_ports,
    allocate_tailnet_port,
    container_name_for_box,
    default_box,
    load_registry,
    locked_registry,
    port_bindable,
    repo_git_dir,
    trusted_box_name,
    validate_slug,
)
from ..errors import StxError
from ..forge import Forge, ForgeError, forge_for
from ..lifecycle import (
    RollbackIncomplete,
    SandboxSpec,
    StepAlreadyReported,
    check_repo_compose_contract,
    collect_host_vars,
    current_owner,
    provision_new,
    repo_compose_config,
)
from ..manifest import Manifest
from ..repo import resolve_repo_root
from .code import SandboxCodeCommand
from .rm import SandboxRmCommand


class SandboxNewCommand(Command):
    """Provision a new sandbox: worktree + DB + Redis index + app box, plus an
    optional dmz sidecar — see docs/reference.md (`new` row)."""

    @classmethod
    @override
    def name(cls) -> str:
        return "new"

    @classmethod
    @override
    def help(cls) -> str:
        return "Provision a new sandbox (worktree + DB + Redis index + app; optional dmz sidecar)"

    @override
    def add_parser(self, subparsers) -> argparse.ArgumentParser:
        parser = subparsers.add_parser(self.name(), help=self.help())
        parser.add_argument(
            "-y",
            "--yes",
            action="store_true",
            help="With --replace, tear down the existing sandbox without asking first",
        )
        parser.add_argument("slug", help="Sandbox slug: lowercase [a-z0-9-]+")
        parser.add_argument(
            "--repo",
            default=None,
            metavar="PATH",
            help=(
                "Repo checkout to provision a trusted dev box from (default: the "
                "cwd's checkout); 'none' skips it — dmz-only"
            ),
        )
        parser.add_argument(
            "--with-dmz",
            action="store_true",
            help=(
                "Provision the dmz research sidecar (spend-capped harness key "
                "only, open egress, own network, read-only repo snapshot)"
            ),
        )
        parser.add_argument(
            "--branch",
            default=None,
            metavar="BRANCH",
            help=(
                "Branch this sandbox's commits target, overriding the auto-derived "
                "sandbox/<slug> name (the manifest's `branch:` template) — for "
                "pushing to an existing branch (e.g. "
                "one owned by someone else, or not named per that convention) from "
                "a sandbox whose slug doesn't need to match it. The worktree's own "
                "local branch is always this sandbox's slug-unique name (never the "
                "target itself), tracked to push here; see --base for where its "
                "initial content comes from."
            ),
        )
        parser.add_argument(
            "--base",
            default=None,
            metavar="BRANCH",
            help=(
                "Explicitly pick the worktree's starting content — always honored "
                "when given, never overridden by inference. Left unset, content is "
                "inferred from the target's (the auto-derived name, or --branch) "
                "own most-current state: your own local checkout of it, if you "
                "have one and it isn't behind origin (so a continuation box picks "
                "up not-yet-pushed work); else its state on origin; else this "
                "default (the remote's default branch, or the current branch's "
                "upstream), for a target with no history anywhere yet."
            ),
        )
        parser.add_argument(
            "--ro",
            action="store_true",
            help=(
                "Mint a read-only GitLab token instead of the default scoped write "
                "token (triage/exploration; no push capability)"
            ),
        )
        auth_group = parser.add_mutually_exclusive_group()
        auth_group.add_argument(
            "--oauth",
            action="store_true",
            help=(
                "Authenticate the app box's Claude harness with your Claude.ai "
                "subscription (CLAUDE_CODE_OAUTH_TOKEN) instead of the dev-tier "
                "ANTHROPIC_API_KEY — the default, since metered API billing gets "
                "expensive for long-running implementation sandboxes. Requires "
                "STX_CLAUDE_OAUTH_TOKEN in the secret source "
                "(mint one with `claude setup-token`)"
            ),
        )
        auth_group.add_argument(
            "--api-key",
            action="store_true",
            help=(
                "Authenticate the app box's Claude harness with the dev-tier "
                "ANTHROPIC_API_KEY (metered billing) instead of the default "
                "Claude.ai subscription"
            ),
        )
        parser.add_argument(
            "--shell",
            action="store_true",
            help=(
                "Open an interactive shell in the new sandbox's box immediately "
                "after provisioning (app if present, else dmz)"
            ),
        )
        parser.add_argument(
            "--code",
            action="store_true",
            help=(
                "Open the new sandbox's worktree in Cursor or VS Code immediately "
                "after provisioning — complementary to --shell, combine both or "
                "use either alone"
            ),
        )
        parser.add_argument(
            "--no-hosts",
            action="store_true",
            help=(
                "Skip adding the hosts-file entry for <slug>.localhost and each "
                "<route>.<slug>.localhost. Added by default (last, after everything "
                "else, since it's the only step that needs sudo) so non-browser "
                "clients — curl, editors — can resolve it; browsers already "
                "resolve *.localhost without this"
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
        parser.add_argument(
            "--link",
            action="append",
            metavar="NAME=TARGET",
            help=(
                "Link this sandbox to an existing trusted sandbox TARGET under "
                "the manifest's declared link NAME (repeatable). Attaches this "
                "box to TARGET's network and renders {link.NAME}/"
                "{link.NAME.internal} in templates — see docs/manifest.md "
                "(links). Fixed for the sandbox's life"
            ),
        )
        parser.add_argument(
            "--replace",
            action="store_true",
            help=(
                "If a sandbox with this slug already exists, tear it down first "
                "and provision a fresh one in its place. Runs `stx rm`'s normal "
                "safety guards on the old one — uncommitted or unpushed work, "
                "undelivered handoff files, or linked-by sandboxes all abort the "
                "replace (clear them, or run `stx rm --force` by hand first)"
            ),
        )
        return parser

    @override
    def _execute(self, args: argparse.Namespace) -> int | None:
        slug = args.slug
        repo = args.repo
        with_dmz = args.with_dmz
        base_explicit = args.base is not None
        base = args.base
        replace = args.replace
        read_only = args.ro
        oauth_explicit = args.oauth
        api_key_explicit = args.api_key
        oauth_auth = not api_key_explicit

        try:
            validate_slug(slug)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

        if repo == "none" and not with_dmz:
            print(
                "error: --repo none with no dmz box would create nothing. "
                "Add --with-dmz.",
                file=sys.stderr,
            )
            return 1

        if repo == "none" and read_only:
            print(
                "error: --ro has no effect without an app box. "
                "Drop --repo none or drop --ro.",
                file=sys.stderr,
            )
            return 1

        if repo == "none" and args.branch is not None:
            print(
                "error: --branch has no effect without an app box. "
                "Drop --repo none or drop --branch.",
                file=sys.stderr,
            )
            return 1

        if repo == "none" and (oauth_explicit or api_key_explicit):
            print(
                "error: --oauth/--api-key has no effect without an app box. "
                "Drop --repo none or drop the auth flag.",
                file=sys.stderr,
            )
            return 1

        try:
            root = resolve_repo_root(repo, Path.cwd())
        except StxError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        manifest = Manifest.load(root) if root else None
        services = stx_platform.services_for(manifest)
        registry = load_registry()

        # Links are validated before any change — an undeclared name, a
        # nonexistent/dmz-only target, or a route the target doesn't have
        # are all refused here (see docs/manifest.md, links).
        try:
            resolved_links = linking.resolve_links(manifest, args.link, registry)
        except StxError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

        # The repo compose allow-list needs only Docker and the repo's own
        # file — runs before anything stateful (platform up, worktree,
        # branch) so a non-compliant repo is refused before touching
        # anything.
        host_vars = []
        if manifest is not None:
            try:
                repo_config = repo_compose_config(root, manifest, slug)
                check_repo_compose_contract(repo_config, manifest=manifest, root=root)
                host_vars = collect_host_vars(repo_config)
            except StxError as e:
                print(f"error: {e}", file=sys.stderr)
                return 1

        # Same rationale throughout: fail loudly before provisioning starts,
        # not with a silently-blank env var the harness can't authenticate
        # with. The harness var is gated on an app box existing at all — the
        # default auth mode is otherwise irrelevant to a dmz-only sandbox.
        required = [*stx_platform.required_secrets(services)]
        if manifest is not None:
            required += manifest.provision_host_vars()
            required += [v.name for v in host_vars if v.required]
        if repo != "none":
            required.append(
                "STX_CLAUDE_OAUTH_TOKEN" if oauth_auth else "ANTHROPIC_API_KEY"
            )
        if with_dmz:
            required.append("STX_DMZ_ANTHROPIC_API_KEY")
        if manifest is not None and manifest.forge is not None:
            required.append("STX_GITLAB_MINTER_TOKEN")
        try:
            secret_env = secrets.source_for(load_config()).ensure_loaded(
                required, optional=[v.name for v in host_vars if not v.required]
            )
        except secrets.SecretsError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

        try:
            forge: Forge | None = forge_for(manifest) if manifest is not None else None
        except ForgeError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

        if slug in registry:
            if not replace:
                print(
                    f"error: sandbox '{slug}' already exists. "
                    f"Use `stx shell {slug}` to enter it, "
                    f"`stx rm {slug}` to remove it, "
                    f"or `stx new {slug} --replace` to recreate it.",
                    file=sys.stderr,
                )
                return 1
            # --replace: fully tear down the sandbox being replaced first, via
            # `stx rm`'s own path and its normal guards — a dirty or linked-to
            # sandbox aborts the replace rather than being silently destroyed
            # (see docs/reference.md, `new` row).
            ret = _replace_existing(slug, yes=args.yes)
            if ret:
                return ret

        # Ensure the platform services this manifest needs are running — a bare
        # box (no manifest, or one declaring no routes/postgres/redis/otel)
        # starts none.
        ret = stx_platform.run_platform_up(services)
        if ret:
            return ret

        # The main checkout's name, so every linked worktree of a repo
        # records the same one.
        repo_name = repo_git_dir(root).parent.name if root else ""
        repo_root_str = str(root) if root else ""

        try:
            # Resolved before the slug is reserved so a failure here (e.g. the
            # branch-username chain can't resolve) prints and returns without
            # leaving a dangling reservation behind.
            branch = (
                (
                    args.branch
                    or manifest.branch_for(
                        slug, resolve_branch_username(forge), links=resolved_links
                    )
                )
                if manifest is not None
                else None
            )
            # Rendered now, before the slug is even reserved, so a crash
            # between reserve() and finish() still leaves the registry entry
            # carrying its own database names for `rm` to use — see
            # docs/reference.md (Teardown guards).
            databases = (
                manifest.render_databases(slug, links=resolved_links)
                if manifest
                else []
            )
            with locked_registry() as reg:
                redis_index = (
                    allocate_redis_index(reg.all())
                    if manifest and manifest.platform.redis
                    else None
                )
                tailnet_port = (
                    allocate_tailnet_port(reg.all(), is_free=port_bindable)
                    if manifest and manifest.publish
                    else None
                )
                route_ports = (
                    dict(
                        zip(
                            manifest.routes,
                            allocate_route_ports(
                                reg.all(), len(manifest.routes), is_free=port_bindable
                            ),
                        )
                    )
                    if manifest and manifest.routes
                    else {}
                )
                box_names = tuple(
                    n
                    for n in (
                        manifest.name if manifest else None,
                        DMZ_BOX if with_dmz else None,
                    )
                    if n is not None
                )
                owner_pid, owner_started = current_owner()
                reg.reserve(
                    slug,
                    created=datetime.now(timezone.utc).isoformat(),
                    redis_index=redis_index,
                    tailnet_port=tailnet_port,
                    route_ports=route_ports,
                    services=services,
                    repo=repo_name,
                    repo_root=repo_root_str,
                    databases=tuple(databases),
                    box_names=box_names,
                    owner_pid=owner_pid,
                    owner_started=owner_started,
                )
        except StxError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

        spec = SandboxSpec(
            slug=slug,
            root=root,
            repo_name=repo_name,
            manifest=manifest,
            forge=forge,
            services=services,
            with_dmz=with_dmz,
            claude_auth_mode=(
                ClaudeAuthMode.OAUTH if oauth_auth else ClaudeAuthMode.API_KEY
            ),
            read_only=read_only,
            timeout=args.timeout,
            branch=branch,
            base=base,
            base_explicit=base_explicit,
            links=resolved_links,
            host_vars=host_vars,
            secret_env=secret_env,
            databases=tuple(databases),
            redis_index=redis_index,
            tailnet_port=tailnet_port,
            route_ports=route_ports,
        )

        try:
            outcome = provision_new(spec, registry=registry, yes=args.yes)
        except StepAlreadyReported:
            return 1
        except RollbackIncomplete as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        except StxError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            # provision_new already unwound (or kept the entry for a retry);
            # end with the hint, never a bare traceback — see docs/reference.md
            # (`new` row, rollback).
            print(
                f"\ninterrupted — rolled back what it could; if '{slug}' is "
                f"still registered, run `stx rm {slug}` to finish cleaning it up",
                file=sys.stderr,
            )
            return 130

        # Last, deliberately — the only step needing sudo (see
        # docs/reference.md, Hosts entries).
        hosts_added = False
        if manifest is not None and manifest.routes and not args.no_hosts:
            try:
                add_hosts_entry(slug, manifest.routes)
                hosts_added = True
            except OSError as e:
                print(
                    f"warning: failed to add hosts entry: {e} "
                    f"(retry with `stx hosts add {slug}`)",
                    file=sys.stderr,
                )

        _print_entry_instructions(
            slug,
            outcome.boxes,
            manifest,
            tailscale_fqdn=outcome.tailscale_fqdn,
            tailnet_port=tailnet_port,
            hosts_added=hosts_added,
        )

        if args.code:
            # Delegates to `stx code`'s own command rather than duplicating its
            # multi-IDE detection — runs before --shell below since that one execs
            # and never returns, so it would otherwise pre-empt this.
            SandboxCodeCommand().execute(
                argparse.Namespace(slug=slug, box=None, attach=False, ide="auto")
            )

        if args.shell:
            box = default_box(load_registry()[slug])
            container = container_name_for_box(box, slug)
            user = outcome.boxes[box].user
            # Replace this process with docker exec so the TTY is inherited directly —
            # same mechanism as `stx shell`.
            os.execvp(
                "docker",
                ["docker", "exec", "-it", "-u", user, container, "bash"],
            )
            return 1  # execvp never returns on success

        if outcome.setup_error:
            print(f"error: {outcome.setup_error}", file=sys.stderr)
            return 1

        return None


def _replace_existing(slug: str, *, yes: bool) -> int | None:
    """Fully tear down the sandbox `--replace` is about to recreate —
    containers, DB, Redis index, worktree, forge tokens — via the same path
    `stx rm` uses, with its normal guards in force (never `--force`): a
    sandbox with uncommitted/unpushed work, undelivered handoff files, or a
    linked-by sandbox aborts the replace rather than being silently destroyed.
    Returns `rm`'s own exit code; a non-None result means the slug must not be
    reused yet."""
    return SandboxRmCommand()._execute(
        argparse.Namespace(
            slug=slug,
            rm_branch=False,
            keep_db=False,
            force=False,
            yes=yes,
            verbose=False,
            dry_run=False,
            debug=False,
        )
    )


def _print_entry_instructions(
    slug: str,
    boxes: dict[str, BoxRecord],
    manifest: Manifest | None,
    *,
    tailscale_fqdn: str | None = None,
    tailnet_port: int | None = None,
    hosts_added: bool = False,
) -> None:
    print(f"\n✓ sandbox '{slug}' ready")
    routes = manifest.routes if manifest else {}
    trusted = trusted_box_name(boxes)
    if trusted is not None:
        print(f"  enter:  stx shell {slug}")
        if routes:
            hostname = f"{slug}.localhost"
            print(f"  open:   stx open {slug}  →  http://{hostname}")
            if hosts_added:
                print(f"  curl:   http://{hostname}  (added to hosts file)")
            else:
                print(
                    f"  curl:   run `stx hosts add {slug}` to resolve "
                    f"{hostname} outside a browser"
                )
        if tailscale_fqdn and tailnet_port:
            print(f"  mobile: http://{tailscale_fqdn}:{tailnet_port}")
        if boxes[trusted].claude_auth_mode == ClaudeAuthMode.OAUTH:
            print("  auth:   Claude subscription (CLAUDE_CODE_OAUTH_TOKEN)")
    if DMZ_BOX in boxes:
        print(f"  enter dmz: stx shell {slug} dmz")
