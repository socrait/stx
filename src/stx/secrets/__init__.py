"""Host-side secrets — a dotenv file (default) or a 1Password Environment,
chosen in user config. See docs/secrets.md (Source)."""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

from ..errors import StxError

#: Re-exec guard so a re-exec'd OpSource doesn't loop.
_REEXEC_GUARD_VAR = "_STX_SECRETS_LOADED"

#: Never loaded from a dotenv file — see docs/secrets.md (`dotenv` paragraph).
_PROCESS_CONTROL_VARS = frozenset({"PATH", "HOME", "SHELL", "IFS"})
_PROCESS_CONTROL_PREFIXES = ("LD_", "DYLD_", "PYTHON")


def _is_process_control_var(key: str) -> bool:
    return key in _PROCESS_CONTROL_VARS or key.startswith(_PROCESS_CONTROL_PREFIXES)


class SecretsError(StxError):
    """Raised when required host secrets are missing, or the configured
    source itself can't be used — see docs/secrets.md (Source)."""

    def __init__(
        self, missing: Sequence[str] = (), *, reason: str | None = None
    ) -> None:
        self.missing = list(missing)
        message = (
            reason
            if reason is not None
            else f"missing required secret(s): {', '.join(self.missing)}"
        )
        super().__init__(message)


class SecretSource(Protocol):
    def ensure_loaded(
        self, required: Sequence[str], *, optional: Sequence[str] = ()
    ) -> dict[str, str]:
        """Make every name in `required` present in `os.environ`, or raise
        `SecretsError` naming whichever are still missing. May re-exec the
        process. Returns the source's own loaded values (a dotenv file's
        parsed mapping; for `op`, the requested names, `optional` ones only
        when set) — the only thing compose/provision variables may resolve
        from, never the ambient shell. See docs/secrets.md."""
        ...


def _missing(required: Sequence[str]) -> list[str]:
    """Names in `required` that are absent from `os.environ`, or present but
    empty — an empty string is not a usable secret value."""
    return [name for name in required if not os.environ.get(name)]


class DotenvSource:
    """Parses `path` as `KEY=VALUE` lines — see docs/secrets.md (`dotenv`
    paragraph)."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def ensure_loaded(
        self, required: Sequence[str], *, optional: Sequence[str] = ()
    ) -> dict[str, str]:
        loaded: dict[str, str] = {}
        for key, value in _parse_dotenv(self._path).items():
            if _is_process_control_var(key):
                print(
                    f"warning: ignoring {key} from dotenv file {self._path} — "
                    "a secrets file may not set process-control variables",
                    file=sys.stderr,
                )
                continue
            os.environ.setdefault(key, value)
            loaded[key] = value
        missing = _missing(required)
        if missing:
            raise SecretsError(missing)
        return loaded


class OpSource:
    """Re-execs under `op run` so 1Password injects secrets directly — see
    docs/secrets.md (`op` paragraph)."""

    def __init__(self, environment: str) -> None:
        self._environment = environment

    def ensure_loaded(
        self, required: Sequence[str], *, optional: Sequence[str] = ()
    ) -> dict[str, str]:
        if _REEXEC_GUARD_VAR in os.environ:
            missing = _missing(required)
            if missing:
                raise SecretsError(missing)
            # After the re-exec every requested name present was injected by
            # `op` — we stripped the operator's own values for them before the
            # re-exec. Restrict the mapping to exactly the requested names so
            # an unrelated op/ambient var can't satisfy a repo's `${...}`.
            return {
                name: os.environ[name]
                for name in [*required, *optional]
                if os.environ.get(name)
            }
        # `op run` passes the whole parent environment through to the child, so
        # a requested name already set in the operator's shell would survive
        # the re-exec and be indistinguishable from one `op` injected. Remove
        # every requested name from the child's environment first; whatever is
        # present under one of these names afterward came from `op`.
        requested = {*required, *optional}
        child_env = {k: v for k, v in os.environ.items() if k not in requested}
        child_env[_REEXEC_GUARD_VAR] = "1"
        try:
            os.execvpe(
                "op",
                [
                    "op",
                    "run",
                    "--environment",
                    self._environment,
                    "--",
                    sys.executable,
                    *sys.argv,
                ],
                child_env,
            )
        except FileNotFoundError as e:
            raise SecretsError(
                reason=(
                    "the 1Password CLI ('op') is not installed or not on PATH — "
                    "install it, or switch secrets.source to 'dotenv' in `stx config`"
                )
            ) from e


def _default_dotenv_path() -> Path:
    """Mirrors `stx.config.default_dotenv_path`'s rule (beside `config.toml`)
    without importing that module, to avoid a circular import."""
    if override := os.environ.get("STX_CONFIG_PATH"):
        config_dir = Path(override).resolve().parent
    else:
        xdg_config_home = os.environ.get("XDG_CONFIG_HOME") or str(
            Path.home() / ".config"
        )
        config_dir = Path(xdg_config_home) / "stx"
    return config_dir / "secrets.env"


def source_for(config: Mapping[str, object]) -> SecretSource:
    """Build the configured `SecretSource` from `config` (the nested-table
    mapping `stx.config.load_config()` returns)."""
    secrets_table = config.get("secrets", {})
    source = secrets_table.get("source", "dotenv")
    if source == "dotenv":
        dotenv_path = secrets_table.get("dotenv", {}).get("path")
        return DotenvSource(
            Path(dotenv_path) if dotenv_path else _default_dotenv_path()
        )
    if source == "op":
        environment = secrets_table.get("op", {}).get("environment")
        if not environment:
            raise SecretsError(["secrets.op.environment"])
        return OpSource(environment)
    raise StxError(f"secrets.source {source!r} is not one of 'dotenv', 'op'")


def dotenv_mode_ok(path: Path) -> bool:
    """Whether `path` is mode 600 or stricter (no group/other bits set).

    A nonexistent file reports True — `doctor` has nothing to warn about until
    the file exists."""
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        return True
    return mode & 0o077 == 0


def _parse_dotenv(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return {}
    values: dict[str, str] = {}
    for lineno, raw_line in enumerate(lines, start=1):
        parsed = _parse_dotenv_line(raw_line, path=path, lineno=lineno)
        if parsed is not None:
            key, value = parsed
            values[key] = value
    return values


def _parse_dotenv_line(
    raw_line: str, *, path: Path, lineno: int
) -> tuple[str, str] | None:
    line = raw_line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[len("export ") :].lstrip()
    if "=" not in line:
        return None
    key, _, rest = line.partition("=")
    key = key.strip()
    rest = rest.strip()
    if not key:
        return None
    if rest and rest[0] in "\"'":
        quote = rest[0]
        end = rest.find(quote, 1)
        if end == -1:
            raise StxError(f"{path}:{lineno}: unterminated quote in dotenv value")
        value = rest[1:end]
        return key, value
    return key, _strip_unquoted_comment(rest)


def _strip_unquoted_comment(rest: str) -> str:
    """Strip a trailing `# comment` — see docs/secrets.md (`dotenv` paragraph)."""
    for i, ch in enumerate(rest):
        if ch == "#" and (i == 0 or rest[i - 1].isspace()):
            return rest[:i].rstrip()
    return rest
