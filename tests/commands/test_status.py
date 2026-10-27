from unittest.mock import MagicMock, patch

from stx.commands.status import SandboxStatusCommand
from stx.platform import ALL_SERVICES, DriftState, ServiceDrift

_MOD = "stx.commands.status"


def test_classifies_every_platform_service_in_order(capsys):
    classified = []
    with (
        patch(
            f"{_MOD}.stx_platform.classify_services",
            side_effect=lambda services: classified.extend(services)
            or [ServiceDrift(s, DriftState.MATCHING) for s in services],
        ),
        patch(f"{_MOD}.load_registry", return_value={}),
        patch(f"{_MOD}.traefik_routes_warning", return_value=None),
    ):
        result = SandboxStatusCommand().execute(MagicMock())

    assert result is None
    assert classified == list(ALL_SERVICES)
    out = capsys.readouterr().out
    for service in ALL_SERVICES:
        assert service in out


def test_prints_state_and_detail_for_each_drift_kind(capsys):
    drifts = [
        ServiceDrift("traefik", DriftState.MATCHING),
        ServiceDrift("postgres", DriftState.MISSING),
        ServiceDrift("redis", DriftState.STOPPED),
        ServiceDrift("otel", DriftState.DIFFERING, "image changed"),
    ]
    with (
        patch(f"{_MOD}.stx_platform.classify_services", return_value=drifts),
        patch(f"{_MOD}.load_registry", return_value={}),
        patch(f"{_MOD}.traefik_routes_warning", return_value=None),
    ):
        result = SandboxStatusCommand().execute(MagicMock())

    assert result is None
    out = capsys.readouterr().out
    assert "traefik: matching" in out
    assert "postgres: missing" in out
    assert "redis: stopped" in out
    assert "otel: differing (image changed)" in out


def test_shows_the_traefik_routes_mismatch_warning(capsys):
    with (
        patch(
            f"{_MOD}.stx_platform.classify_services",
            return_value=[
                ServiceDrift("traefik", DriftState.DIFFERING, "config changed")
            ],
        ),
        patch(f"{_MOD}.load_registry", return_value={}),
        patch(
            f"{_MOD}.traefik_routes_warning",
            return_value="warning: 'stx-traefik' mount mismatch",
        ),
    ):
        result = SandboxStatusCommand().execute(MagicMock())

    assert result is None
    assert "mount mismatch" in capsys.readouterr().out
