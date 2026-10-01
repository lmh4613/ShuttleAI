import os
from contextlib import contextmanager

import pytest

from database import database_connection
from push_subscription_store import (
    deactivate_subscription,
    endpoint_hash,
    is_current_subscription_registered,
    register_subscription,
)


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_AIVEN_INTEGRATION_TESTS") != "1" or not os.getenv("AIVEN_DATABASE_URL"),
    reason="Set RUN_AIVEN_INTEGRATION_TESTS=1 with AIVEN_DATABASE_URL for Aiven tests.",
)


def test_aiven_push_upsert_transfer_owner_only_deactivate_and_rollback():
    first = {
        "endpoint": "https://push.integration.invalid/shuttleai/device-one",
        "expirationTime": None,
        "keys": {"p256dh": "integration-public-one", "auth": "integration-auth-one"},
    }
    second = {
        "endpoint": "https://push.integration.invalid/shuttleai/device-two",
        "expirationTime": None,
        "keys": {"p256dh": "integration-public-two", "auth": "integration-auth-two"},
    }
    hashes = [endpoint_hash(first["endpoint"]), endpoint_hash(second["endpoint"])]

    with database_connection() as connection:
        with connection.transaction(force_rollback=True):
            with connection.cursor() as cursor:
                cursor.execute("SELECT kakao_user_id FROM users ORDER BY id LIMIT 2")
                user_ids = [row[0] for row in cursor.fetchall()]
            assert len(user_ids) == 2

            @contextmanager
            def same_transaction():
                yield connection

            @contextmanager
            def same_connection():
                yield connection

            register_subscription(user_ids[0], first, transaction_factory=same_transaction)
            register_subscription(user_ids[0], first, transaction_factory=same_transaction)
            register_subscription(user_ids[0], second, transaction_factory=same_transaction)
            assert is_current_subscription_registered(
                user_ids[0], first, connection_factory=same_connection
            )

            register_subscription(user_ids[1], first, transaction_factory=same_transaction)
            assert not is_current_subscription_registered(
                user_ids[0], first, connection_factory=same_connection
            )
            assert is_current_subscription_registered(
                user_ids[1], first, connection_factory=same_connection
            )
            assert is_current_subscription_registered(
                user_ids[0], second, connection_factory=same_connection
            )
            assert not deactivate_subscription(
                user_ids[0], first, transaction_factory=same_transaction
            )
            assert deactivate_subscription(
                user_ids[1], first, transaction_factory=same_transaction
            )
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM push_subscriptions WHERE endpoint_hash=ANY(%s)",
                    (hashes,),
                )
                assert cursor.fetchone() == (2,)

    with database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM push_subscriptions WHERE endpoint_hash=ANY(%s)",
                (hashes,),
            )
            assert cursor.fetchone() == (0,)
