import os

import pytest

from database import check_database_connection, database_connection
from db_migrate import run_migrations


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_AIVEN_INTEGRATION_TESTS") != "1" or not os.getenv("AIVEN_DATABASE_URL"),
    reason="Set RUN_AIVEN_INTEGRATION_TESTS=1 with AIVEN_DATABASE_URL for Aiven tests.",
)

EXPECTED_TABLES = {
    "schema_migrations", "regions", "routes", "stops", "route_stops", "users",
    "kakao_credentials", "notification_settings", "notification_active_days",
    "favorites", "favorite_notifications", "push_subscriptions",
}


def test_aiven_health_and_v1_schema():
    assert check_database_connection()
    result = run_migrations()
    assert set(result) == {"applied", "skipped"}

    with database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = ANY(%s)",
                (list(EXPECTED_TABLES),),
            )
            assert {row[0] for row in cursor.fetchall()} == EXPECTED_TABLES
            cursor.execute("SELECT code FROM regions ORDER BY code")
            assert [row[0] for row in cursor.fetchall()] == ["gyeonggi", "seoul"]
            cursor.execute(
                "SELECT version, char_length(checksum) FROM schema_migrations WHERE version = '001'"
            )
            assert cursor.fetchone() == ("001", 64)
            cursor.execute("SELECT count(*) FROM push_subscriptions")
            assert cursor.fetchone() == (0,)


def test_v1_constraints_and_indexes_exist():
    with database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT conname FROM pg_constraint WHERE conname = ANY(%s)",
                ([
                    "uq_routes_region_name", "uq_route_stops_route_order",
                    "uq_route_stops_id_route", "fk_favorites_boarding_route_stop",
                    "fk_favorites_alighting_route_stop", "uq_favorites_selection",
                ],),
            )
            names = {row[0] for row in cursor.fetchall()}
            assert names == {
                "uq_routes_region_name", "uq_route_stops_route_order",
                "uq_route_stops_id_route", "fk_favorites_boarding_route_stop",
                "fk_favorites_alighting_route_stop", "uq_favorites_selection",
            }
            cursor.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' AND indexname = ANY(%s)",
                ([
                    "idx_routes_region_active_name", "idx_stops_region_name",
                    "idx_route_stops_scheduled_time", "uq_route_stops_default_dropoff",
                    "idx_notification_days_weekday_user", "idx_favorite_notifications_enabled",
                    "idx_push_subscriptions_enabled_user",
                    "idx_push_subscriptions_enabled_expiration",
                ],),
            )
            assert len(cursor.fetchall()) == 8
