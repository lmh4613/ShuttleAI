from contextlib import contextmanager
from datetime import datetime, time, timezone

import notification_repository as repository


TARGET_ROW = (
    10, 20, 0, True, "Asia/Seoul", "KAKAO", "출근 A", "morning", time(8, 0), 10,
    "탑승", 37.1, 127.1, "하차", 37.2, 127.2,
)


class Cursor:
    def __init__(self, target_rows, device_rows):
        self.target_rows = target_rows
        self.device_rows = device_rows
        self.current = []
        self.sql = []

    def __enter__(self): return self
    def __exit__(self, *_args): pass

    def execute(self, sql, params):
        self.sql.append((sql, params))
        if "WITH first_stops" in sql:
            self.current = self.target_rows
        elif "FROM push_subscriptions" in sql:
            self.current = self.device_rows
        elif sql.startswith("UPDATE push_subscriptions"):
            self.current = [(params[0],)] if params == (7, 10) else []
        else:
            raise AssertionError(sql)

    def fetchall(self): return list(self.current)
    def fetchone(self): return self.current[0] if self.current else None


class Connection:
    def __init__(self, cursor): self._cursor = cursor
    def cursor(self): return self._cursor


def factory(cursor):
    @contextmanager
    def open_connection():
        yield Connection(cursor)
    return open_connection


def test_target_query_filters_and_attaches_all_active_devices():
    cursor = Cursor(
        [TARGET_ROW],
        [(7, 10, "https://push.example.test/one", "p1", "a1"),
         (8, 10, "https://push.example.test/two", "p2", "a2")],
    )
    targets = repository.load_notification_targets({0, 1}, connection_factory=factory(cursor))
    assert len(targets) == 1
    target = targets[0]
    assert target.scheduled_time == time(8, 0)
    assert target.delivery_channel == "KAKAO"
    assert [device.id for device in target.devices] == [7, 8]
    sql = cursor.sql[0][0]
    assert "u.enabled=TRUE" in sql
    assert "fn.enabled=TRUE" in sql
    assert "f.active=TRUE" in sql
    assert "r.active=TRUE" in sql
    assert "boarding_rs.active=TRUE" in sql
    assert "destination_rs.active=TRUE" in sql
    assert "rs.active=TRUE" in sql
    assert "nad.weekday=ANY" in sql
    assert "first_rs.scheduled_time" in sql
    push_sql = cursor.sql[1][0]
    assert "enabled=TRUE" in push_sql
    assert "expiration_time IS NULL OR expiration_time > now()" in push_sql


def test_owner_scoped_device_deactivation():
    cursor = Cursor([], [])
    assert repository.deactivate_push_device(7, 10, transaction_factory=factory(cursor))
    assert not repository.deactivate_push_device(7, 11, transaction_factory=factory(cursor))
    update_sql = cursor.sql[0][0]
    assert "id=%s AND user_id=%s" in update_sql


class HistoryCursor:
    def __init__(self, results):
        self.results = list(results)
        self.current = None
        self.calls = []

    def __enter__(self): return self
    def __exit__(self, *_args): pass

    def execute(self, sql, params):
        self.calls.append((" ".join(sql.split()), params))
        self.current = self.results.pop(0)

    def fetchone(self):
        return self.current

    def fetchall(self):
        return list(self.current)


def test_atomic_run_claim_uses_unique_conflict_and_duplicate_returns_none():
    cursor = HistoryCursor([(101,), None])
    connection_factory = factory(cursor)
    first = repository.claim_notification_run(
        10, 20, "2026-10-01", time(8, 0), "PUSH",
        transaction_factory=connection_factory,
    )
    duplicate = repository.claim_notification_run(
        10, 20, "2026-10-01", time(8, 0), "PUSH",
        transaction_factory=connection_factory,
    )
    assert first == 101
    assert duplicate is None
    sql, params = cursor.calls[0]
    assert "ON CONFLICT (user_id, favorite_id, service_date, scheduled_time)" in sql
    assert "DO NOTHING RETURNING id" in sql
    assert params == (10, 20, "2026-10-01", time(8, 0), "PUSH")
    assert "title" not in sql.lower() and "body" not in sql.lower()


def test_delivery_and_run_history_store_only_structured_status():
    cursor = HistoryCursor([(201,), (201,), (101,)])
    connection_factory = factory(cursor)
    delivery_id = repository.start_notification_delivery(
        101, "PUSH", 7, transaction_factory=connection_factory
    )
    repository.complete_notification_delivery(
        delivery_id, "EXPIRED", "PUSH_EXPIRED",
        transaction_factory=connection_factory,
    )
    repository.complete_notification_run(
        101, "FAILED", "PUSH_EXPIRED", transaction_factory=connection_factory
    )
    assert delivery_id == 201
    combined_sql = " ".join(call[0] for call in cursor.calls).lower()
    for forbidden in ("message", "endpoint", "p256dh", "auth", "token", "kakao_user_id"):
        assert forbidden not in combined_sql
    assert cursor.calls[0][1] == (101, "PUSH", 7)


def test_history_rejects_invalid_or_sensitive_freeform_values():
    import pytest

    with pytest.raises(ValueError):
        repository.claim_notification_run(10, 20, "2026-10-01", time(8), "EMAIL")
    with pytest.raises(ValueError):
        repository.start_notification_delivery(1, "PUSH", None)
    with pytest.raises(ValueError):
        repository.complete_notification_delivery(1, "FAILED", "raw secret exception")
    with pytest.raises(ValueError):
        repository.complete_notification_run(1, "FAILED", "raw secret exception")


def test_history_query_filters_caps_orders_and_aggregates_deliveries():
    started = datetime(2026, 10, 1, 7, 50, tzinfo=timezone.utc)
    row = (
        101, started, 3, "관리자", 20, "경기", "출근 A", "morning",
        "탑승", "판교", time(8, 0), "PUSH", "PARTIAL", "PUSH_SEND_FAILED",
        1, 1, 0,
    )
    cursor = HistoryCursor([[row]])
    entries = repository.load_notification_history(
        3, limit=500, connection_factory=factory(cursor)
    )
    assert len(entries) == 1
    assert entries[0].success_count == 1
    assert entries[0].failed_count == 1
    assert entries[0].route_name == "출근 A"
    sql, params = cursor.calls[0]
    assert params == (3, 100)
    assert "nr.started_at >= now() - (%s * INTERVAL '1 day')" in sql
    assert "ORDER BY nr.started_at DESC" in sql
    assert "LIMIT %s" in sql
    assert "COUNT(nd.id) FILTER (WHERE nd.status='SUCCESS')" in sql
    assert "LEFT JOIN notification_deliveries nd" in sql
    for sensitive in ("kakao_user_id", "endpoint", "p256dh", "auth", "token"):
        assert sensitive not in sql.lower()


def test_history_query_empty_and_invalid_period():
    cursor = HistoryCursor([[]])
    assert repository.load_notification_history(
        1, connection_factory=factory(cursor)
    ) == []
    import pytest
    with pytest.raises(ValueError):
        repository.load_notification_history(30, connection_factory=factory(cursor))


def test_history_query_database_error_is_safe():
    @contextmanager
    def broken_connection():
        raise RuntimeError("postgresql://secret-host/private")
        yield

    import pytest
    with pytest.raises(repository.NotificationRepositoryError) as captured:
        repository.load_notification_history(7, connection_factory=broken_connection)
    assert str(captured.value) == "알림 발송 이력을 처리하지 못했습니다."
    assert "secret" not in str(captured.value)
