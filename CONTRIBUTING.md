# Contributing

## Dev setup

```bash
uv sync
```

Run the test suite:

```bash
uv run python -m pytest -q
```

This skips two kinds of test by default (see `addopts` in `pyproject.toml`):

- **`docker`-marked tests** need a real Docker daemon and Compose ≥ 2.20 — they build
  images and run real containers. Run them with:

  ```bash
  uv run python -m pytest -m docker
  ```

- **`build`-marked tests** run a real `uv build`. Run them with:

  ```bash
  uv run python -m pytest -m build
  ```

Lint and format:

```bash
uv run ruff check
uv run ruff format --check   # drop --check to apply formatting instead of just checking
```

All three — `ruff check`, `ruff format --check`, and `pytest -q` — must pass before a
commit.

## Pull requests and issues

Open a pull request against `main` from a branch (or a fork). Fill in
[`.github/PULL_REQUEST_TEMPLATE.md`](.github/PULL_REQUEST_TEMPLATE.md) — it prompts for what
changed, why, and how it was verified. CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml))
runs `ruff check`, `ruff format --check`, and `pytest -q`, plus the `docker`- and
`build`-marked jobs; all must be green before a PR merges.

Bug reports and feature requests go to GitHub Issues, using the templates under
[`.github/ISSUE_TEMPLATE`](.github/ISSUE_TEMPLATE). **Security vulnerabilities do not** —
report those privately per [SECURITY.md](SECURITY.md), never in a public issue or PR.

## Working on `stx` in an `stx` box

This repo has its own `sandbox.yaml`, so you can develop `stx` inside the same kind of
sandbox it provisions for other repos:

```bash
stx new stx-dev     # on the host — needs a harness credential, like any `stx new`
stx shell stx-dev
```

The box builds from `.devcontainer/Dockerfile` and runs `uv sync` on first boot (see
`.devcontainer/setup.sh`). `uv sync` only populates the virtualenv; to run the `stx` CLI
itself inside the box use `uv run stx …`, or `uv tool install .` to put it on `PATH`.

Two limits of the box shape the dev loop inside it:

- **No Docker daemon.** A box can't build images or run containers, so the `docker`-marked
  tests (`pytest -m docker`) can't run there — run those on the host. Plain `pytest -q` and
  the linters work fine inside the box.
- **Egress is PyPI-only.** The box's allowlist lets `uv` reach PyPI and nothing else: you
  can't `git push` or use `gh` from inside it. Commit inside the box if you like, but push
  and open PRs from the host.

This is a convenient way to try a change against a real box and firewall without touching
your own machine's Docker setup — but it's not required; working directly in a normal
checkout is fine too.

## Commit style

Short, present-tense, imperative summary lines — what changed, and (after a colon)
why it matters if that isn't obvious from the change itself:

```
Agent liveness uses the shell's kill: images without procps have no kill executable
A manifest's origin is its own checkout's, never an enclosing repo's
```

Keep commits logically scoped — one behavioral change per commit, not a batch of
unrelated fixes.

## License

`stx` is MIT-licensed (see [LICENSE](LICENSE)). By contributing, you agree your
contributions are licensed under the same terms.
