import os
import re
import subprocess
from pathlib import Path

# Docker label keys every box container carries (set in overlay.py, read back
# by commands that scan containers directly — prune's orphan detection,
# status/ls's container lookups).
LABEL_SLUG = "stx.slug"
LABEL_BOX = "stx.box"
LABEL_REDIS_INDEX = "stx.redis_index"
LABEL_REPO_ROOT = "stx.repo_root"
LABEL_DATABASES = "stx.databases"


def state_root() -> Path:
    """The machine-global home for sandbox state — see docs/reference.md
    (State layout). Honours STX_STATE_ROOT if set."""
    if override := os.environ.get("STX_STATE_ROOT"):
        path = Path(override).resolve()
    else:
        xdg_state_home = os.environ.get("XDG_STATE_HOME") or str(
            Path.home() / ".local" / "state"
        )
        path = Path(xdg_state_home) / "stx"
    path.mkdir(parents=True, exist_ok=True)
    return path


def state_root_origin() -> str:
    """Which setting decided `state_root`: the variable's name, or `default`."""
    for name in ("STX_STATE_ROOT", "XDG_STATE_HOME"):
        if os.environ.get(name):
            return name
    return "default"


def repo_git_dir(root: Path) -> Path:
    """The main repo's .git dir — when root is a worktree, root/.git is a
    file, not a dir, which Docker can't bind-mount."""
    result = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        capture_output=True,
        text=True,
        check=True,
        cwd=str(root),
    )
    path = Path(result.stdout.strip())
    if not path.is_absolute():
        path = (root / path).resolve()
    return path


def repo_cache_id(root: Path) -> str:
    """The repo's identity for scoping shared-cache volume names: the main
    checkout's directory name, the same whichever of the repo's worktrees
    `root` is (see `overlay.shared_cache_prefix`). A name that ends with `-` or
    contains `--` can't delimit that prefix — `overlay.repo_cache_id_problem`
    refuses it rather than this returning something unusable."""
    if (root / ".git").is_file():
        return repo_git_dir(root).parent.name
    return root.name


def host_git_identity(root: Path) -> tuple[str, str]:
    """The (name, email) a commit from root's checkout would carry — see
    docs/box-contract.md (App environment). Falls back to a placeholder when
    either isn't configured."""
    name = subprocess.run(
        ["git", "config", "user.name"],
        capture_output=True,
        text=True,
        cwd=str(root),
    ).stdout.strip()
    email = subprocess.run(
        ["git", "config", "user.email"],
        capture_output=True,
        text=True,
        cwd=str(root),
    ).stdout.strip()
    return name or "Sandbox", email or "sandbox@localhost"


_PLACEHOLDER_IDENTITY_MARKERS = (
    "test@test",
    "example.com",
    "you@example",
    "your name",
    "your.name",
    "john doe",
    "placeholder",
)

# Catches a fat-fingered `git config user.name user.email` — the value
# itself looks like a config key, not a human name.
_CONFIG_KEY_SHAPED_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")


def _config_origin(root: Path, key: str) -> str:
    """Which config file supplies key for root's checkout — local, global, or system."""
    result = subprocess.run(
        ["git", "config", "--show-origin", "--get", key],
        capture_output=True,
        text=True,
        cwd=str(root),
    ).stdout.strip()
    return result.split("\t", 1)[0].removeprefix("file:") if result else "unconfigured"


def identity_provenance_summary(root: Path) -> str:
    """Human-readable git identity summary, reporting origin per key and
    warning on a placeholder — see docs/reference.md (Doctor)."""
    name, email = host_git_identity(root)
    name_origin = _config_origin(root, "user.name")
    email_origin = _config_origin(root, "user.email")

    if name_origin == email_origin:
        summary = f"git identity: {name} <{email}> (from {name_origin})"
    else:
        summary = (
            f"git identity: {name} <{email}> "
            f"(name from {name_origin}, email from {email_origin})"
        )
    lowered = f"{name} {email}".lower()
    suspicious = any(
        marker in lowered for marker in _PLACEHOLDER_IDENTITY_MARKERS
    ) or bool(_CONFIG_KEY_SHAPED_RE.match(name))
    if suspicious:
        summary += (
            "\n  warning: this looks like a placeholder, not a real operator — if "
            "it's not you, fix it with `git config user.name`/`user.email` before "
            "sandbox commits inherit it"
        )
    return summary


def worktree_path(slug: str) -> Path:
    return state_root() / "worktrees" / slug


def drop_dir(slug: str) -> Path:
    """Trusted-authored handoff dir. rw in trusted boxes at /drop; not mounted in dmz."""
    return state_root() / "drop" / slug


def drop_dmz_dir(slug: str) -> Path:
    """Dmz-authored handoff dir — the sole channel for the dmz box to reach trusted boxes.

    rw in the dmz box at /drop, :ro in every trusted box at /drop-dmz.
    """
    return state_root() / "drop-dmz" / slug


def dir_has_files(path: Path) -> bool:
    """Whether a directory exists and contains any regular file, recursively."""
    return path.is_dir() and any(p.is_file() for p in path.rglob("*"))


def code_dmz_root(slug: str) -> Path:
    """A sandbox's whole dmz-snapshot home, independent of which repo it holds."""
    return state_root() / "code-dmz" / slug


def code_dmz_dir(slug: str, repo: str) -> Path:
    """Dmz repo snapshot dir — a git-archive checkout, never a live worktree.
    See docs/security.md."""
    return code_dmz_root(slug) / repo


def logs_root(slug: str) -> Path:
    """Dispatch-log home for a sandbox: manifest.jsonl plus one dir per box."""
    return state_root() / "logs" / slug


def compose_overlay_path(slug: str) -> Path:
    """The generated box-contract overlay compose file for a sandbox (see overlay.py)."""
    return state_root() / "compose" / f"{slug}.json"


def allowlist_path(slug: str) -> Path:
    """The rendered tinyproxy allowlist for a sandbox's trusted box (see egress.py)."""
    return state_root() / "allowlists" / slug


def logs_dir(slug: str, box: str) -> Path:
    """A box's dispatch-capture dir, mounted rw at /logs in that box's container.

    Holds one <index>.jsonl (enveloped stream-json) and <index>.stderr.log per
    dispatch, named by the box-scoped index agent-wrapper.sh receives as its
    third argument.
    """
    return logs_root(slug) / box
