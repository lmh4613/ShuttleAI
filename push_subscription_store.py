"""Persistent ownership store for browser Web Push subscriptions."""

from __future__ import annotations

import hashlib
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from database import database_connection, database_transaction
from web_push import validate_subscription


logger = logging.getLogger(__name__)


class PushSubscriptionError(RuntimeError):
    """A safe, user-displayable Push subscription error."""


class PushSubscriptionValidationError(PushSubscriptionError):
    pass


class PushSubscriptionUserNotFound(PushSubscriptionError):
    pass


class PushSubscriptionDatabaseError(PushSubscriptionError):
    pass


@dataclass(frozen=True)
class CanonicalSubscription:
    endpoint: str
    p256dh: str
    auth: str
    expiration_time: datetime | None
    endpoint_hash: str


OWNERSHIP_NONE = "none"
OWNERSHIP_CURRENT = "current"
OWNERSHIP_OTHER = "other"


def endpoint_hash(endpoint: str) -> str:
    """Return the stable lowercase SHA-256 endpoint identity."""
    if not isinstance(endpoint, str) or not endpoint:
        raise PushSubscriptionValidationError("Push subscription endpoint is missing.")
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()


def _expiration_time(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PushSubscriptionValidationError("Push subscription expiration is invalid.")
    if not math.isfinite(value) or value < 0:
        raise PushSubscriptionValidationError("Push subscription expiration is invalid.")
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise PushSubscriptionValidationError("Push subscription expiration is invalid.") from None


def canonicalize_subscription(value: object) -> CanonicalSubscription:
    """Validate browser JSON and create the exact database representation."""
    try:
        normalized = validate_subscription(value)
    except ValueError as exc:
        message = str(exc)
        if "endpoint" in message:
            reason = "Push subscription endpoint is missing or invalid."
        elif "p256dh" in message:
            reason = "Push subscription p256dh key is missing."
        elif "auth" in message:
            reason = "Push subscription auth key is missing."
        else:
            reason = "Push subscription is invalid."
        raise PushSubscriptionValidationError(reason) from None
    endpoint = normalized["endpoint"]
    return CanonicalSubscription(
        endpoint=endpoint,
        p256dh=normalized["keys"]["p256dh"],
        auth=normalized["keys"]["auth"],
        expiration_time=_expiration_time(normalized.get("expirationTime")),
        endpoint_hash=endpoint_hash(endpoint),
    )


def _lookup_enabled_user_id(cursor, kakao_user_id: int) -> int:
    cursor.execute(
        "SELECT id FROM users WHERE kakao_user_id=%s AND enabled=TRUE",
        (kakao_user_id,),
    )
    row = cursor.fetchone()
    if row is None:
        raise PushSubscriptionUserNotFound(
            "등록된 사용자를 찾을 수 없습니다. 다시 로그인한 뒤 시도해 주세요."
        )
    return row[0]


def register_subscription(
    kakao_user_id: int,
    subscription: object,
    *,
    transaction_factory: Callable = database_transaction,
) -> None:
    """Atomically insert, refresh, or transfer one endpoint to this user."""
    canonical = canonicalize_subscription(subscription)
    try:
        with transaction_factory() as connection:
            with connection.cursor() as cursor:
                user_id = _lookup_enabled_user_id(cursor, kakao_user_id)
                cursor.execute(
                    "INSERT INTO push_subscriptions "
                    "(user_id, endpoint, endpoint_hash, p256dh, auth, expiration_time, enabled, "
                    "last_seen_at, owner_changed_at, revoked_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, TRUE, now(), now(), NULL) "
                    "ON CONFLICT (endpoint_hash) DO UPDATE SET "
                    "user_id=EXCLUDED.user_id, endpoint=EXCLUDED.endpoint, "
                    "p256dh=EXCLUDED.p256dh, auth=EXCLUDED.auth, "
                    "expiration_time=EXCLUDED.expiration_time, enabled=TRUE, "
                    "last_seen_at=now(), revoked_at=NULL, "
                    "owner_changed_at=CASE "
                    "WHEN push_subscriptions.user_id IS DISTINCT FROM EXCLUDED.user_id "
                    "THEN now() ELSE push_subscriptions.owner_changed_at END",
                    (
                        user_id, canonical.endpoint, canonical.endpoint_hash,
                        canonical.p256dh, canonical.auth, canonical.expiration_time,
                    ),
                )
    except PushSubscriptionError:
        raise
    except Exception as exc:
        logger.warning("Push subscription registration failed type=%s", type(exc).__name__)
        raise PushSubscriptionDatabaseError(
            "기기 알림 정보를 저장하지 못했습니다. 잠시 후 다시 시도해 주세요."
        ) from None


def is_current_subscription_registered(
    kakao_user_id: int,
    subscription: object,
    *,
    connection_factory: Callable = database_connection,
) -> bool:
    """Check whether this browser endpoint is enabled for this exact user."""
    return subscription_ownership(
        kakao_user_id, subscription, connection_factory=connection_factory
    ) == OWNERSHIP_CURRENT


def subscription_ownership(
    kakao_user_id: int,
    subscription: object,
    *,
    connection_factory: Callable = database_connection,
) -> str:
    """Return none/current/other for the browser endpoint's active DB owner."""
    canonical = canonicalize_subscription(subscription)
    try:
        with connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT u.kakao_user_id FROM push_subscriptions ps "
                    "JOIN users u ON u.id=ps.user_id "
                    "WHERE ps.endpoint_hash=%s AND ps.enabled=TRUE AND u.enabled=TRUE",
                    (canonical.endpoint_hash,),
                )
                row = cursor.fetchone()
                if row is None:
                    return OWNERSHIP_NONE
                return OWNERSHIP_CURRENT if row[0] == kakao_user_id else OWNERSHIP_OTHER
    except PushSubscriptionError:
        raise
    except Exception as exc:
        logger.warning("Push subscription lookup failed type=%s", type(exc).__name__)
        raise PushSubscriptionDatabaseError(
            "기기 알림 등록 상태를 확인하지 못했습니다. 잠시 후 다시 시도해 주세요."
        ) from None


def deactivate_subscription(
    kakao_user_id: int,
    subscription: object,
    *,
    transaction_factory: Callable = database_transaction,
) -> bool:
    """Soft-deactivate only when the requesting user owns this endpoint."""
    canonical = canonicalize_subscription(subscription)
    try:
        with transaction_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE push_subscriptions ps SET enabled=FALSE, revoked_at=now() "
                    "FROM users u WHERE u.id=ps.user_id AND u.kakao_user_id=%s "
                    "AND ps.endpoint_hash=%s AND ps.enabled=TRUE RETURNING ps.id",
                    (kakao_user_id, canonical.endpoint_hash),
                )
                return cursor.fetchone() is not None
    except PushSubscriptionError:
        raise
    except Exception as exc:
        logger.warning("Push subscription deactivation failed type=%s", type(exc).__name__)
        raise PushSubscriptionDatabaseError(
            "기기 알림 등록 해제를 저장하지 못했습니다. 잠시 후 다시 시도해 주세요."
        ) from None


def count_active_push_subscriptions(
    kakao_user_id: int,
    *,
    connection_factory: Callable = database_connection,
) -> int:
    """Count this enabled user's active, unrevoked, unexpired devices."""
    try:
        with connection_factory() as connection:
            with connection.cursor() as cursor:
                user_id = _lookup_enabled_user_id(cursor, kakao_user_id)
                cursor.execute(
                    "SELECT count(*) FROM push_subscriptions "
                    "WHERE user_id=%s AND enabled=TRUE AND revoked_at IS NULL "
                    "AND (expiration_time IS NULL OR expiration_time>now())",
                    (user_id,),
                )
                return int(cursor.fetchone()[0])
    except PushSubscriptionError:
        raise
    except Exception as exc:
        logger.warning("Push subscription count failed type=%s", type(exc).__name__)
        raise PushSubscriptionDatabaseError(
            "등록된 알림 기기 수를 확인하지 못했습니다. 잠시 후 다시 시도해 주세요."
        ) from None


def deactivate_all_subscriptions(
    kakao_user_id: int,
    *,
    transaction_factory: Callable = database_transaction,
) -> int:
    """Soft-revoke every active Push subscription owned by one enabled user."""
    try:
        with transaction_factory() as connection:
            with connection.cursor() as cursor:
                user_id = _lookup_enabled_user_id(cursor, kakao_user_id)
                cursor.execute(
                    "UPDATE push_subscriptions SET enabled=FALSE, revoked_at=now() "
                    "WHERE user_id=%s AND enabled=TRUE AND revoked_at IS NULL "
                    "RETURNING id",
                    (user_id,),
                )
                return len(cursor.fetchall())
    except PushSubscriptionError:
        raise
    except Exception as exc:
        logger.warning("Push subscription bulk deactivation failed type=%s", type(exc).__name__)
        raise PushSubscriptionDatabaseError(
            "모든 기기 알림을 해제하지 못했습니다. 잠시 후 다시 시도해 주세요."
        ) from None


def mark_subscription_expired(
    kakao_user_id: int,
    subscription: object,
    *,
    transaction_factory: Callable = database_transaction,
) -> bool:
    """Apply the same owner-only deactivation after a 404/410 send response."""
    return deactivate_subscription(
        kakao_user_id,
        subscription,
        transaction_factory=transaction_factory,
    )
