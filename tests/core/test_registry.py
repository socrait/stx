import json
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from stx.core.dispatch import AgentState, AgentStatus
from stx.core.registry import (
    BoxRecord,
    ForgeBoxRecord,
    ForgePackageRecord,
    ForgePushRecord,
    ForgeSourceRecord,
    LinkRecord,
    SandboxRecord,
    SandboxState,
    Zone,
    allocate_redis_index,
    allocate_route_ports,
    allocate_tailnet_port,
    linkers_of,
    load_registry,
    load_registry_with_unparsed,
    locked_registry,
    port_bindable,
    sandbox_live_boxes,
    save_registry,
)
from stx.errors import StxError

_MOD = "stx.core.registry"
_WORKING_STATUS = AgentStatus(state=AgentState.WORKING, pid=4242)
_DONE_STATUS = AgentStatus(state=AgentState.DONE, pid=4242, exit_code=0)


def _make_record(
    slug: str,
    *,
    redis_index: int | None = 1,
    tailnet_port: int | None = None,
    route_ports: dict[str, int] | None = None,
) -> SandboxRecord:
    return SandboxRecord(
        slug=slug,
        created="2026-06-26T00:00:00+00:00",
        state=SandboxState.READY,
        redis_index=redis_index,
        tailnet_port=tailnet_port,
        route_ports=route_ports if route_ports is not None else {},
        services=["postgres", "redis"],
        repo="widgetshop",
        repo_root="/tmp/widgetshop",
        boxes={
            "app": BoxRecord(
                zone=Zone.TRUSTED,
                user="devuser",
                branch=f"feat/alex/{slug}",
                worktree=f"/tmp/wt/{slug}",
            )
        },
    )


class TestRoundTrip:
    def test_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))

        record = _make_record("foo", redis_index=3, tailnet_port=5099)
        save_registry({"foo": record})
        loaded = load_registry()

        assert "foo" in loaded
        assert loaded["foo"] == record

    def test_roundtrip_dmz_only(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))

        record = SandboxRecord(
            slug="triage",
            created="2026-07-02T00:00:00+00:00",
            state=SandboxState.READY,
            redis_index=None,
            tailnet_port=None,
            route_ports={},
            services=[],
            boxes={"dmz": BoxRecord(zone=Zone.DMZ, user="devuser", base_sha="abc123")},
        )
        save_registry({"triage": record})
        loaded = load_registry()

        assert loaded["triage"] == record

    def test_roundtrip_forge_record(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))

        record = _make_record("fix-447")
        record.boxes["app"].forge = ForgeBoxRecord(
            kind="gitlab",
            push=ForgePushRecord(
                scope_id=123,
                token_id=456,
                scope="write_repository",
                expires_at="2026-07-09",
            ),
            package=ForgePackageRecord(
                scope_id=789, token_id=1011, expires_at="2026-07-14"
            ),
        )
        save_registry({"fix-447": record})
        loaded = load_registry()

        assert loaded["fix-447"].boxes["app"].forge.push.token_id == 456
        assert loaded["fix-447"].boxes["app"].forge.package.scope_id == 789

    def test_roundtrip_routes_and_links(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))

        record = _make_record("web-1")
        record.boxes["app"].routes = {"web": 3000}
        record.boxes["app"].links = {
            "api": LinkRecord(
                target="be-1", route="api", port=8080, container="stx-be-1--app"
            )
        }
        save_registry({"web-1": record})
        loaded = load_registry()

        app = loaded["web-1"].boxes["app"]
        assert app.routes == {"web": 3000}
        assert app.links["api"] == LinkRecord(
            target="be-1", route="api", port=8080, container="stx-be-1--app"
        )
        assert app.links["api"].url == "http://api.be-1.localhost"
        assert app.links["api"].internal_url == "http://stx-be-1--app:8080"

    def test_roundtrip_route_ports(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))

        record = _make_record("web-1", route_ports={"api": 5599, "admin": 5598})
        save_registry({"web-1": record})
        loaded = load_registry()

        assert loaded["web-1"].route_ports == {"api": 5599, "admin": 5598}

    def test_roundtrip_provisioning_state(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))

        record = _make_record("mid-flight")
        record.state = SandboxState.PROVISIONING
        save_registry({"mid-flight": record})

        assert load_registry()["mid-flight"].state == SandboxState.PROVISIONING

    def test_roundtrip_databases(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))

        record = _make_record("fix-447")
        record.databases = ("stx_fix_447", "stx_fix_447_test")
        save_registry({"fix-447": record})

        assert load_registry()["fix-447"].databases == (
            "stx_fix_447",
            "stx_fix_447_test",
        )

    def test_roundtrip_forge_source(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))

        record = _make_record("fix-447")
        record.boxes["app"].forge_source = ForgeSourceRecord(
            kind="gitlab", host="gitlab.com", project="widgetshop/widgetshop"
        )
        save_registry({"fix-447": record})

        loaded = load_registry()["fix-447"].boxes["app"].forge_source
        assert loaded == ForgeSourceRecord(
            kind="gitlab", host="gitlab.com", project="widgetshop/widgetshop"
        )

    def test_empty_when_file_absent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        assert load_registry() == {}

    def test_corrupt_file_raises_instead_of_returning_empty(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        path = tmp_path / "registry.json"
        path.write_text("not valid json")

        with pytest.raises(StxError, match="corrupt"):
            load_registry()

    def test_non_object_top_level_raises_instead_of_reading_empty(
        self, tmp_path, monkeypatch
    ):
        # A top-level JSON array (or any non-object) parses fine but is not a
        # registry — reading it as empty would then overwrite it on the next
        # save, destroying whatever it was. Raise, same as a corrupt file.
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        path = tmp_path / "registry.json"
        path.write_text('["not", "a", "registry"]')

        with pytest.raises(StxError):
            load_registry()

    def test_non_object_top_level_is_never_overwritten_by_a_locked_save(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        path = tmp_path / "registry.json"
        path.write_text('["not", "a", "registry"]')

        with pytest.raises(StxError), locked_registry() as reg:
            reg.reserve(
                "fix-447",
                created="now",
                redis_index=None,
                tailnet_port=None,
                route_ports={},
                services=[],
                repo="",
                repo_root="",
            )

        assert path.read_text() == '["not", "a", "registry"]'

    def test_unreadable_file_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        path = tmp_path / "registry.json"
        path.write_text('{"schema": 3, "sandboxes": {}}')

        def _raise_read_text(self, *a, **kw):
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "read_text", _raise_read_text)

        with pytest.raises(StxError, match="cannot read registry"):
            load_registry()

    def test_corrupt_file_is_never_overwritten_by_a_locked_save(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        path = tmp_path / "registry.json"
        path.write_text("not valid json")

        with pytest.raises(StxError), locked_registry() as reg:
            reg.reserve(
                "fix-447",
                created="now",
                redis_index=None,
                tailnet_port=None,
                route_ports={},
                services=[],
                repo="",
                repo_root="",
            )

        assert path.read_text() == "not valid json"

    def test_unparseable_entry_survives_a_locked_save(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        path = tmp_path / "registry.json"

        good = _make_record("good", redis_index=1)
        save_registry({"good": good})
        raw = json.loads(path.read_text())
        raw["sandboxes"]["stale"] = {
            "slug": "stale",
            "created": "2026-01-01T00:00:00+00:00",
        }
        path.write_text(json.dumps(raw))

        with locked_registry() as reg:
            reg.reserve(
                "new-one",
                created="now",
                redis_index=None,
                tailnet_port=None,
                route_ports={},
                services=[],
                repo="",
                repo_root="",
            )

        raw_after = json.loads(path.read_text())
        assert raw_after["sandboxes"]["stale"] == {
            "slug": "stale",
            "created": "2026-01-01T00:00:00+00:00",
        }
        loaded = load_registry()
        assert set(loaded) == {"good", "new-one"}

    def test_skips_unparseable_entry_but_keeps_others(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        path = tmp_path / "registry.json"

        good = _make_record("good", redis_index=1)
        save_registry({"good": good})
        raw = json.loads(path.read_text())
        raw["sandboxes"]["stale"] = {
            "slug": "stale",
            "created": "2026-01-01T00:00:00+00:00",
        }
        path.write_text(json.dumps(raw))

        loaded = load_registry()

        assert set(loaded) == {"good"}
        assert loaded["good"] == good
        assert "stale" in capsys.readouterr().err

    def test_unparseable_entry_warning_names_stx_prune_as_the_fix(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        path = tmp_path / "registry.json"
        path.write_text(
            json.dumps(
                {
                    "schema": 3,
                    "sandboxes": {
                        "stale": {
                            "slug": "stale",
                            "created": "2026-01-01T00:00:00+00:00",
                        }
                    },
                }
            )
        )

        load_registry()

        assert "stx prune" in capsys.readouterr().err

    def test_load_registry_with_unparsed_returns_both_halves(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        path = tmp_path / "registry.json"

        good = _make_record("good", redis_index=1)
        save_registry({"good": good})
        raw = json.loads(path.read_text())
        raw["sandboxes"]["stale"] = {
            "slug": "stale",
            "created": "2026-01-01T00:00:00+00:00",
        }
        path.write_text(json.dumps(raw))

        registry, unparsed = load_registry_with_unparsed()

        assert set(registry) == {"good"}
        assert set(unparsed) == {"stale"}
        raw_entry, err = unparsed["stale"]
        assert raw_entry == {
            "slug": "stale",
            "created": "2026-01-01T00:00:00+00:00",
        }
        assert "stale" not in err  # the KeyError names the missing field, not the slug

    def test_reserve_refuses_a_slug_that_has_an_unparseable_entry(
        self, tmp_path, monkeypatch
    ):
        # The slug is taken by a broken entry — reserving it would have the
        # save write the raw entry back over the reservation (same key), so a
        # later `new` step reads it back unparseable and dies with KeyError.
        # Refuse up front and point at `stx prune`.
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        path = tmp_path / "registry.json"
        save_registry({"good": _make_record("good", redis_index=1)})
        raw = json.loads(path.read_text())
        raw["sandboxes"]["stale"] = {
            "slug": "stale",
            "created": "2026-01-01T00:00:00+00:00",
        }
        path.write_text(json.dumps(raw))

        with (
            pytest.raises(StxError, match="prune"),
            locked_registry() as reg,
        ):
            reg.reserve(
                "stale",
                created="now",
                redis_index=None,
                tailnet_port=None,
                route_ports={},
                services=[],
                repo="",
                repo_root="",
            )

        # The broken entry is untouched, not clobbered by a half-reservation.
        raw_after = json.loads(path.read_text())
        assert raw_after["sandboxes"]["stale"] == {
            "slug": "stale",
            "created": "2026-01-01T00:00:00+00:00",
        }

    def test_save_registry_does_not_let_raw_overwrite_a_real_record(
        self, tmp_path, monkeypatch
    ):
        # A parsed record always wins over a same-slug raw carry-over — the
        # reservation must never be written back as its old broken self.
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        record = _make_record("foo", redis_index=4)
        save_registry(
            {"foo": record},
            extra_raw={"foo": {"slug": "foo", "created": "stale"}},
        )

        loaded = load_registry()
        assert loaded["foo"].redis_index == 4

    def test_load_registry_with_unparsed_empty_when_all_parse(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        save_registry({"good": _make_record("good", redis_index=1)})

        registry, unparsed = load_registry_with_unparsed()

        assert set(registry) == {"good"}
        assert unparsed == {}

    def test_entry_missing_a_field_is_skipped_not_defaulted(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        path = tmp_path / "registry.json"

        record = _make_record("foo", redis_index=1)
        save_registry({"foo": record})
        raw = json.loads(path.read_text())
        del raw["sandboxes"]["foo"]["boxes"]["app"]["claude_auth_mode"]
        path.write_text(json.dumps(raw))

        assert load_registry() == {}

    def test_entry_written_before_databases_was_recorded_is_skipped(
        self, tmp_path, monkeypatch
    ):
        """An entry from before this field existed has no `databases` key at
        all — strict parsing must skip it (as unparseable), not default it to
        `()` and risk a later `rm` treating a nonempty set of databases as
        empty. See docs/reference.md (Teardown guards)."""
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        path = tmp_path / "registry.json"

        record = _make_record("foo", redis_index=1)
        save_registry({"foo": record})
        raw = json.loads(path.read_text())
        del raw["sandboxes"]["foo"]["databases"]
        path.write_text(json.dumps(raw))

        assert load_registry() == {}

    def test_entry_missing_forge_source_is_skipped_not_defaulted(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        path = tmp_path / "registry.json"

        record = _make_record("foo", redis_index=1)
        save_registry({"foo": record})
        raw = json.loads(path.read_text())
        del raw["sandboxes"]["foo"]["boxes"]["app"]["forge_source"]
        path.write_text(json.dumps(raw))

        assert load_registry() == {}

    def test_unknown_schema_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        path = tmp_path / "registry.json"
        path.write_text('{"schema": 1, "sandboxes": {}}')

        with pytest.raises(StxError, match="schema"):
            load_registry()

    def test_missing_schema_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        path = tmp_path / "registry.json"
        path.write_text('{"sandboxes": {}}')

        with pytest.raises(StxError, match="schema"):
            load_registry()

    def test_save_registry_leaves_no_tmp_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        path = tmp_path / "registry.json"

        save_registry({"x": _make_record("x")})

        assert list(tmp_path.glob("*.tmp")) == []
        assert path.exists()

    def test_creates_parent_dirs(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "a" / "b"))
        nested = tmp_path / "a" / "b" / "registry.json"

        save_registry({"x": _make_record("x")})
        assert nested.exists()

    def test_persists_multiple_entries(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))

        registry = {
            "a": _make_record("a", redis_index=1),
            "b": _make_record("b", redis_index=2),
        }
        save_registry(registry)
        loaded = load_registry()

        assert set(loaded.keys()) == {"a", "b"}
        assert loaded["b"].redis_index == 2

    def test_schema_is_3(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        save_registry({"x": _make_record("x")})
        raw = json.loads((tmp_path / "registry.json").read_text())
        assert raw["schema"] == 3


class TestAllocateRedisIndex:
    def test_picks_fifteen_when_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        assert allocate_redis_index({}) == 15

    def test_picks_highest_free_top_down(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        registry = {"a": _make_record("a", redis_index=15)}
        assert allocate_redis_index(registry) == 14

    def test_fills_gap_from_the_top(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        registry = {
            "a": _make_record("a", redis_index=15),
            "b": _make_record("b", redis_index=13),
        }
        assert allocate_redis_index(registry) == 14

    def test_raises_when_full(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        registry = {str(i): _make_record(str(i), redis_index=i) for i in range(1, 16)}
        with pytest.raises(StxError, match="in use"):
            allocate_redis_index(registry)


class TestAllocateTailnetPort:
    def test_picks_top_of_pool_when_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        assert allocate_tailnet_port({}) == 5099

    def test_picks_highest_free_top_down(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        registry = {"a": _make_record("a", tailnet_port=5099)}
        assert allocate_tailnet_port(registry) == 5098

    def test_raises_when_full(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        registry = {
            str(p): _make_record(str(p), tailnet_port=p) for p in range(5001, 5100)
        }
        with pytest.raises(StxError, match="in use"):
            allocate_tailnet_port(registry)

    def test_independent_of_redis_index(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        registry = {"a": _make_record("a", redis_index=None, tailnet_port=5099)}
        assert allocate_tailnet_port(registry) == 5098

    def test_skips_a_host_busy_port(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        # 5099 is free in the registry but busy on the host.
        assert allocate_tailnet_port({}, is_free=lambda p: p != 5099) == 5098


class TestAllocateRoutePorts:
    def test_picks_top_of_pool_when_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        assert allocate_route_ports({}, 1) == [5599]

    def test_picks_count_highest_free_top_down(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        assert allocate_route_ports({}, 3) == [5599, 5598, 5597]

    def test_skips_ports_already_in_use(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        registry = {"a": _make_record("a", route_ports={"api": 5599})}
        assert allocate_route_ports(registry, 1) == [5598]

    def test_counts_ports_across_every_sandbox(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        registry = {
            "a": _make_record("a", route_ports={"api": 5599}),
            "b": _make_record("b", route_ports={"admin": 5598}),
        }
        assert allocate_route_ports(registry, 1) == [5597]

    def test_raises_when_not_enough_free_ports_remain(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        registry = {"a": _make_record("a", route_ports={"api": 5599})}
        with pytest.raises(StxError, match="Not enough"):
            allocate_route_ports(registry, 10_000)

    def test_independent_of_tailnet_port(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        registry = {"a": _make_record("a", tailnet_port=5599)}
        assert allocate_route_ports(registry, 1) == [5599]

    def test_skips_host_busy_ports(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        # 5599 and 5598 are free in the registry but busy on the host, so the
        # allocator must probe past them rather than hand out a doomed port.
        busy = {5599, 5598}
        assert allocate_route_ports({}, 1, is_free=lambda p: p not in busy) == [5597]

    def test_port_bindable_true_for_a_free_port_false_for_a_bound_one(self):
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
            held.bind(("", 0))
            taken = held.getsockname()[1]
            assert not port_bindable(taken)
        # Once released, the same number binds again (SO_REUSEADDR-free probe
        # may briefly see TIME_WAIT; a fresh ephemeral number avoids that).
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as free:
            free.bind(("", 0))
            free_port = free.getsockname()[1]
        assert port_bindable(free_port)


class TestLockedRegistry:
    def test_reserve_sets_provisioning_state(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))

        with locked_registry() as reg:
            rec = reg.reserve(
                "fix-447",
                created="2026-06-26T00:00:00+00:00",
                redis_index=5,
                tailnet_port=5050,
                route_ports={"api": 5599},
                services=["postgres", "redis"],
                repo="widgetshop",
                repo_root="/tmp/widgetshop",
            )
            assert rec.state == SandboxState.PROVISIONING

        loaded = load_registry()
        assert loaded["fix-447"].state == SandboxState.PROVISIONING
        assert loaded["fix-447"].redis_index == 5
        assert loaded["fix-447"].route_ports == {"api": 5599}

    def test_reserve_records_databases_from_the_start(self, tmp_path, monkeypatch):
        """Recorded at `reserve()` time, not only once `finish()` runs — so a
        sandbox stuck mid-provisioning (crash between reserve and finish)
        still has its database names in the registry for `rm` to use. See
        docs/reference.md (Teardown guards)."""
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))

        with locked_registry() as reg:
            reg.reserve(
                "fix-447",
                created="now",
                redis_index=None,
                tailnet_port=None,
                route_ports={},
                services=[],
                repo="",
                repo_root="",
                databases=("stx_fix_447", "stx_fix_447_test"),
            )

        assert load_registry()["fix-447"].databases == (
            "stx_fix_447",
            "stx_fix_447_test",
        )

    def test_reserve_defaults_databases_to_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))

        with locked_registry() as reg:
            reg.reserve(
                "fix-447",
                created="now",
                redis_index=None,
                tailnet_port=None,
                route_ports={},
                services=[],
                repo="",
                repo_root="",
            )

        assert load_registry()["fix-447"].databases == ()

    def test_reserve_raises_if_slug_taken(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        save_registry({"fix-447": _make_record("fix-447")})

        with (
            locked_registry() as reg,
            pytest.raises(StxError, match="already registered"),
        ):
            reg.reserve(
                "fix-447",
                created="now",
                redis_index=None,
                tailnet_port=None,
                route_ports={},
                services=[],
                repo="",
                repo_root="",
            )

    def test_reserve_records_box_names_from_the_start(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))

        with locked_registry() as reg:
            reg.reserve(
                "fix-447",
                created="now",
                redis_index=None,
                tailnet_port=None,
                route_ports={},
                services=[],
                repo="",
                repo_root="",
                box_names=("app",),
            )

        assert load_registry()["fix-447"].box_names == ("app",)


class TestReserveCollisions:
    """`reserve()`'s under-the-lock collision check — see docs/reference.md
    (Teardown guards isn't the right section; this is purely a `new`-time
    refusal, nothing to tear down)."""

    def _reserve(self, reg, slug, **overrides):
        kwargs = dict(
            created="now",
            redis_index=None,
            tailnet_port=None,
            route_ports={},
            services=[],
            repo="",
            repo_root="",
        )
        kwargs.update(overrides)
        return reg.reserve(slug, **kwargs)

    def test_rendered_database_name_collides_across_different_slugs(
        self, tmp_path, monkeypatch
    ):
        # The documented example: slug 'a-test's own base db ('stx_a_test')
        # equals slug 'a' with a '{db}_test' template entry.
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        with locked_registry() as reg:
            self._reserve(reg, "a-test", databases=("stx_a_test",))

        with (
            locked_registry() as reg,
            pytest.raises(StxError, match="collide.*'a-test'.*database.*stx_a_test"),
        ):
            self._reserve(reg, "a", databases=("stx_a", "stx_a_test"))

    def test_container_and_network_names_never_collide_for_distinct_slugs(
        self, tmp_path, monkeypatch
    ):
        # Two distinct, grammar-valid slugs never produce the same
        # `stx-<slug>--net` string — sanity check that reserve() doesn't
        # false-positive on unrelated sandboxes.
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        with locked_registry() as reg:
            self._reserve(reg, "foo", box_names=("app",))

        with locked_registry() as reg:
            self._reserve(reg, "bar", box_names=("app",))

        assert set(load_registry()) == {"foo", "bar"}

    def test_refuses_a_container_name_longer_than_63_chars(self, tmp_path, monkeypatch):
        # Both lengths are individually valid (slug <= 40, box <= 20) but the
        # derived `stx-<slug>--<box>` container name (a DNS label) can reach 66
        # chars — past the 63-char limit. reserve() must refuse, not hand out a
        # name docker/DNS silently truncates or rejects later.
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        long_slug = "a" * 40
        long_box = "b" * 20
        with (
            pytest.raises(StxError, match="63"),
            locked_registry() as reg,
        ):
            self._reserve(reg, long_slug, box_names=(long_box,))

    def test_allows_a_container_name_at_the_limit(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        # stx-<slug>--<box> == 6 + len(slug) + len(box); 57 total fits in 63.
        with locked_registry() as reg:
            self._reserve(reg, "a" * 40, box_names=("b" * 17,))
        assert set(load_registry()) == {"a" * 40}


class TestLockedRegistryLifecycle:
    def test_unreserve_removes_entry_and_frees_its_index(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))

        with locked_registry() as reg:
            reg.reserve(
                "fix-447",
                created="now",
                redis_index=15,
                tailnet_port=None,
                route_ports={},
                services=[],
                repo="",
                repo_root="",
            )

        with locked_registry() as reg:
            reg.unreserve("fix-447")

        assert load_registry() == {}
        assert allocate_redis_index(load_registry()) == 15

    def test_unreserve_frees_its_route_ports(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))

        with locked_registry() as reg:
            reg.reserve(
                "fix-447",
                created="now",
                redis_index=None,
                tailnet_port=None,
                route_ports={"api": 5599},
                services=[],
                repo="",
                repo_root="",
            )

        with locked_registry() as reg:
            reg.unreserve("fix-447")

        assert load_registry() == {}
        assert allocate_route_ports(load_registry(), 1) == [5599]

    def test_finish_sets_ready_and_records_boxes(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))

        with locked_registry() as reg:
            reg.reserve(
                "fix-447",
                created="now",
                redis_index=None,
                tailnet_port=None,
                route_ports={},
                services=[],
                repo="widgetshop",
                repo_root="/tmp/widgetshop",
            )

        boxes = {"app": BoxRecord(zone=Zone.TRUSTED, user="devuser")}
        with locked_registry() as reg:
            reg.finish("fix-447", boxes)

        loaded = load_registry()
        assert loaded["fix-447"].state == SandboxState.READY
        assert loaded["fix-447"].boxes["app"].user == "devuser"

    def test_remove_drops_entry(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        save_registry({"fix-447": _make_record("fix-447")})

        with locked_registry() as reg:
            reg.remove("fix-447")

        assert load_registry() == {}

    def test_exception_inside_block_does_not_persist_changes(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))

        with pytest.raises(ValueError), locked_registry() as reg:
            reg.reserve(
                "fix-447",
                created="now",
                redis_index=None,
                tailnet_port=None,
                route_ports={},
                services=[],
                repo="",
                repo_root="",
            )
            raise ValueError("boom")

        assert load_registry() == {}

    def test_concurrent_reservations_do_not_collide_on_redis_index(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        results: list[int | None] = []
        errors: list[Exception] = []

        def reserve(slug: str) -> None:
            try:
                with locked_registry() as reg:
                    redis_index = allocate_redis_index(reg.all())
                    rec = reg.reserve(
                        slug,
                        created="now",
                        redis_index=redis_index,
                        tailnet_port=None,
                        route_ports={},
                        services=["redis"],
                        repo="widgetshop",
                        repo_root="/tmp/widgetshop",
                    )
                    results.append(rec.redis_index)
            except Exception as e:  # noqa: BLE001 — surfaced to the main thread below
                errors.append(e)

        threads = [
            threading.Thread(target=reserve, args=(f"slug-{i}",)) for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert len(results) == len(set(results)) == 5

    def test_all_exposes_underlying_dict(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path))
        save_registry({"fix-447": _make_record("fix-447")})

        with locked_registry() as reg:
            assert "fix-447" in reg
            assert reg["fix-447"].slug == "fix-447"
            assert set(reg.all()) == {"fix-447"}


class TestContainerNameHelpers:
    def test_default_box_prefers_app(self):
        from stx.core.registry import default_box

        record = _make_record("fix-447")
        assert default_box(record) == "app"

    def test_default_box_falls_back_to_dmz(self):
        from stx.core.registry import default_box

        record = SandboxRecord(
            slug="triage",
            created="now",
            state=SandboxState.READY,
            redis_index=None,
            tailnet_port=None,
            route_ports={},
            services=[],
            boxes={"dmz": BoxRecord(zone=Zone.DMZ)},
        )
        assert default_box(record) == "dmz"


class TestSandboxLiveBoxes:
    def _record_with_dmz(self) -> SandboxRecord:
        return SandboxRecord(
            slug="fix-447",
            created="now",
            state=SandboxState.READY,
            redis_index=None,
            tailnet_port=None,
            route_ports={},
            services=[],
            boxes={
                "app": BoxRecord(zone=Zone.TRUSTED, user="dev"),
                "dmz": BoxRecord(zone=Zone.DMZ, user="dev"),
            },
        )

    def test_no_dispatch_ever_started_is_not_live(self):
        record = self._record_with_dmz()
        with patch(f"{_MOD}.read_agent_status", return_value=None):
            assert sandbox_live_boxes("fix-447", record) == []

    def test_working_and_pid_alive_is_live(self):
        record = self._record_with_dmz()
        with (
            patch(f"{_MOD}.read_agent_status", return_value=_WORKING_STATUS),
            patch(f"{_MOD}.agent_pid_alive", return_value=True),
        ):
            assert sandbox_live_boxes("fix-447", record) == ["app", "dmz"]

    def test_finished_dispatch_with_a_dead_pid_is_not_live(self):
        record = self._record_with_dmz()
        with (
            patch(f"{_MOD}.read_agent_status", return_value=_DONE_STATUS),
            patch(f"{_MOD}.agent_pid_alive", return_value=False),
        ):
            assert sandbox_live_boxes("fix-447", record) == []

    def test_only_the_box_with_a_live_pid_is_reported(self):
        record = self._record_with_dmz()

        def _status(container, user):
            return _WORKING_STATUS if container.endswith("-dmz") else None

        with (
            patch(f"{_MOD}.read_agent_status", side_effect=_status),
            patch(f"{_MOD}.agent_pid_alive", return_value=True),
        ):
            assert sandbox_live_boxes("fix-447", record) == ["dmz"]


class TestLinkersOf:
    def _record_linking_to(self, target: str) -> SandboxRecord:
        record = _make_record("web-1")
        record.boxes["app"].links = {
            "api": LinkRecord(
                target=target, route="api", port=8080, container=f"stx-{target}--app"
            )
        }
        return record

    def test_finds_a_single_linker(self):
        registry = {
            "be-1": _make_record("be-1"),
            "web-1": self._record_linking_to("be-1"),
        }
        assert linkers_of("be-1", registry) == ["web-1"]

    def test_no_linkers_is_empty(self):
        registry = {"be-1": _make_record("be-1"), "web-1": _make_record("web-1")}
        assert linkers_of("be-1", registry) == []

    def test_a_linker_to_a_different_target_is_excluded(self):
        registry = {
            "be-1": _make_record("be-1"),
            "be-2": _make_record("be-2"),
            "web-1": self._record_linking_to("be-2"),
        }
        assert linkers_of("be-1", registry) == []

    def test_dmz_only_sandbox_is_never_a_linker(self):
        dmz_only = SandboxRecord(
            slug="triage",
            created="2026-07-02T00:00:00+00:00",
            state=SandboxState.READY,
            redis_index=None,
            tailnet_port=None,
            route_ports={},
            services=[],
            boxes={"dmz": BoxRecord(zone=Zone.DMZ, user="devuser")},
        )
        registry = {"be-1": _make_record("be-1"), "triage": dmz_only}
        assert linkers_of("be-1", registry) == []
