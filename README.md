# stx

`stx` gives a coding agent (or a human) a disposable sandbox to work in: its own git
worktree, its own databases, and network access limited to a short list of hosts you
name. The point is to let an agent run unattended — editing code, running commands,
installing packages — without being able to reach your secrets or the open internet
except where you've explicitly allowed it.

Each sandbox is one or two boxes:

- a **trusted** box holds dev-tier credentials (scoped, short-lived, worthless if
  stolen) behind a default-deny egress allowlist — it can only talk to the hosts your
  manifest names.
- an optional **dmz** box has open internet access but no secrets beyond a dedicated,
  spend-capped API key, for research or open-web tasks that shouldn't touch the
  trusted box at all.

See [docs/security.md](docs/security.md) for the full trust model and what's left to
you as the repo author.

**Glossary:** a **box** is one container in a sandbox; the **trusted** box holds scoped
credentials behind an egress allowlist, the optional **dmz** box has open internet but no
secrets; a **forge** is the git host (GitLab today) `stx` mints short-lived push/package
tokens against; the **platform** is the shared Postgres/Redis/Traefik/OTel services
sandboxes attach to on demand.

## Requirements

- macOS or Linux
- Docker Desktop, or Docker Engine with Compose ≥ 2.20
- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/)

## Install

```bash
uv tool install git+https://github.com/socrait/stx
```

## Before you start

- You're in a **git repository with at least one commit** — `stx` provisions a worktree,
  which needs a base commit to branch from.
- **Docker is running** (Desktop, or Engine with Compose ≥ 2.20) — `stx` builds images and
  starts containers on your own daemon.
- The **first `stx new` builds the box image** from your Dockerfile and is slow (minutes);
  later runs reuse the cached image.
- **macOS and Linux** are supported. On Windows, run `stx` inside **WSL2** and treat it as
  the Linux case; native Windows isn't supported.
- Provisioning a box needs a **harness credential** in your secret source (see
  [Secrets](#secrets)) — run `stx doctor` to check your local setup.

Run `stx init` in the repo first to drop a starter `sandbox.yaml`, then edit it to taste.

## Smallest working example

`stx` needs three things in a repo: a Dockerfile meeting the [box
contract](docs/box-contract.md)'s image requirements, a `compose.yaml` that builds it,
and a `sandbox.yaml` manifest. A minimal set, for a repo with nothing else going on:

`Dockerfile`:

```dockerfile
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git iproute2 iptables jq tinyproxy ulogd2 \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /bin/bash dev
RUN su -s /bin/bash -c "curl -fsSL https://claude.ai/install.sh | bash" dev
ENV PATH="/home/dev/.local/bin:$PATH"

WORKDIR /workspace
USER dev
```

`compose.yaml`:

```yaml
services:
  app:
    build:
      context: .
```

`sandbox.yaml`:

```yaml
schema: 1
```

That's a valid manifest — every field defaults from the repo itself. From the repo root:

```bash
stx new demo       # provision a sandbox named "demo"
stx shell demo     # work inside it
stx rm demo        # tear it down
```

Or dispatch a headless coding-agent run into it instead of working by hand — the supported
harness is **Claude Code**:

```bash
stx agent demo "run the test suite and fix any failures"   # dispatch a headless run
stx await demo                                             # block until it finishes, print the result
```

"Ready" means `stx new` printed `✓ sandbox 'demo' ready` — it waited for the box to
boot, egress to come up, and any `setup` scripts to finish before saying so. A real repo
will want more than this: ports to route, a database, an allowlisted host or two — see
[docs/manifest.md](docs/manifest.md) for the full schema, and
[examples/widgetshop](examples/widgetshop) for a worked example with Postgres, Redis,
and a forge token.

## Secrets

Provisioning a box with `stx new` needs a **harness credential** so the box's Claude Code
agent can authenticate: either `STX_CLAUDE_OAUTH_TOKEN` (from `claude setup-token`, using
your Claude.ai subscription — the default), or an `ANTHROPIC_API_KEY` with `stx new
--api-key`. That's the only secret a bare box needs. Platform admin passwords
(Postgres/Redis) are needed only if your manifest declares a `platform` block, and a forge
minter token only if it declares `forge`. These go in `~/.config/stx/secrets.env`, never in
the repo or in `sandbox.yaml` — see [docs/secrets.md](docs/secrets.md) for the full table
and how `stx` loads them.

## Commands

| Command | Does |
|---|---|
| `stx init` | Write a starter `sandbox.yaml` in the current checkout (run this first) |
| `stx new <slug>` | Provision a sandbox: worktree, box(es), platform services, routes. `--replace` tears down an existing sandbox of the same slug first |
| `stx shell <slug>` | Open a shell in a box (`-b`/`--box` picks which), or run one command |
| `stx start <slug>` | Start a stopped sandbox's containers back up (e.g. after a host reboot) |
| `stx code <slug>` | Open the sandbox's worktree in an IDE |
| `stx agent <slug> "<task>"` | Dispatch a headless coding-agent run (`-b`/`--box` picks the box; `--task-file PATH` reads the task from a file) |
| `stx await <slug>` | Block on a dispatched run and print its result (`-b`/`--box`, `--timeout SECONDS`) |
| `stx logs <slug>` | Inspect dispatch history and captured output (`-b`/`--box`) |
| `stx status` | Report shared platform service health |
| `stx ls` | List sandboxes |
| `stx open <slug> [route]` | Open one of a sandbox's routes in a browser |
| `stx hosts add\|remove <slug>` | Manage `/etc/hosts` entries for a sandbox's routes |
| `stx refresh <slug>` | Re-mint credentials and recreate a sandbox's trusted box |
| `stx rebuild` | Rebuild the current repo's box image |
| `stx rm <slug>` | Tear down a sandbox |
| `stx prune` | Clean up zombie sandboxes and orphaned branches |
| `stx up` / `stx down` | Start or stop shared platform services |
| `stx config get\|set\|unset\|list` | Manage per-human settings |
| `stx doctor` | Check the local setup |
| `stx guide` | Print the host skill guide standalone (no repo, no plugin) |

See [docs/reference.md](docs/reference.md) for flags and full behavior.

## Limitations

- **GitLab is the only forge `stx` talks to today.** A manifest's `forge:` block mints
  a scoped push token (and, optionally, a package-registry credential) against a
  GitLab project. Other forges aren't supported yet.
- **`forge` is optional.** Without it, a sandbox's box still works — you just get no
  minted push token and no git remote rewriting; pushing from inside a box uses
  whatever git credentials you've set up yourself.
- **Tailscale publishing is optional.** A manifest can set `publish` on a route to
  expose it on your tailnet (useful for reaching a sandbox from a phone or another
  machine); without Tailscale installed and running, `stx` just skips it and the route
  stays reachable on `localhost` only.

## Docs

- [Reference](docs/reference.md) — commands and flags, user config, state layout, dispatch,
  egress logs, teardown guards, tailnet publishing, hosts entries, on-demand platform services.
- [Manifest](docs/manifest.md) — the `sandbox.yaml` schema, field by field.
- [Box contract](docs/box-contract.md) — what a repo supplies vs. what `stx` enforces.
- [Secrets](docs/secrets.md) — where host secrets come from, what the repo compose file can
  pull in, what never reaches a box.
- [Security](docs/security.md) — the trust model, and what's left to you.

## Agent integration

The `stx` Claude Code plugin (`claude plugin marketplace add socrait/stx && claude
plugin install stx@stx`) installs a skill that teaches a host-side agent to drive `stx`
itself: provisioning a sandbox, dispatching a headless run into it, reading back
results, and tearing it down safely. If you'd rather not install the plugin, `stx
guide` prints the same skill as plain text — no repo checkout or Claude Code plugin
required — so you can paste it into whatever you're using instead.
