"""PostgreSQL connection helpers.

Connection settings are read exclusively from environment variables so that no
credentials are ever hardcoded:

    PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD   (standard libpq names)
    DATABASE_URL                                     (overrides the above)

Every helper propagates database errors; nothing is swallowed.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import psycopg
from psycopg import Connection

_SCHEMA_FILE = Path(__file__).resolve().parent / "schema.sql"


def connection_info() -> dict[str, str]:
    """Return libpq connection keyword arguments from the environment."""
    dsn = os.getenv("DATABASE_URL")
    if dsn:
        return {"conninfo": dsn}
    return {
        "host": os.getenv("PGHOST", "localhost"),
        "port": os.getenv("PGPORT", "5432"),
        "dbname": os.getenv("PGDATABASE", "erp"),
        "user": os.getenv("PGUSER", "erp"),
        "password": os.getenv("PGPASSWORD", ""),
    }


def connect(autocommit: bool = False) -> Connection:
    """Open a new connection. Raises on failure (never silently ignored)."""
    info = connection_info()
    if "conninfo" in info:
        return psycopg.connect(info["conninfo"], autocommit=autocommit)
    return psycopg.connect(
        host=info["host"],
        port=info["port"],
        dbname=info["dbname"],
        user=info["user"],
        password=info["password"],
        autocommit=autocommit,
    )


@contextmanager
def get_connection(autocommit: bool = False) -> Iterator[Connection]:
    conn = connect(autocommit=autocommit)
    try:
        yield conn
        if not autocommit:
            conn.commit()
    except Exception:
        if not autocommit:
            conn.rollback()
        raise
    finally:
        conn.close()


def create_schema(schema_path: Path | None = None) -> None:
    """(Re)create the ERP schema by executing schema.sql.

    schema.sql begins with DROP SCHEMA IF EXISTS erp CASCADE, so this is an
    idempotent reset of every object owned by the generator.
    """
    path = schema_path or _SCHEMA_FILE
    sql = path.read_text(encoding="utf-8")
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(sql)


def table_names() -> list[str]:
    """Return the generator's tables in the erp schema."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'erp' AND table_type = 'BASE TABLE'
            ORDER BY table_name
            """
        )
        return [row[0] for row in cur.fetchall()]
