from __future__ import annotations

from typing import TYPE_CHECKING

import psycopg
import redis as redis_lib
from psycopg import sql

from ..errors import StxError
from .names import db_name

if TYPE_CHECKING:
    # Deferred: ..platform imports back from ..core, so a top-level import
    # here would make import order matter.
    from ..platform import PostgresAdminParams, RedisAdminParams

# Both admin connections are host-local infrastructure (the platform's own
# Postgres/Redis) — a real outage should surface quickly, not hang the CLI.
_PG_CONNECT_TIMEOUT_S = 5
_REDIS_CONNECT_TIMEOUT_S = 5

_PROVISIONER_ROLE = "stx_provisioner"


def ensure_provisioner_role(
    admin: PostgresAdminParams, provisioner: PostgresAdminParams
) -> None:
    """Idempotently create, or repoint the password of, the non-superuser
    `stx_provisioner` role — LOGIN CREATEDB CREATEROLE only, so (Postgres 16
    semantics) it can administer only roles it itself creates. Provisioning
    connects as this role instead of the platform admin — see
    docs/secrets.md (Provisioning credentials). Raises StxError on a
    connection failure."""
    try:
        with psycopg.connect(
            host=admin.host,
            port=admin.port,
            dbname="postgres",
            user=admin.user,
            password=admin.password,
            autocommit=True,
            connect_timeout=_PG_CONNECT_TIMEOUT_S,
        ) as conn:
            exists = conn.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s", (_PROVISIONER_ROLE,)
            ).fetchone()
            verb = sql.SQL("ALTER") if exists else sql.SQL("CREATE")
            conn.execute(
                sql.SQL("{} ROLE {} WITH LOGIN CREATEDB CREATEROLE PASSWORD {}").format(
                    verb,
                    sql.Identifier(_PROVISIONER_ROLE),
                    sql.Literal(provisioner.password),
                )
            )
    except psycopg.Error as e:
        raise StxError(
            f"could not ensure the '{_PROVISIONER_ROLE}' role at "
            f"{admin.host}:{admin.port}: {e}"
        ) from e


def existing_databases(databases: list[str], params: PostgresAdminParams) -> list[str]:
    """The names from `databases` that already exist. Raises StxError on a
    connection failure — the caller decides what "unknown" means."""
    if not databases:
        return []
    try:
        with psycopg.connect(
            host=params.host,
            port=params.port,
            dbname="postgres",
            user=params.user,
            password=params.password,
            autocommit=True,
            connect_timeout=_PG_CONNECT_TIMEOUT_S,
        ) as conn:
            existing = []
            for database in databases:
                row = conn.execute(
                    "SELECT 1 FROM pg_database WHERE datname = %s", (database,)
                ).fetchone()
                if row:
                    existing.append(database)
        return existing
    except psycopg.Error as e:
        raise StxError(
            f"could not connect to postgres at {params.host}:{params.port}: {e}"
        ) from e


def list_sandbox_databases(slug: str, params: PostgresAdminParams) -> list[str]:
    """Every existing database whose name matches `stx_<slug>%` — the
    fallback when a zombie (or an unparseable registry entry) has no
    recorded/labeled database names to use instead of guessing; see
    docs/reference.md (Teardown guards). Raises StxError on a connection
    failure."""
    prefix = db_name(slug)
    like_pattern = prefix.replace("\\", "\\\\").replace("_", r"\_") + "%"
    try:
        with psycopg.connect(
            host=params.host,
            port=params.port,
            dbname="postgres",
            user=params.user,
            password=params.password,
            autocommit=True,
            connect_timeout=_PG_CONNECT_TIMEOUT_S,
        ) as conn:
            rows = conn.execute(
                "SELECT datname FROM pg_database WHERE datname LIKE %s ESCAPE '\\'",
                (like_pattern,),
            ).fetchall()
        return sorted(row[0] for row in rows)
    except psycopg.Error as e:
        raise StxError(
            f"could not list databases for '{slug}' at {params.host}:{params.port}: {e}"
        ) from e


def drop_databases(databases: list[str], params: PostgresAdminParams) -> None:
    """Drop every named database. No-op on an empty list. Raises StxError on
    failure rather than letting teardown continue as if it had succeeded."""
    if not databases:
        return
    try:
        with psycopg.connect(
            host=params.host,
            port=params.port,
            dbname="postgres",
            user=params.user,
            password=params.password,
            autocommit=True,
            connect_timeout=_PG_CONNECT_TIMEOUT_S,
        ) as conn:
            for database in databases:
                conn.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {}").format(
                        sql.Identifier(database)
                    )
                )
        print(f"  dropped {', '.join(databases)}")
    except psycopg.Error as e:
        raise StxError(
            f"could not drop database(s) {', '.join(databases)} at "
            f"{params.host}:{params.port}: {e}"
        ) from e


def flush_redis(index: int, params: RedisAdminParams) -> None:
    """Flush a sandbox's Redis db-index so a future sandbox can reuse it
    cleanly. Raises StxError on a connection failure."""
    try:
        client = redis_lib.Redis(
            host=params.host,
            port=params.port,
            username=params.user,
            password=params.password,
            db=index,
            socket_connect_timeout=_REDIS_CONNECT_TIMEOUT_S,
        )
        try:
            client.flushdb()
        finally:
            client.close()
        print(f"  flushed redis db {index}")
    except redis_lib.RedisError as e:
        raise StxError(
            f"could not flush redis db {index} at {params.host}:{params.port}: {e}"
        ) from e
