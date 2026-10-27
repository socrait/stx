from __future__ import annotations

from typing import TYPE_CHECKING, override

if TYPE_CHECKING:
    import argparse

from .. import platform as stx_platform
from ..command import Command
from ..core import load_registry, traefik_routes_warning, trusted_box_name

_ICON = {
    stx_platform.DriftState.MATCHING: "✓",
    stx_platform.DriftState.MISSING: "✗",
    stx_platform.DriftState.STOPPED: "✗",
    stx_platform.DriftState.DIFFERING: "⚠",
}


class SandboxStatusCommand(Command):
    """Show platform drift and active sandbox summary."""

    @classmethod
    @override
    def name(cls) -> str:
        return "status"

    @classmethod
    @override
    def help(cls) -> str:
        return "Show platform service drift and active sandbox count"

    @override
    def add_parser(self, subparsers) -> argparse.ArgumentParser:
        return subparsers.add_parser(self.name(), help=self.help())

    @override
    def _execute(self, args: argparse.Namespace) -> int | None:
        print("platform:")
        for drift in stx_platform.classify_services(list(stx_platform.ALL_SERVICES)):
            detail = f" ({drift.detail})" if drift.detail else ""
            print(
                f"  {_ICON[drift.state]} {drift.service}: {drift.state.value}{detail}"
            )

        warning = traefik_routes_warning(stx_platform.container_name("traefik"))
        if warning:
            print(f"  {warning}")

        registry = load_registry()
        n = len(registry)
        print("sandboxes:")
        if n == 0:
            print("  none")
        else:
            trusted_n = sum(1 for r in registry.values() if trusted_box_name(r.boxes))
            dmz_only_n = n - trusted_n
            parts = []
            if trusted_n:
                parts.append(f"{trusted_n} with a trusted box")
            if dmz_only_n:
                parts.append(f"{dmz_only_n} dmz-only")
            print(f"  {n} active ({', '.join(parts)})  · run 'stx ls' to list them")

        return None
