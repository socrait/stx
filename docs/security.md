# Security

## The lethal trifecta

Three capabilities are dangerous only in combination: (1) access to valuable data or
secrets, (2) exposure to untrusted content, (3) the ability to communicate externally. An
agent (or a human, equally) that processes a repo or the open web — exposure to untrusted
content is essentially unavoidable for either — becomes dangerous the moment it *also* holds
real secrets and has open egress: it can be driven, by content it merely reads, into
exfiltrating what it holds. `stx`'s whole design is one move: never let a box hold both (1)
and (3) at once.

## The two zones

| Zone | Boxes | Secrets | Code | Egress | Network |
|---|---|---|---|---|---|
| `trusted` | the repo's box(es) | dev-tier credentials only (scoped, short-lived, worthless if stolen) | a live, writable worktree | default-deny allowlist of hostnames the manifest names | its own per-sandbox network, joined by the platform services it uses and (only if explicitly linked) one other sandbox's network |
| `dmz` | the dmz sidecar | nothing but a dedicated, spend-capped harness key | a read-only, inert snapshot | open internet, minus a small deny-set | its own network, never the platform's |

A trusted box breaks the *egress* leg of the trifecta — it can hold real credentials because
it can only ever talk to a short, declared list of hosts. A dmz box breaks the *data* leg —
it can talk to anything because the only thing it holds is a credential designed to be
disposable. Same mechanism throughout (one environment-variable path, one firewall, two
rule-sets keyed off which zone a box is) — only the contents differ.

## The two invariants, plainly

**No box ever gets real power.** Nothing a box holds — secrets, data, or a path back out —
should be worth more than the box itself. If a box is fully compromised, the cost of that
compromise is bounded by what dev-tier credentials and a disposable API key are worth, not
by what production access would be worth. This depends on an author only ever handing `stx`
dev-tier secrets — `stx` has no way to tell a dev-tier GitLab token from a production one.

**No communicating set of boxes holds both secrets and open egress.** The trifecta
reasoning applies to a *union* of boxes that can talk to each other, not just to one box in
isolation — a coordinated prompt injection can drive every box reachable from the poisoned
content at once. Trusted and dmz boxes share no network and no filesystem path the
secret-holding side can write into the egress-holding side; the one handoff directory that
crosses the boundary flows only dmz-to-trusted, never the other way. A shared, live worktree
would be a byte-perfect channel even mounted read-only on the open-egress side — which is why
the dmz box only ever gets an inert, file-copied snapshot, never a worktree.

## Isolation between sandboxes

Two trusted boxes belonging to different sandboxes are isolated from each other by default:
each gets its own Docker network (`stx-<slug>--net`), and the trusted-zone firewall accepts
only the box's own network interfaces — looked up from the container at startup, not a fixed
private-range allowlist. Per-sandbox networks close off cross-sandbox reachability at the
network layer — a compromised box can't reach another sandbox's running app or dev
credentials directly — the same way dmz isolation itself works, rather than relying on a
firewall rule that could have a bug.

Traefik joins no sandbox network, for the same reason: it is the one container every sandbox
with routes would otherwise need attached, which makes it a cross-sandbox bridge if it ever
joined more than one `stx-<slug>--net` at a time (a box could then resolve `stx-traefik` by
name and ask it to route to another sandbox's own app). Instead it reaches each box's own
route through the host — see [reference.md](reference.md#routing) for how a route's port gets
there. A box's own trusted-zone firewall already scopes private-network access to the Docker
network it's actually attached to (see
[box-contract.md](box-contract.md#the-runtimes-init-sequence), step 4) — which covers both
`host.docker.internal` and the host address a route publishes to, since neither is on the
box's own network.

A **link** (`stx new --link <name>=<target>`, declared in the manifest's `links`) is the one
explicit exception: it attaches the linking sandbox's box to `<target>`'s whole network, not
just to one port or route. The two boxes then share a network, so the target's box can reach
the linking box too. The linking box can also reach every platform service the target's
network carries, such as the shared Postgres and Redis, even when its own manifest uses none.
It gets no credentials for them, so it still can't sign in. Reach for a link only when an agent genuinely needs to exercise
one sandbox's app against another's live backend — most cross-sandbox needs (a shared read
replica, a shared external API) are better served by a platform service or an allowlisted
host than by opening a sandbox's own network to another box. A link is fixed for the
sandbox's life: there is no way to narrow or revoke just the network opening without
recreating the sandbox, so treat `--link` the same as any other decision with a lasting
security consequence.

## What `stx` enforces

- The [box contract](box-contract.md): a repo's compose file cannot grant itself a capability,
  a network, a mount, or a secret the overlay didn't already decide to give it — checked by
  an allow-list, then checked again against what Compose actually merged. The same allow-list
  reserves the environment names that would subvert the root entrypoint — the `STX_`/`GIT_`/
  `LD_` families, the proxy and interpreter-hook variables, `BASH_FUNC_` and any name
  containing `%` (a bash function-export smuggled into the NET_ADMIN script) — and the
  entrypoint additionally runs `bash -p` so no such function is imported even if one slipped
  through.
- Host git never trusts a sandbox worktree's own metadata. A trusted box's worktree and parts
  of the repo's common git dir are mounted read-write (see the box contract's Mounts row), so
  a box can replace the worktree's gitfile with a real `.git` directory holding its own
  `core.fsmonitor`/`core.hooksPath`/`core.sshCommand`/a filter driver, or rewrite its own
  admin dir's `commondir`/`gitdir` files — any of which would run as the host user the moment
  a host-side `git -C <worktree>` command discovered it. `core.hostgit` is the one path every
  such call goes through: it verifies the worktree's gitfile and admin dir still hold exactly
  what `git worktree add` wrote, refuses outright if a `config.worktree` (per-worktree or
  top-level) or a top-level `commondir` — files `git worktree add` never writes — is present
  at all, then runs with an explicit `GIT_DIR`/`GIT_COMMON_DIR`/`GIT_WORK_TREE` (never
  discovery, so the box's own `commondir` pointer is never read to find the shared dir) and
  command-line config overrides (`core.fsmonitor=false`, `core.hooksPath=/dev/null`,
  `core.sshCommand=false`, `diff.external=`, `protocol.allow=never`, plus
  `GIT_CONFIG_NOSYSTEM`/`GIT_ATTR_NOSYSTEM`) that win over anything a box could have written,
  even if verification were somehow bypassed. It also reads attributes from the empty tree
  (`GIT_ATTR_SOURCE`), not the worktree's own in-tree `.gitattributes`, so a command that
  touches working-tree content — `git status`, which runs the clean filter to decide whether
  a path changed — can't be steered into a `filter`/`diff` textconv driver the box labelled a
  path with (see the residual-risk note below for the narrow case this closes). `rm`,
  `prune`, and `code` (before opening a worktree in a host IDE) all go through it; a
  verification failure reads the same as any other failed check (refuse without `--force`,
  warn and proceed with it) — `rm`/`prune --force` skip the inspection outright, the
  recovery path for a sandbox whose metadata was altered.
- A host command run from *inside* a sandbox worktree (the operator `cd`'d into
  `~/.local/state/stx/worktrees/<slug>`) maps that worktree back to its repo by slug through
  the registry, never through `git worktree list` — whose output is parsed from the
  worktree's own `commondir`/`gitdir` admin files, which a box can write.
- Zone assignment: derived from box kind, never a flag a manifest or a command line can
  override.
- The deny-host-set, applied in every zone regardless of the egress allowlist, including the
  trusted zone's own. It covers the Tailscale CGNAT range, cloud-metadata/link-local
  addresses, and the two host addresses resolvable by name — the default-route gateway
  (which on a Linux bridge network *is* the host, and sits inside the box's own scope-link
  subnet) and `host.docker.internal` where it resolves. These drops are applied before the
  trusted zone's subnet ACCEPT, so the box can talk to its own subnet without ever reaching
  the host through the gateway. It is *not* every host-owned address: on Linux the host also
  answers on `docker0` (172.17.0.1), other bridge gateways, and its LAN address, all RFC1918.
  A trusted-zone `egress.ports` carve-out — which otherwise has no destination limit — is kept
  off RFC1918/CGNAT/link-local (except the box's own already-accepted subnet), so publishing a
  route on `docker0` doesn't hand the box user a path to a host service there; the dmz zone
  drops those ranges wholesale.
- The trusted zone's private-network scoping and the mandatory IPv6 drop — see the
  [box contract](box-contract.md)'s init sequence.
- Mutual exclusion between harness credentials — see [secrets.md](secrets.md#harness-auth).
- Forge scope is pinned to the repo itself: `forge.project` must equal the project path
  parsed from the repo's own `origin` remote (refused at manifest load otherwise), and
  `forge.package_registry.group` must be an ancestor group of it — a manifest can't name a
  different project or an unrelated group to get a token minted against it (see
  [manifest.md](manifest.md#forge-optional)).
- Secrets never land on disk: `stx` builds a trimmed environment for the `docker compose`
  process it runs — never its own full process environment — holding only what that
  invocation actually needs (see [secrets.md](secrets.md)). The generated overlay file holds
  no secret values, and is still owner-only and deleted on teardown.
- Network topology, not firewall rules, as the thing that actually makes dmz isolation hold —
  a firewall rule can have a bug; a sidecar on a separate Docker network that was never
  joined to the platform's cannot reach it no matter what rule is misconfigured. The dmz
  box's repo snapshot is a read-only, `.git`-less file copy, never a live worktree, so even a
  read-only mount can't become a byte-perfect channel back to trusted content via a shared
  object store.

### Credential lifetime

Forge tokens are short-lived and scoped to one project — 2 days for the default write-scoped
token, 7 days (the maximum for any sandbox token) for `--ro`'s read-only one. A sandbox
removal or refresh revokes the old ones; the expiry is the backstop for whatever a revoke
itself can't reach (a crashed `new`, a lost registry entry). Each minted token's own expiry
date is recorded in the registry's forge record; `stx ls` and `stx guide` warn, naming the
sandbox and `stx refresh`, when any sandbox's tokens expire within 24h.

## What a repo author still has to get right

- **Only ever configure dev-tier credentials.** `stx` has no way to distinguish a scoped,
  disposable credential from a production one — the manifest's `env` and `provision.env`
  will faithfully wire up whatever the secret source hands them.
- **Choose non-attacker-readable egress hosts.** The allowlist filters by hostname, not by
  what the content at that hostname does. A manifest that allowlists a generic paste site or
  open bucket gives a compromised trusted box somewhere to smuggle data to, even though the
  connection itself is "allowed." Allowlist your own services and authenticated vendor APIs,
  not generic sinks.
- **Be deliberate with `egress.ports` carve-outs.** These bypass the hostname-filtering
  proxy entirely, for a declared `port/proto` token. They exist for traffic that can't go
  through a CONNECT proxy (some real-time protocols need this) — treat each one as a hole
  you specifically decided to open, not a default.
- **Keep `setup` scripts trustworthy.** They run with the box's own user privileges, inside
  the trusted zone, with egress already live — the same zone that holds real (if dev-tier)
  secrets. A script that fetches and executes unreviewed remote content undermines the zone's
  own guarantee from the inside.
- **Meet the image requirements.** A box missing a required binary fails loudly in `doctor`
  rather than booting into a half-enforced state.

## Residual risks

- **DNS resolution from a trusted box is an open channel.** The egress allowlist filters
  outbound `CONNECT` by hostname, but DNS resolution itself isn't filtered the same way —
  nothing today stops a box resolving, or tunneling data through, an arbitrary DNS query to
  whatever resolver it ends up using. Closing this needs a filtering resolver in the loop;
  that's tracked as follow-up work, not shipped yet.
- **Egress enforcement runs from binaries that live inside the repo's own image.** `iptables`,
  `tinyproxy`, and `ulogd` — the tools that actually apply the firewall and the allowlist — are
  installed by the repo's own Dockerfile to satisfy the image requirements (see
  [box-contract.md](box-contract.md#image-requirements)), not supplied by `stx` itself. A
  compromised or misbehaving base image could ship versions of those tools that don't enforce
  what `stx` assumes they do. Moving enforcement into an `stx`-owned sidecar, so a repo's image
  never has to be trusted for this, is tracked as follow-up work, not shipped yet.
- **The image build itself runs with an open network, on the host's own Docker daemon.** The
  build context is confined to the repo root (see [box-contract.md](box-contract.md#the-repo-compose-allow-list)),
  but any `RUN` step in the repo's own Dockerfile executes with full build-time network access
  and host Docker daemon privileges — `stx` reviews only what the build *copies in* (the
  context), never what a `RUN` step does at build time. A malicious Dockerfile is equivalent to
  arbitrary code execution on the host at image-build time, same trust level as the repo itself.
- **The dmz snapshot is still your source.** It carries no secrets, but the repo's source is
  still confidential intellectual property, and a dmz box has open egress by design. The
  snapshot is read-only and inert (no `.git`, no way to push from it), and the dispatch is
  audited — but a sufficiently motivated prompt injection against a dmz box with a repo
  snapshot mounted could still exfiltrate that source. Weigh whether a given research task
  actually needs the snapshot at all, or whether it can do its planning in the trusted zone
  instead (which has its own allowlist friction, but no open exfiltration channel at all).
- **The egress allowlist is a hostname filter on the CONNECT line, not TLS inspection.** It
  does not — and cannot, without a CA it would have to install — see inside an allowed
  connection. An allowed host whose own infrastructure is compromised, or that is multi-tenant
  with attacker-controlled content on the same hostname, is a gap the allowlist can't close.
- **`stx`'s own always-allowlisted hosts (the coding-agent harness's Anthropic API endpoint,
  the forge host) are themselves generic upload-capable services.** The allowlist controls
  *which hosts* a box can reach, not *what* it sends them — a compromised box still holds the
  harness's own credential and (in the trusted zone) a scoped forge token, and both the
  Anthropic API and the forge host accept arbitrary attacker-chosen content authenticated with
  the box's own real credentials (a prompt sent to the model, a file pushed to a throwaway
  branch). This is the same gap as the line above, just via a host `stx` itself adds rather
  than one the manifest names.
- **Dev-tier credentials are still real credentials.** They're scoped and short-lived by
  design, but a leaked one is still a leaked credential until it expires or is revoked.
- **A trusted box's confinement is only as good as the manifest and image an author wrote.**
  `stx` enforces the contract mechanically, but an author who allowlists too broadly, or
  whose setup script is itself unsafe, has still weakened their own sandbox within what the
  contract permits.
- **A box's ref writes are narrowed to two local-branch namespaces; objects and
  remote-tracking refs stay shared and writable.** Reopened read-write over the read-only top
  (below) are `objects/`, `refs/remotes/`, `logs/`, and — of `refs/heads/` — only
  `refs/heads/stx/` (where the box's own `stx/<slug>` branch lives) and `refs/heads/tmp/<slug>/`
  (its own cross-branch scratch namespace, see [reference](reference.md), Branches). The rest of
  `refs/heads/` falls through the read-only top. So a box **can't** plant a loose
  `refs/heads/main` (a loose ref overrides the packed one) or move whatever branch the operator
  has checked out — the write that would otherwise carry a box-made commit into the operator's
  identity on their next pull/commit/push. It also can't write another box's `refs/heads/tmp/<other>/`
  (`refs/heads/tmp/` itself stays read-only), delete any ref (git takes the shared `packed-refs`
  lock for every delete, and that lock lives in the read-only top), or rewrite `packed-refs`,
  `refs/tags`, or `modules/`. Repacking/pruning — which also rewrite `packed-refs` — are turned
  off in the box's git config (`gc.auto=0`, `fetch.prune=false`) so ordinary work never reaches
  for a rewrite the mount would reject. A box **can** still move another sandbox's `stx/*` branch:
  `refs/heads/stx/` is shared across the repo's slugs, not scoped per slug.
- **Two shared-write exposures remain.** Neither the object store nor the remote-tracking refs
  are scoped per slug:
  - *Objects are shared and writable.* A box can replace loose objects and pack files that the
    operator's own checkout reads — content the operator may later commit on top of, or push.
  - *`refs/remotes/` is writable.* A box can plant a remote-tracking ref (e.g. `origin/main`)
    pointing at a commit it chose; the operator's next `rebase`/`merge` *without a fetch first*
    would silently use it. For the same reason the unpushed-work teardown guard (`rm`/`prune`
    comparing `HEAD` against `--remotes`) can be fooled into reading a box's own unpushed
    commits as pushed: it protects against an operator's mistake, not against the box.

  Practical advice for an operator sharing a repo with untrusted boxes: **fetch before you
  rebase or merge** (so your remote-tracking refs come from the remote, not a box), and **don't
  trust local objects** from such a repo — if a box may have misbehaved, `git fsck` the checkout
  before pushing from it.
- **The top of the common git dir is read-only; only the subtrees above are reopened.** `/git`
  is mounted read-only, and `objects/`/`refs/heads/stx/`/`refs/heads/tmp/<slug>/`/`refs/remotes/`/`logs/`
  and the box's own `worktrees/<slug>/` are the only read-write mounts layered back on top. This is what stops a
  box *creating* a new top-level file — a `commondir`, a `config.worktree`, a `config` — that
  git would then read for the **main** repo too: such a file would otherwise steer the
  operator's own `git status` (and their IDE, and `stx`) into a config the box chose, i.e.
  code execution on the host. The box's own `worktrees/<slug>/commondir` and `.../gitdir`
  pointers are likewise pinned read-only on top of its otherwise-writable admin dir, so it
  can't retarget its own administrative lookups either (host git refuses an altered pointer
  as well — see `core.hostgit` — but the mount stops the alteration happening at all).
- **A box can still read, just not write, another sandbox's own `worktrees/<other>/` admin
  dir.** The whole `worktrees/` directory is read-only — a live view of the host directory
  (it falls through the read-only `/git` top), not a snapshot, so a sibling sandbox's worktree
  created *after* this box started is covered automatically, with no need to recreate this
  box's overlay — and only this box's own `worktrees/<slug>/` gets a read-write mount layered
  back on top. What's in there is git plumbing (HEAD, the index, a reflog) — not a secret — so
  this is a visibility gap, not a write one.
- **A box-written `.gitattributes` can still invoke a filter driver the common config defines,
  but not through a command stx runs.** `core.hostgit`'s hardened invocations read attributes
  from the empty tree (`GIT_ATTR_SOURCE`), on top of `core.attributesFile=/dev/null` and
  `GIT_ATTR_NOSYSTEM=1`, so the worktree's own in-tree `.gitattributes` selects no
  `filter`/`diff` driver for any git stx itself runs — closing the path where `git status`
  would have run the operator's clean filter as the host user. The attribute override is
  ignored by a git older than 2.40; the escalation it most mattered against — a box
  *defining* its own filter in a `config.worktree` and then labelling a path with it — is
  closed regardless, since `core.hostgit` refuses a worktree that has a `config.worktree` at
  all. A filter the operator defined in the read-only common `config` can still be invoked by
  the operator's own ad-hoc `git -C <worktree>` (one that doesn't go through `core.hostgit`),
  running with whatever trust they already placed in it; that doesn't grant a box anything
  beyond that.
- **Opening a sandbox worktree in a host IDE is a point-in-time check, not a standing guard.**
  `stx code` verifies the worktree's metadata right before launching the IDE, but the IDE's
  own git integration keeps running against that worktree for as long as it stays open — a
  box that alters the metadata *after* the editor opened isn't caught until the next `code`
  invocation (or `rm`/`prune`) re-checks it.
- **Every sandbox shares the platform Redis's `app` user.** Redis ACLs can't confine a user
  to one database index, so a box holding the app password can read and write every other
  sandbox's Redis data. `stx` refuses a compose file that references the Redis admin
  password (`STX_REDIS_PASSWORD`), so no box can change ACLs or flush the server, but the
  shared app user is a cross-sandbox channel by construction. Keep anything sensitive out of
  sandbox Redis.
- **Every sandbox's `provision.steps` share the one platform-wide `stx_provisioner` role**
  (see [secrets.md](secrets.md#provisioning-credentials)). It's `CREATEDB CREATEROLE`, not
  superuser, and Postgres 16 bounds `CREATEROLE` to roles the grantee itself created — but a
  provisioning step from one repo can still create databases/roles visible to (and, for
  anything it itself created, administrable by) another sandbox's provisioning step, since
  the role isn't scoped per-slug. Name provisioned databases and roles distinctly per repo
  (as the widgetshop example does, templating `{db}` from the slug) to avoid collisions, not
  as an isolation boundary.
- **A bare `*/udp` in `egress.ports` lets the box user send UDP to any host, not just the
  ones in `hosts`.** It exists so real-time media (WebRTC/ICE) can reach whatever port a
  peer negotiates, which can't be known ahead of time — but it bypasses the proxy allowlist
  entirely for that protocol, wider than "WebRTC media" on its face. Prefer a scoped
  `<port>/udp` token in the manifest wherever the protocol permits it (see
  [manifest.md](manifest.md#egress-optional)).

## Reporting a vulnerability

See [SECURITY.md](../SECURITY.md) for how to report privately, supported versions, and the
response and disclosure timelines.
