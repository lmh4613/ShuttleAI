import hashlib
import os
from contextlib import contextmanager

import pytest

from database import database_connection
from notification_repository import load_notification_targets


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_AIVEN_INTEGRATION_TESTS") != "1" or not os.getenv("AIVEN_DATABASE_URL"),
    reason="Set RUN_AIVEN_INTEGRATION_TESTS=1 with AIVEN_DATABASE_URL for Aiven tests.",
)


def test_worker_target_query_devices_filter_and_rollback():
    endpoints = [f"https://worker.integration.invalid/device-{index}" for index in range(3)]
    hashes = [hashlib.sha256(value.encode()).hexdigest() for value in endpoints]

    with database_connection() as connection:
        with connection.transaction(force_rollback=True):
            inserted_ids = []
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT u.id FROM users u JOIN favorites f ON f.user_id=u.id "
                    "JOIN favorite_notifications fn ON fn.favorite_id=f.id "
                    "WHERE u.enabled=TRUE AND fn.enabled=TRUE ORDER BY u.id LIMIT 1"
                )
                user_id = cursor.fetchone()[0]
                for index, (endpoint, endpoint_hash) in enumerate(zip(endpoints, hashes)):
                    cursor.execute(
                        "INSERT INTO push_subscriptions "
                        "(user_id, endpoint, endpoint_hash, p256dh, auth, enabled) "
                        "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                        (user_id, endpoint, endpoint_hash, f"public-{index}",
                         f"auth-{index}", index < 2),
                    )
                    inserted_ids.append(cursor.fetchone()[0])

            @contextmanager
            def same_connection():
                yield connection

            targets = load_notification_targets(range(7), connection_factory=same_connection)
            owned = [target for target in targets if target.user_id == user_id]
            assert owned
            for target in owned:
                device_ids = {device.id for device in target.devices}
                assert set(inserted_ids[:2]).issubset(device_ids)
                assert inserted_ids[2] not in device_ids

    with database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM push_subscriptions WHERE endpoint_hash=ANY(%s)",
                (hashes,),
            )
            assert cursor.fetchone() == (0,)
