import hashlib
from pathlib import Path

import pytest

import database
from db_migrate import (
    Migration,
    MigrationChecksumError,
    MigrationError,
    discover_migrations,
    migration_action,
)


def migration(checksum="a" * 64):
    return Migration("001", "initial_schema", Path("001_initial_schema.sql"), "SELECT 1", checksum)


def test_missing_database_url_is_handled(monkeypatch):
    monkeypatch.delenv("AIVEN_DATABASE_URL", raising=False)
    monkeypatch.setattr(database, "load_dotenv", lambda: None)

    class EmptySecrets:
        def get(self, _name, default=""):
            return default

    import streamlit as st
    monkeypatch.setattr(st, "secrets", EmptySecrets())
    with pytest.raises(database.DatabaseConfigurationError, match="not configured"):
        database.get_database_url()


def test_connection_error_does_not_expose_secret(caplog):
    secret = "do-not-leak-password"

    def fail_connector(*_args, **_kwargs):
        raise RuntimeError(f"could not connect with {secret}")

    with pytest.raises(database.DatabaseConnectionError, match="Database connection failed") as error:
        database.open_database_connection(
            "host=db.example.test dbname=app", connector=fail_connector
        )

    assert secret not in str(error.value)
    assert secret not in caplog.text


def test_health_check_success_and_failure():
    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def execute(self, sql): assert sql == "SELECT 1"
        def fetchone(self): return (1,)

    class Connection:
        closed = False
        def cursor(self): return Cursor()
        def close(self): self.closed = True

    connection = Connection()
    assert database.check_database_connection(
        "host=db.example.test dbname=app", connector=lambda *_a, **_k: connection
    )
    assert connection.closed
    assert not database.check_database_connection(
        "host=db.example.test dbname=app",
        connector=lambda *_a, **_k: (_ for _ in ()).throw(OSError("offline")),
    )


def test_migration_version_parsing_and_checksum(tmp_path):
    sql = b"SELECT 1;\n"
    path = tmp_path / "001_initial_schema.sql"
    path.write_bytes(sql)
    migrations = discover_migrations(tmp_path)

    assert [(item.version, item.name) for item in migrations] == [("001", "initial_schema")]
    assert migrations[0].checksum == hashlib.sha256(sql).hexdigest()


def test_invalid_and_duplicate_migration_versions_are_rejected(tmp_path):
    (tmp_path / "bad.sql").write_text("SELECT 1", encoding="utf-8")
    with pytest.raises(MigrationError, match="Invalid migration filename"):
        discover_migrations(tmp_path)

    (tmp_path / "bad.sql").unlink()
    (tmp_path / "001_first.sql").write_text("SELECT 1", encoding="utf-8")
    (tmp_path / "001_second.sql").write_text("SELECT 2", encoding="utf-8")
    with pytest.raises(MigrationError, match="Duplicate migration version"):
        discover_migrations(tmp_path)


def test_applied_migration_is_skipped_and_mismatch_is_rejected():
    item = migration()
    assert migration_action(item, None) == "apply"
    assert migration_action(item, item.checksum) == "skip"
    with pytest.raises(MigrationChecksumError, match="checksum"):
        migration_action(item, "b" * 64)


def test_initial_schema_contains_only_v1_tables_and_core_constraints():
    sql = Path("migrations/001_initial_schema.sql").read_text("utf-8")
    required = {
        "schema_migrations", "regions", "routes", "stops", "route_stops", "users",
        "kakao_credentials", "notification_settings", "notification_active_days",
        "favorites", "favorite_notifications", "push_subscriptions",
    }
    for table in required:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
    for excluded in ("route_imports", "notification_runs", "notification_deliveries", "oauth_login_states"):
        assert f"CREATE TABLE IF NOT EXISTS {excluded}" not in sql
    assert "UNIQUE (id, route_id)" in sql
    assert "FOREIGN KEY (boarding_route_stop_id, route_id)" in sql
    assert "FOREIGN KEY (alighting_route_stop_id, route_id)" in sql
    assert "CHECK (weekday BETWEEN 0 AND 6)" in sql
    assert "CHECK (lead_minutes BETWEEN 1 AND 180)" in sql
    assert "WHERE is_default_dropoff" in sql
    assert "access_token TEXT" not in sql
    assert "refresh_token TEXT" not in sql
