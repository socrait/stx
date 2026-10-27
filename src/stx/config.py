"""Per-human, per-machine `stx` preferences — see docs/reference.md (User
config), docs/secrets.md (Source)."""

import os
import subprocess
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import tomlkit

from .core.names import SLUG_RE
from .errors import StxError


class _Forge(Protocol):
    def username(self) -> str | None: ...


_CONFIG_PATH_ENV = "STX_CONFIG_PATH"

_SECRET_SOURCES = ("dotenv", "op")


@dataclass
class ConfigKeySpec:
    description: str
    validator: Callable[[str], None] | None = None


def _validate_branch_user(value: str) -> None:
    if not SLUG_RE.fullmatch(value):
        raise ValueError(f"branch.user '{value}' must match [a-z0-9-]+")


def _validate_secret_source(value: str) -> None:
    if value not in _SECRET_SOURCES:
        raise ValueError(
            f"secrets.source '{value}' must be one of {', '.join(_SECRET_SOURCES)}"
        )


def _validate_non_empty(value: str) -> None:
    if not value.strip():
        raise ValueError("value must not be empty")


KNOWN_CONFIG_KEYS: dict[str, ConfigKeySpec] = {
    "branch.user": ConfigKeySpec(
        description="Username segment for the manifest's {user} branch template",
        validator=_validate_branch_user,
    ),
    "secrets.source": ConfigKeySpec(
        description="Where host secrets come from: dotenv (default) or op (1Password)",
        validator=_validate_secret_source,
    ),
    "secrets.dotenv.path": ConfigKeySpec(
        description="Path to the dotenv secret file (default ~/.config/stx/secrets.env)",
        validator=_validate_non_empty,
    ),
    "secrets.op.environment": ConfigKeySpec(
        description="1Password Environment id (required when secrets.source=op)",
        validator=_validate_non_empty,
    ),
}


def config_path() -> Path:
    """The per-human `stx` preferences file — see docs/reference.md (User
    config)."""
    if override := os.environ.get(_CONFIG_PATH_ENV):
        return Path(override).resolve()
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(xdg_config_home) / "stx" / "config.toml"


def config_path_origin() -> str:
    """Which setting decided `config_path`: the variable's name, or `default`."""
    for name in (_CONFIG_PATH_ENV, "XDG_CONFIG_HOME"):
        if os.environ.get(name):
            return name
    return "default"


def default_dotenv_path() -> Path:
    """The default dotenv secret file, beside config.toml under $XDG_CONFIG_HOME/stx."""
    return config_path().parent / "secrets.env"


def _get_path(config: Mapping[str, Any], dotted: str) -> Any | None:
    """Dotted-path lookup into a nested mapping — works on both the plain
    dict `load_config()` returns and a `tomlkit` document/table."""
    node: Any = config
    for part in dotted.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def _set_path(doc: tomlkit.TOMLDocument, dotted: str, value: str) -> None:
    parts = dotted.split(".")
    node: Any = doc
    for part in parts[:-1]:
        existing = node.get(part) if isinstance(node, Mapping) else None
        if not isinstance(existing, Mapping):
            node[part] = tomlkit.table()
        node = node[part]
    node[parts[-1]] = value


def _unset_path(doc: tomlkit.TOMLDocument, dotted: str) -> None:
    parts = dotted.split(".")
    node: Any = doc
    for part in parts[:-1]:
        if not isinstance(node, Mapping) or part not in node:
            return
        node = node[part]
    if isinstance(node, Mapping):
        node.pop(parts[-1], None)


def load_config() -> dict[str, Any]:
    """Load the nested-table config; {} if missing. Raises StxError on a
    corrupt/unreadable file — see docs/reference.md (User config)."""
    path = config_path()
    if not path.exists():
        return {}
    try:
        data = path.read_bytes()
    except OSError as e:
        raise StxError(f"cannot read config at {path}: {e}") from e
    try:
        return tomllib.loads(data.decode())
    except tomllib.TOMLDecodeError as e:
        raise StxError(f"config at {path} is corrupt: {e}") from e


def config_value(config: Mapping[str, Any], key: str) -> Any | None:
    """Dotted-path lookup into a config mapping — the CLI's key spelling is
    also the TOML spelling (nested tables)."""
    return _get_path(config, key)


def _load_document() -> tomlkit.TOMLDocument:
    """Load the config file with `tomlkit`, preserving comments and
    ordering, for `set`/`unset` to rewrite."""
    path = config_path()
    if not path.exists():
        return tomlkit.document()
    try:
        text = path.read_text()
    except OSError as e:
        raise StxError(f"cannot read config at {path}: {e}") from e
    try:
        return tomlkit.parse(text)
    except tomlkit.exceptions.ParseError as e:
        raise StxError(f"config at {path} is corrupt: {e}") from e


def _write_document(doc: tomlkit.TOMLDocument) -> None:
    """Write the config atomically so a concurrent load_config() never sees
    a torn file."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(tomlkit.dumps(doc))
    tmp_path.replace(path)


def set_config_value(key: str, value: str) -> None:
    """Write one key's value, keeping the rest of the user's file — comments
    and table ordering — untouched."""
    doc = _load_document()
    _set_path(doc, key, value)
    _write_document(doc)


def unset_config_value(key: str) -> None:
    """Remove one key's override, keeping the rest of the user's file intact."""
    doc = _load_document()
    _unset_path(doc, key)
    _write_document(doc)


def git_username() -> str:
    """The git email's local part, or a neutral placeholder with neither configured.

    The last link of the {user} chain, once user config and the forge have
    both come up empty.
    """
    email_result = subprocess.run(
        ["git", "config", "user.email"],
        capture_output=True,
        text=True,
    )
    email = email_result.stdout.strip()
    return email.split("@")[0] if "@" in email else "dev"


def resolve_branch_username(forge: _Forge | None = None) -> str:
    """The {user} chain: user-config `branch.user`, else `forge.username()`
    when a forge exists, else the git email's local part."""
    configured = config_value(load_config(), "branch.user")
    if configured:
        return configured
    if forge is not None:
        username = forge.username()
        if username:
            return username
    return git_username()
