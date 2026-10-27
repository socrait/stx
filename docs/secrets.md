# Secrets

A host secret is anything `stx` needs that must never sit in the repo or in the manifest:
platform admin credentials, forge minter tokens, harness auth, and an app's own runtime
credentials. This is the person's secret material, not the repo's — where it comes from is
user config, never `sandbox.yaml`.

## What secrets `stx` needs

`stx` loads only the secrets a given verb actually needs (see [Source](#source) below). The
one every `stx new`/`stx refresh` of a sandbox *with an app box* needs is a harness
credential — even a bare box (no platform, no forge, no dmz) can't be provisioned without
one, because the box runs a Claude harness. Everything else is gated on the manifest: a
`platform` block pulls in the platform passwords, a `forge` block pulls in the minter token,
`--with-dmz` pulls in the dmz key.

| Secret | Required when | How to get it |
|---|---|---|
| `STX_CLAUDE_OAUTH_TOKEN` | `new`/`refresh` of an app box in the default `oauth` auth mode | `claude setup-token` — mints a token against your Claude.ai subscription |
| `ANTHROPIC_API_KEY` | `new`/`refresh` of an app box run with `--api-key` instead | an API key from the [Anthropic Console](https://console.anthropic.com/) |
| `STX_DMZ_ANTHROPIC_API_KEY` | any sandbox provisioned `--with-dmz` | a dedicated, spend-capped Anthropic API key — never the trusted box's own |
| `STX_GITLAB_MINTER_TOKEN` | the manifest declares a `forge:` block | a GitLab personal access token with the `api` scope, owned by a user who is at least **Maintainer** on `forge.project` (to mint project access tokens) and **Owner** on `forge.package_registry.group` when one is set (to mint group deploy tokens). Scope it no wider than those; it never enters a box — `stx` uses it host-side only, to mint and revoke the box's own short-lived tokens |
| `STX_POSTGRES_PASSWORD` | the manifest declares `platform.postgres` (or you run `stx up postgres`) | an admin password you choose for the shared platform Postgres; set once, reused by every sandbox |
| `STX_REDIS_PASSWORD` | the manifest declares `platform.redis` | an admin password you choose for the shared platform Redis |
| `STX_REDIS_APP_PASSWORD` | the manifest declares `platform.redis` | the app-tier Redis password handed to boxes (distinct from the admin one above) |

## Source

| Key | Values | Default |
|---|---|---|
| `secrets.source` | `dotenv` \| `op` | `dotenv` |
| `secrets.dotenv.path` | file path | `$XDG_CONFIG_HOME/stx/secrets.env` |
| `secrets.op.environment` | a 1Password Environment id | — (required when `secrets.source=op`) |

Set with `stx config set <key> <value>` (see [reference.md](reference.md#user-config)).

**`dotenv`** parses the file as `KEY=VALUE` lines and sets only the keys not already present
in the calling process's own environment — an explicit host env var always wins over the
file. `#` starts a comment at the beginning of a line or after whitespace, never mid-token
(so a value that's itself a URL fragment or hex color isn't truncated); `export KEY=VALUE` is
accepted; a value may be single- or double-quoted, and an unterminated quote is an error
naming the file and line. A missing file behaves like an empty one — not an error by itself,
though a *required* key still missing afterward is. An empty value (`KEY=`) counts as
missing, the same as the key being absent entirely. `PATH`, `HOME`, `SHELL`, `IFS`, and
anything prefixed `LD_`, `DYLD_`, or `PYTHON` are never loaded from the file even if present
— a secrets file may carry credentials, not process-control variables — and a line setting
one of these is skipped with a warning.

**`op`** re-execs the current process under `op run --environment <id> -- ...` so 1Password
injects secrets directly into the re-exec'd process's real environment; `stx`'s own code
never reads, holds, or parses a secret value in this path. Requires the `op` CLI on `PATH`.
`op run` passes the operator's whole shell environment through to the child, so before the
re-exec `stx` strips every name it is about to request (required and optional alike) from the
child's environment — after it, whatever is present under one of those names came from
1Password, never the operator's shell. The requested names are known before the re-exec:
they are the harness/forge/platform secrets plus every variable the repo's own compose file
references, all computed from the manifest and compose file first.

A verb loads only the secrets it actually needs for what it's about to do — provisioning an
app box always needs its harness credential (see [What secrets `stx` needs](#what-secrets-stx-needs)
above), but a bare box needs nothing beyond that; read-only verbs (`ls`, `status`, `logs`,
`await`) load nothing at all. A load failure
names every missing key at once, not just the first, so fixing it doesn't take one `stx`
re-run per key. `stx doctor` reports which user-config keys are set and whether the dotenv
file is mode `600` or stricter — report-only; no verb refuses to proceed on a world-readable
dotenv file today.

## What reaches a box

`stx` never runs `docker compose` with its own full process environment. It builds a
fresh one holding exactly: `PATH`, `HOME`, and the Docker client variables; the tool
variables the overlay itself references (paths, slug, template values); the harness and
forge secrets for this one box; and every `${NAME}` the repo's own compose file actually
references, resolved **only** from the loaded secret source — a dotenv file's parsed
mapping, or for `op` the exact names `stx` asked it to load — never this process's own
ambient environment. `stx` scans the repo's normalized, uninterpolated compose config for
those references itself — the manifest carries no list of its own: a `${NAME}` with no
default is required and an error naming it when unset in the secret source; a
`${NAME:-…}`/`${NAME-…}` is optional, included under its bare name when set and non-empty,
left out of the box environment otherwise. Nested interpolation (`${A:-${B}}`) is refused
at the scan, since `stx` resolves each name once and could not expand a reference buried
in a default. A reference to one of `stx`'s own credentials
(`STX_GITLAB_MINTER_TOKEN`, `STX_CLAUDE_OAUTH_TOKEN`, `STX_DMZ_ANTHROPIC_API_KEY`,
`STX_POSTGRES_PASSWORD`, `STX_REDIS_PASSWORD`) is refused, naming it — so the repo's
compose file cannot pull one of those, or a real variable from the operator's shell that
the secret source doesn't define, into the box. A reference to a name the trimmed
environment itself carries — `PATH`, `HOME`, or any `DOCKER_*` (exactly what `stx` passes
`docker compose` as its floor) — is likewise refused, naming it: an optional such reference
(`${HOME:-}`, `${DOCKER_AUTH_CONFIG:-}`) is dropped from the resolved secret values but would
still interpolate from that floor, i.e. from the operator's shell, so the repo compose file
may not reference one at all. `stx`'s own platform compose file is exempt — it is not
repo-authored and references only `TRAEFIK_ROUTES_PATH` and `STX_*`.

The generated overlay file (`state/compose/<slug>.json`, see
[reference.md](reference.md#state-layout-xdg)) holds no secret values either way — it is
still written owner-only (`0600`, directory `0700`) and deleted by `rm`, `prune`, and a
failed `new`'s rollback, as a second layer under the trimmed-environment approach above.
Teardown (`rm`, `prune`) binds every name either the repo compose file or the rendered
overlay references to `""` — `compose down` must resolve each name to avoid an
unset-variable warning, but never uses its value, so teardown needs no secrets loaded at
all.

### Harness auth

Exactly one of two variables ever reaches the app box's environment, never both — `stx`
decides which at the overlay level, not by relying on compose interpolation to drop the
unused one:

| Mode | Host secret | Box variable |
|---|---|---|
| `oauth` (default) | `STX_CLAUDE_OAUTH_TOKEN` | `CLAUDE_CODE_OAUTH_TOKEN` |
| `api_key` | `ANTHROPIC_API_KEY` | `ANTHROPIC_API_KEY` |

A dmz box always gets `ANTHROPIC_API_KEY`, sourced from the separate `STX_DMZ_ANTHROPIC_API_KEY`
— a dedicated, spend-capped key, never the trusted box's own credential.

### Forge credentials

A manifest with `forge:` gets `STX_FORGE_TOKEN`/`STX_FORGE_HOST`/
`STX_FORGE_GIT_USER` in the box environment, from the push credential `stx new`/`stx
refresh` minted (see [box-contract.md](box-contract.md)). `forge.package_registry.env`
names the two variables (username, password) a minted package-registry credential writes
to, if the manifest declares one. The two credentials are minted as a pair: if the
package-registry mint fails after the push credential already succeeded, `stx` revokes that
push credential before reporting the error, rather than leaving a sandbox half-provisioned
with one live credential and no record of the other half ever having existed.

### Provisioning credentials

Each `provision.steps` run gets a Postgres connection directly from `stx` — `PGHOST`,
`PGPORT`, `PGUSER`, `PGPASSWORD` (libpq's own names) — never from the host secret source,
since no secret source sets the platform's own admin credentials; a manifest referencing one
of these via `provision.env`'s `${NAME}` needs nothing loaded for it. These bind to
`stx_provisioner`, a per-platform role `stx` itself creates (idempotent, on `stx up`/`new`,
alongside the admin setup) with `CREATEDB CREATEROLE` only — never the platform admin
(superuser). Postgres 16 restricts `CREATEROLE` to administering only roles the grantee
itself created, so a repo's provisioning step can create and configure its own app roles and
databases but can't reach the admin role or any other sandbox's. Its password is derived
deterministically from the admin's own and never stored or transmitted separately.

Any other `${NAME}` in `provision.env` resolves only from a host variable the repo's own
compose file already references (scanned the same way as the box's own `env` — see "What
reaches a box" above) — never this process's own full environment, so a manifest can't smuggle
in a host secret just by naming it in `provision.env` with nothing else referencing it. A
`${NAME}` naming one of `stx`'s own reserved names (`STX_`/`SANDBOX_`/`GIT_`-prefixed, a proxy
variable, or one of `stx`'s own credential names) is refused outright, at manifest load.

## 1Password requirement

`op run --environment` needs a current-enough `op` CLI to support 1Password Environments;
check `op`'s own release notes for the minimum version before relying on `secrets.source=op`.
