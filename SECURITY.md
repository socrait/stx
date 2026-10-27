# Security policy

## Supported versions

`stx` is pre-1.0 and ships from a single line of development. Only the latest release
(currently the `0.1.x` series) and the current `main` receive security fixes; there are
no maintained back-branches to backport to. Upgrade to the latest version before
reporting, in case the issue is already fixed.

## Reporting a vulnerability

Report it privately through this repository's **Security** tab ("Report a
vulnerability", GitHub private vulnerability reporting) rather than a public issue.
Include a description and reproduction steps.

Expect an acknowledgement within **3 business days**, and an initial assessment
(accepted / needs-more-info / not-in-scope) within **7 business days**. We aim to ship a
fix for an accepted report within **90 days**; please hold public disclosure until a fix
is released or that 90-day window has elapsed, whichever comes first. If a report stalls
without a response, you may disclose after the 90 days.

## Scope

In scope: `stx` itself — the CLI, the box runtime and its egress enforcement, the
generated compose overlay, credential handling, and the Claude Code plugin/skill
shipped in this repo.

Out of scope: vulnerabilities in a repo's own Dockerfile, compose file, or `setup`
scripts (see [docs/box-contract.md](docs/box-contract.md) for what a repo is trusted
with), and vulnerabilities in third-party dependencies — report those upstream.

See [docs/security.md](docs/security.md) for the trust model this project is built on,
including what `stx` enforces and what's left to a repo's own author.
