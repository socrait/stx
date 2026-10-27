from __future__ import annotations

from types import SimpleNamespace

import pytest

from stx.core.registry import BoxRecord, SandboxRecord, SandboxState, Zone
from stx.errors import StxError
from stx.linking import parse_link_args, resolve_links


def _manifest(links: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(links=links or {})


def _target_record(
    *, slug: str = "be-1", routes: dict | None = None, repo_root: str = ""
) -> SandboxRecord:
    return SandboxRecord(
        slug=slug,
        created="2026-10-21T00:00:00+00:00",
        state=SandboxState.READY,
        redis_index=None,
        tailnet_port=None,
        repo_root=repo_root,
        boxes={
            "app": BoxRecord(
                zone=Zone.TRUSTED,
                user="devuser",
                routes=routes or {},
            )
        },
    )


class TestParseLinkArgs:
    def test_parses_name_and_target(self):
        assert parse_link_args(["api=be-1"]) == {"api": "be-1"}

    def test_parses_multiple(self):
        assert parse_link_args(["api=be-1", "admin=be-2"]) == {
            "api": "be-1",
            "admin": "be-2",
        }

    def test_none_is_empty(self):
        assert parse_link_args(None) == {}

    def test_missing_equals_rejected(self):
        with pytest.raises(StxError, match="NAME=TARGET"):
            parse_link_args(["api-be-1"])

    def test_repeated_name_rejected(self):
        with pytest.raises(StxError, match="more than once"):
            parse_link_args(["api=be-1", "api=be-2"])

    def test_bad_name_shape_rejected(self):
        with pytest.raises(StxError):
            parse_link_args(["My-Api=be-1"])


class TestResolveLinks:
    def test_resolves_a_valid_link(self):
        manifest = _manifest({"api": SimpleNamespace(route="api", default="")})
        registry = {"be-1": _target_record(routes={"api": 8080})}

        resolved = resolve_links(manifest, ["api=be-1"], registry)

        assert resolved["api"].target == "be-1"
        assert resolved["api"].port == 8080
        assert resolved["api"].container == "stx-be-1--app"
        assert resolved["api"].url == "http://api.be-1.localhost"
        assert resolved["api"].internal_url == "http://stx-be-1--app:8080"

    def test_no_link_args_is_empty(self):
        assert resolve_links(_manifest(), None, {}) == {}

    def test_undeclared_link_name_rejected(self):
        manifest = _manifest({})
        registry = {"be-1": _target_record(routes={"api": 8080})}
        with pytest.raises(StxError, match="not declared"):
            resolve_links(manifest, ["api=be-1"], registry)

    def test_unknown_target_rejected(self):
        manifest = _manifest({"api": SimpleNamespace(route="api", default="")})
        with pytest.raises(StxError, match="does not exist"):
            resolve_links(manifest, ["api=be-1"], {})

    def test_dmz_only_target_rejected(self):
        manifest = _manifest({"api": SimpleNamespace(route="api", default="")})
        dmz_only = SandboxRecord(
            slug="triage",
            created="2026-10-21T00:00:00+00:00",
            state=SandboxState.READY,
            redis_index=None,
            tailnet_port=None,
            boxes={"dmz": BoxRecord(zone=Zone.DMZ, user="devuser")},
        )
        with pytest.raises(StxError, match="no trusted box"):
            resolve_links(manifest, ["api=triage"], {"triage": dmz_only})

    def test_missing_route_rejected(self):
        manifest = _manifest({"api": SimpleNamespace(route="api", default="")})
        registry = {"be-1": _target_record(routes={"admin": 9090})}
        with pytest.raises(StxError, match="no route 'api'"):
            resolve_links(manifest, ["api=be-1"], registry)

    def test_none_manifest_rejects_any_link(self):
        with pytest.raises(StxError, match="not declared"):
            resolve_links(None, ["api=be-1"], {"be-1": _target_record()})
