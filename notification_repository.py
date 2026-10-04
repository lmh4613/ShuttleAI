"""Aiven-backed notification targets and delivery state for the local worker."""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, time

from database import database_connection, database_transaction
from token_crypto import decrypt_token, encrypt_token


logger = logging.getLogger(__name__)
RUN_STATUSES = {"PROCESSING", "SUCCESS", "PARTIAL", "FAILED", "SKIPPED"}
DELIVERY_STATUSES = {"PROCESSING", "SUCCESS", "FAILED", "EXPIRED"}
RUN_ERROR_CODES = {
    "NO_PUSH_SUBSCRIPTION", "PUSH_EXPIRED", "PUSH_SEND_FAILED",
    "KAKAO_SEND_FAILED", "WEATHER_UNAVAILABLE",
}
DELIVERY_ERROR_CODES = {"PUSH_EXPIRED", "PUSH_SEND_FAILED", "KAKAO_SEND_FAILED"}


class NotificationRepositoryError(RuntimeError):
    """Safe repository failure; details remain in developer logs."""


def _history_error(operation: str, exc: Exception) -> NotificationRepositoryError:
    logger.warning("Notification history %s failed type=%s", operation, type(exc).__name__)
    return NotificationRepositoryError("알림 발송 이력을 처리하지 못했습니다.")


@dataclass(frozen=True)
class NotificationHistoryEntry:
    id: int
    started_at: datetime
    user_id: int
    nickname: str | None
    favorite_id: int | None
    region_name: str | None
    route_name: str | None
    trip_type: str | None
    boarding_name: str | None
    destination_name: str | None
    scheduled_time: time
    effective_channel: str
    status: str
    error_code: str | None
    success_count: int
    failed_count: int
    expired_count: int


HISTORY_SQL = """
SELECT nr.id, nr.started_at, nr.user_id, u.nickname, nr.favorite_id,
       rg.name, r.name, r.trip_type, boarding_stop.name, destination_stop.name,
       nr.scheduled_time, nr.effective_channel, nr.status, nr.error_code,
       COUNT(nd.id) FILTER (WHERE nd.status='SUCCESS') AS success_count,
       COUNT(nd.id) FILTER (WHERE nd.status='FAILED') AS failed_count,
       COUNT(nd.id) FILTER (WHERE nd.status='EXPIRED') AS expired_count
  FROM notification_runs nr
  JOIN users u ON u.id=nr.user_id
  LEFT JOIN favorites f ON f.id=nr.favorite_id
  LEFT JOIN routes r ON r.id=f.route_id
  LEFT JOIN regions rg ON rg.id=r.region_id
  LEFT JOIN route_stops boarding_rs ON boarding_rs.id=f.boarding_route_stop_id
  LEFT JOIN stops boarding_stop ON boarding_stop.id=boarding_rs.stop_id
  LEFT JOIN route_stops destination_rs ON destination_rs.id=f.alighting_route_stop_id
  LEFT JOIN stops destination_stop ON destination_stop.id=destination_rs.stop_id
  LEFT JOIN notification_deliveries nd ON nd.notification_run_id=nr.id
 WHERE nr.started_at >= now() - (%s * INTERVAL '1 day')
 GROUP BY nr.id, nr.started_at, nr.user_id, u.nickname, nr.favorite_id,
          rg.name, r.name, r.trip_type, boarding_stop.name, destination_stop.name,
          nr.scheduled_time, nr.effective_channel, nr.status, nr.error_code
 ORDER BY nr.started_at DESC
 LIMIT %s
"""


def load_notification_history(
    days: int = 3,
    *,
    limit: int = 100,
    connection_factory: Callable = database_connection,
) -> list[NotificationHistoryEntry]:
    """Load compact Admin history in one query without delivery or credential details."""
    if days not in {1, 3, 7}:
        raise ValueError("Invalid history period")
    limit = min(max(int(limit), 1), 100)
    try:
        with connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(HISTORY_SQL, (days, limit))
                rows = cursor.fetchall()
    except Exception as exc:
        raise _history_error("query", exc) from None
    return [NotificationHistoryEntry(*row) for row in rows]


def claim_notification_run(
    user_id: int,
    favorite_id: int,
    service_date,
    scheduled_time,
    effective_channel: str,
    *,
    transaction_factory: Callable = database_transaction,
) -> int | None:
    """Atomically claim one logical notification; duplicates return None."""
    if effective_channel not in {"PUSH", "KAKAO"}:
        raise ValueError("Invalid delivery channel")
    try:
        with transaction_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO notification_runs "
                    "(user_id, favorite_id, service_date, scheduled_time, "
                    "effective_channel, status) VALUES (%s,%s,%s,%s,%s,'PROCESSING') "
                    "ON CONFLICT (user_id, favorite_id, service_date, scheduled_time) "
                    "DO NOTHING RETURNING id",
                    (user_id, favorite_id, service_date, scheduled_time, effective_channel),
                )
                row = cursor.fetchone()
                return row[0] if row else None
    except Exception as exc:
        raise _history_error("claim", exc) from None


def start_notification_delivery(
    notification_run_id: int,
    channel: str,
    push_subscription_id: int | None = None,
    *,
    transaction_factory: Callable = database_transaction,
) -> int:
    """Create a durable attempt row before an external send begins."""
    if channel not in {"PUSH", "KAKAO"}:
        raise ValueError("Invalid delivery channel")
    if (channel == "PUSH") != (push_subscription_id is not None):
        raise ValueError("Push delivery requires one subscription id")
    try:
        with transaction_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO notification_deliveries "
                    "(notification_run_id, channel, push_subscription_id, status) "
                    "VALUES (%s,%s,%s,'PROCESSING') RETURNING id",
                    (notification_run_id, channel, push_subscription_id),
                )
                return cursor.fetchone()[0]
    except Exception as exc:
        raise _history_error("delivery start", exc) from None


def complete_notification_delivery(
    delivery_id: int,
    status: str,
    error_code: str | None = None,
    *,
    transaction_factory: Callable = database_transaction,
) -> None:
    if status not in DELIVERY_STATUSES - {"PROCESSING"}:
        raise ValueError("Invalid delivery status")
    if error_code is not None and error_code not in DELIVERY_ERROR_CODES:
        raise ValueError("Invalid delivery error code")
    try:
        with transaction_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE notification_deliveries SET status=%s, error_code=%s, "
                    "completed_at=now() WHERE id=%s AND status='PROCESSING' RETURNING id",
                    (status, error_code, delivery_id),
                )
                if cursor.fetchone() is None:
                    raise RuntimeError("Delivery is not processing")
    except Exception as exc:
        raise _history_error("delivery complete", exc) from None


def complete_notification_run(
    notification_run_id: int,
    status: str,
    error_code: str | None = None,
    *,
    transaction_factory: Callable = database_transaction,
) -> None:
    if status not in RUN_STATUSES - {"PROCESSING"}:
        raise ValueError("Invalid run status")
    if error_code is not None and error_code not in RUN_ERROR_CODES:
        raise ValueError("Invalid run error code")
    try:
        with transaction_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE notification_runs SET status=%s, error_code=%s, "
                    "completed_at=now() WHERE id=%s AND status='PROCESSING' RETURNING id",
                    (status, error_code, notification_run_id),
                )
                if cursor.fetchone() is None:
                    raise RuntimeError("Run is not processing")
    except Exception as exc:
        raise _history_error("run complete", exc) from None


@dataclass(frozen=True)
class PushDevice:
    id: int
    endpoint: str
    p256dh: str
    auth: str

    def subscription(self) -> dict:
        return {
            "endpoint": self.endpoint,
            "keys": {"p256dh": self.p256dh, "auth": self.auth},
        }


@dataclass
class NotificationTarget:
    user_id: int
    favorite_id: int
    active_weekday: int
    exclude_holidays: bool
    timezone: str
    delivery_channel: str
    route_name: str
    trip_type: str
    scheduled_time: time
    lead_minutes: int
    boarding_name: str
    boarding_latitude: float
    boarding_longitude: float
    destination_name: str
    destination_latitude: float
    destination_longitude: float
    devices: list[PushDevice] = field(default_factory=list)


TARGET_SQL = """
WITH first_stops AS (
    SELECT DISTINCT ON (rs.route_id)
           rs.route_id, rs.stop_id, rs.scheduled_time
      FROM route_stops rs
     WHERE rs.active=TRUE
     ORDER BY rs.route_id, rs.stop_order
)
SELECT u.id, f.id, nad.weekday, ns.exclude_holidays, ns.timezone, ns.delivery_channel,
       r.name, r.trip_type,
       CASE WHEN r.trip_type='evening' THEN first_rs.scheduled_time
            ELSE boarding_rs.scheduled_time END AS scheduled_time,
       fn.lead_minutes,
       CASE WHEN r.trip_type='evening' THEN first_stop.name
            ELSE boarding_stop.name END AS boarding_name,
       CASE WHEN r.trip_type='evening' THEN first_stop.latitude
            ELSE boarding_stop.latitude END AS boarding_latitude,
       CASE WHEN r.trip_type='evening' THEN first_stop.longitude
            ELSE boarding_stop.longitude END AS boarding_longitude,
       destination_stop.name, destination_stop.latitude, destination_stop.longitude
  FROM users u
  JOIN notification_settings ns ON ns.user_id=u.id
  JOIN notification_active_days nad ON nad.user_id=u.id AND nad.weekday=ANY(%s)
  JOIN favorites f ON f.user_id=u.id AND f.active=TRUE
  JOIN favorite_notifications fn ON fn.favorite_id=f.id AND fn.enabled=TRUE
  JOIN routes r ON r.id=f.route_id AND r.active=TRUE
  JOIN route_stops boarding_rs ON boarding_rs.id=f.boarding_route_stop_id
       AND boarding_rs.active=TRUE
  JOIN stops boarding_stop ON boarding_stop.id=boarding_rs.stop_id AND boarding_stop.active=TRUE
  JOIN route_stops destination_rs ON destination_rs.id=f.alighting_route_stop_id
       AND destination_rs.active=TRUE
  JOIN stops destination_stop ON destination_stop.id=destination_rs.stop_id AND destination_stop.active=TRUE
  LEFT JOIN first_stops first_rs ON first_rs.route_id=r.id
  LEFT JOIN stops first_stop ON first_stop.id=first_rs.stop_id AND first_stop.active=TRUE
 WHERE u.enabled=TRUE
   AND CASE WHEN r.trip_type='evening' THEN first_rs.scheduled_time
            ELSE boarding_rs.scheduled_time END IS NOT NULL
 ORDER BY u.id, f.id, nad.weekday
"""


PUSH_SQL = """
SELECT id, user_id, endpoint, p256dh, auth
  FROM push_subscriptions
 WHERE user_id=ANY(%s) AND enabled=TRUE
   AND (expiration_time IS NULL OR expiration_time > now())
 ORDER BY user_id, id
"""


def load_notification_targets(
    active_weekdays: Iterable[int],
    *,
    connection_factory: Callable = database_connection,
) -> list[NotificationTarget]:
    """Load enabled favorites and all active devices for today/tomorrow."""
    weekdays = sorted(set(active_weekdays))
    if not weekdays:
        return []
    try:
        with connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(TARGET_SQL, (weekdays,))
                target_rows = cursor.fetchall()
                user_ids = sorted({row[0] for row in target_rows})
                device_rows = []
                if user_ids:
                    cursor.execute(PUSH_SQL, (user_ids,))
                    device_rows = cursor.fetchall()
    except Exception as exc:
        logger.warning("Notification target query failed type=%s", type(exc).__name__)
        raise NotificationRepositoryError("알림 대상 정보를 불러오지 못했습니다.") from None

    devices_by_user = defaultdict(list)
    for row in device_rows:
        devices_by_user[row[1]].append(PushDevice(row[0], row[2], row[3], row[4]))

    return [
        NotificationTarget(
            user_id=row[0], favorite_id=row[1], active_weekday=row[2],
            exclude_holidays=row[3], timezone=row[4], delivery_channel=row[5],
            route_name=row[6], trip_type=row[7], scheduled_time=row[8], lead_minutes=row[9],
            boarding_name=row[10], boarding_latitude=float(row[11]),
            boarding_longitude=float(row[12]), destination_name=row[13],
            destination_latitude=float(row[14]), destination_longitude=float(row[15]),
            devices=list(devices_by_user[row[0]]),
        )
        for row in target_rows
    ]


def deactivate_push_device(
    subscription_id: int,
    user_id: int,
    *,
    transaction_factory: Callable = database_transaction,
) -> bool:
    """Deactivate only the selected device owned by the selected DB user."""
    try:
        with transaction_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE push_subscriptions SET enabled=FALSE, revoked_at=now() "
                    "WHERE id=%s AND user_id=%s AND enabled=TRUE RETURNING id",
                    (subscription_id, user_id),
                )
                return cursor.fetchone() is not None
    except Exception as exc:
        logger.warning("Expired Push update failed type=%s", type(exc).__name__)
        raise NotificationRepositoryError("만료된 기기 알림 상태를 저장하지 못했습니다.") from None


def load_kakao_credentials(
    user_id: int,
    *,
    connection_factory: Callable = database_connection,
) -> tuple[str | None, str | None]:
    """Decrypt one worker user's DB credentials without exposing plaintext."""
    try:
        with connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT access_token_ciphertext, refresh_token_ciphertext "
                    "FROM kakao_credentials WHERE user_id=%s",
                    (user_id,),
                )
                row = cursor.fetchone()
        if row is None:
            return None, None
        return decrypt_token(row[0]), decrypt_token(row[1])
    except Exception as exc:
        logger.warning("Kakao credential load failed type=%s", type(exc).__name__)
        raise NotificationRepositoryError("카카오 알림 인증정보를 불러오지 못했습니다.") from None


def save_kakao_credentials(
    user_id: int,
    access_token: str,
    refresh_token: str | None,
    *,
    transaction_factory: Callable = database_transaction,
) -> None:
    """Persist refreshed encrypted credentials for the worker's DB user."""
    try:
        with transaction_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE kakao_credentials SET access_token_ciphertext=%s, "
                    "refresh_token_ciphertext=COALESCE(%s, refresh_token_ciphertext), "
                    "token_updated_at=now() WHERE user_id=%s",
                    (encrypt_token(access_token), encrypt_token(refresh_token), user_id),
                )
    except Exception as exc:
        logger.warning("Kakao credential update failed type=%s", type(exc).__name__)
        raise NotificationRepositoryError("갱신된 카카오 인증정보를 저장하지 못했습니다.") from None
