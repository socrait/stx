import fcntl
import os
import platform
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from .paths import state_root

CompletedProcessRunner = Callable[..., "subprocess.CompletedProcess[str]"]

_HOSTS_BLOCK_BEGIN = "# BEGIN stx hosts (managed by `stx hosts` — do not edit)"
_HOSTS_BLOCK_END = "# END stx hosts"


def hosts_path() -> Path:
    """Return the hosts file to manage. Honours STX_HOSTS_PATH so tests never
    touch the real /etc/hosts; unset in real usage, where it always resolves there."""
    return Path(os.environ.get("STX_HOSTS_PATH", "/etc/hosts"))


def hosts_hostnames_for(slug: str, routes: dict[str, int]) -> list[str]:
    """Every local hostname a sandbox resolves: the bare `<slug>.localhost`
    main route, plus `<route>.<slug>.localhost` for each of its own routes.
    Empty if `routes` is empty — a routeless sandbox gets no hosts entry."""
    if not routes:
        return []
    return [f"{slug}.localhost", *(f"{name}.{slug}.localhost" for name in routes)]


def _is_slugs_hostname(hostname: str, slug: str) -> bool:
    return hostname == f"{slug}.localhost" or hostname.endswith(f".{slug}.localhost")


def _hosts_line_for(slug: str, routes: dict[str, int]) -> str:
    return "127.0.0.1\t" + " ".join(hosts_hostnames_for(slug, routes))


def _read_hosts_lines() -> list[str]:
    path = hosts_path()
    return path.read_text().splitlines() if path.exists() else []


def _hosts_block_bounds(lines: list[str]) -> tuple[int, int] | None:
    if _HOSTS_BLOCK_BEGIN not in lines:
        return None
    start = lines.index(_HOSTS_BLOCK_BEGIN)
    try:
        end = lines.index(_HOSTS_BLOCK_END, start)
    except ValueError:
        return None
    return start, end


def hosts_entry_exists(slug: str, routes: dict[str, int]) -> bool:
    """Whether this exact hosts entry (the bare hostname plus every route
    hostname `routes` implies) is already present. Read-only, no elevation."""
    lines = _read_hosts_lines()
    bounds = _hosts_block_bounds(lines)
    if bounds is None:
        return False
    start, end = bounds
    return _hosts_line_for(slug, routes) in lines[start + 1 : end]


def _dns_flush_command() -> str | None:
    """Best-effort DNS-cache flush command for this platform, or None if
    unknown — a soft no-op, not a failure, if not found."""
    system = platform.system()
    if system == "Darwin":
        return "dscacheutil -flushcache && killall -HUP mDNSResponder"
    if system == "Linux" and shutil.which("resolvectl"):
        return "resolvectl flush-caches"
    return None


def _hosts_lock_path() -> Path:
    return state_root() / "hosts.lock"


@contextmanager
def _locked_hosts() -> Iterator[None]:
    """Hold hosts.lock for one read-modify-write so two concurrent callers
    (e.g. `stx hosts add` racing a `stx new`) can't interleave and lose an
    update — same pattern as `registry.locked_registry()`."""
    lock_path = _hosts_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _apply_hosts_lines(
    new_lines: list[str], *, runner: CompletedProcessRunner = subprocess.run
) -> None:
    """Overwrite the hosts file with new_lines via one sudo invocation that also
    best-effort flushes the DNS cache — one password prompt, not two.

    Writes to a temp file first and passes both paths as positional shell
    parameters ($1/$2), never interpolated into the script text itself.
    Raises OSError if the sudo command fails or is declined. `runner` is a
    seam for tests to inject a fake subprocess result.
    """
    content = "\n".join(new_lines) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix="stx-hosts-")
    try:
        with os.fdopen(fd, "w") as tmp:
            tmp.write(content)
        script = 'install -m 644 "$1" "$2"'
        if flush := _dns_flush_command():
            # `ec` isolates the write's exit code from the flush's — a flush failure
            # (e.g. a stripped-down macOS/CI environment missing mDNSResponder) must
            # never be mistaken for a failed hosts-file write.
            script = f"{script}; ec=$?; {flush}; exit $ec"
        result = runner(["sudo", "sh", "-c", script, "sh", tmp_name, str(hosts_path())])
    finally:
        os.unlink(tmp_name)
    if result.returncode != 0:
        raise OSError("sudo failed while updating the hosts file")


def add_hosts_entry(
    slug: str,
    routes: dict[str, int],
    *,
    runner: CompletedProcessRunner = subprocess.run,
) -> None:
    """Add one hosts-file line for a sandbox — `127.0.0.1` followed by
    `<slug>.localhost` and every `<route>.<slug>.localhost` from `routes` —
    see docs/reference.md (Hosts entries). No-op if `routes` is empty (nothing
    to resolve) or the line is already present. Raises OSError if the
    elevated write fails. `runner` is a seam for tests to inject a fake
    subprocess result."""
    if not routes:
        return
    with _locked_hosts():
        lines = _read_hosts_lines()
        bounds = _hosts_block_bounds(lines)
        line = _hosts_line_for(slug, routes)
        if bounds is None:
            new_lines = [*lines, _HOSTS_BLOCK_BEGIN, line, _HOSTS_BLOCK_END]
        else:
            start, end = bounds
            block = lines[start + 1 : end]
            if line in block:
                return
            new_lines = [
                *lines[:start],
                _HOSTS_BLOCK_BEGIN,
                *block,
                line,
                _HOSTS_BLOCK_END,
                *lines[end + 1 :],
            ]
        _apply_hosts_lines(new_lines, runner=runner)


def remove_hosts_entry(
    slug: str, *, runner: CompletedProcessRunner = subprocess.run
) -> list[str]:
    """Remove the sandbox's hosts line(s), dropping the managed block entirely
    once empty. Matched by hostname, not by routes — every name on a line
    that equals `<slug>.localhost` or ends in `.<slug>.localhost` — so this
    doesn't need the routes, and also cleans up an old single-name
    `api.<slug>.localhost` line from a prior stx version.

    No-ops (no sudo prompt, returns []) if nothing matches. Raises OSError if
    the elevated write fails (e.g. sudo declined). Returns the hostnames
    removed. `runner` is a seam for tests to inject a fake subprocess result.
    """
    with _locked_hosts():
        lines = _read_hosts_lines()
        bounds = _hosts_block_bounds(lines)
        if bounds is None:
            return []
        start, end = bounds
        block = lines[start + 1 : end]
        removed: list[str] = []
        remaining = []
        for entry in block:
            names = entry.split()[1:]
            if any(_is_slugs_hostname(name, slug) for name in names):
                removed.extend(names)
            else:
                remaining.append(entry)
        if not removed:
            return []
        if remaining:
            new_lines = [
                *lines[:start],
                _HOSTS_BLOCK_BEGIN,
                *remaining,
                _HOSTS_BLOCK_END,
                *lines[end + 1 :],
            ]
        else:
            new_lines = [*lines[:start], *lines[end + 1 :]]
        _apply_hosts_lines(new_lines, runner=runner)
        return removed
