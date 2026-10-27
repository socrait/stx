from __future__ import annotations

from unittest.mock import MagicMock, patch

from stx.core import BoxRecord, SandboxRecord, SandboxState
from stx.lifecycle import ContainerTeardown, TeardownPolicy, teardown_sandbox

_MOD = "stx.lifecycle.teardown"


def _clean() -> ContainerTeardown:
    return ContainerTeardown(down_ok=True, used_label_fallback=False, remaining=[])


def _unclean() -> ContainerTeardown:
    return ContainerTeardown(
        down_ok=False, used_label_fallback=True, remaining=["stx-s--app"]
    )


def _record(
    slug: str = "s",
    *,
    boxes: dict[str, BoxRecord] | None = None,
    box_names: tuple[str, ...] = ("app",),
    services: list[str] | None = None,
    redis_index: int | None = None,
    databases: tuple[str, ...] = (),
    state: SandboxState = SandboxState.READY,
) -> SandboxRecord:
    return SandboxRecord(
        slug=slug,
        created="2026-06-26T00:00:00+00:00",
        state=state,
        redis_index=redis_index,
        tailnet_port=None,
        services=services or [],
        databases=databases,
        box_names=box_names,
        boxes=boxes if boxes is not None else {"app": BoxRecord(zone="trusted")},
    )


def _run(record, *, policy, tmp_path, monkeypatch, **overrides):
    """Drive teardown_sandbox with every external seam stubbed, returning
    (result, calls) where calls records which cleanup ran."""
    monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
    calls: dict[str, list] = {
        "overlay": [],
        "egress": [],
        "disconnect": [],
        "remove_network": [],
        "drop": [],
        "flush": [],
        "route": [],
        "hosts": [],
        "worktree": [],
        "list_db": [],
    }
    kwargs = dict(
        policy=policy,
        root=overrides.get("root", tmp_path / "repo"),
        wt_path=overrides.get("wt_path"),
        databases=overrides.get("databases", record.databases),
        db_params=overrides.get("db_params", MagicMock()),
        redis_params=overrides.get("redis_params", MagicMock()),
    )
    with (
        patch(f"{_MOD}.bring_down_containers", return_value=overrides["container"]),
        patch(
            f"{_MOD}.overlay.remove", side_effect=lambda s: calls["overlay"].append(s)
        ),
        patch(f"{_MOD}.egress.remove", side_effect=lambda s: calls["egress"].append(s)),
        patch(
            f"{_MOD}.stx_network.disconnect",
            side_effect=lambda net, c: calls["disconnect"].append((net, c)),
        ),
        patch(
            f"{_MOD}.stx_network.remove_network",
            side_effect=lambda net: calls["remove_network"].append(net),
        ),
        patch(
            f"{_MOD}.drop_databases",
            side_effect=lambda dbs, p: calls["drop"].append(dbs),
        ),
        patch(f"{_MOD}.flush_redis", side_effect=lambda i, p: calls["flush"].append(i)),
        patch(
            f"{_MOD}.list_sandbox_databases",
            side_effect=lambda s, p: calls["list_db"].append(s)
            or overrides.get("existing_db", []),
        ),
        patch(
            f"{_MOD}.remove_traefik_route",
            side_effect=lambda s: calls["route"].append(s),
        ),
        patch(
            f"{_MOD}.remove_hosts_entry", side_effect=lambda s: calls["hosts"].append(s)
        ),
        patch(
            f"{_MOD}.remove_locked_worktree",
            side_effect=lambda wt, *, cwd: calls["worktree"].append((wt, cwd)) or True,
        ),
    ):
        result = teardown_sandbox(record, **kwargs)
    return result, calls


def test_clean_teardown_removes_everything(tmp_path, monkeypatch):
    record = _record(services=["postgres", "redis"], redis_index=5)
    result, calls = _run(
        record,
        policy=TeardownPolicy(),
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        container=_clean(),
        databases=("stx_s", "stx_s_test"),
    )

    assert result.clean
    assert calls["overlay"] == ["s"]
    assert calls["egress"] == ["s"]
    assert calls["remove_network"] == ["stx-s--net"]
    assert calls["drop"] == [["stx_s", "stx_s_test"]]
    assert calls["flush"] == [5]
    assert calls["route"] == ["s"]
    assert calls["hosts"] == ["s"]


def test_unclean_without_continue_stops_and_keeps_everything(tmp_path, monkeypatch):
    # rm's default (no --force): containers survived, so keep the overlay and
    # touch nothing else — the entry stays, nothing is orphaned.
    record = _record(services=["postgres"], databases=())
    result, calls = _run(
        record,
        policy=TeardownPolicy(continue_when_unclean=False),
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        container=_unclean(),
    )

    assert result.stopped_unclean
    assert not result.clean
    assert calls["overlay"] == []
    assert calls["remove_network"] == []
    assert calls["drop"] == []


def test_prune_keeps_overlay_when_down_fails_but_tears_down_the_rest(
    tmp_path, monkeypatch
):
    # Bug: prune's old zombie path skipped the down with no overlay and still
    # claimed "pruned". Now the down runs; a failed down keeps the overlay/
    # egress for a retry, but databases/network/etc still come down.
    record = _record(services=["postgres"], redis_index=3)
    result, calls = _run(
        record,
        policy=TeardownPolicy(continue_when_unclean=True),
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        container=_unclean(),
        databases=("stx_s",),
    )

    assert not result.clean  # containers survived
    assert calls["overlay"] == []  # kept
    assert calls["egress"] == []  # kept
    assert "overlay and egress allowlist" in result.left_behind
    assert calls["remove_network"] == ["stx-s--net"]  # rest still torn down
    assert calls["drop"] == [["stx_s"]]
    assert calls["flush"] == [3]


def test_force_drops_overlay_even_when_containers_survive(tmp_path, monkeypatch):
    record = _record(databases=())
    result, calls = _run(
        record,
        policy=TeardownPolicy(
            force=True, continue_when_unclean=True, drop_overlay_when_unclean=True
        ),
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        container=_unclean(),
    )

    assert calls["overlay"] == ["s"]
    assert calls["egress"] == ["s"]


def test_stuck_entry_with_no_boxes_still_has_its_network_removed(tmp_path, monkeypatch):
    # Bug: a stuck entry has boxes={}, so a `trusted is None` gate skipped the
    # network. Gate on box_names (recorded at reserve), not live boxes.
    record = _record(boxes={}, box_names=("app",), state=SandboxState.PROVISIONING)
    result, calls = _run(
        record,
        policy=TeardownPolicy(),
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        container=_clean(),
        databases=(),
    )

    assert calls["remove_network"] == ["stx-s--net"]


def test_dmz_only_sandbox_never_touches_a_network(tmp_path, monkeypatch):
    record = _record(
        boxes={"dmz": BoxRecord(zone="dmz")}, box_names=("dmz",), databases=()
    )
    result, calls = _run(
        record,
        policy=TeardownPolicy(),
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        container=_clean(),
    )

    assert calls["remove_network"] == []


def test_db_drop_failure_is_reported_not_raised(tmp_path, monkeypatch):
    from stx.errors import StxError

    record = _record(databases=("stx_s",))
    monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
    with (
        patch(f"{_MOD}.bring_down_containers", return_value=_clean()),
        patch(f"{_MOD}.overlay.remove"),
        patch(f"{_MOD}.egress.remove"),
        patch(f"{_MOD}.stx_network.disconnect"),
        patch(f"{_MOD}.stx_network.remove_network"),
        patch(f"{_MOD}.drop_databases", side_effect=StxError("connection refused")),
        patch(f"{_MOD}.remove_traefik_route"),
        patch(f"{_MOD}.remove_hosts_entry"),
    ):
        result = teardown_sandbox(
            record,
            policy=TeardownPolicy(),
            root=tmp_path,
            wt_path=None,
            databases=("stx_s",),
            db_params=MagicMock(),
            redis_params=None,
        )

    assert not result.clean
    assert any(step == "databases" for step, _ in result.failures)


def test_unknown_databases_are_listed_and_left_never_guessed(tmp_path, monkeypatch):
    record = _record(databases=())
    result, calls = _run(
        record,
        policy=TeardownPolicy(),
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        container=_clean(),
        databases=None,
        existing_db=["stx_s", "stx_s_test"],
    )

    assert calls["drop"] == []  # never guessed
    assert calls["list_db"] == ["s"]
    assert any("stx_s" in item for item in result.left_behind)


def test_redis_without_params_is_a_failure(tmp_path, monkeypatch):
    record = _record(redis_index=4, databases=())
    result, _ = _run(
        record,
        policy=TeardownPolicy(),
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        container=_clean(),
        redis_params=None,
    )

    assert any(step == "redis" for step, _ in result.failures)


def test_guard_unsaved_leaves_a_dirty_worktree(tmp_path, monkeypatch):
    wt = tmp_path / "wt"
    wt.mkdir()
    record = _record(databases=())
    with (
        patch(f"{_MOD}.bring_down_containers", return_value=_clean()),
        patch(f"{_MOD}.overlay.remove"),
        patch(f"{_MOD}.egress.remove"),
        patch(f"{_MOD}.stx_network.disconnect"),
        patch(f"{_MOD}.stx_network.remove_network"),
        patch(f"{_MOD}.remove_traefik_route"),
        patch(f"{_MOD}.remove_hosts_entry"),
        patch(f"{_MOD}.common_dir_for", return_value=tmp_path / "common"),
        patch(f"{_MOD}.has_uncommitted_changes", return_value=True),
        patch(f"{_MOD}.has_unpushed_commits", return_value=False),
        patch(
            f"{_MOD}.remove_locked_worktree",
            side_effect=AssertionError("must not remove an unsaved worktree"),
        ),
    ):
        monkeypatch.setenv("STX_STATE_ROOT", str(tmp_path / "state"))
        result = teardown_sandbox(
            record,
            policy=TeardownPolicy(guard_unsaved=True),
            root=tmp_path,
            wt_path=wt,
            databases=(),
            db_params=None,
            redis_params=None,
        )

    assert any("worktree" in item for item in result.left_behind)
    assert wt.exists()
