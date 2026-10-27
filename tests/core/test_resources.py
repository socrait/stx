from unittest.mock import MagicMock

import psycopg
import pytest
import redis as redis_lib

import stx.core.resources as _resources
from stx.core.resources import (
    drop_databases,
    ensure_provisioner_role,
    existing_databases,
    flush_redis,
    list_sandbox_databases,
)
from stx.errors import StxError
from stx.platform import PostgresAdminParams, RedisAdminParams


def _pg_params() -> PostgresAdminParams:
    return PostgresAdminParams(host="localhost", port=5432, user="admin", password="x")


def _provisioner_params() -> PostgresAdminParams:
    return PostgresAdminParams(
        host="localhost", port=5432, user="stx_provisioner", password="derived"
    )


def _redis_params() -> RedisAdminParams:
    return RedisAdminParams(host="localhost", port=6379, user="admin", password="x")


class TestExistingDatabases:
    def test_empty_list_returns_empty(self):
        assert existing_databases([], _pg_params()) == []

    def test_returns_every_database_present(self, monkeypatch):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = (1,)
        conn.__enter__.return_value = conn
        monkeypatch.setattr(_resources.psycopg, "connect", lambda **kw: conn)

        assert existing_databases(["stx_foo", "stx_foo_test"], _pg_params()) == [
            "stx_foo",
            "stx_foo_test",
        ]

    def test_returns_only_the_ones_present(self, monkeypatch):
        conn = MagicMock()
        rows = iter([(1,), None])
        conn.execute.return_value.fetchone.side_effect = lambda: next(rows)
        conn.__enter__.return_value = conn
        monkeypatch.setattr(_resources.psycopg, "connect", lambda **kw: conn)

        assert existing_databases(["stx_foo", "stx_bar"], _pg_params()) == ["stx_foo"]

    def test_empty_when_none_present(self, monkeypatch):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        conn.__enter__.return_value = conn
        monkeypatch.setattr(_resources.psycopg, "connect", lambda **kw: conn)

        assert existing_databases(["stx_foo"], _pg_params()) == []

    def test_raises_on_connection_failure(self, monkeypatch):
        def _raise(**kw):
            raise psycopg.OperationalError("connection refused")

        monkeypatch.setattr(_resources.psycopg, "connect", _raise)

        with pytest.raises(StxError, match="could not connect to postgres"):
            existing_databases(["stx_foo"], _pg_params())

    def test_passes_connect_timeout(self, monkeypatch):
        captured = {}

        def _connect(**kw):
            captured.update(kw)
            raise psycopg.OperationalError("boom")

        monkeypatch.setattr(_resources.psycopg, "connect", _connect)

        with pytest.raises(StxError):
            existing_databases(["stx_foo"], _pg_params())

        assert captured["connect_timeout"] == 5


class TestListSandboxDatabases:
    def test_returns_matching_database_names(self, monkeypatch):
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = [
            ("stx_foo_test",),
            ("stx_foo",),
        ]
        conn.__enter__.return_value = conn
        monkeypatch.setattr(_resources.psycopg, "connect", lambda **kw: conn)

        assert list_sandbox_databases("foo", _pg_params()) == [
            "stx_foo",
            "stx_foo_test",
        ]

    def test_escapes_like_wildcards_in_the_pattern(self, monkeypatch):
        # A slug is validated to [a-z0-9-]+ elsewhere, but db_name() turns
        # hyphens into underscores — a LIKE wildcard — so the query must
        # escape them or "fix-447" would also match "stxXfixX447-anything".
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = []
        conn.__enter__.return_value = conn
        monkeypatch.setattr(_resources.psycopg, "connect", lambda **kw: conn)

        list_sandbox_databases("fix-447", _pg_params())

        (query, params), _kwargs = conn.execute.call_args
        assert params == (r"stx\_fix\_447%",)
        assert "ESCAPE" in query

    def test_empty_when_nothing_matches(self, monkeypatch):
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = []
        conn.__enter__.return_value = conn
        monkeypatch.setattr(_resources.psycopg, "connect", lambda **kw: conn)

        assert list_sandbox_databases("foo", _pg_params()) == []

    def test_raises_on_connection_failure(self, monkeypatch):
        def _raise(**kw):
            raise psycopg.OperationalError("connection refused")

        monkeypatch.setattr(_resources.psycopg, "connect", _raise)

        with pytest.raises(StxError, match="could not list databases"):
            list_sandbox_databases("foo", _pg_params())


class TestDropDatabases:
    def test_empty_list_is_a_noop(self, monkeypatch):
        monkeypatch.setattr(
            _resources.psycopg,
            "connect",
            lambda **kw: (_ for _ in ()).throw(AssertionError("should not connect")),
        )
        drop_databases([], _pg_params())

    def test_drops_every_named_database(self, monkeypatch):
        conn = MagicMock()
        conn.__enter__.return_value = conn
        monkeypatch.setattr(_resources.psycopg, "connect", lambda **kw: conn)

        drop_databases(["stx_foo", "stx_foo_test"], _pg_params())

        executed = [c.args[0].as_string(None) for c in conn.execute.call_args_list]
        assert any("stx_foo" in q for q in executed)
        assert any("stx_foo_test" in q for q in executed)

    def test_quotes_the_database_name_as_an_identifier(self, monkeypatch):
        # A name reaching here isn't guaranteed safe by this function's own
        # contract (manifest.render_databases validates its own callers'
        # input, but this is a lower-layer function) — the identifier must
        # be quoted via psycopg.sql, not interpolated into raw SQL text.
        conn = MagicMock()
        conn.__enter__.return_value = conn
        monkeypatch.setattr(_resources.psycopg, "connect", lambda **kw: conn)

        drop_databases(['stx_foo"; DROP TABLE users; --'], _pg_params())

        (composed,) = conn.execute.call_args_list[0].args
        assert isinstance(composed, psycopg.sql.Composed)
        rendered = composed.as_string(None)
        assert rendered == 'DROP DATABASE IF EXISTS "stx_foo""; DROP TABLE users; --"'

    def test_raises_on_connection_failure(self, monkeypatch):
        def _raise(**kw):
            raise psycopg.OperationalError("connection refused")

        monkeypatch.setattr(_resources.psycopg, "connect", _raise)

        with pytest.raises(StxError, match="could not drop database"):
            drop_databases(["stx_foo"], _pg_params())


class TestEnsureProvisionerRole:
    def test_creates_the_role_when_absent(self, monkeypatch):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        conn.__enter__.return_value = conn
        monkeypatch.setattr(_resources.psycopg, "connect", lambda **kw: conn)

        ensure_provisioner_role(_pg_params(), _provisioner_params())

        (composed,) = conn.execute.call_args_list[1].args
        rendered = composed.as_string(None)
        assert rendered.startswith("CREATE ROLE")
        assert "stx_provisioner" in rendered
        assert "CREATEDB CREATEROLE" in rendered
        assert "derived" in rendered

    def test_alters_the_password_when_already_present(self, monkeypatch):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = (1,)
        conn.__enter__.return_value = conn
        monkeypatch.setattr(_resources.psycopg, "connect", lambda **kw: conn)

        ensure_provisioner_role(_pg_params(), _provisioner_params())

        (composed,) = conn.execute.call_args_list[1].args
        rendered = composed.as_string(None)
        assert rendered.startswith("ALTER ROLE")

    def test_never_a_superuser_grant(self, monkeypatch):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        conn.__enter__.return_value = conn
        monkeypatch.setattr(_resources.psycopg, "connect", lambda **kw: conn)

        ensure_provisioner_role(_pg_params(), _provisioner_params())

        (composed,) = conn.execute.call_args_list[1].args
        assert "SUPERUSER" not in composed.as_string(None)

    def test_connects_as_the_admin_not_the_provisioner(self, monkeypatch):
        captured = {}

        def _connect(**kw):
            captured.update(kw)
            conn = MagicMock()
            conn.execute.return_value.fetchone.return_value = None
            conn.__enter__.return_value = conn
            return conn

        monkeypatch.setattr(_resources.psycopg, "connect", _connect)

        ensure_provisioner_role(_pg_params(), _provisioner_params())

        assert captured["user"] == "admin"

    def test_raises_on_connection_failure(self, monkeypatch):
        def _raise(**kw):
            raise psycopg.OperationalError("connection refused")

        monkeypatch.setattr(_resources.psycopg, "connect", _raise)

        with pytest.raises(StxError, match="stx_provisioner"):
            ensure_provisioner_role(_pg_params(), _provisioner_params())


class TestFlushRedis:
    def test_flushes_the_given_index(self, monkeypatch):
        client = MagicMock()
        monkeypatch.setattr(_resources.redis_lib, "Redis", lambda **kw: client)

        flush_redis(5, _redis_params())

        client.flushdb.assert_called_once()
        client.close.assert_called_once()

    def test_raises_on_connection_failure(self, monkeypatch):
        client = MagicMock()
        client.flushdb.side_effect = redis_lib.ConnectionError("connection refused")
        monkeypatch.setattr(_resources.redis_lib, "Redis", lambda **kw: client)

        with pytest.raises(StxError, match="could not flush redis"):
            flush_redis(5, _redis_params())

    def test_closes_client_even_when_flush_fails(self, monkeypatch):
        client = MagicMock()
        client.flushdb.side_effect = redis_lib.ConnectionError("boom")
        monkeypatch.setattr(_resources.redis_lib, "Redis", lambda **kw: client)

        with pytest.raises(StxError):
            flush_redis(5, _redis_params())

        client.close.assert_called_once()

    def test_passes_socket_connect_timeout(self, monkeypatch):
        captured = {}

        def _redis(**kw):
            captured.update(kw)
            return MagicMock()

        monkeypatch.setattr(_resources.redis_lib, "Redis", _redis)

        flush_redis(5, _redis_params())

        assert captured["socket_connect_timeout"] == 5
