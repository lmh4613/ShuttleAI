"""Opt-in write tests for a dedicated PostgreSQL test database only."""

from contextlib import contextmanager
import os

import pytest

from database import open_database_connection
from route_repository import (
    load_admin_route_snapshot,
    preview_region_reconcile,
    reconcile_region_routes,
)


TEST_DATABASE_URL = os.getenv("SHUTTLE_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    os.getenv("RUN_ROUTE_WRITE_INTEGRATION_TESTS") != "1" or not TEST_DATABASE_URL,
    reason="Requires RUN_ROUTE_WRITE_INTEGRATION_TESTS=1 and a dedicated SHUTTLE_TEST_DATABASE_URL.",
)


def test_time_update_preserves_route_and_route_stop_ids_then_rolls_back():
    connection = open_database_connection(TEST_DATABASE_URL)

    @contextmanager
    def shared_connection():
        yield connection

    try:
        snapshot = load_admin_route_snapshot(connection_factory=shared_connection)
        row = next(item for item in snapshot["rows"] if item["arrival_time"])
        region = row["region"]
        region_rows = [dict(item) for item in snapshot["rows"] if item["region"] == region]
        target = next(item for item in region_rows if item["_identity_key"] == row["_identity_key"])
        target["arrival_time"] = "23:58" if target["arrival_time"] != "23:58" else "23:57"
        preview = preview_region_reconcile(
            region, region_rows, identity_map=snapshot["identity_map"],
            connection_factory=shared_connection,
        )
        identity = snapshot["identity_map"][target["_identity_key"]]
        reconcile_region_routes(
            region, region_rows, expected_snapshot=preview["snapshot"],
            identity_map=snapshot["identity_map"], transaction_factory=shared_connection,
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT rs.id, r.id FROM route_stops rs JOIN routes r ON r.id=rs.route_id "
                "WHERE rs.id=%s",
                (identity["route_stop_id"],),
            )
            assert cursor.fetchone() == (identity["route_stop_id"], identity["route_id"])
    finally:
        connection.rollback()
        connection.close()
