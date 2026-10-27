import os


def in_sandbox_box() -> bool:
    """Whether this process is running inside a sandbox box, not the host —
    see docs/reference.md (Commands and flags)."""
    return bool(os.environ.get("STX_ZONE"))


def host_only_error(verb: str) -> str:
    """Error text a sandbox lifecycle command prints when run inside a box —
    see docs/reference.md (Commands and flags)."""
    return (
        f"error: 'stx {verb}' is host-only — you're already running "
        "inside a sandbox. Manage sandboxes from the host session, not from "
        "in here."
    )
