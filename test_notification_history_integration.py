import hashlib
import os
from contextlib import contextmanager
from datetime import date

import pytest

from database import database_connection
from notification_repository import (
    claim_notification_run,
    complete_notification_delivery,
    complete_notification_run,
    start_notification_delivery,
)


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_AIVEN_INTEGRATION_TESTS") != "1" or not os.getenv("AIVEN_DATABASE_URL"),
    reason="Set RUN_AIVEN_INTEGRATION_TESTS=1 with AIVEN_DATABASE_URL for Aiven tests.",
)


def test_atomic_history_claim_delivery_completion_and_rollback():
    endpoint = "https://history.integration.invalid/device"
    endpoint_hash = hashlib.sha256(endpoint.encode()).hexdigest()
    run_id = None

    with database_connection() as connection:
        with connection.transaction(force_rollback=True):
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT f.user_id, f.id, "
                    "CASE WHEN r.trip_type='evening' THEN first_rs.scheduled_time "
                    "ELSE boarding_rs.scheduled_time END "
                    "FROM favorites f JOIN routes r ON r.id=f.route_id "
                    "JOIN route_stops boarding_rs ON boarding_rs.id=f.boarding_route_stop_id "
                    "LEFT JOIN LATERAL (SELECT scheduled_time FROM route_stops "
                    "WHERE route_id=r.id ORDER BY stop_order LIMIT 1) first_rs ON TRUE "
                    "WHERE CASE WHEN r.trip_type='evening' THEN first_rs.scheduled_time "
                    "ELSE boarding_rs.scheduled_time END IS NOT NULL LIMIT 1"
                )
                user_id, favorite_id, scheduled_time = cursor.fetchone()
                cursor.execute(
                    "INSERT INTO push_subscriptions "
                    "(user_id, endpoint, endpoint_hash, p256dh, auth, enabled) "
                    "VALUES (%s,%s,%s,'integration-public','integration-auth',TRUE) "
                    "RETURNING id",
                    (user_id, endpoint, endpoint_hash),
                )
                subscription_id = cursor.fetchone()[0]

            @contextmanager
            def same_connection():
                yield connection

            service_date = date(2099, 12, 31)
            run_id = claim_notification_run(
                user_id, favorite_id, service_date, scheduled_time, "PUSH",
                transaction_factory=same_connection,
            )
            assert run_id is not None
            assert claim_notification_run(
                user_id, favorite_id, service_date, scheduled_time, "PUSH",
                transaction_factory=same_connection,
            ) is None

            delivery_id = start_notification_delivery(
                run_id, "PUSH", subscription_id, transaction_factory=same_connection
            )
            complete_notification_delivery(
                delivery_id, "SUCCESS", transaction_factory=same_connection
            )
            complete_notification_run(
                run_id, "SUCCESS", transaction_factory=same_connection
            )

            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT status, effective_channel, error_code, completed_at IS NOT NULL "
                    "FROM notification_runs WHERE id=%s",
                    (run_id,),
                )
                assert cursor.fetchone() == ("SUCCESS", "PUSH", None, True)
                cursor.execute(
                    "SELECT status, channel, push_subscription_id, error_code, "
                    "completed_at IS NOT NULL FROM notification_deliveries WHERE id=%s",
                    (delivery_id,),
                )
                assert cursor.fetchone() == (
                    "SUCCESS", "PUSH", subscription_id, None, True
                )
                cursor.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name IN "
                    "('notification_runs','notification_deliveries')"
                )
                columns = {row[0] for row in cursor.fetchall()}
                assert not columns.intersection({
                    "message", "message_title", "message_body", "endpoint",
                    "p256dh", "auth", "kakao_user_id", "token",
                })

    with database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM notification_runs WHERE id=%s", (run_id,))
            assert cursor.fetchone() == (0,)
