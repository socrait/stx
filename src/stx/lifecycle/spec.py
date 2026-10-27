"""The immutable description a sandbox is provisioned from.

`new` and `refresh` each build one of these — `new` from the manifest, the
args, and freshly allocated registry resources; `refresh` from the manifest,
the args, and the existing registry entry — then hand it to the one provision
path (see `provision`). Everything the box half needs to render the overlay,
compose the env, and start the containers is fixed here, derived once, so the
provision path never reaches back into args or the registry to recompute it.

Resources a step *creates* (the resolved image user, the worktree sha, minted
forge credentials, the rendered overlay path) are provisioning outputs, not
inputs — they live on the mutable `ProvisionState`, never here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..core import ClaudeAuthMode, ComposeVariable, LinkRecord
from ..forge import Forge
from ..manifest import Manifest


@dataclass(frozen=True)
class SandboxSpec:
    """What a sandbox should be — names, overlay inputs, env, databases,
    services, ports, links, forge — fixed before the first side effect."""

    slug: str
    root: Path | None
    repo_name: str
    manifest: Manifest | None
    forge: Forge | None
    services: list[str]
    with_dmz: bool
    claude_auth_mode: ClaudeAuthMode
    read_only: bool
    timeout: float
    # App-box worktree inputs; unused for a dmz-only sandbox and for refresh
    # (which recreates the box over the worktree `new` already made).
    branch: str | None = None
    base: str | None = None
    base_explicit: bool = False
    links: dict[str, LinkRecord] = field(default_factory=dict)
    host_vars: list[ComposeVariable] = field(default_factory=list)
    # The loaded secret source's own mapping — the only thing compose and
    # provision variables resolve from, never the ambient shell (see
    # docs/secrets.md). Populated from `SecretSource.ensure_loaded`.
    secret_env: dict[str, str] = field(default_factory=dict)
    databases: tuple[str, ...] = ()
    redis_index: int | None = None
    tailnet_port: int | None = None
    route_ports: dict[str, int] = field(default_factory=dict)
