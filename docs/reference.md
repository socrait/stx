# Reference

## Commands and flags

Every verb below is host-only: `stx` refuses to run any of them from inside a box, naming
the reason, rather than failing confusingly partway through. A box has no registry, no
secret source, and no Docker socket of its own — manage sandboxes from the host.

| Verb | Flags | Behavior |
|---|---|---|
| `init` | | Write a `sandbox.yaml` starter inferred from the repo — the compose file Compose would find, the Dockerfile it builds, ecosystem egress hints, a private-registry guess. Refuses if one already exists. See [manifest.md](manifest.md#generating-a-starter-manifest). |
| `new <slug>` | `--repo PATH\|none`, `--with-dmz`, `--base BRANCH`, `--branch NAME`, `--ro`, `--oauth`/`--api-key`, `--shell`, `--code`, `--no-hosts`, `--link NAME=TARGET`, `--replace`, `-y`/`--yes`, `--timeout SECONDS` | Provision a sandbox: worktree, box(es), platform services it needs, routes, credentials, its own network. Refuses outright if any of its `platform.postgres.databases` already exist — see Teardown guards below; no flag adopts them. Waits for the app box's `setup` scripts to finish before reporting ready — `--timeout` (default 900s/15m), with a heartbeat every 30s; a setup failure or timeout is reported but never rolls back what already provisioned, so the box is left running to debug (see Setup below). If the container itself stops before setup finishes (entrypoint crash, firewall setup failed, tinyproxy didn't bind), that's reported immediately, with the container's last log lines, rather than waiting out the full timeout. `--shell`/`--code` open the new sandbox immediately after provisioning. `--link` (repeatable) links this sandbox to an existing trusted one under a manifest-declared name — see [manifest.md](manifest.md#links-optional) and [security.md](security.md#isolation-between-sandboxes); fixed for the sandbox's life. `--replace` tears down an existing sandbox of the same slug first — through `stx rm`'s normal path and *its* guards (uncommitted/unpushed work, undelivered handoff files, linked-by sandboxes all abort the replace), never a silent force-remove; `-y`/`--yes` skips `rm`'s teardown confirmation during that `--replace` and has no effect otherwise. If provisioning fails, rollback runs; if rollback itself can't reclaim everything, the entry is kept in `provisioning` (with its redis index, database names, and any minted token ids) so `stx rm` can retry rather than orphan what's left. A terminating signal (SIGTERM/SIGHUP) mid-run unwinds like Ctrl+C rather than killing the process with everything behind. |
| `shell <slug>` | `-b`/`--box`, `-- <cmd>`, `-c "<cmd>"`, `--detach` | Interactive shell by default; `--` runs one command with no shell; `-c` runs it through `bash -c`; `--detach` backgrounds it and redirects output to a log file. |
| `start <slug>` | `--timeout SECONDS` | Start a stopped sandbox's containers back up (e.g. after a host reboot — containers don't come back on their own), re-attach the platform services it uses to its own network, and wait for the app box's `setup` scripts to finish the same way `new` does. A box already running is left alone. Refuses naming `stx new`/`stx refresh` if a box's container was removed outright, not just stopped — `start` can't recreate that. `shell`/`agent`/`code` hint at this when a box isn't running. |
| `code <slug>` | `-b`/`--box`, `--attach`, `--ide auto\|cursor\|vscode` | Open the sandbox's worktree in Cursor or VS Code (tries Cursor, then VS Code, by default). `--attach` opens the running container itself via Dev Containers instead of the host worktree folder — tries Cursor first here too, since VS Code's Dev Containers extension has been observed to refuse a valid attach target that Cursor opens successfully against the identical container. |
| `agent <slug> "<task>"` | `-b`/`--box`, `--model ALIAS`, `--wait`, `--task-file PATH` | Dispatch a headless coding-agent run. `--model` picks the harness model alias or id (default `sonnet`). Without `--wait`, returns immediately; pair with `await`. One dispatch per box at a time. `--task-file` reads the task from a host-side path instead of the positional argument (mutually exclusive with it); refuses a missing or empty file. |
| `await <slug>` | `-b`/`--box`, `--timeout SECONDS` | Block until the box's current dispatch finishes and print its result. `--timeout` gives up after that many seconds (default: wait forever). |
| `logs <slug>` | `-b`/`--box`, `--follow`, `--list`, `--raw`, `-v` | Inspect dispatch history. No flag pretty-prints the latest dispatch. `-v` also prints successful tool results, not just failures. |
| `status` | | Report each platform service's drift state (missing/stopped/matching/differing) and active sandbox count. Also warns, same as `new`, when the running `stx-traefik`'s `/routes` mount doesn't point at stx's own routes dir. |
| `ls` | `--wide`/`-w` | List sandboxes: slug, status, age, forge token expiry, dispatch status, branch by default; `--wide` adds boxes, scope, db, redis, state, repo, port, auth, and links. Warns, naming the sandbox and `stx refresh`, when any sandbox's forge tokens expire within 24h, and again once they've actually expired. Warns and skips any entry it can't parse, naming `stx prune` as the fix. |
| `open <slug> [route]` | | Open a route in a browser; no `route` opens the bare `<slug>.localhost`, which serves the manifest's first route. Naming a route opens its own `<route>.<slug>.localhost` instead. |
| `hosts add\|remove <slug>` | | Add or remove `/etc/hosts` entries for a sandbox's routes. |
| `refresh <slug>` | `--force`, `--oauth`, `--api-key`, `--timeout SECONDS` | Recreate the trusted box, and — only when the repo declares a forge and the box carries one — re-mint its credentials in the same pass. Mint fresh, recreate the box on them (a failed recreate leaves the old credentials intact), then, *before* waiting for setup, swap the registry over to the fresh credentials and revoke the old ones: an interrupt during the (long) wait must never leave the box on new credentials with the registry still recording — and `rm` still revoking — the old ones. An old-credential revoke that fails keeps the old ids on the entry (`pending_forge`) for `stx rm` to retry. A sandbox with no forge is still recreatable (new env, rebuilt image, an added route) — it just has nothing to re-mint; a route added to the manifest since `new` is allocated a host port and published. Optionally switches harness auth mode in the same pass. A dmz sidecar, if present, is recreated alongside. Refuses while a dispatch is running in either box, unless `--force`; a concurrent `rm` that removes the entry mid-refresh aborts cleanly (revoking the orphaned fresh credentials). |
| `rebuild` | `--repo PATH`, `--dmz` | Rebuild the current repo's box image (the manifest's `image`); `--dmz` force-rebuilds `stx`'s own dmz image instead. Running sandboxes keep their old image until recreated. |
| `rm <slug>` | `--force`, `--rm-branch`, `--keep-db` | Tear down a sandbox: boxes, databases, Redis index, forge token revocation, worktree, handoff dirs. Preflights that it *can* finish (secrets available, a forge client rebuildable) before touching anything irreversible, aborting with everything intact if not. Containers come down manifest-free — by compose project name, falling back to removing by the `stx.slug` label — so a broken `sandbox.yaml` never blocks teardown; if a container survives both, teardown aborts with the entry and overlay kept rather than orphaning a zombie, unless `--force`. Databases and the forge client to revoke with come from the registry entry itself, never the checkout's manifest — see Teardown guards below. A revoke that fails keeps the entry for a retry unless `--force`. Refuses while another sandbox links to it, naming them; `--force` disconnects the linkers' boxes from its network first. Refuses a `provisioning` entry whose owning `new` is still alive — it's in flight, not stuck — unless `--force`. `--keep-db` preserves the databases for manual inspection; `stx new` on the same slug later still refuses until they're dropped. Refuses outright on an unparseable registry entry, pointing at `stx prune`. |
| `prune` | `--dry-run`, `--force`, `-y`/`--yes` | Clean up zombie sandboxes (containers with no registry entry), registry entries a *crashed* `new` left stuck in `provisioning`, unparseable registry entries, orphaned `stx/*` branch config, and orphaned `tmp/*` scratch branches. A `provisioning` entry whose owning `new` process is still alive is skipped — it's in flight, not stuck (the owner's pid and a start-time token are recorded at `reserve()` to tell the two apart across pid reuse). Zombie and stuck entries tear down through the same shared path `rm` uses (containers by project/label, then DB, Redis, network, route, hosts, worktree, dirs); a failed container down keeps the overlay/egress for a retry rather than claiming success. Prints what it found, then asks to confirm before removing anything — pass `--yes` to skip the prompt, required in a non-interactive session. If the secret source is unavailable after confirmation, it warns and skips only the DB/Redis cleanup — the stuck-entry and branch sweeps still run. |
| `up [service...]` | `--recreate`, `-y`/`--yes` | Start shared platform services. No args starts everything; naming one or more services starts only those. Never recreates a running one — a service that's drifted from its own compose definition is left alone and reported, not fixed. `--recreate` is the only path that recreates a drifted service; it refuses while a sandbox using one has an agent dispatch in progress, unless `-y`/`--yes` is also passed. |
| `down` | `-f`/`--force` | Stop the platform. Refuses while any sandbox has a trusted box running, unless `--force`. |
| `config get\|set\|unset\|list` | | Manage per-human settings (below). |
| `paths [NAME]` | `--slug SLUG` | Print where `stx` keeps things, resolved for this shell's environment. No argument lists the machine-wide paths — `state`, `config`, `dotenv`, `platform`, `routes` — each with the setting that decided it (`STX_STATE_ROOT`, `XDG_STATE_HOME`, `secrets.dotenv.path`, `default`, …); `--slug` lists one sandbox's instead — `worktree`, `drop`, `drop-dmz`, `code-dmz`, `logs`, `compose`, `allowlist` — whether or not that sandbox exists. Naming one prints just its path, for scripts: `stx paths state`, `stx paths --slug demo worktree`. |
| `doctor` | | Report-only: git-identity provenance, hazardous universal git config, user config, dotenv file permissions, Compose version, and (inside a repo with a manifest) image requirements. |
| `guide` | `--raw` | Print the host skill guide standalone — no repo checkout or Claude Code plugin required. No flag prints the skill body only; `--raw` prints the whole file, frontmatter included. Warns, same as `ls`, when any sandbox's forge tokens expire within 24h. |

## User config

Per-human, per-machine settings, never version-controlled and never part of sandbox state.
Nested TOML tables in `$XDG_CONFIG_HOME/stx/config.toml` (default
`~/.config/stx/config.toml`; override with `STX_CONFIG_PATH`). The CLI addresses a key by
its dotted path, which is also its TOML spelling (`branch.user` is `[branch]` / `user =
...`). Every key is optional.

| Key | Values | Default | Used by |
|---|---|---|---|
| `branch.user` | `[a-z0-9-]+` | the forge's own username, else the local part of your git email | manifest `branch`'s `{user}` |
| `secrets.source` | `dotenv` \| `op` | `dotenv` | every verb that loads secrets |
| `secrets.dotenv.path` | file path | `$XDG_CONFIG_HOME/stx/secrets.env` | the `dotenv` source |
| `secrets.op.environment` | a 1Password Environment id | — (required when `secrets.source=op`) | the `op` source |

A first-time user needs no config at all beyond putting platform secrets in
`~/.config/stx/secrets.env`. `stx doctor` reports which keys are set and whether the dotenv
file is mode `600`. `config.toml` sits beside, not inside, the state root — it's
human-authored and survives a state wipe, unlike the disposable, machine-derived records
under `$XDG_STATE_HOME/stx/`. A corrupt or unreadable `config.toml` raises rather than
silently resetting every setting to default, same as the registry (below). `stx config set`
rewrites the file with `tomlkit`, keeping the rest of it — comments, table order — exactly as
the user left it.

## Repo root resolution

Every repo-scoped verb (`new`, `rebuild`, `doctor`, …) resolves exactly one repo root: the
checkout containing the cwd, or the path given with `--repo` (`--repo none` means no repo
— a dmz-only sandbox). A linked git worktree of the repo is itself a valid root, so
compose/Dockerfile/manifest changes can be tried on a branch without touching the main
checkout — except a *sandbox's own* worktree (anything under `worktrees/` in the state
layout below), which redirects back to the main checkout, so running a verb from inside a
sandbox on the host still finds the real repo. Resolution fails with an error naming the
path when the resolved root carries no `sandbox.yaml`.

## State layout (XDG)

Runtime state lives under `$XDG_STATE_HOME/stx/` (default `~/.local/state/stx/`; override
with `STX_STATE_ROOT`); `stx paths state` prints the resolved one. Nothing here is a cache — worktrees and handoff dirs hold real work,
and the registry tracks live containers, databases, and Redis indexes — so none of it is
safe to delete casually.

| Path | Holds |
|---|---|
| `registry.json`, `registry.lock` | The sandbox registry: one record per slug (state, creation time, Redis index, tailnet port, route ports, platform services, repo root, rendered database names) with one entry per box (zone, user, branch, worktree, image, forge credentials, forge source). Every read-modify-write holds a file lock. |
| `worktrees/` | Trusted-box git worktrees |
| `code-dmz/` | Dmz-box repo snapshots |
| `drop/`, `drop-dmz/` | Handoff directories (see teardown guards, below); `stx new` pre-creates both before the first compose invocation so docker never auto-creates the bind source root-owned |
| `routes/` | Traefik route files |
| `logs/` | Dispatch capture files and each sandbox's `manifest.jsonl` |
| `compose/` | Generated overlay files, one per slug |
| `allowlists/` | Rendered egress allowlists, one per slug |
| `conduct/` | Reserved for coding-agent orchestration tooling built on top of `stx`; `stx` itself does not write here |
| `platform/` | The one exception to "nothing here is a cache" — a stable on-disk copy of the platform package's compose/config files, rewritten from package data whenever its content changes; safe to delete, `stx` regenerates it on the next platform command |

A registry entry is `provisioning` from the moment `stx new` reserves its slug until
provisioning finishes, then `ready`. The reservation records the owning `new` process's pid
and a start-time token, so `stx prune` can tell a *crashed* `new` (safe to clean up) from one
still running (left alone). A `new` that crashes or is killed mid-run leaves its entry stuck
at `provisioning` — `stx ls` shows it, and `stx rm`/`stx prune` clean it up like any other
sandbox; a `new` whose rollback couldn't reclaim everything deliberately leaves the entry at
`provisioning` (carrying its redis index, database names, and minted token ids) so `stx rm`
can retry rather than orphan what's left. A corrupt or unreadable `registry.json`, or one whose `schema` doesn't
match what this `stx` build writes, stops the command with an error rather than silently
proceeding as if nothing were registered — a registry that can't be trusted must never be
treated as an empty one.

## Dispatch (`agent`, `await`, `logs`)

`stx agent <slug> "<task>" [-b box]` runs a harness headlessly inside a box to work one task.
`--task-file PATH` reads the task from a host-side file instead — refuses a missing or empty
file, and is mutually exclusive with the positional task; write non-trivial tasks to a file
rather than inlining them. Without `--wait` it returns immediately; `stx await <slug> [-b box]`
then blocks until the run finishes and prints its answer. Only one dispatch runs per box at
a time — `agent` refuses to start a second one until the first is awaited. `await`/`agent
--wait` raise rather than misreport if the box stops running before the agent finishes, or
if its pid has been reused by an unrelated process since a box restart (a stale `kill -0`
match from a fresh pid namespace); either way the exit code was never actually recorded. It also refuses
while the box's own `setup` scripts are still running (no outcome record yet at
`/run/stx/setup-status.json`) — `new`/`refresh` already wait for that record before
reporting ready, but a box entered via `stx shell` before then could otherwise race a
dispatch against setup. A `setup` record that reads `failed` doesn't block dispatch — that's
a known outcome, not "still running," and the point of leaving the box up is to debug it.

A dispatched harness runs with its permission prompts off (Claude Code's
`--permission-mode bypassPermissions`). Nobody is there to answer a prompt in a headless
box, and the box's confinement, not the harness's prompts, is what limits what an agent can
reach (see [security.md](security.md)). A repo's own harness settings still apply on top,
so a repo can deny specific tools.

Every dispatch's output is captured to the host, not just held inside the container: each
event is teed into `logs/<slug>/<box>/<n>.jsonl` (`<n>` a monotonic, box-scoped dispatch
index), wrapped in a `{"t": ..., "e": ...}` envelope. A sibling `logs/<slug>/manifest.jsonl`
durably records every dispatch — index, box, task, model, timestamps, exit code, session id,
cost — so dispatch history survives past the box's own in-memory state.

| `logs` flag | Behavior |
|---|---|
| *(none)* | Pretty-print the latest dispatch: header, one line per tool call/message, tool results shown only on error |
| `--follow` | Live-tail the latest dispatch; prints an idle heartbeat if nothing arrives for 90s |
| `--list` | Table of every dispatch recorded for the box, plus a running total cost |
| `--raw` | Emit the raw envelope JSONL for the latest dispatch |
| `-v` | With any of the above, show every tool result, not just errors |

`stx ls` shows each sandbox's current dispatch state in its DISPATCH column (e.g. `working
#4 · 8s` or `done #4 · exit 0`). A finished headless run is resumable by its exact session id
(shown in full by `logs --list`) from inside the box. The box's own status file is
informational; the actual ground truth for whether a dispatch is still running is a liveness
check against its recorded process id, run as the box's own user — this survives a wrapper
crash that would otherwise leave the status file reading "working" forever.

`stx ls`'s default columns are SLUG, STATUS, AGE, EXPIRES, DISPATCH, BRANCH — `--wide`/`-w`
adds BOXES, SCOPE, DB, REDIS, STATE, REPO, PORT, AUTH, and LINKS. AGE is the time since the
sandbox was created (`45m`, `5h`, `3d` — largest unit, rounded down). EXPIRES is the
earliest expiry date among the trusted box's forge tokens (`10-21`, or `today`); it reads
`expired` once that date has passed, and `-` when there's no token. LINKS lists this
sandbox's own outbound `--link` entries as `name→target` (comma-separated), or `-` if none.

```
$ stx ls
SLUG            STATUS   AGE  EXPIRES  DISPATCH          BRANCH
--------------------------------------------------------------------------
api-research    running  45m  -        -                 -
old-experiment  running  3d   expired  done #3 · exit 0  feat/alice/old-experiment
widgetshop-fix  running  5h   10-22    working #3 · 40s  feat/alice/widgetshop-fix
'old-experiment' forge tokens have expired — run `stx refresh old-experiment`

$ stx ls --wide
SLUG            BOXES   SCOPE  DB                   REDIS  STATE   STATUS   AGE  EXPIRES  REPO        PORT  AUTH   DISPATCH          LINKS       BRANCH
--------------------------------------------------------------------------------------------------------------------------------------------------------
api-research    dmz     -      -                    -      ready   running  45m  -        -           -     -      -                 -           -
old-experiment  app     rw     stx_old_experiment  7      ready   running  3d   expired  stx         -     api    done #3 · exit 0  -           feat/alice/old-experiment
widgetshop-fix  app     rw     stx_widgetshop_fix  3      ready   running  5h   10-22    widgetshop  5042  oauth  working #3 · 40s  api→demo    feat/alice/widgetshop-fix
'old-experiment' forge tokens have expired — run `stx refresh old-experiment`
```

## Setup

A box's `setup` scripts run once on first boot, after the box is up and its egress is in
force (see [box-contract.md](box-contract.md), step 8). `init-box.sh` writes a small status
record when they finish — the script name, its exit code, and a `done`/`failed` state —
which `new`, `start`, and `refresh` block on before reporting ready. A failed or timed-out
setup is reported but never rolls back what already provisioned: the box is left running so
the failure can be debugged, with the failing script named and its log pointed at. The wait
has a `--timeout` (default 900s/15m) and a heartbeat every 30s; if the container stops
before setup finishes, that's surfaced immediately rather than waited out.

## Egress logs

Blocked outbound connections are logged to flat files inside the box — the first place to
check when something can't reach an external service.

| Log file | Contents |
|---|---|
| `/var/log/iptables/iptables.log` | Every dropped packet: source/destination IP, port, protocol, UID |
| `/var/log/ulogd.log` | ulogd2 daemon startup and error messages |
| `/var/log/stx/tinyproxy.log` | tinyproxy CONNECT decisions (trusted zone only) |

| Prefix | Meaning |
|---|---|
| `[STX-DENY]` | Matched the deny-host-set (Tailscale CGNAT range, link-local addresses, or the host gateway) — blocked in every zone |
| `[STX-DROP]` | Matched the zone's catch-all drop — blocked by the egress policy itself |

Each entry carries `UID=`/`GID=`. The box's non-root user is the application side; UID 0 is
root (tinyproxy itself, whose own packets are allowed through).

## Branches

A sandbox's worktree always checks out its own local branch, `stx/<slug>` — never the
branch it actually targets (the auto-derived `feat/<user>/<slug>`, or `--branch`). Tracking
config (`branch.stx/<slug>.{remote,merge}`) maps the local branch onto the real target for
push purposes; `git branch --show-current` inside the box shows `stx/<slug>`, not the
target. `stx new` also stamps `branch.stx/<slug>.stx = true` alongside those two keys,
marking the config as its own — see Teardown guards below for why. The worktree itself is
created with `git worktree add --lock`, so a bare `git
worktree prune` run from anywhere in the shared `.git` — including from inside a box, where
every host path looks unresolvable and therefore "prunable" — can't unregister it; only `stx
rm`/`stx prune` remove it deliberately, and removing a locked worktree needs `--force` passed
twice (once for the lock, once for any uncommitted-changes guard).

`--base` picks the worktree's starting content explicitly when given; left unset, `stx new`
infers it from the target branch's own freshest state (its local ref, if not behind origin;
else origin; else `--base`'s own default — the remote's default branch (`origin/HEAD`),
falling back to the current branch's upstream). A slug reused after `stx rm` (no
`--rm-branch`) finds its old `stx/<slug>` still present — `stx new` fast-forwards it to the
inferred content when it carries no real divergence, otherwise prints what it found and asks
before discarding anything.

If `stx new` fails partway through, it rolls back everything it had already done — compose
teardown, forge token revocation, database drop, worktree removal, handoff-dir cleanup, and
the registry reservation — before returning the error, the same set of steps `stx rm`
performs on a complete sandbox. `new` never adopts pre-existing databases to roll back in
the first place — see Teardown guards below.

Inside a box, `push.default=upstream` makes a bare `git push` from `stx/<slug>` land on its
target despite the name mismatch.

A box can't delete or rename a branch, stash, or write git config (so no `push -u`):
git takes the shared `packed-refs` lock for every ref delete, and the top of the shared
`.git`, where that lock and the config live, is read-only in the box (see
[security](security.md)). Commit, fetch and push all work. Creating branches works only in
the two namespaces the box legitimately writes — its own `stx/<slug>` and `tmp/<slug>/*`
(the directories `refs/heads/stx/` and `refs/heads/tmp/<slug>/` are its only writable slices
of `refs/heads/`). A box can't write a branch outside them: it can't move `main` (or any other
operator branch), and it can't write another box's `tmp/<other>/*`. It *can* still move another
sandbox's `stx/*` branch, since `refs/heads/stx/` is shared across the repo's slugs. Branch
cleanup happens on the host, through `stx rm`, `stx prune` or plain git.

### Cross-branch work in a box

A box normally stays on its own `stx/<slug>` branch. Some tasks legitimately touch **other**
branches instead: a fix sweep applying one review's findings across many branches, a
multi-branch stack rebase, a docs commit to a spec branch. Provisioning a box per branch
would be out of proportion to that work. `tmp/<slug>/<task>` is the scratch namespace for it:
a local-only branch name, never pushed under its own name, never checked out outside this
one box's worktree.

It fits one box touching *many existing* branches in one session. One box producing new
commits for *one* target is ordinary `stx/<slug>` work and doesn't need it.

Five rules govern it:

1. **Namespace.** `tmp/<slug>/<task>`. The slug prefix makes cross-box collisions impossible
   and shows each branch's owner in `git branch --list 'tmp/<slug>/*'`.
2. **Birth.** `git fetch origin <target>`, then `git checkout -B tmp/<slug>/<task>
   origin/<target>`. Start from the fetched remote ref, never a local branch name: origin is
   the authoritative tip, and a local name may be checked out in a sibling worktree.
3. **Push.** By refspec only: `git push origin HEAD:<target>`. A `tmp/*` branch has no
   tracking config, so a bare push fails with "no upstream branch", and `push.default=upstream`
   can't help. On a non-fast-forward, fetch, rebase and retry. Use `--force-with-lease` only
   when the task explicitly calls for a restack, and only on the branch it names.

   *When* to push is the dispatcher's call, stated in the task. For a genuinely
   multi-branch sweep the default is to commit to `tmp/*` and stop, so the host can review
   every branch's change and land them in one batch. A few-branch or urgent task can say
   "push each branch as it's done" instead; the mechanics are the same.
4. **Death.** Before the dispatch ends, `git checkout stx/<slug>`. A box can't delete
   branches (see Branches above), so its `tmp/<slug>/*` branches stay until teardown sweeps
   them (below), or the host deletes them sooner. Recovery from an interrupted cross-branch task is to dispatch it again: its
   inputs are already on origin.
5. **`stx/<slug>` is never repurposed.** It only ever points at the box's assigned branch.

**Host side: keep local refs in sync.** A push from a box, by refspec or from `stx/<slug>`,
moves only `origin/<target>`, never a local ref on the host. Local feature branches lag
until re-pointed. A guarded fetch does it in one shot:

```
git fetch origin '+refs/heads/<pattern>:refs/heads/<pattern>'
```

The `+` allows non-fast-forward updates after a restack. Git refuses to fetch into a ref
checked out in any worktree, so the host's own active checkout is never moved, which makes
the fetch safe to run unconditionally. Run it at cycle-closing points (after a push batch or
a restack), not while tips are still moving.

**Teardown sweeps what's left.** `stx rm` and `stx prune` both list a slug's `tmp/<slug>/*`
branches and delete each one whose commits are all reachable from some remote. A branch with
commits on no remote is kept, with a recovery hint, unless `--force`: the sweep can't tell an
unlanded fix from one deliberately held for the host's batch.

## Teardown guards

`rm` and `prune` both refuse to destroy things they can't confirm are safe to lose, absent
`--force`:

- **The trusted box's git worktree.** Checked for uncommitted changes and commits on `HEAD`
  that no remote-tracking ref reaches (`git rev-list HEAD --not --remotes` — any remote, not
  just the box's own push target, so a worktree based on an already-pushed branch that never
  pushed its own doesn't false-positive). `rm` hard-errors on either; `prune` warns and leaves
  the worktree in place so the rest of teardown still completes for other zombies in the same
  batch. `rm --force` still prints the at-risk commits (short SHA and subject, up to 5, then
  "and N more") and a recovery hint before proceeding, since the registry entry — the other
  record of what was on it — is deleted moments later.
- **The handoff dirs** (`drop/<slug>`, `drop-dmz/<slug>`). Plain state directories, not git
  repos, so the worktree guard can't say anything about them. Any file present at all trips
  the guard — `rm` refuses, `prune` warns and skips. Whatever writes to these dirs is
  responsible for clearing them; a non-empty dir reads as "something hasn't finished with
  this yet."

`rm` without `--rm-branch` deliberately leaves a torn-down sandbox's local branch and its
tracking config behind for manual recovery. `prune` separately sweeps *orphaned* tracking
config — a `branch.stx/<slug>.*` section with no matching registry entry — and deletes it
only if the branch is itself safe to delete (no unmerged commits). This sweep only ever
touches config `stx new` marked as its own (`branch.stx/<slug>.stx = true`, written
alongside the `.remote`/`.merge` tracking keys); a `stx/<slug>` branch another tool set up
while sharing this repo carries no such marker and is never even considered, let alone
pruned. `prune` recovers a zombie sandbox's slug, Redis index, and repo root from container
labels (`stx.slug`, `stx.redis_index`, `stx.repo_root`) rather than the registry, since a
zombie is by definition a lost registry entry. Its databases come from the `stx.databases`
label the same container carries (comma-joined names, set at `new` alongside the others) —
never guessed. A container predating that label (or one whose manifest declared no
databases at all — the label is still present, just empty) has nothing to read: `prune`
lists whatever `stx_<slug>%` databases actually exist in Postgres, prints them, and leaves
them for a human to drop by hand rather than risk a false-positive guess. Before flushing a
zombie's recovered Redis index or dropping its database names, `prune` checks the live
registry under the lock and withholds any that a current entry now holds — `rm --force` on a
zombie whose containers survived frees that entry's index and database names, which a
brand-new sandbox can then claim, so flushing them off the stale labels would destroy the new
sandbox's data; the skip is reported. A zombie's forge token is never revoked, relying on its
own short expiry instead.

### Unparseable registry entries are zombies too

A registry entry that fails to parse — a hand edit, corruption, or a schema change like the
one that made `databases`/`forge_source` required fields — is invisible to every command
that reads `SandboxRecord`s: `ls` warns and skips it (now naming `stx prune` as the fix),
and `rm <slug>` refuses outright with the same pointer rather than a bare "no sandbox named"
error. `prune` is the one command that can still act on it, since it has the raw JSON to work
from: it tears the entry down exactly like a container-only zombie above (compose down,
network, databases — from the raw JSON's own `databases` list when present, else the same
list-and-leave fallback — Redis index, route file, worktree, with the same unpushed-work
guard), then deletes the raw entry outright. It is never rewritten or "repaired" to the
current schema. `--dry-run` lists each one alongside its own parse error. An unparseable
entry's container is excluded from `prune`'s separate container-only zombie scan — it's
already accounted for, just by a different path, so it isn't torn down twice.

### Databases and forge revocation come from the registry, never the manifest

`SandboxRecord.databases` (the rendered `platform.postgres.databases` names) and
`BoxRecord.forge_source` (kind/host/project — never a secret) are recorded at `new`'s
`reserve()` call, before any box exists — so a sandbox stuck mid-provisioning (a crash
between `reserve()` and `finish()`) still carries its own database names and forge source on
the registry entry. The forge token ids themselves are recorded the moment `new` mints them
(on the entry's `pending_forge`), before the box is built, so a crash between minting and
`finish()` leaves them tracked for `rm`/`prune` to revoke rather than leaked;
`finish()` clears `pending_forge` once the full box record (carrying the same ids) lands.
`refresh` reuses the same slot to hold an old credential set it couldn't revoke, so a later
`rm` retries it. `rm`'s database drop and forge-client rebuild (`forge_for_source`) both
read these off the registry entry exclusively; the checkout's `sandbox.yaml` is never
consulted for either, so a checkout that's switched branches, lost its manifest, or been
deleted entirely doesn't change what gets cleaned up. `refresh` keeps rebuilding its forge
client from the *current* manifest (that's correct — it's minting fresh credentials from the
current branch) but updates `forge_source` to match, so a later `rm` revokes against what
`refresh` actually built, not what `new` originally minted.

`rm` runs these checks as a **preflight**, before the first irreversible step (bringing the
containers down, removing the overlay): if a cleanup step can't even be attempted — secrets
unavailable, or no `forge_source` recorded to rebuild a client from — it warns naming what's
left behind and aborts with the entry and every resource fully intact (exits non-zero),
unless `--force`, which warns but removes the sandbox anyway. A forge revoke that is
attempted but fails is treated the same way: the entry is kept so the ids aren't lost and a
retry can revoke them, unless `--force`. A database drop or Redis flush that's attempted and
fails mid-attempt always keeps the registry entry regardless of `--force`, since a failure's
blast radius isn't known.

`stx new` refuses outright if any database it's about to create already exists — naming
them, and whichever registered sandbox's own `databases` still claims them (fix: `stx rm
<that slug>`), or, if none does, that they're orphaned and must be dropped by hand. There is
no flag to adopt them; a prior sandbox's data (including user accounts) must never carry
over into a fresh one silently. A Redis index has no equivalent problem — it's allocated
fresh per sandbox (`allocate_redis_index`, picking an index no live registry entry holds) and
always flushed before being freed on teardown.

## Routing

Each of a trusted box's own `routes` gets its own host port, allocated from `stx`'s pool
(5100–5599, lowest free) and recorded on the sandbox's own registry record so it survives
restarts and is freed on `rm`/`prune` — independent of [Tailnet publishing](#tailnet-publishing)
below, which allocates from a separate pool for one specific route only. The box publishes
each port on the host the same way; the Traefik route file's service points at
`http://host.docker.internal:<port>` rather than the box's own container, since Traefik joins
no sandbox network — see [security.md](security.md#isolation-between-sandboxes) for why.

Allocation skips not just ports another registry entry already holds but any port busy on
the host right now (a bind probe) — otherwise a port some unrelated process held would be
handed out and fail `compose up` on every retry. The tailnet pool below is probed the same
way. `refresh` reuses this to give a route added to the manifest since `new` its own port.

The host address a route's port binds to — never `0.0.0.0` — is chosen per platform:
loopback (`127.0.0.1`) on Docker Desktop (macOS, Windows), which forwards
`host.docker.internal` to the host's own loopback regardless of which Docker network
resolves it; the default bridge network's gateway (`docker0`) on Linux, which has no such
forwarding — a container's `host.docker.internal` there (via `extra_hosts: host-gateway`,
set on the Traefik service itself, not the box) resolves to that address whichever network
the container is on, so the port has to bind there to be reachable. A daemon configured with
its own `host-gateway-ip` breaks this.

## Tailnet publishing

A sandbox whose manifest sets `publish` gets a host port allocated from `stx`'s own pool
(5001–5099, lowest free) that publishes the named route directly — useful for reaching a
sandbox from a phone or another machine on the same Tailscale network, where MagicDNS
resolves only the machine's own name, not a per-slug subdomain. `routes` with no `publish`
still gets a Traefik route and a hosts entry, just no tailnet port.

The port binds to the host's own Tailscale address — never `0.0.0.0` — only while Tailscale
is detected running; otherwise it stays loopback-only. Without this, the port would
otherwise sit open on whatever network the host joins — LAN, hotel wifi — regardless of
whether Tailscale is even installed. `stx new` prints the reachable tailnet URL when
Tailscale is running; `stx ls` shows it for any already-provisioned sandbox.

## Hosts entries

`stx new` routes `Host(<route>.<slug>.localhost)` to a box's container, for every route the
manifest declares, through the platform's Traefik, which binds `127.0.0.1` only — plus one
extra router, `Host(<slug>.localhost)` (bare, no route prefix), for the manifest's *first*
route specifically (see [manifest.md](manifest.md#routes-optional)). Browsers resolve
`*.localhost` to `127.0.0.1` themselves, so every route works with no setup for a browser or
`stx open <slug> [route]`.

Non-browser clients (curl, editor REST clients) go through the OS resolver instead, which
typically only special-cases the bare `localhost` name — so `stx new` also writes one
hosts-file entry by default (skip with `--no-hosts`): `<slug>.localhost` plus every
`<route>.<slug>.localhost` the manifest declares, all on one line. A slug and a route name
are both single DNS labels (no dots), so the bare hostname (2 labels) and a route-prefixed
one (3 labels) can never collide regardless of what either is named. Manage the hosts entry
independently of sandbox lifecycle with `stx hosts add|remove <slug>` — `add` reads the
routes from the registry's own record of the sandbox (not the manifest, which may have
changed on the branch since); `remove` matches by hostname instead, so it doesn't need the
routes, and also cleans up a stale single-name `api.<slug>.localhost` line left by a prior
stx version. A sandbox with no routes gets no hosts entry, and `hosts add` says so. Both
`add` and `remove` require elevated privileges — the only step in `stx new` that does,
deliberately last so nothing earlier in provisioning needs root. A best-effort DNS cache
flush follows the write, to evict any prior negative resolution.

## Platform services on demand

`stx new` computes which shared platform services a manifest actually needs — Traefik
whenever `routes` is non-empty, Postgres/Redis/the OTel collector per the manifest's
`platform` block — and starts only those. The platform's own shared network (`stx-net`) is
always created — by `stx` itself (`docker network create`), raising on any failure other
than it already existing, before `compose up` ever runs; the platform compose file declares
it `external: true` so Compose doesn't also try to own it and warn that it didn't create it.
A bare manifest with no `platform` block starts no platform service at all. A
trusted box itself never joins `stx-net` — see
[Per-sandbox networks and links](#per-sandbox-networks-and-links) below for what it joins
instead.

`stx up` with no arguments starts every platform service; `stx up <service>...` starts a
named subset. Both `new` and `up` classify each requested service first — missing, stopped,
running and matching its own compose definition, or running and differing from it (its
`stx.definition_hash` label no longer matches, or its image id no longer matches the
locally pulled/built id for its configured tag) — and only run `compose up` for the
missing/stopped ones. A differing service is left running and reported with a warning
naming `stx up --recreate`; an already-running service found unhealthy is still an error.
This is what keeps bringing the platform up from silently recreating a service (and
dropping every live sandbox's Postgres/Redis connection) over something that didn't
actually need to change — see `stx up --recreate` above for the one path that does
recreate on purpose, and `stx status` for a standalone drift report.

`stx.definition_hash` is `stx`'s own label, not Compose's native config-hash: it's computed
by rendering the service with every platform secret (`STX_POSTGRES_PASSWORD`,
`STX_REDIS_PASSWORD`, `STX_REDIS_APP_PASSWORD`) bound to a fixed placeholder, folded
together with the content hash of whatever it bind-mounts (see below). That makes drift
detection mean the same thing whether or not the secret source happens to be loaded —
`stx status` and the classifier never read it. A secret value changing on its own is
*not* drift: rotating a platform password takes an explicit `stx up --recreate <service>`.
Postgres in particular only reads `STX_POSTGRES_PASSWORD` the first time its data volume
initializes — rotating the host secret alone doesn't change the running database's actual
password, so a recreate there doesn't help either; drop and reinitialize the volume, or
change the password inside Postgres directly. A container with no `stx.definition_hash`
label at all (stamped before this label existed) reports differing — correct, since one
recreate stamps it.

Starting or recreating a subset still has to resolve every service's variables: Compose
interpolates the whole `compose.platform.yaml`, not just the services named on the command
line. Every service outside the requested set has its own required (no-default) secrets
bound to a blank placeholder — the same way `stx rm`'s teardown does — so e.g. `stx up
--recreate postgres` doesn't warn about Redis's credentials being unset.

Recreating Traefik specifically is cheap: it holds no database connections, so `stx up
--recreate traefik` is always safe to run even with sandboxes active. `new` and `status`
both check the running `stx-traefik` container's `/routes` bind mount directly and name
this fix when it points somewhere other than stx's own routes dir (e.g. a container left
over from a different compose project) — the generic drift warning above doesn't say that
routes are actually broken, just that *something* differs.

The compose file (`compose.platform.yaml`) and the config/entrypoint files it bind-mounts
are package data, copied into `<state root>/platform/` (write-only-when-content-differs,
atomic) before every Compose invocation — see State layout below. Compose resolves a
bind mount's host path relative to the compose file's own directory, so this is what
makes that path stable across `stx` installs (an editable checkout vs. `uv tool`, a Python
minor upgrade) rather than drifting with wherever `stx` happens to be installed. Each
service's bind-mounted files' content is folded into its `stx.definition_hash` label (see
above), so a content-only change (no image or path change) still shows up as drift — a
bind mount is by path, not content, so Compose's own config render can't otherwise see it.

`stx down` keeps its own guard regardless: it refuses while any registered sandbox still
has a trusted box, unless `--force`, since stopping the platform out from under a running
box's database connection is disruptive.

The platform Postgres carries no repo-owned setup — it starts with only its admin superuser.
App roles and databases are created per sandbox by the repo's own `provision` steps, which
makes `platform.postgres.databases` the only thing `stx` itself creates or drops.

## Per-sandbox networks and links

Every sandbox with a trusted box gets its own Docker network, `stx-<slug>--net`, created at
`new` and removed at `rm`/`prune` — never the platform's shared `stx-net` (see
[security.md](security.md#isolation-between-sandboxes) for why). `stx` connects each
platform service the sandbox actually uses (Postgres, Redis, OTel) onto that network with
`docker network connect`; container-name DNS (e.g. `stx-postgres`) keeps working unchanged.
Bringing the platform up, or recreating a service with `stx up --recreate`, reconnects it to
every registered sandbox's network that needs it — a freshly (re)started container
otherwise has none. Traefik is always skipped here, even for a sandbox that declares
`routes`: it reaches a box's own route [through the host](#routing) instead, never by
joining the sandbox's own network.

`stx new --link <name>=<target>` (repeatable; see [manifest.md](manifest.md#links-optional))
additionally attaches the linking sandbox's box to `<target>`'s network, so it can reach
`<target>`'s box directly, and adds `<target>`'s container name to the linking box's
`NO_PROXY`. A link is fixed for the sandbox's life — changing one means a new sandbox.
`stx rm <target>` refuses while another sandbox links to it, naming them; `--force`
disconnects the linkers' boxes from `<target>`'s network before removing it. `stx prune`
removes a zombie's own network unconditionally; one still holding another sandbox's dangling
link is left in place with a warning, never a crash.

## Doctor

`stx doctor` never mutates anything; it only reports. Beyond Compose version, user config,
and dotenv permissions (above), it checks the shared `.git/config` for a short list of keys
that have no per-box override (`core.worktree`, `core.checkstat`) — unlike identity, GPG
signing, and push defaults, which every box forces via its own environment regardless of
what sits in that file, a local value on one of these corrupts every worktree sharing it,
not just one. It also reports the git identity (name/email, and which config file supplied
each) that a commit from the current checkout would carry, since every sandbox box inherits
it — flagging anything that looks like a leftover placeholder (`test@test.com`, `your.name`,
and similar) rather than a real operator.

Inside a repo with no `sandbox.yaml` at all, the image-requirements check is skipped
quietly. A `sandbox.yaml` that exists but fails to load (a schema error, a bad YAML
reference) is a different case — its load error is printed and `doctor` fails, rather than
being folded into the same quiet skip.
