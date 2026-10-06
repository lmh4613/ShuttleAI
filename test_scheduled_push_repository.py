from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

import scheduled_push_repository as repository


class Cursor:
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


class Connection:
    def __init__(self, cursor): self._cursor = cursor
    def cursor(self): return self._cursor


def factory(cursor):
    @contextmanager
    def open_connection():
        yield Connection(cursor)
    return open_connection


def test_create_scheduled_push_test_validates_owned_target_devices():
    scheduled_at = datetime(2026, 10, 6, 18, 30, tzinfo=timezone.utc)
    cursor = Cursor([(10,), [(7,), (8,)], (101,)])
    test_id = repository.create_scheduled_push_test(
        5070327065,
        scheduled_at,
        "모바일 오프라인 수신 테스트",
        "high",
        [8, 7, 7],
        transaction_factory=factory(cursor),
    )

    assert test_id == 101
    assert cursor.calls[0][1] == (5070327065,)
    assert "id=ANY(%s)" in cursor.calls[1][0]
    assert cursor.calls[1][1] == (10, [7, 8])
    assert cursor.calls[2][1] == (
        10, scheduled_at, "모바일 오프라인 수신 테스트", "high", [7, 8]
    )


def test_create_scheduled_push_test_rejects_unowned_device():
    cursor = Cursor([(10,), [(7,)]])
    with pytest.raises(repository.ScheduledPushRepositoryError):
        repository.create_scheduled_push_test(
            5070327065,
            datetime(2026, 10, 6, 18, 30, tzinfo=timezone.utc),
            "test",
            "normal",
            [7, 8],
            transaction_factory=factory(cursor),
        )


def test_claim_due_scheduled_push_tests_uses_processing_claim_and_skip_locked():
    scheduled_at = datetime(2026, 10, 6, 18, 30, tzinfo=timezone.utc)
    cursor = Cursor([[
        (101, 10, scheduled_at, "테스트", "normal", 60, [7, 8], "PROCESSING")
    ]])
    tests = repository.claim_due_scheduled_push_tests(
        scheduled_at, transaction_factory=factory(cursor)
    )

    assert len(tests) == 1
    assert tests[0].target_subscription_ids == (7, 8)
    sql, params = cursor.calls[0]
    assert "SET status='PROCESSING', worker_started_at=now()" in sql
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert params == (scheduled_at, 5)


def test_list_scheduled_push_tests_aggregates_delivery_counts_without_secrets():
    scheduled_at = datetime(2026, 10, 6, 18, 30, tzinfo=timezone.utc)
    row = (
        101, scheduled_at, "테스트", "high", 60, "SUCCESS",
        scheduled_at, scheduled_at, None, 2, 0, 0,
    )
    cursor = Cursor([(10,), [row]])
    history = repository.list_scheduled_push_tests(
        5070327065, connection_factory=factory(cursor)
    )

    assert history[0].success_count == 2
    combined_sql = " ".join(call[0] for call in cursor.calls).lower()
    for sensitive in ("endpoint", "p256dh", "auth", "token"):
        assert sensitive not in combined_sql
