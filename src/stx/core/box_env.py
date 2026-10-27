from __future__ import annotations

from collections.abc import Iterable

#: A fixed PATH of root-owned system directories, mirroring `init-box.sh`'s own
#: `_SYSTEM_PATH`. Host-side `docker exec`s that must run as root pass this as
#: an explicit `--env PATH=` so a box user who owns a directory earlier on the
#: image's inherited PATH (e.g. `~/.local/bin`) can't shadow the binary the
#: exec resolves. See docs/box-contract.md.
SYSTEM_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

#: Names the box's own runtime reads to confine the box, plus interpreter
#: hooks the root entrypoint's own tools (bash, python, perl, node, ruby)
#: could pick up before dropping to the box user — see docs/box-contract.md
#: (Box environment). A repo may never set them, through the manifest's
#: `env` or its compose file's `environment`. `PATH` is reserved too: the
#: root entrypoint resolves iptables/getent/jq by name, so a repo-set PATH
#: pointing at a box-user-writable directory would let the box user shadow
#: them. `SANDBOX_` is kept reserved
#: even though nothing sets a `SANDBOX_*` var anymore (superseded by `STX_*`)
#: — harmless, and protective against a repo reusing the old prefix.
#: `BASH_FUNC_` is how bash exports a function definition into a child's
#: environment (`BASH_FUNC_name%%=() { ...; }`); the root entrypoint is
#: `/bin/bash init-box.sh`, so a repo-set `BASH_FUNC_iptables%%` would make
#: bash define `iptables` as a function and run repo code from the NET_ADMIN
#: script. A `%` in a name is a bash-function export marker and never part of
#: a legitimate env name a repo needs, so any name containing one is refused.
_RESERVED_PREFIXES = ("STX_", "SANDBOX_", "GIT_", "LD_", "BASH_FUNC_")
_RESERVED_NAMES = frozenset(
    {
        "PATH",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "ALL_PROXY",
        "BASH_ENV",
        "ENV",
        "PROMPT_COMMAND",
        "PYTHONSTARTUP",
        "PERL5OPT",
        "NODE_OPTIONS",
        "RUBYOPT",
    }
)


def reserved_env_names(names: Iterable[str]) -> list[str]:
    """The given environment names a repo may not set, sorted."""
    return sorted(
        name
        for name in names
        if name.upper().startswith(_RESERVED_PREFIXES)
        or name.upper() in _RESERVED_NAMES
        or "%" in name
    )
