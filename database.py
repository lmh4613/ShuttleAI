"""Secret-safe PostgreSQL connection helpers shared by future app processes."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from dotenv import load_dotenv
from psycopg import connect
from psycopg.conninfo import conninfo_to_dict, make_conninfo


load_dotenv()
logger = logging.getLogger(__name__)


class DatabaseConfigurationError(RuntimeError):
    """The database connection has not been configured."""


class DatabaseConnectionError(RuntimeError):
    """A database operation failed without exposing connection credentials."""


def get_database_url() -> str:
    """Read the URL using the app's environment-first configuration policy."""
    value = os.getenv("AIVEN_DATABASE_URL", "").strip()
    if not value:
        try:
            import streamlit as st

            value = str(st.secrets.get("AIVEN_DATABASE_URL", "")).strip()
        except Exception:
            value = ""
    if not value:
        raise DatabaseConfigurationError("AIVEN_DATABASE_URL is not configured.")
    return value


def _ssl_required_conninfo(database_url: str) -> str:
    """Preserve explicit SSL settings and require TLS when none is supplied."""
    try:
        parameters = conninfo_to_dict(database_url)
    except Exception as exc:
        logger.warning("Invalid database configuration type=%s", type(exc).__name__)
        raise DatabaseConfigurationError("AIVEN_DATABASE_URL is invalid.") from None
    parameters.setdefault("sslmode", "require")
    return make_conninfo(**parameters)


def safe_database_target(database_url: str | None = None) -> tuple[str, str]:
    """Return only host/database names for an operator preflight message."""
    try:
        parameters = conninfo_to_dict(database_url or get_database_url())
    except DatabaseConfigurationError:
        raise
    except Exception as exc:
        logger.warning("Invalid database configuration type=%s", type(exc).__name__)
        raise DatabaseConfigurationError("AIVEN_DATABASE_URL is invalid.") from None
    return parameters.get("host", "unknown"), parameters.get("dbname", "unknown")


def open_database_connection(
    database_url: str | None = None,
    *,
    connector: Callable = connect,
):
    """Open one TLS connection; callers own the returned connection."""
    conninfo = _ssl_required_conninfo(database_url or get_database_url())
    try:
        return connector(conninfo, connect_timeout=10, application_name="shuttleai")
    except Exception as exc:
        logger.warning("Database connection failed type=%s", type(exc).__name__)
        raise DatabaseConnectionError("Database connection failed.") from None


@contextmanager
def database_connection(
    database_url: str | None = None,
    *,
    connector: Callable = connect,
) -> Iterator:
    """Yield a single transaction-capable connection and always close it."""
    connection = open_database_connection(database_url, connector=connector)
    try:
        yield connection
    finally:
        connection.close()


@contextmanager
def database_transaction(
    database_url: str | None = None,
    *,
    connector: Callable = connect,
) -> Iterator:
    """Run caller operations in one transaction with automatic rollback."""
    with database_connection(database_url, connector=connector) as connection:
        with connection.transaction():
            yield connection


def check_database_connection(
    database_url: str | None = None,
    *,
    connector: Callable = connect,
) -> bool:
    """Return a credential-free health result."""
    try:
        with database_connection(database_url, connector=connector) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                return cursor.fetchone() == (1,)
    except (DatabaseConfigurationError, DatabaseConnectionError):
        return False
    except Exception as exc:
        logger.warning("Database health check failed type=%s", type(exc).__name__)
        return False
