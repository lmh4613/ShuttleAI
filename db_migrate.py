"""Explicit, checksum-protected SQL migration runner for ShuttleAI."""

from __future__ import annotations

import argparse
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from database import (
    DatabaseConfigurationError,
    DatabaseConnectionError,
    database_transaction,
    safe_database_target,
)


MIGRATIONS_DIR = Path(__file__).parent / "migrations"
MIGRATION_NAME = re.compile(r"^(?P<version>\d{3,})_(?P<name>[a-z0-9_]+)\.sql$")
LEDGER_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version VARCHAR(255) PRIMARY KEY,
    checksum CHAR(64) NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


class MigrationError(RuntimeError):
    pass


class MigrationChecksumError(MigrationError):
    pass


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    path: Path
    sql: str
    checksum: str


def discover_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    migrations = []
    seen = set()
    for path in sorted(directory.glob("*.sql")):
        match = MIGRATION_NAME.fullmatch(path.name)
        if not match:
            raise MigrationError(f"Invalid migration filename: {path.name}")
        version = match.group("version")
        if version in seen:
            raise MigrationError(f"Duplicate migration version: {version}")
        seen.add(version)
        raw = path.read_bytes()
        migrations.append(Migration(
            version=version,
            name=match.group("name"),
            path=path,
            sql=raw.decode("utf-8"),
            checksum=hashlib.sha256(raw).hexdigest(),
        ))
    return migrations


def migration_action(migration: Migration, applied_checksum: str | None) -> str:
    if applied_checksum is None:
        return "apply"
    if applied_checksum != migration.checksum:
        raise MigrationChecksumError(
            f"Migration {migration.version} checksum does not match the applied version."
        )
    return "skip"


def run_migrations(directory: Path = MIGRATIONS_DIR) -> dict[str, list[str]]:
    migrations = discover_migrations(directory)
    result = {"applied": [], "skipped": []}
    with database_transaction() as connection:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(hashtext('shuttleai_schema_migrations'))")
                cursor.execute(LEDGER_SQL)
                cursor.execute("SELECT version, checksum FROM schema_migrations")
                applied = dict(cursor.fetchall())
                for migration in migrations:
                    action = migration_action(migration, applied.get(migration.version))
                    if action == "skip":
                        result["skipped"].append(migration.version)
                        continue
                    cursor.execute(migration.sql)
                    cursor.execute(
                        "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
                        (migration.version, migration.checksum),
                    )
                    result["applied"].append(migration.version)
        except MigrationError:
            raise
        except Exception as exc:
            raise MigrationError(f"Database migration failed ({type(exc).__name__}).") from None
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply ShuttleAI PostgreSQL migrations.")
    parser.add_argument("command", choices=("migrate", "target"), nargs="?", default="migrate")
    args = parser.parse_args()
    try:
        host, database = safe_database_target()
        print(f"Database target: host={host}, database={database}")
        if args.command == "target":
            return 0
        result = run_migrations()
        print("Applied migrations: " + (", ".join(result["applied"]) or "none"))
        print("Skipped migrations: " + (", ".join(result["skipped"]) or "none"))
        return 0
    except (DatabaseConfigurationError, DatabaseConnectionError, MigrationError) as exc:
        print(f"Migration error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
