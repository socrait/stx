# Manifest

A repo's `sandbox.yaml`, at its root, states only the overrides a generic default gets
wrong for this repo — every key is optional unless noted, and defaults from the repo
itself (the compose file, the Dockerfile, git) wherever one exists. A repo with a
manifest and a compliant `compose.yaml`/Dockerfile (see [box contract](box-contract.md))
is all `stx` needs to sandbox it.

Fields below, in schema order, then one complete manifest for a small fictional repo,
`examples/widgetshop`: an app on port 8080, backed by Postgres, Redis, and the OTel
collector, allowed to reach PyPI.

## Format

YAML, parsed with a safe loader. `schema: 1` is required at the top; `stx` refuses any other
value. Unknown top-level or nested keys are errors — a typo in a key name fails loudly rather
than being silently ignored. There is no `box:` section: every key is top-level.

## Generating a starter manifest

`stx init` writes a `sandbox.yaml` inferred from what's already in the repo, rather than a
fixed template — every inferred key still wants a human read before `stx new` runs for real:

- **`compose`**: searched for by Compose's own lookup order, tried at the repo root first,
  then `.devcontainer/` and `docker/`. Only written when the match isn't at the default root
  location — the common case needs no `compose:` key at all.
- **Allow-list**: whatever compose file `init` found is checked against the same allowed-key
  sets `new` enforces for real (see [box contract](box-contract.md#the-repo-compose-allow-list)),
  without needing Docker — a best-effort approximation, good enough to catch the common cases
  early. A violating file never gets written as `compose:`; `init` instead lists every refused
  key and suggests a minimal build-and-environment-only file to place next to it.
- **Dockerfile**: resolved from the compose file's own `build` block. No `USER` instruction, or
  `USER root`, gets a note; the image requirements ([box contract](box-contract.md#image-requirements))
  always print as one line regardless. A Dockerfile with more than one `FROM` gets a suggestion
  to add a dedicated build stage for the box's own tools rather than reusing one of the app's.
- **Ecosystem egress hints**: `package.json` implies `registry.npmjs.org`; `uv.lock` or
  `pyproject.toml` implies `pypi.org`/`files.pythonhosted.org`. Written as real `egress.hosts`
  entries, not comments.
- **Private package registry**: if `.npmrc` or `pyproject.toml` names a GitLab registry
  alongside a `${VAR}` token, `init` writes `forge.package_registry` with that variable as
  `password` (see [`forge`](#forge-optional) below) and a comment that `setup` must install
  dependencies itself — the token only exists inside the box at runtime, never at image build
  time.
- **Routes**: a port from the Dockerfile's `EXPOSE`, or the compose file's own `ports:`, becomes
  a commented `routes:` suggestion — commented because publishing a route is a deliberate
  choice `init` shouldn't make silently.
- Nothing `init` can't infer gets a stub. In particular, it never writes `branch:` — the
  default (`sandbox/{slug}`, below) already applies when the key is absent.

### Frontend repos

A Vite/React (or similar) repo's dependencies are usually npm-registry packages, installed by
`setup` rather than baked into the image — the private-registry token above only exists once
the box is running, so a `setup` script (`npm ci`) is what actually needs it, not the
Dockerfile's own build. The dev server itself is typically started by hand once the sandbox is
up, not as the box's `CMD`: `stx shell <slug> --detach -- npm run dev` backgrounds it and tees
its output to a log file. A package-cache volume (`node_modules`, or npm's own cache dir) is
one of the repo compose file's own named volumes either way (see the allow-list above); giving
it an explicit `name: stx-cache-<repo-id>--npm` (the repo id is the main checkout's directory
name) shares one cache across every sandbox of the repo instead
of one per slug, which is fine specifically because npm verifies package integrity on install
— a stale or cross-sandbox-shared cache can't silently serve the wrong code. An explicit
`name:` is only ever allowed with the repo's own `stx-cache-<repo-id>--` prefix (see the
allow-list above) — it
names a shared cache, never a specific platform or sandbox volume.

## Fields

| Key | Required | Default | Meaning |
|---|---|---|---|
| `name` | no | the compose file's sole service | The compose service name. Also the box's container suffix (`stx-<slug>--<name>`) and `shell`/`agent`/`code`'s default target. An override must match `[a-z0-9-]+` and may not be `dmz` (reserved for the generated sidecar). |
| `compose` | no | Compose's own lookup order (`compose.yaml`, `compose.yml`, `docker-compose.yaml`, `docker-compose.yml`) | Repo-relative path to the compose file defining this service. |
| `image` | no | `stx-<repo>-<hash>`, where `<repo>` is the main checkout's directory name, lowercased and sanitized to a valid tag component (the parent of the git common dir, so a sandbox's own linked worktree never changes it), and `<hash>` is 8 hex chars of the common git dir's own path — so two checkouts that happen to share a directory name get different tags instead of rebuilding over each other | Tag for the box image, shared across every sandbox of this repo. |
| `user` | no | the built image's own `USER` (resolved at `new`/`refresh`/`doctor` time, via `docker image inspect` — refused when that's root or unset) | The non-root user the box runs as, and setup scripts run as. Must exist in the image with a home directory. |
| `rebuild_on` | no | the build's Dockerfile plus every file its `COPY`/`ADD` instructions name (`--from=` stage copies, remote `ADD` URLs, and glob sources excluded — none resolve to a fixed path to hash) | List of additional repo-relative paths; `stx` rebuilds the image when any of the combined set's hashes changes since the last build. |
| `setup` | no | `[]` | List of repo-relative scripts, run in order inside the box (as the resolved `user`, from `/workspace`, in the trusted zone, after egress is up) when the sandbox is created. |

### `routes` (optional)

A map of route name → port inside the box, e.g. `api: 8080`. Names must be DNS labels; ports
1–65535. An empty or absent `routes` means no Traefik route and no hosts entry.

The first entry (YAML preserves key order) is this repo's *main* route: besides its own
`<route>.<slug>.localhost`, it's also served bare at `<slug>.localhost` — `stx open <slug>`
with no route and `stx new`'s own `open:`/`curl:` output both use the bare form. Every route,
first or not, stays reachable at its own prefixed hostname too; naming a route explicitly
(`stx open <slug> <route>`, or a link's `{link.<name>}`) always uses the prefixed form.

### `publish` (optional)

Names the one `routes` entry whose port is also bound directly on the tailnet (see
[reference.md](reference.md#tailnet-publishing)). Must name an existing `routes` key.
Omitted means nothing is published on the tailnet, even with `routes` set.

### `branch` (optional)

A template string for the branch a sandbox's worktree pushes to by default, e.g.
`"feat/{user}/{slug}"`. Defaults to `"sandbox/{slug}"`. `--branch` on `stx new` overrides it
outright.

### `platform` (optional)

Declares which shared platform services this repo's boxes need. Each omitted service never
starts for this sandbox.

| Key | Meaning |
|---|---|
| `postgres.databases` | List of database-name templates to create on `new` and drop on `rm`. Each entry must contain `{db}`. |
| `redis: true` | Allocate a Redis logical database index for this sandbox. |
| `otel: true` | Make the OTel collector available at `stx-otel:4318`. |

Traefik starts automatically whenever `routes` is non-empty — it has no key of its own here.

### `provision` (optional)

One-shot setup steps, run in order via `compose run --rm <name>` against the newly created
box — as the box user, never root (see
[box-contract.md](box-contract.md#the-runtimes-init-sequence)). `steps` and `env` are both
required to sit directly
under `provision` — a bare sequence of argv lists can't share a YAML mapping node with a
sibling `env:` key, so the step list always lives at `provision.steps`, never at `provision`
itself.

| Key | Meaning |
|---|---|
| `steps` | A list of steps, each a bare argv list, e.g. `[python, manage.py, migrate]`. Steps run in order; each must be idempotent, since every `stx new` runs them again, including on a reused slug whose databases survived `stx rm --keep-db`. |
| `env` | A map of environment variable name → value, reaching only the provision run — not the box's own long-lived environment. A value may reference `${NAME}`: either the provisioning Postgres connection `stx` itself sets in every provision run (`PGHOST`, `PGPORT`, `PGUSER`, `PGPASSWORD`, `PGDATABASE` — libpq's own names, bound to the non-superuser `stx_provisioner` role, never the platform admin; see [secrets.md](secrets.md#provisioning-credentials)) or a host environment variable the repo's own compose file already references, pulled after the secret source has loaded. A `${NAME}` naming one of `stx`'s own reserved names (`STX_`/`SANDBOX_`/`GIT_`-prefixed, or a proxy variable — see [box-contract.md](box-contract.md#box-environment)) is refused at manifest load. |

### `env` (optional)

A map rendered into the box service's own environment at every start, using the template
variables below. It overrides the platform connection variables `stx` sets itself (`PGHOST`,
`PGPORT`, `PGDATABASE` — see [box-contract.md](box-contract.md#platform-services)). Names starting `STX_`, `SANDBOX_` or `GIT_`, and the proxy variables, are
reserved and refused: see [box-contract.md](box-contract.md#box-environment).

### `egress` (optional)

| Key | Meaning |
|---|---|
| `hosts` | List of allowed hostnames. A plain hostname is escaped and matched exactly; a string starting `~` is taken as a regex verbatim. `stx` always adds the coding-agent harness's own hosts and, when `forge` is configured, the forge host — a repo never has to list them. |
| `ports` | List of `port/proto` tokens in Docker's own notation (e.g. `7881/tcp`, `*/udp`), allowed to bypass the HTTP(S) proxy entirely, for the box user only — for protocols that can't go through a CONNECT proxy. Defaults to none. `*/tcp` is refused: it would open every TCP port directly, defeating the allowlist proxy. `80/tcp` and `443/tcp` are refused too: either would let the box user reach any host directly on the open web, bypassing the `hosts` allowlist entirely — the same hole as `*/tcp`, just narrowed to the ports browsers and most clients actually use. A bare `*/udp` is allowed, for real-time media (WebRTC/ICE) that negotiates arbitrary ports — but it lets the box user send UDP to any host, not just the ones in `hosts`, which is a wider carve-out than "WebRTC media" on its face. Prefer a scoped `<port>/udp` token wherever the protocol permits it; see [security.md](security.md#residual-risks). |

An empty or absent `egress` means the box reaches nothing beyond the harness/forge hosts
`stx` always adds and whatever every zone allows by default.

### `forge` (optional)

| Key | Required | Default | Meaning |
|---|---|---|---|
| `kind` | no | inferred from the `origin` remote's host (`gitlab` when the host contains "gitlab") | Forge implementation. `gitlab` today. |
| `project` | no | the `origin` remote's path | The repo's project path on the forge. When an `origin` remote exists, an explicit value must equal its path exactly — refused otherwise, naming both (a mismatch would mint a token scoped to a different project than this repo; see [security.md](security.md#what-stx-enforces)). With no `origin` remote at all, this can't be defaulted or checked and must be set explicitly. |
| `package_registry.group` | no | `project`'s top-level path segment | Optional: a group to mint a read-only package-registry credential against. Group-scoped, not project-scoped — package managers that resolve against GitLab's PyPI-style endpoint generally need the group-level aggregated endpoint, which only a group-scoped credential can authenticate against. An explicit value must be an ancestor (sub)group of `project` — refused otherwise. |
| `package_registry.env` | yes (if `package_registry` given) | — | Map with `username`/`password` keys naming the environment variables that credential is written to in the box. |

A manifest with no `forge` key gets no forge credentials and no git remote rewriting — a
plain mounted worktree, pointed at whatever remote the host checkout already has configured.
With no `origin` remote at all, `kind`/`project` can't default and must be set explicitly.

### `links` (optional)

Declares every link name this repo accepts from `stx new --link <name>=<target>` (see
[security.md](security.md#isolation-between-sandboxes) for why a link is an explicit, named
exception rather than a free-form override):

| Key | Required | Default | Meaning |
|---|---|---|---|
| `route` | yes | — | The target's route name (one of the target's own `routes` keys) this link resolves to. Must be a DNS label. |
| `default` | no | `""` | What `{link.<name>}`/`{link.<name>.internal}` render when this sandbox wasn't actually linked (`--link <name>=...` omitted) — e.g. a mock-mode base URL. |

```yaml
links:
  api:
    route: api
    default: mock
```

A name not declared here is refused at `stx new --link`; a name declared here but never
linked renders `default` everywhere it's used.

## Template variables

Available in `env`, `provision.steps`, `platform.postgres.databases`, and `branch`:

| Variable | Resolves to |
|---|---|
| `{slug}` | The sandbox's slug |
| `{db}` | `stx_` + the slug with `-` replaced by `_` |
| `{redis_index}` | The sandbox's allocated Redis index. Using this without also setting `platform.redis: true` fails validation. |
| `{user}` | (`branch` only) the resolved branch username |
| `{link.<name>}` | The browser URL of link `<name>`'s target route, `http://<route>.<target>.localhost` — or `<name>`'s `default` if this sandbox wasn't linked under it. `<name>` must be declared in `links`, or validation refuses the template. |
| `{link.<name>.internal}` | The in-box URL of the same link, `http://<target box container>:<port>` — reachable only because linking also attaches this box to the target's network (see [security.md](security.md#isolation-between-sandboxes)). Same `default` fallback as the non-`.internal` form. |

### Grammar

Templates are **not** `str.format` — a small closed substituter instead, so a template can
only ever mean one of the variables above, nothing else. `{{` and `}}` are the only escapes
(literal `{`/`}`); every other `{...}` must be exactly one of the names in the table, with no
format spec and no attribute access beyond the one `.internal` suffix on a link — `{slug:>12}`,
`{slug.__class__.__name__}`, and `{link.api.foo}` are all refused, not silently
mis-interpreted. An unrecognized name (`{slgu}`), a stray unescaped `{` or `}`, or any of the
above is a load-time error naming the manifest key and the bad token — never a bare Python
`ValueError`/`AttributeError`, and never deferred until whatever command first happens to
render that key.

`${NAME}` inside `provision.env` values reads `stx`'s own provisioning vars (`PGHOST`,
`PGPORT`, `PGUSER`, `PGPASSWORD`, `PGDATABASE`) or the host process environment after the secret source has
loaded; nothing else in the manifest reads host environment variables. This is a separate,
unrelated grammar — `provision.env` values are not run through the `{...}` substituter above.

## Validation

Checked when the manifest loads: `schema` equals `1`; an overriding `name` matches
`[a-z0-9-]+` and isn't `dmz`; every path under `rebuild_on`, `setup`, and `compose` exists
under the repo root; `routes` ports are 1–65535 with DNS-label names; `publish` names a key in
`routes`; `egress.hosts` regexes compile; `egress.ports` tokens match `(\*|\d+)/(tcp|udp)` and
reject `*/tcp`; every `{...}` in `branch`, `env`, `platform.postgres.databases`, and
`provision.steps` matches the template grammar above; `{redis_index}` is only used when
`platform.redis: true`; every `platform.postgres.databases` entry contains `{db}`; a `links`
name matches `[a-z][a-z0-9_]*` (it must also work as a template attribute) and its `route` is a
DNS label; `{link.<name>}`/`{link.<name>.internal}` is only used for a name declared in
`links`; unknown keys anywhere are errors. Every error names the file, the key, and the
problem.

A `--link <name>=<target>` itself is validated at `stx new`, not at manifest load, since it
needs the target's registry record: the target must exist, have a trusted box, and declare
the route `links.<name>.route` names.

Resolving `user` from the image's own `USER` needs a built image, so it isn't checked at
manifest load — it happens at `new`/`refresh`/`doctor` time, after the image is confirmed
fresh. The repo's compose file is checked against the
[allow-list](box-contract.md) separately, at `new`/`refresh`, before either command changes
anything else — that check needs Docker too.

## Worked example: `examples/widgetshop`

A small fictional shop app (Flask, `uv`-managed). Its `compose.yaml` defines only the box
service, build and app environment, per the [box contract](box-contract.md):

```yaml
# examples/widgetshop/compose.yaml
services:
  app:
    build:
      context: .
      dockerfile: .devcontainer/Dockerfile
    environment:
      WIDGETSHOP_ENV: local
    volumes:
      - widgetshop-cache:/home/devuser/.cache

volumes:
  widgetshop-cache:
```

And its manifest (`image` and `forge` are set explicitly here only because this example lives
nested inside the `stx` repo itself, so its own default resolution — the parent of the git
common dir, the `origin` remote — would resolve against the wrong repo; a real standalone repo
needs neither):

```yaml
# examples/widgetshop/sandbox.yaml
schema: 1
image: widgetshop-devbox
rebuild_on: [uv.lock]
setup: [scripts/box-init.sh]
routes:
  api: 8080
publish: api
branch: "feat/{user}/{slug}"
platform:
  postgres:
    databases: ["{db}", "{db}_test"]
  redis: true
  otel: true
provision:
  steps:
    - [bash, scripts/box-provision.sh]
    - [python, -m, widgetshop.scripts.create, --database, "{db}"]
    - [python, -m, widgetshop.scripts.create, --database, "{db}_test"]
    - [python, -m, widgetshop.scripts.migrate, --database, "{db}"]
  env:
    DATABASE_USER: "${PGUSER}"
    DATABASE_PASSWORD: "${PGPASSWORD}"
env:
  DATABASE_DB: "{db}"
  DATABASE_TEST_DB: "{db}_test"
  REDIS_DB: "{redis_index}"
  OTLP_ENDPOINT: http://stx-otel:4318
  PUBLIC_BASE_URL: "http://api.{slug}.localhost"
egress:
  hosts:
    - pypi.org
    - files.pythonhosted.org
  ports: ["*/udp"]
forge:
  kind: gitlab
  project: widgetshop/widgetshop
  package_registry:
    env: {username: UV_INDEX_WIDGETSHOP_USERNAME, password: UV_INDEX_WIDGETSHOP_PASSWORD}
```

`stx new demo` against this repo: defaults `name` to `app` (the compose file's sole service);
creates `stx_demo` and `stx_demo_test` on the shared Postgres, allocates a Redis index and
renders it into `REDIS_DB`, starts Postgres, Redis, Traefik, and the OTel collector, runs the
four provisioning steps in order (role creation, two database creates, one migration) with
`DATABASE_USER`/`DATABASE_PASSWORD` bound from `stx`'s own platform-admin connection, renders
an egress allowlist with `pypi.org`/`files.pythonhosted.org` plus the harness hosts and
`gitlab.com` (the forge host — all three added automatically), a `*/udp` port carve-out,
routes `api.demo.localhost` to port 8080 and publishes it on the tailnet (since the
route is named `api` and `publish: api`), and mints a GitLab push token scoped to
`widgetshop/widgetshop` plus a read-only package-registry deploy token for the `widgetshop`
group (defaulted from the project's own top-level segment), written into
`UV_INDEX_WIDGETSHOP_USERNAME`/`UV_INDEX_WIDGETSHOP_PASSWORD`.

## Worked example: a frontend linked to `widgetshop`

A separate frontend repo (`widgetshop-web`) that talks to `widgetshop`'s API in dev. Its
manifest declares the one link it accepts and a mock-mode fallback for when it isn't linked:

```yaml
# widgetshop-web/sandbox.yaml
schema: 1
links:
  api:
    route: api
    default: mock
env:
  API_BASE_URL: "{link.api}"
  API_INTERNAL_URL: "{link.api.internal}"
```

`stx new web-1 --link api=demo` (against the `widgetshop` sandbox from the example above)
renders `API_BASE_URL=http://api.demo.localhost` (what `web-1`'s own browser-side code should
use) and `API_INTERNAL_URL=http://stx-demo--app:8080` (what code running inside `web-1`'s own
box — a build step, an in-box curl check — should use instead, since `*.localhost` always
resolves to loopback even inside a container). It also attaches `web-1`'s box to `demo`'s own
network, so either URL is actually reachable. `stx new web-2` with no `--link` leaves
`API_BASE_URL`/`API_INTERNAL_URL` both at `mock` — `web-2` never touches `demo`'s network at
all.
