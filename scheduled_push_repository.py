"""Aiven-backed scheduled Web Push tests for admin diagnostics."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from database import database_connection, database_transaction
from notification_repository import PushDevice


logger = logging.getLogger(__name__)
TEST_STATUSES = {"SCHEDULED", "PROCESSING", "SUCCESS", "PARTIAL", "FAILED"}
TEST_DELIVERY_STATUSES = {"PROCESSING", "SUCCESS", "FAILED", "EXPIRED"}
TEST_ERROR_CODES = {"NO_PUSH_SUBSCRIPTION", "PUSH_EXPIRED", "PUSH_SEND_FAILED"}
TEST_URGENCIES = {"normal", "high"}


class ScheduledPushRepositoryError(RuntimeError):
    """Safe scheduled Push failure; details remain in logs."""


def _safe_error(operation: str, exc: Exception) -> ScheduledPushRepositoryError:
    logger.warning("Scheduled Push %s failed type=%s", operation, type(exc).__name__)
    return ScheduledPushRepositoryError("예약 Push 테스트 정보를 처리하지 못했습니다.")


@dataclass(frozen=True)
class ScheduledPushDevice:
    id: int
    label: str


@dataclass(frozen=True)
class ScheduledPushTest:
    id: int
    user_id: int
    scheduled_at: datetime
    message: str
    urgency: str
    ttl_seconds: int
    target_subscription_ids: tuple[int, ...] | None
    status: str


@dataclass(frozen=True)
class ScheduledPushTestSummary:
    id: int
    scheduled_at: datetime
    message: str
    urgency: str
    ttl_seconds: int
    status: str
    worker_started_at: datetime | None
    send_completed_at: datetime | None
    target_count: int | None
    success_count: int
    failed_count: int
    expired_count: int


def _lookup_enabled_user_id(cursor, kakao_user_id: int) -> int:
    cursor.execute(
        "SELECT id FROM users WHERE kakao_user_id=%s AND enabled=TRUE",
        (kakao_user_id,),
    )
    row = cursor.fetchone()
    if row is None:
        raise ScheduledPushRepositoryError(
            "등록된 사용자를 찾을 수 없습니다. 다시 로그인한 뒤 시도해 주세요."
        )
    return row[0]


def load_push_test_devices(
    kakao_user_id: int,
    *,
    connection_factory: Callable = database_connection,
) -> list[ScheduledPushDevice]:
    """List active devices without exposing endpoint/key material."""
    try:
        with connection_factory() as connection:
            with connection.cursor() as cursor:
                user_id = _lookup_enabled_user_id(cursor, kakao_user_id)
                cursor.execute(
                    "SELECT id, last_seen_at, created_at FROM push_subscriptions "
                    "WHERE user_id=%s AND enabled=TRUE AND revoked_at IS NULL "
                    "AND (expiration_time IS NULL OR expiration_time>now()) "
                    "ORDER BY last_seen_at DESC NULLS LAST, id DESC",
                    (user_id,),
                )
                rows = cursor.fetchall()
    except ScheduledPushRepositoryError:
        raise
    except Exception as exc:
        raise _safe_error("device query", exc) from None
    return [
        ScheduledPushDevice(
            id=row[0],
            label=f"기기 #{row[0]} · 최근 확인 {row[1] or row[2]}",
        )
        for row in rows
    ]


def create_scheduled_push_test(
    kakao_user_id: int,
    scheduled_at: datetime,
    message: str,
    urgency: str,
    target_subscription_ids: list[int] | None = None,
    *,
    transaction_factory: Callable = database_transaction,
) -> int:
    message = (message or "").strip()
    if not message or len(message) > 500:
        raise ValueError("Invalid scheduled Push message")
    if urgency not in TEST_URGENCIES:
        raise ValueError("Invalid scheduled Push urgency")
    target_ids = sorted(set(int(item) for item in target_subscription_ids or []))
    try:
        with transaction_factory() as connection:
            with connection.cursor() as cursor:
                user_id = _lookup_enabled_user_id(cursor, kakao_user_id)
                if target_ids:
                    cursor.execute(
                        "SELECT id FROM push_subscriptions "
                        "WHERE user_id=%s AND enabled=TRUE AND revoked_at IS NULL "
                        "AND (expiration_time IS NULL OR expiration_time>now()) "
                        "AND id=ANY(%s)",
                        (user_id, target_ids),
                    )
                    owned_ids = {row[0] for row in cursor.fetchall()}
                    if owned_ids != set(target_ids):
                        raise ScheduledPushRepositoryError(
                            "선택한 Push 기기를 찾을 수 없습니다."
                        )
                cursor.execute(
                    "INSERT INTO scheduled_push_tests "
                    "(user_id, scheduled_at, message, urgency, target_subscription_ids) "
                    "VALUES (%s,%s,%s,%s,%s) RETURNING id",
                    (user_id, scheduled_at, message, urgency, target_ids or None),
                )
                return cursor.fetchone()[0]
    except ScheduledPushRepositoryError:
        raise
    except Exception as exc:
        raise _safe_error("create", exc) from None


def list_scheduled_push_tests(
    kakao_user_id: int,
    *,
    limit: int = 20,
    connection_factory: Callable = database_connection,
) -> list[ScheduledPushTestSummary]:
    limit = min(max(int(limit), 1), 50)
    try:
        with connection_factory() as connection:
            with connection.cursor() as cursor:
                user_id = _lookup_enabled_user_id(cursor, kakao_user_id)
                cursor.execute(
                    """
                    SELECT spt.id, spt.scheduled_at, spt.message, spt.urgency,
                           spt.ttl_seconds, spt.status, spt.worker_started_at,
                           spt.send_completed_at,
                           CASE WHEN spt.target_subscription_ids IS NULL
                                THEN NULL ELSE cardinality(spt.target_subscription_ids) END,
                           COUNT(d.id) FILTER (WHERE d.status='SUCCESS'),
                           COUNT(d.id) FILTER (WHERE d.status='FAILED'),
                           COUNT(d.id) FILTER (WHERE d.status='EXPIRED')
                      FROM scheduled_push_tests spt
                      LEFT JOIN scheduled_push_test_deliveries d
                        ON d.scheduled_push_test_id=spt.id
                     WHERE spt.user_id=%s
                     GROUP BY spt.id
                     ORDER BY spt.created_at DESC, spt.id DESC
                     LIMIT %s
                    """,
                    (user_id, limit),
                )
                rows = cursor.fetchall()
    except ScheduledPushRepositoryError:
        raise
    except Exception as exc:
        raise _safe_error("list", exc) from None
    return [ScheduledPushTestSummary(*row) for row in rows]


def claim_due_scheduled_push_tests(
    now: datetime,
    *,
    limit: int = 5,
    transaction_factory: Callable = database_transaction,
) -> list[ScheduledPushTest]:
    try:
        with transaction_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE scheduled_push_tests spt
                       SET status='PROCESSING', worker_started_at=now()
                     WHERE spt.id IN (
                         SELECT id FROM scheduled_push_tests
                          WHERE status='SCHEDULED' AND scheduled_at<=%s
                          ORDER BY scheduled_at, id
                          LIMIT %s
                          FOR UPDATE SKIP LOCKED
                     )
                     RETURNING id, user_id, scheduled_at, message, urgency,
                               ttl_seconds, target_subscription_ids, status
                    """,
                    (now, limit),
                )
                rows = cursor.fetchall()
    except Exception as exc:
        raise _safe_error("claim due", exc) from None
    return [
        ScheduledPushTest(
            id=row[0],
            user_id=row[1],
            scheduled_at=row[2],
            message=row[3],
            urgency=row[4],
            ttl_seconds=row[5],
            target_subscription_ids=tuple(row[6]) if row[6] is not None else None,
            status=row[7],
        )
        for row in rows
    ]


def load_scheduled_push_devices_for_test(
    test: ScheduledPushTest,
    *,
    connection_factory: Callable = database_connection,
) -> list[PushDevice]:
    params = [test.user_id]
    target_filter = ""
    if test.target_subscription_ids is not None:
        params.append(list(test.target_subscription_ids))
        target_filter = "AND id=ANY(%s)"
    try:
        with connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT id, endpoint, p256dh, auth FROM push_subscriptions "
                    "WHERE user_id=%s AND enabled=TRUE AND revoked_at IS NULL "
                    "AND (expiration_time IS NULL OR expiration_time>now()) "
                    f"{target_filter} ORDER BY id",
                    tuple(params),
                )
                rows = cursor.fetchall()
    except Exception as exc:
        raise _safe_error("delivery device query", exc) from None
    return [PushDevice(row[0], row[1], row[2], row[3]) for row in rows]


def start_scheduled_push_delivery(
    test_id: int,
    push_subscription_id: int,
    *,
    transaction_factory: Callable = database_transaction,
) -> int:
    try:
        with transaction_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO scheduled_push_test_deliveries "
                    "(scheduled_push_test_id, push_subscription_id, status) "
                    "VALUES (%s,%s,'PROCESSING') RETURNING id",
                    (test_id, push_subscription_id),
                )
                return cursor.fetchone()[0]
    except Exception as exc:
        raise _safe_error("delivery start", exc) from None


def complete_scheduled_push_delivery(
    delivery_id: int,
    status: str,
    error_code: str | None = None,
    *,
    transaction_factory: Callable = database_transaction,
) -> None:
    if status not in TEST_DELIVERY_STATUSES - {"PROCESSING"}:
        raise ValueError("Invalid scheduled Push delivery status")
    if error_code is not None and error_code not in TEST_ERROR_CODES:
        raise ValueError("Invalid scheduled Push delivery error code")
    try:
        with transaction_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE scheduled_push_test_deliveries "
                    "SET status=%s, error_code=%s, send_completed_at=now() "
                    "WHERE id=%s AND status='PROCESSING' RETURNING id",
                    (status, error_code, delivery_id),
                )
                if cursor.fetchone() is None:
                    raise RuntimeError("Delivery is not processing")
    except Exception as exc:
        raise _safe_error("delivery complete", exc) from None


def complete_scheduled_push_test(
    test_id: int,
    status: str,
    error_code: str | None = None,
    *,
    transaction_factory: Callable = database_transaction,
) -> None:
    if status not in TEST_STATUSES - {"SCHEDULED", "PROCESSING"}:
        raise ValueError("Invalid scheduled Push test status")
    if error_code is not None and error_code not in TEST_ERROR_CODES:
        raise ValueError("Invalid scheduled Push test error code")
    try:
        with transaction_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE scheduled_push_tests "
                    "SET status=%s, error_code=%s, send_completed_at=now() "
                    "WHERE id=%s AND status='PROCESSING' RETURNING id",
                    (status, error_code, test_id),
                )
                if cursor.fetchone() is None:
                    raise RuntimeError("Scheduled Push test is not processing")
    except Exception as exc:
        raise _safe_error("complete", exc) from None
