---
name: stx
description: Drive stx, the sandbox dev/agent box CLI — provision a sandbox, dispatch a headless harness into it, read back its results, and tear it down. Use when a task needs an isolated dev box (own worktree, own DBs, egress-limited), when dispatching an agent into one, when checking on or reading a dispatch's output, or when cleaning up a sandbox that's done.
---

# Driving stx

`stx` provisions disposable dev/agent boxes: a trusted box (secrets behind an
egress allowlist) and an optional dmz sidecar (open egress, no secrets beyond a
spend-capped key). This skill covers the host-side agent's job: provision, dispatch,
read results, tear down, and the safety rules around all four. Command reference,
flags, the manifest schema, and the security model live in the repo's own docs —
this skill doesn't restate them: [README.md](../../README.md),
[docs/reference.md](../../docs/reference.md), [docs/manifest.md](../../docs/manifest.md),
[docs/box-contract.md](../../docs/box-contract.md), [docs/secrets.md](../../docs/secrets.md),
[docs/security.md](../../docs/security.md). Printed standalone (no repo, no Claude Code
plugin) by `stx guide`. Inside a box, the zone's own short guide is pre-installed at
`~/.claude/CLAUDE.md` and always readable at `/run/stx/AGENTS.md`.

## Lifecycle

1. `stx new <slug>` from inside a repo with a `sandbox.yaml` — add `--with-dmz` for a
   research sidecar alongside the trusted box. A bare `--repo none --with-dmz` works
   with no repo at all.
2. `stx shell <slug>` for an interactive session, or dispatch a headless agent (below).
3. `stx ls` / `stx status` to see what's live; `stx logs <slug>` to read a dispatch.
4. `stx rm <slug>` when done. `stx prune` sweeps zombies (containers whose registry
   entry was lost) and orphaned branch config — not a substitute for `rm` on a sandbox
   you still know the slug of.

## Dispatch practice

- **Write non-trivial tasks to a file, don't inline them.** `stx agent --task-file
  <path> <slug> [-b box]` reads the task from a host-side path and refuses a missing or
  empty file — write the brief with your own file tool first, then pass its path. This
  replaces the older "write a file, then dispatch a one-line pointer telling the agent
  to go read it" workaround — the file's content *is* the task now, nothing has to ask
  the dispatched agent to go find it.
- **`--wait` blocks; don't use it for long tasks.** Dispatch without `--wait`, then
  either `stx await <slug> [-b box]` (foreground, blocks until done — fine as a background
  Bash call for a long task) or `stx logs <slug> --raw | tail -c N` to peek without
  blocking. `stx logs <slug> --list` right after dispatch confirms a `working` row
  exists before you walk away from it.
- **`--dry-run` does not stop `stx agent` from dispatching.** It's accepted (every
  command takes it) but the agent command never reads it — a "dry run" still starts a
  real, billed harness run. Don't use it to probe; there's no dry-run mode for dispatch.
- **Large tasks: the dispatched agent does the work itself.** No handing off to a
  background subagent and ending its own turn — a headless run exits the moment its
  turn ends, and an orphaned background subagent's uncommitted work dies with it. Say
  so explicitly in the task file for anything substantial: do the work directly,
  foreground only, commit and push after each green step.
- **A dispatched agent can't launch a nested `claude -p`.** The harness scrubs its own
  auth token from tool subprocess environments, so a nested headless call fails "not
  logged in." Have the dispatch do setup and write a script, then run that script from
  the host via `stx shell <slug> --detach -- bash <script>` (that shell carries the
  token).
- **`stx shell --detach`'s log file is named after the command's first token**, not a
  fixed name — `VAR=1 bash x.sh` logs to `/logs/VAR-1.log`, not `bash.log` or `x.log`.
  Put env assignments inside the script instead of prefixing the command, and read the
  printed log path rather than assuming one.
- **`.devcontainer/` edits need a human's approval a headless run can't get.** Have the
  dispatch stage the full file contents plus a diff in its drop dir; the host reviews
  and applies them from there — never have the dispatch commit under `.devcontainer/`
  directly.

## Safety rules

- **Confirm with the user before `stx rm` or `stx prune`** — both are destructive
  (worktree, DBs, Redis index, branches) and `prune` acts across every sandbox on the
  repo, not just one slug.
- **Never run `stx up` just to check things are working.** It can recreate the shared
  platform containers even when nothing you changed should affect them, dropping every
  other live sandbox's DB/Redis connections for the few seconds the recreate takes. Use
  `stx status` or `stx ls` to check health — both are read-only.
- **Refresh a sandbox's forge tokens before they lapse.** They're short-lived
  (2-day write, 7-day read-only) by design; `stx refresh <slug>` re-mints and swaps them
  in without losing the worktree. `stx ls`/`stx guide` warn when any sandbox's tokens expire
  within 24h — don't wait for that warning to check on a long-running sandbox.
- **Push explicit refspecs, never a bare `git push`, from inside a box.** A sandbox's
  local branch is never the branch it targets (tracking config points elsewhere) — `git
  push origin HEAD:refs/heads/<branch>` says exactly where a dispatch's work lands; a
  bare push risks landing on whatever the ambient tracking config happens to resolve to.
- **Unset `GIT_DIR`/`GIT_WORK_TREE`/`GIT_CONFIG_*` before running git against any repo
  other than the one already mounted in the box.** Those vars point at the box's own
  worktree and are inherited by anything that execs git — left set, a git command
  against a second checkout silently operates on the *first* one instead of erroring.
- **Never run `git worktree prune/add/remove/repair` inside a box.** Every sandbox
  worktree is created locked specifically so a stray `prune` can't sweep it, but a
  direct `remove`/`repair` bypasses the lock entirely — `stx rm`/`stx prune` own
  worktree teardown; nothing else should touch it.
- **Never stash inside a box.** `git stash` is repository-wide, not per-worktree — one
  box's stash lands on the same stack as every other sandbox's and the host's.
- **Clear a run's handoff dirs before tearing down.** `stx rm`/`stx prune` refuse on a
  non-empty `/drop` or `/drop-dmz` without `--force` — commit or delete anything still
  sitting there as part of finishing the run, not as a workaround to removal.

## Recovery

- **After a host reboot**: the shared platform (Postgres, Redis, Traefik, the OTLP
  collector) restarts on its own; every box exits. `stx ls`/`stx status` to see what's
  down, then `docker start stx-<slug>--<box>` per box — container state (in-box `/tmp`,
  brief files) survives; anything that was only running as a foreground process inside
  the box does not and needs restarting. Never `stx up` to probe.
- **A box that exits again right after `docker start` with a 401** on its package
  registry has an expired token — `stx refresh <slug>`.
- **A dependency baked into the shared image *is* picked up by `refresh`** — it runs the
  same staleness check `stx new` does and rebuilds first if a `rebuild_on` path's hash
  changed. `stx rebuild` is for forcing a rebuild directly when nothing is about to call
  `new` or `refresh` on its own.
- **A stuck/zombie sandbox** (its registry entry is gone but containers or worktree are
  still live) is what `stx prune` discovers from container labels — unsaved worktree
  work or a non-empty handoff dir still blocks it without `--force`, same as `rm`.
- **Ephemeral scratch state outside a sandbox's own registered dirs doesn't survive a
  reboot.** Push or otherwise record anything worth keeping before ending a session,
  rather than assuming a running box is durable storage.
