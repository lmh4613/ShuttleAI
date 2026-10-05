from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

import push_subscription_store as store
from web_push import WebPushConfig, send_test_push


def subscription(suffix="one", **changes):
    value = {
        "endpoint": f"https://push.example.test/device/{suffix}",
        "expirationTime": None,
        "keys": {"p256dh": f"public-{suffix}", "auth": f"auth-{suffix}"},
    }
    value.update(changes)
    return value


class MemoryDatabase:
    def __init__(self):
        self.users = {101: 1, 202: 2}
        self.rows = {}
        self.next_id = 1

    @contextmanager
    def transaction(self):
        yield MemoryConnection(self)

    @contextmanager
    def connection(self):
        yield MemoryConnection(self)


class MemoryConnection:
    def __init__(self, database):
        self.database = database

    def cursor(self):
        return MemoryCursor(self.database)


class MemoryCursor:
    def __init__(self, database):
        self.database = database
        self.result = None

    def __enter__(self): return self
    def __exit__(self, *_args): pass

    def execute(self, sql, params):
        if sql.startswith("SELECT id FROM users"):
            user_id = self.database.users.get(params[0])
            self.result = (user_id,) if user_id else None
        elif sql.startswith("INSERT INTO push_subscriptions"):
            user_id, endpoint, endpoint_hash, p256dh, auth, expiration = params
            existing = self.database.rows.get(endpoint_hash)
            row_id = existing["id"] if existing else self.database.next_id
            if existing is None:
                self.database.next_id += 1
            self.database.rows[endpoint_hash] = {
                "id": row_id, "user_id": user_id, "endpoint": endpoint,
                "p256dh": p256dh, "auth": auth, "expiration": expiration,
                "enabled": True, "revoked": False,
            }
            self.result = None
        elif sql.startswith("SELECT u.kakao_user_id"):
            endpoint_hash = params[0]
            row = self.database.rows.get(endpoint_hash)
            if row and row["enabled"]:
                kakao_user_id = next(
                    key for key, user_id in self.database.users.items()
                    if user_id == row["user_id"]
                )
                self.result = (kakao_user_id,)
            else:
                self.result = None
        elif sql.startswith("SELECT count(*) FROM push_subscriptions"):
            user_id = params[0]
            self.result = (sum(
                1 for row in self.database.rows.values()
                if row["user_id"] == user_id and row["enabled"] and not row["revoked"]
                and (row["expiration"] is None or row["expiration"] > datetime.now(timezone.utc))
            ),)
        elif "WHERE user_id=%s" in sql and sql.startswith("UPDATE push_subscriptions"):
            user_id = params[0]
            changed = []
            for row in self.database.rows.values():
                if row["user_id"] == user_id and row["enabled"] and not row["revoked"]:
                    row["enabled"] = False
                    row["revoked"] = True
                    changed.append((row["id"],))
            self.result = changed
        elif sql.startswith("UPDATE push_subscriptions"):
            kakao_user_id, endpoint_hash = params
            user_id = self.database.users.get(kakao_user_id)
            row = self.database.rows.get(endpoint_hash)
            if row and row["user_id"] == user_id and row["enabled"]:
                row["enabled"] = False
                self.result = (row["id"],)
            else:
                self.result = None
        else:
            raise AssertionError("Unexpected SQL statement")

    def fetchone(self):
        return self.result

    def fetchall(self):
        return list(self.result or [])


def test_subscription_validation_and_canonicalization():
    result = store.canonicalize_subscription(subscription())
    assert result.endpoint_hash == store.endpoint_hash(result.endpoint)
    assert result.p256dh == "public-one"
    assert result.auth == "auth-one"
    assert result.expiration_time is None

    invalid_values = [
        ({"keys": {"p256dh": "p", "auth": "a"}}, "endpoint"),
        ({"endpoint": "https://push.example.test", "keys": {"auth": "a"}}, "p256dh"),
        ({"endpoint": "https://push.example.test", "keys": {"p256dh": "p"}}, "auth"),
    ]
    for value, message in invalid_values:
        with pytest.raises(store.PushSubscriptionValidationError, match=message):
            store.canonicalize_subscription(value)


def test_endpoint_hash_is_deterministic_lowercase_sha256():
    first = store.endpoint_hash(subscription()["endpoint"])
    second = store.endpoint_hash(subscription()["endpoint"])
    assert first == second
    assert len(first) == 64
    assert first == first.lower()
    assert all(character in "0123456789abcdef" for character in first)


def test_same_user_upsert_and_new_endpoint_support_multiple_devices():
    database = MemoryDatabase()
    first = subscription("one")
    refreshed = subscription("one")
    refreshed["keys"] = {"p256dh": "new-public", "auth": "new-auth"}
    store.register_subscription(101, first, transaction_factory=database.transaction)
    store.register_subscription(101, refreshed, transaction_factory=database.transaction)
    store.register_subscription(101, subscription("two"), transaction_factory=database.transaction)
    assert len(database.rows) == 2
    assert database.rows[store.endpoint_hash(first["endpoint"])]["p256dh"] == "new-public"
    assert all(row["user_id"] == 1 for row in database.rows.values())


def test_cross_user_transfer_preserves_other_devices():
    database = MemoryDatabase()
    first = subscription("one")
    other = subscription("two")
    store.register_subscription(101, first, transaction_factory=database.transaction)
    store.register_subscription(101, other, transaction_factory=database.transaction)
    store.register_subscription(202, first, transaction_factory=database.transaction)
    assert database.rows[store.endpoint_hash(first["endpoint"])]["user_id"] == 2
    assert database.rows[store.endpoint_hash(other["endpoint"])]["user_id"] == 1


def test_current_lookup_and_owner_only_deactivate():
    database = MemoryDatabase()
    current = subscription()
    store.register_subscription(101, current, transaction_factory=database.transaction)
    assert store.is_current_subscription_registered(
        101, current, connection_factory=database.connection
    )
    assert not store.is_current_subscription_registered(
        202, current, connection_factory=database.connection
    )
    assert not store.deactivate_subscription(
        202, current, transaction_factory=database.transaction
    )
    assert store.deactivate_subscription(
        101, current, transaction_factory=database.transaction
    )
    assert not store.is_current_subscription_registered(
        101, current, connection_factory=database.connection
    )


def test_active_count_and_deactivate_all_are_owner_scoped_soft_revocations():
    database = MemoryDatabase()
    for suffix in ("one", "two", "three"):
        store.register_subscription(
            101, subscription(suffix), transaction_factory=database.transaction
        )
    store.register_subscription(
        202, subscription("other-user"), transaction_factory=database.transaction
    )

    assert store.count_active_push_subscriptions(
        101, connection_factory=database.connection
    ) == 3
    assert store.deactivate_all_subscriptions(
        101, transaction_factory=database.transaction
    ) == 3
    assert store.count_active_push_subscriptions(
        101, connection_factory=database.connection
    ) == 0
    other = database.rows[store.endpoint_hash(subscription("other-user")["endpoint"])]
    assert other["enabled"] is True
    assert other["revoked"] is False


def test_active_count_excludes_disabled_revoked_and_expired_devices():
    database = MemoryDatabase()
    for suffix in ("active", "disabled", "revoked", "expired"):
        store.register_subscription(
            101, subscription(suffix), transaction_factory=database.transaction
        )
    database.rows[store.endpoint_hash(subscription("disabled")["endpoint"])]["enabled"] = False
    database.rows[store.endpoint_hash(subscription("revoked")["endpoint"])]["revoked"] = True
    database.rows[store.endpoint_hash(subscription("expired")["endpoint"])]["expiration"] = (
        datetime(2020, 1, 1, tzinfo=timezone.utc)
    )

    assert store.count_active_push_subscriptions(
        101, connection_factory=database.connection
    ) == 1


@pytest.mark.parametrize("status_code", [404, 410])
def test_expired_send_deactivates_only_current_subscription(status_code):
    called = []

    class ExpiredPush(Exception):
        pass

    error = ExpiredPush()
    error.status_code = status_code

    config = WebPushConfig("public", "private", "mailto:admin@example.com", "")
    ok, message = send_test_push(
        subscription(), config, sender=lambda **_kwargs: (_ for _ in ()).throw(error),
        expired_handler=lambda: called.append(True),
    )
    assert not ok and "만료" in message
    assert called == [True]


def test_missing_user_database_failure_and_logs_do_not_leak(caplog):
    database = MemoryDatabase()
    with pytest.raises(store.PushSubscriptionUserNotFound):
        store.register_subscription(999, subscription(), transaction_factory=database.transaction)

    @contextmanager
    def unavailable():
        raise OSError("database unavailable")
        yield

    secret_subscription = subscription("secret-material")
    with pytest.raises(store.PushSubscriptionDatabaseError):
        store.register_subscription(101, secret_subscription, transaction_factory=unavailable)
    assert secret_subscription["endpoint"] not in caplog.text
    assert secret_subscription["keys"]["p256dh"] not in caplog.text
    assert secret_subscription["keys"]["auth"] not in caplog.text
