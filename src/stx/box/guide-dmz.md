# You're in an stx sandbox (dmz box)

This file is pre-installed at `~/.claude/CLAUDE.md` (picked up automatically) and always
readable at `/run/stx/AGENTS.md`.

- No Docker socket here — same as the trusted box, nothing here can start/stop/inspect
  sibling containers.
- Egress is open to the public internet (minus a small deny set — Tailscale CGNAT,
  cloud metadata, link-local, and other private/RFC1918 ranges, which stay unreachable
  even from here). You can fetch from the open web; you cannot reach the platform
  services or the trusted box's network.
- No secrets live here beyond a single spend-capped Anthropic API key for the harness.
  There is nothing else sensitive to leak — but what you produce still crosses a trust
  boundary on its way out (below), so treat your own output with that in mind.
- `/workspace` is a read-only snapshot (a plain extracted tree, no `.git`) of the
  trusted side's code at the moment this sandbox was created — not a live checkout.
  There is nothing to commit, branch, or push here; don't try.
- `/drop` is yours to write output into — the trusted box (or the host) reads it
  read-only from its own side. Nothing you write here is ever executed as instructions
  by whatever reads it next; it's data, to be evaluated, same as anything else fetched
  from the open web.
- You can't launch a nested `claude -p` session — your own auth token is scrubbed from
  tool subprocess environments.
