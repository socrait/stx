import argparse
from argparse import Namespace
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from stx.commands.ls import (
    SandboxLsCommand,
    _auth_label,
    _scope_label,
)
from stx.core import (
    AgentState,
    AgentStatus,
    BoxRecord,
    ClaudeAuthMode,
    SandboxRecord,
    SandboxState,
)
from stx.core.registry import ForgeBoxRecord, ForgePackageRecord, ForgePushRecord


def _utc_today() -> date:
    # Expiry dates are computed in UTC; a local-date comparison flips near midnight.
    return datetime.now(timezone.utc).date()


def _args(*, wide: bool = False) -> Namespace:
    return Namespace(wide=wide)


def _row_cell(out: str, slug: str, column: str) -> str:
    header_tokens = out.splitlines()[0].split()
    idx = header_tokens.index(column)
    row = next(line for line in out.splitlines() if line.startswith(slug))
    return row.split()[idx]


_MOD = "stx.commands.ls"


def test_refuses_when_in_sandbox_box(monkeypatch, capsys):
    monkeypatch.setenv("STX_ZONE", "trusted")
    with patch(f"{_MOD}.load_registry", side_effect=AssertionError("should not run")):
        result = SandboxLsCommand().execute(MagicMock())

    assert result == 1
    assert "host-only" in capsys.readouterr().err


class TestWideFlagParsing:
    def _parse(self, argv: list[str]) -> Namespace:
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        SandboxLsCommand().add_parser(subparsers)
        return parser.parse_args(argv)

    def test_defaults_to_narrow(self):
        assert self._parse(["ls"]).wide is False

    def test_long_flag_enables_wide(self):
        assert self._parse(["ls", "--wide"]).wide is True

    def test_short_flag_enables_wide(self):
        assert self._parse(["ls", "-w"]).wide is True


class TestScopeLabel:
    def test_write_repository_is_rw(self):
        assert _scope_label("write_repository") == "rw"

    def test_read_repository_is_ro(self):
        assert _scope_label("read_repository") == "ro"

    def test_empty_scope_is_dash(self):
        assert _scope_label("") == "-"


class TestAuthLabel:
    def test_api_key_is_api(self):
        assert _auth_label(ClaudeAuthMode.API_KEY) == "api"

    def test_oauth_is_oauth(self):
        assert _auth_label(ClaudeAuthMode.OAUTH) == "oauth"


def _base_record(**overrides) -> SandboxRecord:
    defaults = dict(
        slug="foo",
        created="2026-07-07T00:00:00+00:00",
        state=SandboxState.READY,
        redis_index=1,
        tailnet_port=None,
        repo="widgetshop",
        boxes={"app": BoxRecord(zone="trusted", user="devuser", branch="feat/x/foo")},
    )
    defaults.update(overrides)
    return SandboxRecord(**defaults)


class TestAuthColumn:
    def test_shows_oauth_for_oauth_auth(self, capsys):
        boxes = {
            "app": BoxRecord(
                zone="trusted",
                user="devuser",
                branch="feat/x/foo",
                claude_auth_mode=ClaudeAuthMode.OAUTH,
            )
        }
        record = _base_record(boxes=boxes)
        with (
            patch(f"{_MOD}.load_registry", return_value={"foo": record}),
            patch(f"{_MOD}._container_status", return_value="running"),
            patch(f"{_MOD}.read_agent_status", return_value=None),
        ):
            SandboxLsCommand().execute(_args(wide=True))

        out = capsys.readouterr().out
        assert _row_cell(out, "foo", "AUTH") == "oauth"

    def test_shows_api_for_api_key_auth(self, capsys):
        boxes = {
            "app": BoxRecord(
                zone="trusted",
                user="devuser",
                branch="feat/x/foo",
                claude_auth_mode=ClaudeAuthMode.API_KEY,
            )
        }
        record = _base_record(boxes=boxes)
        with (
            patch(f"{_MOD}.load_registry", return_value={"foo": record}),
            patch(f"{_MOD}._container_status", return_value="running"),
            patch(f"{_MOD}.read_agent_status", return_value=None),
        ):
            SandboxLsCommand().execute(_args(wide=True))

        out = capsys.readouterr().out
        assert _row_cell(out, "foo", "AUTH") == "api"

    def test_auth_absent_from_default_view(self, capsys):
        record = _base_record()
        with (
            patch(f"{_MOD}.load_registry", return_value={"foo": record}),
            patch(f"{_MOD}._container_status", return_value="running"),
            patch(f"{_MOD}.read_agent_status", return_value=None),
        ):
            SandboxLsCommand().execute(_args())

        header = capsys.readouterr().out.splitlines()[0]
        assert "AUTH" not in header.split()


class TestRepoColumn:
    def test_shows_repo_name(self, capsys):
        record = _base_record(repo="widgetshop")
        with (
            patch(f"{_MOD}.load_registry", return_value={"foo": record}),
            patch(f"{_MOD}._container_status", return_value="running"),
            patch(f"{_MOD}.read_agent_status", return_value=None),
        ):
            SandboxLsCommand().execute(_args(wide=True))

        out = capsys.readouterr().out
        assert _row_cell(out, "foo", "REPO") == "widgetshop"

    def test_empty_repo_is_dash(self, capsys):
        record = _base_record(repo="")
        with (
            patch(f"{_MOD}.load_registry", return_value={"foo": record}),
            patch(f"{_MOD}._container_status", return_value="running"),
            patch(f"{_MOD}.read_agent_status", return_value=None),
        ):
            SandboxLsCommand().execute(_args(wide=True))

        out = capsys.readouterr().out
        assert _row_cell(out, "foo", "REPO") == "-"


class TestDbColumn:
    def test_shows_database_when_postgres_is_used(self, capsys):
        record = _base_record(services=["traefik", "postgres"])
        with (
            patch(f"{_MOD}.load_registry", return_value={"foo": record}),
            patch(f"{_MOD}._container_status", return_value="running"),
            patch(f"{_MOD}.read_agent_status", return_value=None),
        ):
            SandboxLsCommand().execute(_args(wide=True))

        assert _row_cell(capsys.readouterr().out, "foo", "DB") == "stx_foo"

    def test_no_postgres_is_dash(self, capsys):
        record = _base_record(services=["traefik"])
        with (
            patch(f"{_MOD}.load_registry", return_value={"foo": record}),
            patch(f"{_MOD}._container_status", return_value="running"),
            patch(f"{_MOD}.read_agent_status", return_value=None),
        ):
            SandboxLsCommand().execute(_args(wide=True))

        assert _row_cell(capsys.readouterr().out, "foo", "DB") == "-"


class TestLinksColumn:
    def test_shows_name_and_target(self, capsys):
        from stx.core.registry import LinkRecord

        boxes = {
            "app": BoxRecord(
                zone="trusted",
                user="devuser",
                branch="feat/x/foo",
                links={
                    "api": LinkRecord(
                        target="be-1", route="api", port=8080, container="stx-be-1--app"
                    )
                },
            )
        }
        record = _base_record(boxes=boxes)
        with (
            patch(f"{_MOD}.load_registry", return_value={"foo": record}),
            patch(f"{_MOD}._container_status", return_value="running"),
            patch(f"{_MOD}.read_agent_status", return_value=None),
        ):
            SandboxLsCommand().execute(_args(wide=True))

        out = capsys.readouterr().out
        assert _row_cell(out, "foo", "LINKS") == "api→be-1"

    def test_no_links_is_dash(self, capsys):
        record = _base_record()
        with (
            patch(f"{_MOD}.load_registry", return_value={"foo": record}),
            patch(f"{_MOD}._container_status", return_value="running"),
            patch(f"{_MOD}.read_agent_status", return_value=None),
        ):
            SandboxLsCommand().execute(_args(wide=True))

        out = capsys.readouterr().out
        assert _row_cell(out, "foo", "LINKS") == "-"

    def test_absent_from_default_view(self, capsys):
        record = _base_record()
        with (
            patch(f"{_MOD}.load_registry", return_value={"foo": record}),
            patch(f"{_MOD}._container_status", return_value="running"),
            patch(f"{_MOD}.read_agent_status", return_value=None),
        ):
            SandboxLsCommand().execute(_args())

        header = capsys.readouterr().out.splitlines()[0]
        assert "LINKS" not in header.split()


class TestStateColumn:
    def test_state_column_shows_provisioning_state(self, capsys):
        record = _base_record(state=SandboxState.PROVISIONING)
        with (
            patch(f"{_MOD}.load_registry", return_value={"foo": record}),
            patch(f"{_MOD}._container_status", return_value="running"),
            patch(f"{_MOD}.read_agent_status", return_value=None),
        ):
            SandboxLsCommand().execute(_args(wide=True))

        out = capsys.readouterr().out
        assert _row_cell(out, "foo", "STATE") == "provisioning"


class TestPortColumn:
    def test_shows_tailnet_port(self, capsys):
        record = _base_record(tailnet_port=5099)
        with (
            patch(f"{_MOD}.load_registry", return_value={"foo": record}),
            patch(f"{_MOD}._container_status", return_value="running"),
            patch(f"{_MOD}.read_agent_status", return_value=None),
        ):
            SandboxLsCommand().execute(_args(wide=True))

        out = capsys.readouterr().out
        assert _row_cell(out, "foo", "PORT") == "5099"

    def test_no_port_is_dash(self, capsys):
        record = _base_record(tailnet_port=None)
        with (
            patch(f"{_MOD}.load_registry", return_value={"foo": record}),
            patch(f"{_MOD}._container_status", return_value="running"),
            patch(f"{_MOD}.read_agent_status", return_value=None),
        ):
            SandboxLsCommand().execute(_args(wide=True))

        out = capsys.readouterr().out
        assert _row_cell(out, "foo", "PORT") == "-"


class TestScopeColumn:
    def test_shows_rw_scope_from_forge_record(self, capsys):
        boxes = {
            "app": BoxRecord(
                zone="trusted",
                user="devuser",
                branch="feat/x/foo",
                forge=ForgeBoxRecord(
                    kind="gitlab",
                    push=ForgePushRecord(
                        scope_id=1,
                        token_id=1,
                        scope="write_repository",
                        expires_at="2026-07-12",
                    ),
                ),
            )
        }
        record = _base_record(boxes=boxes)
        with (
            patch(f"{_MOD}.load_registry", return_value={"foo": record}),
            patch(f"{_MOD}._container_status", return_value="running"),
            patch(f"{_MOD}.read_agent_status", return_value=None),
        ):
            SandboxLsCommand().execute(_args(wide=True))

        out = capsys.readouterr().out
        assert _row_cell(out, "foo", "SCOPE") == "rw"

    def test_no_forge_record_is_dash(self, capsys):
        record = _base_record()
        with (
            patch(f"{_MOD}.load_registry", return_value={"foo": record}),
            patch(f"{_MOD}._container_status", return_value="running"),
            patch(f"{_MOD}.read_agent_status", return_value=None),
        ):
            SandboxLsCommand().execute(_args(wide=True))

        out = capsys.readouterr().out
        assert _row_cell(out, "foo", "SCOPE") == "-"


class TestDispatchColumn:
    def test_no_dispatch_ever_is_dash(self, capsys):
        record = _base_record()
        with (
            patch(f"{_MOD}.load_registry", return_value={"foo": record}),
            patch(f"{_MOD}._container_status", return_value="running"),
            patch(f"{_MOD}.read_agent_status", return_value=None),
        ):
            SandboxLsCommand().execute(_args())

        out = capsys.readouterr().out
        assert _row_cell(out, "foo", "DISPATCH") == "-"

    def test_working_shows_index_and_idle_seconds(self, capsys):
        record = _base_record()
        with (
            patch(f"{_MOD}.load_registry", return_value={"foo": record}),
            patch(f"{_MOD}._container_status", return_value="running"),
            patch(
                f"{_MOD}.read_agent_status",
                return_value=AgentStatus(state=AgentState.WORKING, pid=1, index=4),
            ),
            patch(f"{_MOD}._seconds_since_mtime", return_value=8),
        ):
            SandboxLsCommand().execute(_args())

        out = capsys.readouterr().out
        assert "working #4 · 8s" in out

    def test_done_shows_index_and_exit_code(self, capsys):
        record = _base_record()
        with (
            patch(f"{_MOD}.load_registry", return_value={"foo": record}),
            patch(f"{_MOD}._container_status", return_value="running"),
            patch(
                f"{_MOD}.read_agent_status",
                return_value=AgentStatus(
                    state=AgentState.DONE, pid=1, index=4, exit_code=0
                ),
            ),
        ):
            SandboxLsCommand().execute(_args())

        out = capsys.readouterr().out
        assert "done #4 · exit 0" in out


class TestAgeColumn:
    def test_minutes_for_a_recent_sandbox(self, capsys):
        created = (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat()
        record = _base_record(created=created)
        with (
            patch(f"{_MOD}.load_registry", return_value={"foo": record}),
            patch(f"{_MOD}._container_status", return_value="running"),
            patch(f"{_MOD}.read_agent_status", return_value=None),
        ):
            SandboxLsCommand().execute(_args())

        out = capsys.readouterr().out
        assert _row_cell(out, "foo", "AGE") == "45m"

    def test_hours_for_an_older_sandbox(self, capsys):
        created = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
        record = _base_record(created=created)
        with (
            patch(f"{_MOD}.load_registry", return_value={"foo": record}),
            patch(f"{_MOD}._container_status", return_value="running"),
            patch(f"{_MOD}.read_agent_status", return_value=None),
        ):
            SandboxLsCommand().execute(_args())

        out = capsys.readouterr().out
        assert _row_cell(out, "foo", "AGE") == "5h"

    def test_days_for_a_multi_day_sandbox(self, capsys):
        created = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        record = _base_record(created=created)
        with (
            patch(f"{_MOD}.load_registry", return_value={"foo": record}),
            patch(f"{_MOD}._container_status", return_value="running"),
            patch(f"{_MOD}.read_agent_status", return_value=None),
        ):
            SandboxLsCommand().execute(_args())

        out = capsys.readouterr().out
        assert _row_cell(out, "foo", "AGE") == "3d"

    def test_under_a_minute_is_0m(self, capsys):
        created = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        record = _base_record(created=created)
        with (
            patch(f"{_MOD}.load_registry", return_value={"foo": record}),
            patch(f"{_MOD}._container_status", return_value="running"),
            patch(f"{_MOD}.read_agent_status", return_value=None),
        ):
            SandboxLsCommand().execute(_args())

        out = capsys.readouterr().out
        assert _row_cell(out, "foo", "AGE") == "0m"

    def test_unparseable_created_is_dash(self, capsys):
        record = _base_record(created="not-a-date")
        with (
            patch(f"{_MOD}.load_registry", return_value={"foo": record}),
            patch(f"{_MOD}._container_status", return_value="running"),
            patch(f"{_MOD}.read_agent_status", return_value=None),
        ):
            SandboxLsCommand().execute(_args())

        out = capsys.readouterr().out
        assert _row_cell(out, "foo", "AGE") == "-"


class TestExpiresColumn:
    def _record_with_expiry(
        self, *, push_expires_at: str, package_expires_at: str = ""
    ) -> SandboxRecord:
        boxes = {
            "app": BoxRecord(
                zone="trusted",
                user="devuser",
                branch="feat/x/foo",
                forge=ForgeBoxRecord(
                    kind="gitlab",
                    push=ForgePushRecord(
                        scope_id=1,
                        token_id=2,
                        scope="write_repository",
                        expires_at=push_expires_at,
                    ),
                ),
            )
        }
        if package_expires_at:
            boxes["app"].forge.package = ForgePackageRecord(
                scope_id=3, token_id=4, expires_at=package_expires_at
            )
        return _base_record(boxes=boxes)

    def test_shows_future_date_as_month_dash_day(self, capsys):
        future = _utc_today() + timedelta(days=11)
        record = self._record_with_expiry(push_expires_at=future.isoformat())
        with (
            patch(f"{_MOD}.load_registry", return_value={"foo": record}),
            patch(f"{_MOD}._container_status", return_value="running"),
            patch(f"{_MOD}.read_agent_status", return_value=None),
        ):
            SandboxLsCommand().execute(_args())

        out = capsys.readouterr().out
        assert (
            _row_cell(out, "foo", "EXPIRES") == f"{future.month:02d}-{future.day:02d}"
        )

    def test_shows_today_when_expiring_today(self, capsys):
        record = self._record_with_expiry(push_expires_at=_utc_today().isoformat())
        with (
            patch(f"{_MOD}.load_registry", return_value={"foo": record}),
            patch(f"{_MOD}._container_status", return_value="running"),
            patch(f"{_MOD}.read_agent_status", return_value=None),
        ):
            SandboxLsCommand().execute(_args())

        out = capsys.readouterr().out
        assert _row_cell(out, "foo", "EXPIRES") == "today"

    def test_shows_expired_for_a_past_date(self, capsys):
        past = _utc_today() - timedelta(days=1)
        record = self._record_with_expiry(push_expires_at=past.isoformat())
        with (
            patch(f"{_MOD}.load_registry", return_value={"foo": record}),
            patch(f"{_MOD}._container_status", return_value="running"),
            patch(f"{_MOD}.read_agent_status", return_value=None),
        ):
            SandboxLsCommand().execute(_args())

        out = capsys.readouterr().out
        assert _row_cell(out, "foo", "EXPIRES") == "expired"

    def test_no_tokens_is_dash(self, capsys):
        record = _base_record()
        with (
            patch(f"{_MOD}.load_registry", return_value={"foo": record}),
            patch(f"{_MOD}._container_status", return_value="running"),
            patch(f"{_MOD}.read_agent_status", return_value=None),
        ):
            SandboxLsCommand().execute(_args())

        out = capsys.readouterr().out
        assert _row_cell(out, "foo", "EXPIRES") == "-"

    def test_uses_earliest_of_push_and_package(self, capsys):
        far = _utc_today() + timedelta(days=30)
        near = _utc_today() + timedelta(days=2)
        record = self._record_with_expiry(
            push_expires_at=far.isoformat(), package_expires_at=near.isoformat()
        )
        with (
            patch(f"{_MOD}.load_registry", return_value={"foo": record}),
            patch(f"{_MOD}._container_status", return_value="running"),
            patch(f"{_MOD}.read_agent_status", return_value=None),
        ):
            SandboxLsCommand().execute(_args())

        out = capsys.readouterr().out
        assert _row_cell(out, "foo", "EXPIRES") == f"{near.month:02d}-{near.day:02d}"


class TestExpiringTokenWarning:
    def _record_with_expiry(self, expires_at: str) -> SandboxRecord:
        boxes = {
            "app": BoxRecord(
                zone="trusted",
                user="devuser",
                branch="feat/x/foo",
                forge=ForgeBoxRecord(
                    kind="gitlab",
                    push=ForgePushRecord(
                        scope_id=1,
                        token_id=1,
                        scope="write_repository",
                        expires_at=expires_at,
                    ),
                ),
            )
        }
        return _base_record(boxes=boxes)

    def test_warns_on_stderr_when_a_token_expires_soon(self, capsys):
        record = self._record_with_expiry(_utc_today().isoformat())
        with (
            patch(f"{_MOD}.load_registry", return_value={"foo": record}),
            patch(f"{_MOD}._container_status", return_value="running"),
            patch(f"{_MOD}.read_agent_status", return_value=None),
        ):
            SandboxLsCommand().execute(_args())

        err = capsys.readouterr().err
        assert "'foo'" in err
        assert "stx refresh foo" in err

    def test_no_warning_when_nothing_expires_soon(self, capsys):
        far = (_utc_today() + timedelta(days=30)).isoformat()
        record = self._record_with_expiry(far)
        with (
            patch(f"{_MOD}.load_registry", return_value={"foo": record}),
            patch(f"{_MOD}._container_status", return_value="running"),
            patch(f"{_MOD}.read_agent_status", return_value=None),
        ):
            SandboxLsCommand().execute(_args())

        assert capsys.readouterr().err == ""


class TestExpiredTokenWarning:
    def _record_with_expiry(self, expires_at: str) -> SandboxRecord:
        boxes = {
            "app": BoxRecord(
                zone="trusted",
                user="devuser",
                branch="feat/x/foo",
                forge=ForgeBoxRecord(
                    kind="gitlab",
                    push=ForgePushRecord(
                        scope_id=1,
                        token_id=1,
                        scope="write_repository",
                        expires_at=expires_at,
                    ),
                ),
            )
        }
        return _base_record(boxes=boxes)

    def test_stderr_line_when_a_token_has_expired(self, capsys):
        past = _utc_today() - timedelta(days=1)
        record = self._record_with_expiry(past.isoformat())
        with (
            patch(f"{_MOD}.load_registry", return_value={"foo": record}),
            patch(f"{_MOD}._container_status", return_value="running"),
            patch(f"{_MOD}.read_agent_status", return_value=None),
        ):
            SandboxLsCommand().execute(_args())

        err = capsys.readouterr().err
        assert "'foo' forge tokens have expired — run `stx refresh foo`" in err
        assert "expire within 24h" not in err

    def test_no_expired_line_when_nothing_has_expired(self, capsys):
        record = self._record_with_expiry(_utc_today().isoformat())
        with (
            patch(f"{_MOD}.load_registry", return_value={"foo": record}),
            patch(f"{_MOD}._container_status", return_value="running"),
            patch(f"{_MOD}.read_agent_status", return_value=None),
        ):
            SandboxLsCommand().execute(_args())

        assert "have expired" not in capsys.readouterr().err


class TestColumnSelection:
    def test_default_header_is_narrow(self, capsys):
        record = _base_record()
        with (
            patch(f"{_MOD}.load_registry", return_value={"foo": record}),
            patch(f"{_MOD}._container_status", return_value="running"),
            patch(f"{_MOD}.read_agent_status", return_value=None),
        ):
            SandboxLsCommand().execute(_args())

        header = capsys.readouterr().out.splitlines()[0]
        assert header.split() == [
            "SLUG",
            "STATUS",
            "AGE",
            "EXPIRES",
            "DISPATCH",
            "BRANCH",
        ]

    def test_wide_header_includes_every_column(self, capsys):
        record = _base_record()
        with (
            patch(f"{_MOD}.load_registry", return_value={"foo": record}),
            patch(f"{_MOD}._container_status", return_value="running"),
            patch(f"{_MOD}.read_agent_status", return_value=None),
        ):
            SandboxLsCommand().execute(_args(wide=True))

        header_cols = capsys.readouterr().out.splitlines()[0].split()
        for expected in (
            "SLUG",
            "BOXES",
            "SCOPE",
            "DB",
            "REDIS",
            "STATE",
            "STATUS",
            "AGE",
            "EXPIRES",
            "REPO",
            "PORT",
            "AUTH",
            "DISPATCH",
            "BRANCH",
        ):
            assert expected in header_cols
        assert header_cols[-1] == "BRANCH"
