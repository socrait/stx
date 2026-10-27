import argparse
from unittest.mock import MagicMock, patch

from stx.commands.start import SandboxStartCommand, stopped_box_hint
from stx.core import BoxRecord, SandboxRecord, SandboxState, SetupState, SetupStatus
from stx.errors import StxError

_MOD = "stx.commands.start"
# The network-attach helper now lives in the platform package; patch its own
# network seam there (the same shared stx.network module start reads through).
_PLAT = "stx.platform"


def _args(slug: str = "my-slug", *, timeout: float = 900.0) -> argparse.Namespace:
    return argparse.Namespace(slug=slug, timeout=timeout)


def _ok() -> MagicMock:
    return MagicMock(returncode=0, stdout="", stderr="")


def _failed(stderr: str) -> MagicMock:
    return MagicMock(returncode=1, stdout="", stderr=stderr)


def _record(
    *,
    boxes: dict[str, BoxRecord] | None = None,
    services: list[str] | None = None,
) -> SandboxRecord:
    return SandboxRecord(
        slug="my-slug",
        created="2026-06-26T00:00:00+00:00",
        state=SandboxState.READY,
        redis_index=None,
        tailnet_port=None,
        services=services or [],
        boxes=boxes
        if boxes is not None
        else {"app": BoxRecord(zone="trusted", user="devuser")},
    )


class TestStoppedBoxHint:
    def test_names_the_slug_and_the_fix(self):
        hint = stopped_box_hint("my-slug")
        assert "my-slug" in hint
        assert "stx start my-slug" in hint


class TestSandboxStartCommand:
    def test_errors_when_sandbox_not_registered(self, capsys):
        with patch(f"{_MOD}.load_registry", return_value={}):
            result = SandboxStartCommand().execute(_args("ghost"))

        assert result == 1
        assert "no sandbox named 'ghost'" in capsys.readouterr().err

    def test_starts_every_stopped_box_container(self):
        record = _record(
            boxes={
                "app": BoxRecord(zone="trusted", user="devuser"),
                "dmz": BoxRecord(zone="dmz", user="dmzuser"),
            }
        )
        started = []
        with (
            patch(f"{_MOD}.load_registry", return_value={"my-slug": record}),
            patch(f"{_MOD}.container_status", return_value="exited"),
            patch(
                f"{_MOD}.run_bounded",
                lambda args, **kw: started.append(args) or _ok(),
            ),
            patch(f"{_PLAT}.stx_network.ensure_network"),
            patch(
                f"{_MOD}.wait_for_setup",
                return_value=SetupStatus(state=SetupState.DONE),
            ),
        ):
            SandboxStartCommand().execute(_args())

        assert ["docker", "start", "stx-my-slug--app"] in started
        assert ["docker", "start", "stx-my-slug--dmz"] in started

    def test_already_running_container_is_not_restarted(self):
        record = _record()
        with (
            patch(f"{_MOD}.load_registry", return_value={"my-slug": record}),
            patch(f"{_MOD}.container_status", return_value="running"),
            patch(f"{_MOD}.run_bounded") as run_bounded,
            patch(f"{_PLAT}.stx_network.ensure_network"),
            patch(
                f"{_MOD}.wait_for_setup",
                return_value=SetupStatus(state=SetupState.DONE),
            ),
        ):
            result = SandboxStartCommand().execute(_args())

        assert result is None
        run_bounded.assert_not_called()

    def test_missing_container_raises_naming_new_or_refresh(self, capsys):
        record = _record()
        with (
            patch(f"{_MOD}.load_registry", return_value={"my-slug": record}),
            patch(f"{_PLAT}.stx_network.ensure_network"),
            patch(f"{_MOD}.container_status", return_value=None),
        ):
            result = SandboxStartCommand().execute(_args())

        assert result == 1
        err = capsys.readouterr().err
        assert "doesn't exist" in err
        assert "stx new" in err or "stx refresh" in err

    def test_docker_start_failure_is_reported(self, capsys):
        record = _record()
        with (
            patch(f"{_MOD}.load_registry", return_value={"my-slug": record}),
            patch(f"{_PLAT}.stx_network.ensure_network"),
            patch(f"{_MOD}.container_status", return_value="exited"),
            patch(f"{_MOD}.run_bounded", return_value=_failed("boom")),
        ):
            result = SandboxStartCommand().execute(_args())

        assert result == 1
        assert "boom" in capsys.readouterr().err

    def test_reattaches_needed_platform_services_skipping_traefik(self):
        record = _record(services=["postgres", "redis", "traefik"])
        connected = []
        with (
            patch(f"{_MOD}.load_registry", return_value={"my-slug": record}),
            patch(f"{_MOD}.container_status", return_value="running"),
            patch(f"{_PLAT}.stx_network.ensure_network") as ensure_network,
            patch(
                f"{_PLAT}.stx_network.connect",
                lambda net, container: connected.append((net, container)),
            ),
            patch(
                f"{_MOD}.wait_for_setup",
                return_value=SetupStatus(state=SetupState.DONE),
            ),
        ):
            SandboxStartCommand().execute(_args())

        ensure_network.assert_called_once_with("stx-my-slug--net")
        assert connected == [
            ("stx-my-slug--net", "stx-postgres"),
            ("stx-my-slug--net", "stx-redis"),
        ]

    def test_waits_for_setup_on_the_trusted_box_and_reports_success(self, capsys):
        record = _record()
        with (
            patch(f"{_MOD}.load_registry", return_value={"my-slug": record}),
            patch(f"{_MOD}.container_status", return_value="running"),
            patch(f"{_PLAT}.stx_network.ensure_network"),
            patch(f"{_MOD}.wait_for_setup") as wait_for_setup,
        ):
            wait_for_setup.return_value = SetupStatus(state=SetupState.DONE)
            result = SandboxStartCommand().execute(_args())

        assert result is None
        wait_for_setup.assert_called_once_with("stx-my-slug--app", "app", timeout=900.0)
        assert "started" in capsys.readouterr().out

    def test_setup_timeout_is_reported_as_an_error(self, capsys):
        record = _record()
        with (
            patch(f"{_MOD}.load_registry", return_value={"my-slug": record}),
            patch(f"{_MOD}.container_status", return_value="running"),
            patch(f"{_PLAT}.stx_network.ensure_network"),
            patch(f"{_MOD}.wait_for_setup", side_effect=StxError("timed out after...")),
        ):
            result = SandboxStartCommand().execute(_args())

        assert result == 1
        assert "timed out" in capsys.readouterr().err

    def test_setup_failure_is_reported_as_an_error(self, capsys):
        record = _record()
        with (
            patch(f"{_MOD}.load_registry", return_value={"my-slug": record}),
            patch(f"{_MOD}.container_status", return_value="running"),
            patch(f"{_PLAT}.stx_network.ensure_network"),
            patch(
                f"{_MOD}.wait_for_setup",
                return_value=SetupStatus(
                    state=SetupState.FAILED, script="setup.sh", exit_code=7
                ),
            ),
        ):
            result = SandboxStartCommand().execute(_args())

        assert result == 1
        err = capsys.readouterr().err
        assert "setup.sh" in err
        assert "7" in err

    def test_ensures_the_network_before_starting_containers(self):
        # A box attaches to its network when its container starts, so the
        # network must already exist — ensure it first, not after.
        record = _record(services=["postgres"])
        order = []
        with (
            patch(f"{_MOD}.load_registry", return_value={"my-slug": record}),
            patch(f"{_MOD}.container_status", return_value="exited"),
            patch(
                f"{_PLAT}.stx_network.ensure_network",
                side_effect=lambda net: order.append("ensure"),
            ),
            patch(f"{_PLAT}.stx_network.connect"),
            patch(
                f"{_MOD}.run_bounded",
                side_effect=lambda args, **kw: order.append("start") or _ok(),
            ),
            patch(
                f"{_MOD}.wait_for_setup",
                return_value=SetupStatus(state=SetupState.DONE),
            ),
        ):
            SandboxStartCommand().execute(_args())

        assert order == ["ensure", "start"]

    def test_dmz_only_sandbox_never_creates_a_network(self):
        # A dmz-only sandbox has no network of its own — start must not create
        # one that nothing would ever remove.
        record = _record(boxes={"dmz": BoxRecord(zone="dmz", user="dmzuser")})
        ensured = []
        with (
            patch(f"{_MOD}.load_registry", return_value={"my-slug": record}),
            patch(f"{_MOD}.container_status", return_value="exited"),
            patch(
                f"{_PLAT}.stx_network.ensure_network",
                side_effect=lambda net: ensured.append(net),
            ),
            patch(f"{_MOD}.run_bounded", return_value=_ok()),
        ):
            result = SandboxStartCommand().execute(_args())

        assert result is None
        assert ensured == []

    def test_stuck_entry_with_no_boxes_still_ensures_its_network(self):
        # A crashed `new` leaves boxes={} but recorded box_names — it does have
        # a network, so start must ensure it (gated on box_names, not boxes).
        record = _record(boxes={}, services=["postgres"])
        record.box_names = ("app",)
        ensured = []
        with (
            patch(f"{_MOD}.load_registry", return_value={"my-slug": record}),
            patch(f"{_MOD}.container_status", return_value="exited"),
            patch(
                f"{_PLAT}.stx_network.ensure_network",
                side_effect=lambda net: ensured.append(net),
            ),
            patch(f"{_PLAT}.stx_network.connect"),
            patch(f"{_MOD}.run_bounded", return_value=_ok()),
        ):
            SandboxStartCommand().execute(_args())

        assert ensured == ["stx-my-slug--net"]

    def test_dmz_only_sandbox_skips_the_setup_wait(self):
        record = _record(boxes={"dmz": BoxRecord(zone="dmz", user="dmzuser")})
        with (
            patch(f"{_MOD}.load_registry", return_value={"my-slug": record}),
            patch(f"{_MOD}.container_status", return_value="running"),
            patch(f"{_PLAT}.stx_network.ensure_network"),
            patch(f"{_MOD}.wait_for_setup") as wait_for_setup,
        ):
            result = SandboxStartCommand().execute(_args())

        assert result is None
        wait_for_setup.assert_not_called()
