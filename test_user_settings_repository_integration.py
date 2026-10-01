import os
from contextlib import contextmanager

import pytest

from database import database_connection
from notification_repository import load_notification_targets
from notification_worker import resolve_delivery_channel
from user_settings_repository import (
    create_favorite,
    delete_favorite,
    get_global_notification_policy,
    get_notification_settings,
    list_favorites,
    update_favorite_notification,
    update_global_notification_policy,
    update_notification_settings,
)


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_AIVEN_INTEGRATION_TESTS") != "1" or not os.getenv("AIVEN_DATABASE_URL"),
    reason="Set RUN_AIVEN_INTEGRATION_TESTS=1 with AIVEN_DATABASE_URL for Aiven tests.",
)


def _candidate(cursor, user_id):
    cursor.execute(
        "SELECT r.id, rg.code, r.name, r.trip_type "
        "FROM routes r JOIN regions rg ON rg.id=r.region_id "
        "WHERE r.active=TRUE ORDER BY r.id"
    )
    for route_id, region, route_name, trip_type in cursor.fetchall():
        cursor.execute(
            "SELECT rs.id, s.name, s.latitude, s.longitude, rs.scheduled_time "
            "FROM route_stops rs JOIN stops s ON s.id=rs.stop_id "
            "WHERE rs.route_id=%s AND rs.boarding_allowed=TRUE "
            "ORDER BY rs.stop_order",
            (route_id,),
        )
        boarding_rows = cursor.fetchall()
        cursor.execute(
            "SELECT rs.id, s.name, s.latitude, s.longitude, rs.scheduled_time "
            "FROM route_stops rs JOIN stops s ON s.id=rs.stop_id "
            "WHERE rs.route_id=%s AND rs.alighting_allowed=TRUE "
            "ORDER BY rs.is_default_dropoff DESC, rs.stop_order",
            (route_id,),
        )
        destination_rows = cursor.fetchall()
        if not boarding_rows or not destination_rows:
            continue
        boarding = boarding_rows[0]
        if boarding[4] is None:
            continue
        for destination in destination_rows:
            cursor.execute(
                "SELECT 1 FROM favorites WHERE user_id=%s AND route_id=%s "
                "AND boarding_route_stop_id=%s AND alighting_route_stop_id=%s",
                (user_id, route_id, boarding[0], destination[0]),
            )
            if cursor.fetchone() is None:
                return {
                    "region": region, "route_name": route_name,
                    "trip_type": "퇴근길" if trip_type == "evening" else "출근길",
                    "board_stop": boarding[1], "board_lat": float(boarding[2]),
                    "board_lon": float(boarding[3]), "arrive_stop": destination[1],
                    "arrive_lat": float(destination[2]),
                    "arrive_lon": float(destination[3]),
                }
    raise AssertionError("No non-duplicate favorite candidate exists")


def test_ui_settings_favorite_crud_and_worker_query_share_one_transaction():
    with database_connection() as connection:
        with connection.transaction(force_rollback=True):
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT u.id, u.kakao_user_id FROM users u "
                    "JOIN notification_settings ns ON ns.user_id=u.id "
                    "WHERE u.enabled=TRUE ORDER BY u.id LIMIT 1"
                )
                user_id, kakao_user_id = cursor.fetchone()
                candidate = _candidate(cursor, user_id)

            @contextmanager
            def same_connection():
                yield connection

            settings = get_notification_settings(
                kakao_user_id, connection_factory=same_connection
            )
            update_notification_settings(
                kakao_user_id, active_days=[0, 2, 4], exclude_holidays=False,
                delivery_channel="PUSH", transaction_factory=same_connection,
            )
            updated = get_notification_settings(
                kakao_user_id, connection_factory=same_connection
            )
            assert updated["active_days"] == [0, 2, 4]
            assert updated["exclude_holidays"] is False
            assert updated["delivery_channel"] == "PUSH"

            favorite_id = create_favorite(
                kakao_user_id, candidate, transaction_factory=same_connection
            )
            assert any(
                item["favorite_id"] == favorite_id
                for item in list_favorites(kakao_user_id, connection_factory=same_connection)
            )
            assert update_favorite_notification(
                kakao_user_id, favorite_id, enabled=True, lead_minutes=30,
                transaction_factory=same_connection,
            )
            targets = load_notification_targets([0, 2, 4], connection_factory=same_connection)
            created = [target for target in targets if target.favorite_id == favorite_id]
            assert created
            assert all(target.delivery_channel == "PUSH" for target in created)
            assert all(target.lead_minutes == 30 for target in created)
            assert resolve_delivery_channel(
                get_global_notification_policy(connection_factory=same_connection),
                created[0].delivery_channel,
            ) == "PUSH"
            assert delete_favorite(
                kakao_user_id, favorite_id, transaction_factory=same_connection
            )
            assert not any(
                item["favorite_id"] == favorite_id
                for item in list_favorites(kakao_user_id, connection_factory=same_connection)
            )

            # A rollback also restores the user's pre-test preference and active days.
            assert settings["user_id"] == user_id


def test_admin_global_override_does_not_mutate_user_preference():
    with database_connection() as connection:
        with connection.transaction(force_rollback=True):
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT kakao_user_id FROM users WHERE role='admin' AND enabled=TRUE "
                    "ORDER BY id LIMIT 1"
                )
                admin_kakao_user_id = cursor.fetchone()[0]

            @contextmanager
            def same_connection():
                yield connection

            before = get_notification_settings(
                admin_kakao_user_id, connection_factory=same_connection
            )["delivery_channel"]
            current_policy = get_global_notification_policy(
                connection_factory=same_connection
            )
            replacement = "PUSH" if current_policy != "PUSH" else "KAKAO"
            update_global_notification_policy(
                admin_kakao_user_id, replacement, transaction_factory=same_connection
            )
            assert get_global_notification_policy(
                connection_factory=same_connection
            ) == replacement
            assert get_notification_settings(
                admin_kakao_user_id, connection_factory=same_connection
            )["delivery_channel"] == before
