# Box contract

A dev box is one compose service built from two files merged together: the repo's
`compose.yaml` says what the app needs; `stx`'s generated overlay says how a sandbox box
behaves. Everything the trust model depends on lives in the overlay, and the repo file is
held to an allow-list so it can never reach into the overlay's territory.

## The split

| Concern | Repo supplies | `stx` supplies |
|---|---|---|
| Image | `build:` under the service named by the resolved `name` (the compose file's sole service, or an override), meeting the image requirements below | the image tag (`image`, defaulting to `stx-<repo>-<hash>` — the repo dir name plus a short hash of the checkout's common git dir path), shared across every sandbox of the repo |
| Entry | — | the box runtime's entrypoint and an idle `sleep infinity` command; a `provision.steps` run's argv instead, dropped to the box user before exec (see [The runtime's init sequence](#the-runtimes-init-sequence)) |
| App environment | app vars (DB roles, API keys, app config); may interpolate any host variable the repo's own compose file references, required unless it has a default (see [secrets.md](secrets.md)) | the manifest's `env`, rendered, plus tool-owned variables (slug, zone, git config, proxy config, forge token, harness auth) |
| Mounts | named volumes declared in the same file | the workspace; the repo's common git dir mounted read-write, narrowed back to read-only for `config`, `hooks`, `info`, `refs/tags`, `modules/` (when present), and the whole `worktrees/` dir — with the box's own `worktrees/<slug>/` admin dir reopened read-write on top (never another sandbox's own `worktrees/<other>/` — see [security.md](security.md#residual-risks)); handoff dirs, the box runtime itself, the egress allowlist |
| Labels | any key (not enforced by the allow-list — a colliding `stx.`/`devcontainer.` key is merely overridden, since `stx`'s overlay is always the last compose file merged) | its own identifying labels |
| Network | — | its own per-sandbox network (`stx-<slug>--net`), plus a linked target's network when `--link` is used — see [security.md](security.md#isolation-between-sandboxes) |
| Hardening | — | capability drops, a minimal capability add-back (`NET_ADMIN`, `SETUID`, `SETGID`), `no-new-privileges` |
| Identity | — | container name, hostname, working directory |
| Ports | — | a host port per declared route (so Traefik, which joins no sandbox network, can reach it — see [reference.md](reference.md#routing)), plus the tailnet port if `publish` is set |
| Init | `setup` scripts, run as the resolved user from `/workspace` after egress is up | firewall rules, the deny-host-set, the proxy, logging, forge git credentials, harness setup |
| dmz service | — (the repo never defines one) | the whole service: a read-only repo snapshot mounted at `/workspace`, its own network, its own hardening, the dedicated harness key only |

### Why the split is drawn here

The repo owns exactly what makes its app run: how to build the image, what the app needs
in its environment, what it mounts for its own caches. `stx` owns exactly what makes the box
*safe*: the network it's on, what it can reach, what capabilities it has, what credentials
it's handed. A repo author who wanted a sandbox to behave like a CI runner — scoped
credentials, filtered egress, no path back to the host — gets that by construction, without
having to get any of it right themselves. A repo that could opt into looser confinement
through its own compose file would make that guarantee only as strong as every repo author's
compose file, not as strong as `stx`'s own code.

## The repo compose allow-list

Before merging, `stx` normalizes the repo's compose file alone (resolving `extends`, anchors,
and short syntax) and refuses it unless:

- top-level keys are only `services`, `volumes`, `x-*`, and what Compose itself always adds
  (`name`, and a top-level `networks` map holding nothing but a `default` entry); volume
  definitions have no `driver_opts`, no `labels`, aren't `external`, and use no `driver` other
  than `local`; an explicit `name:` (as opposed to Compose's own computed
  `<project>_<volume-key>` default) is only ever allowed with the per-repo
  `stx-cache-<repo-id>--` prefix (`<repo-id>` is the main checkout's directory name) — a
  shared cache across every sandbox of the repo (see [manifest.md](manifest.md#frontend-repos)),
  never a way to name (and so mount) the platform's own, another sandbox's, or another repo's
  volume; the `--` keeps one repo's prefix off another's (`app` can't reach `app-web`'s cache),
  so a repo whose directory name ends with `-` or contains `--` is refused — rename it;
- `services` has exactly one entry, matching the resolved `name` — no `dmz`, no sidecars;
- that service's keys are only `build`, `environment`, `volumes`, `labels`, `shm_size`, and
  whatever `networks` reference Compose itself always synthesizes for a service with no
  explicit network (the single `default` entry — anything else under `networks` is refused);
- `environment` sets no [reserved name](#box-environment);
- `build` keys are only `context` (inside the repo root), `dockerfile`, `args`, `target`;
- every `volumes` entry is a named volume declared in the same file, mounted outside the
  tool-reserved paths (the workspace, git metadata, handoff dirs, the box runtime, the proxy
  config, and anything beneath them) — no bind mounts, no `volumes_from`.

Anything else — `privileged`, `network_mode`, `cap_add`, `security_opt`, a non-default
`networks` value, `ports`, `extra_hosts`, `dns`, `secrets`, `configs`, `user`, `entrypoint`,
a second service, and more — is refused by name, naming the file, service, and key. A
deny-list was considered and rejected: compose has too many ways to loosen confinement
(volume targets, host mappings, extra capabilities) for a deny-list to keep up, where an
allow-list only has to be right about what it *does* permit. The allow-list grows by
deliberate decision, never by default.

## Box environment

The box's entrypoint runs as root and reads its instructions from environment variables:
which zone it's in, which egress ports to open, which user to drop to, where to write the
setup log. A repo that could set those could open its own firewall. So names starting
`STX_`, `SANDBOX_`, `GIT_`, or `LD_`, the proxy variables (`HTTP_PROXY`, `HTTPS_PROXY`,
`NO_PROXY`, `ALL_PROXY`, in either case), and interpreter hooks the root entrypoint's own
tools (bash, python, perl, node, ruby) could pick up before dropping to the box user
(`BASH_ENV`, `ENV`, `PROMPT_COMMAND`, `PYTHONSTARTUP`, `PERL5OPT`, `NODE_OPTIONS`, `RUBYOPT`),
are reserved. So is `BASH_FUNC_` — bash exports a function definition as
`BASH_FUNC_name%%=() { ...; }`, so a repo-set `BASH_FUNC_iptables%%` would make the root
entrypoint's bash define `iptables` as a function and run repo code from the NET_ADMIN
script — and any name containing a `%` at all (the bash-function export marker, never part of
a name a repo legitimately needs). The entrypoint itself also runs `bash -p`, which refuses
to import an environment function in the first place, as a second layer under this
reservation. Reserved names:

- the manifest's `env` refuses them at load;
- the repo compose file's `environment` refuses them (the allow-list above);
- the overlay sets every variable the entrypoint reads, and refuses to render if the
  manifest's `env` names one of them, so no repo value can win a merge.

## The post-merge check

Belt-and-suspenders against a compose-merge surprise: after generating the overlay, `stx`
asks Compose to merge the two files for real, then asserts — for both the box and dmz
services — that every security-relevant key (capabilities, security options, networks,
ports, user, entrypoint, command, the reserved mount targets, and the absence of every
refused key above) equals exactly what the overlay set. It also asserts the dmz service's
environment holds nothing beyond its five expected keys. A mismatch means a Compose release
changed how merging works; `stx` refuses and names the key, rather than silently running
with weaker confinement than intended.

Together, the allow-list and the post-merge check mean this holds for a repo `stx` has never
seen before: the repo can only supply what the allow-list names, and the result of merging
it with the overlay is checked, not assumed.

## Image requirements

Checked by `stx doctor`, by running a handful of `command -v` checks inside the image:

- `bash`, `iptables`, `ip6tables`, `ss` (from `iproute2`), `tinyproxy`, `ulogd`, `jq`, `git`,
  `su`
- a non-root user matching the resolved `user` (an override, or the image's own `USER`), with a home directory
- the coding-agent harness binary on that user's `PATH`

These are the tools the box runtime needs to enforce egress and run the dispatch wrapper. An
image missing one fails `doctor` with a clear message naming which, rather than failing
obscurely partway through a sandbox's first boot.

Every dmz box, with or without a repo, uses `stx`'s own dmz image — a Dockerfile shipped as
package data, built on first use and tagged `stx-dmz:<stx version>`, meeting these
requirements by construction. Its exec user comes from that image's `USER`, the same way a
repo's own box image is read. `stx rebuild --dmz` forces a rebuild.

## The runtime's init sequence

Every box starts under the box runtime's entrypoint, mounted read-only from the `stx`
package itself, before anything repo-specific runs:

1. **Render runtime configs** — the proxy config and the logging config, resolved against
   the actual image (so the image doesn't need to bake in a particular build-time path).
2. **Start the logging daemon** — before any firewall rule exists, so nothing is ever dropped
   silently in the gap before logging is live.
3. **Apply the deny-host-set** — a small set of addresses blocked in every zone regardless of
   egress policy (link-local addresses, the Tailscale CGNAT range, and the Docker host
   itself), so a compromised box can't reach the one network path back toward the host even
   before the zone's own policy applies.
4. **Apply the zone's egress policy** — the trusted zone's allowlist proxy plus any direct
   carve-outs the manifest declared, with private-network access scoped to the Docker network
   the box is actually attached to (looked up from the container's own interface at start),
   not every private range; the dmz zone's open-but-deny-host-set policy. In both zones, the
   IPv6 drop is mandatory — a failure to apply it aborts startup rather than silently leaving
   an IPv6 bypass open, since tinyproxy and the allowlist only ever see IPv4.
5. **Install first-run harness config and the zone's guide** — the config and the
   `~/.claude/CLAUDE.md` copy only if absent (the harness rewrites its own config at
   runtime, so this never overwrites a box that's already run once). The guide
   (`guide-trusted.md` or `guide-dmz.md`, picked by `STX_ZONE`) is copied to
   `~/.claude/CLAUDE.md` and also symlinked at `/run/stx/AGENTS.md` — `/opt/stx/box` itself
   is read-only, so a fixed, zone-specific symlink can't live there; the symlink is
   (re)written on every start, since it only ever points at the current zone's fixed guide
   file.
6. **Set up forge git credentials** — rewrites the git remote to use the box's own scoped,
   short-lived forge token.
7. **Pre-approve the harness's API-key confirmation prompt** — so a first interactive run
   doesn't stall on a one-time dialog.
8. **Run the repo's own `setup` scripts**, in the trusted zone only, as the resolved user, from
   `/workspace` — last, and only after every security-relevant step above has already run, so
   a repo's own setup (installing dependencies, fetching a build-time credential) never races
   against the box becoming safe to use.
9. **Exec the container's own CMD** — root's traffic is exempt from the firewall (so tinyproxy
   and the init sequence above can reach the network before any policy applies), which makes
   what runs here security-relevant: the long-running box's own CMD (`sleep infinity`, not
   repo-supplied) execs as-is; a `provision.steps` run's argv — repo-supplied, reaching the
   container via `compose run --rm <name> <argv>` — execs as the box user instead, via `su`,
   so the egress policy from step 4 actually applies to it.

The dmz box runs a shorter version of the same sequence — no forge credentials, no
`setup` scripts (it never mounts a live repo to run them against), but the same deny-host-set
and logging steps.
