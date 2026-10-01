"""Aiven source of truth for user favorites and notification preferences."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from decimal import Decimal, InvalidOperation

from database import database_connection, database_transaction


logger = logging.getLogger(__name__)
CHANNELS = {"PUSH", "KAKAO"}
GLOBAL_POLICIES = {"AUTO", "PUSH", "KAKAO"}


class UserSettingsError(RuntimeError):
    """Safe, user-displayable repository error."""


class UserNotFoundError(UserSettingsError):
    pass


class FavoriteMappingError(UserSettingsError):
    pass


class DuplicateFavoriteError(UserSettingsError):
    pass


class PermissionDeniedError(UserSettingsError):
    pass


class InvalidSettingsError(UserSettingsError):
    pass


def _db_error(operation: str, exc: Exception) -> UserSettingsError:
    logger.warning("User settings %s failed type=%s", operation, type(exc).__name__)
    return UserSettingsError("설정 정보를 처리하지 못했습니다. 잠시 후 다시 시도해 주세요.")


def _user_row(cursor, kakao_user_id: int):
    cursor.execute(
        "SELECT id, role FROM users WHERE kakao_user_id=%s AND enabled=TRUE",
        (kakao_user_id,),
    )
    row = cursor.fetchone()
    if row is None:
        raise UserNotFoundError("등록된 사용자를 찾을 수 없습니다. 다시 로그인해 주세요.")
    return row


def get_notification_settings(
    kakao_user_id: int,
    *,
    connection_factory: Callable = database_connection,
) -> dict:
    try:
        with connection_factory() as connection:
            with connection.cursor() as cursor:
                user_id, role = _user_row(cursor, kakao_user_id)
                cursor.execute(
                    "SELECT exclude_holidays, timezone, delivery_channel "
                    "FROM notification_settings WHERE user_id=%s",
                    (user_id,),
                )
                settings = cursor.fetchone()
                if settings is None:
                    raise UserSettingsError("알림 설정을 찾을 수 없습니다.")
                cursor.execute(
                    "SELECT weekday FROM notification_active_days "
                    "WHERE user_id=%s ORDER BY weekday",
                    (user_id,),
                )
                active_days = [row[0] for row in cursor.fetchall()]
                cursor.execute(
                    "SELECT count(*) FROM push_subscriptions WHERE user_id=%s "
                    "AND enabled=TRUE AND (expiration_time IS NULL OR expiration_time>now())",
                    (user_id,),
                )
                push_devices = cursor.fetchone()[0]
        return {
            "user_id": user_id, "role": role, "exclude_holidays": settings[0],
            "timezone": settings[1], "delivery_channel": settings[2],
            "active_days": active_days, "active_push_devices": push_devices,
        }
    except UserSettingsError:
        raise
    except Exception as exc:
        raise _db_error("read", exc) from None


def update_notification_settings(
    kakao_user_id: int,
    *,
    active_days: Iterable[int],
    exclude_holidays: bool,
    delivery_channel: str,
    transaction_factory: Callable = database_transaction,
) -> None:
    days = sorted(set(active_days))
    if any(isinstance(day, bool) or not isinstance(day, int) or not 0 <= day <= 6 for day in days):
        raise InvalidSettingsError("알림 요일을 올바르게 선택해 주세요.")
    if not isinstance(exclude_holidays, bool) or delivery_channel not in CHANNELS:
        raise InvalidSettingsError("알림 설정값이 올바르지 않습니다.")
    try:
        with transaction_factory() as connection:
            with connection.cursor() as cursor:
                user_id, _role = _user_row(cursor, kakao_user_id)
                cursor.execute(
                    "UPDATE notification_settings SET exclude_holidays=%s, delivery_channel=%s "
                    "WHERE user_id=%s",
                    (exclude_holidays, delivery_channel, user_id),
                )
                cursor.execute("DELETE FROM notification_active_days WHERE user_id=%s", (user_id,))
                cursor.executemany(
                    "INSERT INTO notification_active_days (user_id, weekday) VALUES (%s,%s)",
                    [(user_id, day) for day in days],
                )
    except UserSettingsError:
        raise
    except Exception as exc:
        raise _db_error("update", exc) from None


FAVORITES_SQL = """
SELECT f.id, rg.code, r.name, r.trip_type,
       bs.name, brs.scheduled_time, bs.latitude, bs.longitude,
       als.name, ars.scheduled_time, als.latitude, als.longitude,
       fn.enabled, fn.lead_minutes
  FROM favorites f
  JOIN users u ON u.id=f.user_id
  JOIN routes r ON r.id=f.route_id
  JOIN regions rg ON rg.id=r.region_id
  JOIN route_stops brs ON brs.id=f.boarding_route_stop_id AND brs.route_id=f.route_id
  JOIN stops bs ON bs.id=brs.stop_id
  JOIN route_stops ars ON ars.id=f.alighting_route_stop_id AND ars.route_id=f.route_id
  JOIN stops als ON als.id=ars.stop_id
  JOIN favorite_notifications fn ON fn.favorite_id=f.id
 WHERE u.kakao_user_id=%s AND u.enabled=TRUE
 ORDER BY f.created_at, f.id
"""

DASHBOARD_USER_SQL = """
SELECT u.id, u.role, ns.exclude_holidays, ns.timezone, ns.delivery_channel,
       COALESCE(
           (SELECT array_agg(nad.weekday ORDER BY nad.weekday)
              FROM notification_active_days nad WHERE nad.user_id=u.id),
           ARRAY[]::smallint[]
       ) AS active_days,
       (SELECT count(*) FROM push_subscriptions ps
         WHERE ps.user_id=u.id AND ps.enabled=TRUE
           AND (ps.expiration_time IS NULL OR ps.expiration_time>now())) AS active_push_devices
  FROM users u
  JOIN notification_settings ns ON ns.user_id=u.id
 WHERE u.kakao_user_id=%s AND u.enabled=TRUE
"""

FAVORITES_BY_USER_SQL = """
SELECT f.id, rg.code, r.name, r.trip_type,
       bs.name, brs.scheduled_time, bs.latitude, bs.longitude,
       als.name, ars.scheduled_time, als.latitude, als.longitude,
       fn.enabled, fn.lead_minutes
  FROM favorites f
  JOIN routes r ON r.id=f.route_id
  JOIN regions rg ON rg.id=r.region_id
  JOIN route_stops brs ON brs.id=f.boarding_route_stop_id AND brs.route_id=f.route_id
  JOIN stops bs ON bs.id=brs.stop_id
  JOIN route_stops ars ON ars.id=f.alighting_route_stop_id AND ars.route_id=f.route_id
  JOIN stops als ON als.id=ars.stop_id
  JOIN favorite_notifications fn ON fn.favorite_id=f.id
 WHERE f.user_id=%s
 ORDER BY f.created_at, f.id
"""


def _time_text(value) -> str:
    return value.strftime("%H:%M") if value is not None else "-"


def _favorite_record(row) -> dict:
    return {
        "favorite_id": row[0], "region": row[1], "route_name": row[2],
        "trip_type": "퇴근길" if row[3] == "evening" else "출근길",
        "board_stop": row[4], "board_time": _time_text(row[5]),
        "board_lat": float(row[6]), "board_lon": float(row[7]),
        "arrive_stop": row[8], "arrive_time": _time_text(row[9]),
        "arrive_lat": float(row[10]), "arrive_lon": float(row[11]),
        "notify_enabled": row[12], "notify_min": row[13],
    }


def get_user_dashboard_data(
    kakao_user_id: int,
    *,
    connection_factory: Callable = database_connection,
) -> dict:
    """Load all ordinary-user settings in two SELECTs over one connection."""
    try:
        with connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(DASHBOARD_USER_SQL, (kakao_user_id,))
                user_row = cursor.fetchone()
                if user_row is None:
                    raise UserNotFoundError(
                        "등록된 사용자 설정을 찾을 수 없습니다. 다시 로그인해 주세요."
                    )
                cursor.execute(FAVORITES_BY_USER_SQL, (user_row[0],))
                favorite_rows = cursor.fetchall()
        settings = {
            "user_id": user_row[0], "role": user_row[1],
            "exclude_holidays": user_row[2], "timezone": user_row[3],
            "delivery_channel": user_row[4], "active_days": list(user_row[5]),
            "active_push_devices": user_row[6],
        }
        return {
            "kakao_user_id": kakao_user_id,
            "internal_user_id": user_row[0],
            "notification_settings": settings,
            "favorites": [_favorite_record(row) for row in favorite_rows],
        }
    except UserSettingsError:
        raise
    except Exception as exc:
        raise _db_error("dashboard read", exc) from None


def list_favorites(
    kakao_user_id: int,
    *,
    connection_factory: Callable = database_connection,
) -> list[dict]:
    try:
        with connection_factory() as connection:
            with connection.cursor() as cursor:
                _user_row(cursor, kakao_user_id)
                cursor.execute(FAVORITES_SQL, (kakao_user_id,))
                rows = cursor.fetchall()
        return [_favorite_record(row) for row in rows]
    except UserSettingsError:
        raise
    except Exception as exc:
        raise _db_error("favorite list", exc) from None


def _coordinate(value: object) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise FavoriteMappingError("정류장 좌표가 올바르지 않습니다.") from None
    quantized = result.quantize(Decimal("0.000001"))
    if not result.is_finite() or result != quantized:
        raise FavoriteMappingError("정류장 좌표가 DB 정보와 일치하지 않습니다.")
    return quantized


def _resolve_route_stop(cursor, route_id, selection, prefix, required_permission):
    cursor.execute(
        "SELECT rs.id, rs.stop_order, rs.scheduled_time, rs.boarding_allowed, "
        "rs.alighting_allowed, rs.is_default_dropoff, rs.source_kind "
        "FROM route_stops rs JOIN stops s ON s.id=rs.stop_id "
        "WHERE rs.route_id=%s AND s.name=%s AND s.latitude=%s AND s.longitude=%s",
        (route_id, selection[f"{prefix}_stop"], _coordinate(selection[f"{prefix}_lat"]),
         _coordinate(selection[f"{prefix}_lon"])),
    )
    rows = cursor.fetchall()
    if len(rows) != 1:
        raise FavoriteMappingError("선택한 정류장을 노선 DB에서 정확히 찾을 수 없습니다.")
    row = rows[0]
    permission_index = 3 if required_permission == "boarding" else 4
    if not row[permission_index]:
        raise FavoriteMappingError("선택한 정류장은 해당 이용 방식으로 사용할 수 없습니다.")
    return row


def create_favorite(
    kakao_user_id: int,
    selection: Mapping,
    *,
    transaction_factory: Callable = database_transaction,
) -> int:
    required = {"region", "route_name", "trip_type", "board_stop", "board_lat", "board_lon",
                "arrive_stop", "arrive_lat", "arrive_lon"}
    if not required.issubset(selection):
        raise FavoriteMappingError("즐겨찾기 선택 정보가 부족합니다.")
    expected_trip = "evening" if "퇴근" in str(selection["trip_type"]) else "morning"
    try:
        with transaction_factory() as connection:
            with connection.cursor() as cursor:
                user_id, _role = _user_row(cursor, kakao_user_id)
                cursor.execute(
                    "SELECT r.id, r.trip_type FROM routes r JOIN regions rg ON rg.id=r.region_id "
                    "WHERE rg.code=%s AND r.name=%s AND r.active=TRUE",
                    (selection["region"], selection["route_name"]),
                )
                route = cursor.fetchone()
                if route is None or route[1] != expected_trip:
                    raise FavoriteMappingError("선택한 노선을 DB에서 찾을 수 없습니다.")
                route_id = route[0]
                boarding = _resolve_route_stop(cursor, route_id, selection, "board", "boarding")
                destination = _resolve_route_stop(cursor, route_id, selection, "arrive", "alighting")
                if expected_trip == "evening":
                    cursor.execute(
                        "SELECT id FROM route_stops WHERE route_id=%s ORDER BY stop_order LIMIT 1",
                        (route_id,),
                    )
                    if cursor.fetchone()[0] != boarding[0]:
                        raise FavoriteMappingError("퇴근 노선 탑승지는 첫 정류장이어야 합니다.")
                if selection["arrive_stop"] == "판교 제2테크노밸리" and not (
                    destination[5] and destination[6] == "system_default"
                ):
                    raise FavoriteMappingError("판교 기본 하차지를 정확히 매핑하지 못했습니다.")
                if "(하차만)" in str(selection["arrive_stop"]) and (destination[3] or not destination[4]):
                    raise FavoriteMappingError("하차 전용 정류장 정보가 올바르지 않습니다.")
                cursor.execute(
                    "INSERT INTO favorites "
                    "(user_id, route_id, boarding_route_stop_id, alighting_route_stop_id) "
                    "VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING id",
                    (user_id, route_id, boarding[0], destination[0]),
                )
                inserted = cursor.fetchone()
                if inserted is None:
                    raise DuplicateFavoriteError("이미 등록된 구간입니다.")
                favorite_id = inserted[0]
                cursor.execute(
                    "INSERT INTO favorite_notifications (favorite_id, enabled, lead_minutes) "
                    "VALUES (%s,FALSE,10)",
                    (favorite_id,),
                )
                return favorite_id
    except UserSettingsError:
        raise
    except Exception as exc:
        raise _db_error("favorite create", exc) from None


def delete_favorite(
    kakao_user_id: int,
    favorite_id: int,
    *,
    transaction_factory: Callable = database_transaction,
) -> bool:
    try:
        with transaction_factory() as connection:
            with connection.cursor() as cursor:
                user_id, _role = _user_row(cursor, kakao_user_id)
                cursor.execute(
                    "DELETE FROM favorites WHERE id=%s AND user_id=%s RETURNING id",
                    (favorite_id, user_id),
                )
                return cursor.fetchone() is not None
    except UserSettingsError:
        raise
    except Exception as exc:
        raise _db_error("favorite delete", exc) from None


def update_favorite_notification(
    kakao_user_id: int,
    favorite_id: int,
    *,
    enabled: bool,
    lead_minutes: int,
    transaction_factory: Callable = database_transaction,
) -> bool:
    if not isinstance(enabled, bool) or isinstance(lead_minutes, bool) or not isinstance(lead_minutes, int) or not 1 <= lead_minutes <= 180:
        raise InvalidSettingsError("알림 시점은 1~180분 사이여야 합니다.")
    try:
        with transaction_factory() as connection:
            with connection.cursor() as cursor:
                user_id, _role = _user_row(cursor, kakao_user_id)
                cursor.execute(
                    "UPDATE favorite_notifications fn SET enabled=%s, lead_minutes=%s "
                    "FROM favorites f WHERE f.id=fn.favorite_id AND f.id=%s AND f.user_id=%s "
                    "RETURNING fn.favorite_id",
                    (enabled, lead_minutes, favorite_id, user_id),
                )
                return cursor.fetchone() is not None
    except UserSettingsError:
        raise
    except Exception as exc:
        raise _db_error("favorite notification update", exc) from None


def get_global_notification_policy(
    *,
    connection_factory: Callable = database_connection,
) -> str:
    try:
        with connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT notification_channel_policy FROM service_settings WHERE singleton=TRUE"
                )
                row = cursor.fetchone()
                if row is None or row[0] not in GLOBAL_POLICIES:
                    raise UserSettingsError("전역 알림 정책을 찾을 수 없습니다.")
                return row[0]
    except UserSettingsError:
        raise
    except Exception as exc:
        raise _db_error("global policy read", exc) from None


def update_global_notification_policy(
    admin_kakao_user_id: int,
    policy: str,
    *,
    transaction_factory: Callable = database_transaction,
) -> None:
    if policy not in GLOBAL_POLICIES:
        raise InvalidSettingsError("전역 알림 정책이 올바르지 않습니다.")
    try:
        with transaction_factory() as connection:
            with connection.cursor() as cursor:
                _user_id, role = _user_row(cursor, admin_kakao_user_id)
                if role != "admin":
                    raise PermissionDeniedError("관리자만 전역 알림 정책을 변경할 수 있습니다.")
                cursor.execute(
                    "UPDATE service_settings SET notification_channel_policy=%s "
                    "WHERE singleton=TRUE RETURNING singleton",
                    (policy,),
                )
                if cursor.fetchone() is None:
                    raise UserSettingsError("전역 알림 정책을 저장하지 못했습니다.")
    except UserSettingsError:
        raise
    except Exception as exc:
        raise _db_error("global policy update", exc) from None
