"""Explicit user_settings.json to PostgreSQL migration and verification."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

from database import database_connection, database_transaction
from token_crypto import TokenCryptoError, decrypt_token, encrypt_token, validate_encryption_key


USER_SETTINGS_FILE = Path(__file__).parent / "user_settings.json"
ADMIN_KAKAO_USER_IDS = frozenset({5070327065})
DEFAULT_ACTIVE_DAYS = ("월", "화", "수", "목", "금")
WEEKDAY_MAP = {"월": 0, "화": 1, "수": 2, "목": 3, "금": 4, "토": 5, "일": 6}
NO_TIME_VALUES = {"", "-"}
DEFAULT_TIMEZONE = "Asia/Seoul"
DEFAULT_DROPOFF = "판교 제2테크노밸리"
GUEST_USER_KEY = "guest"


class UserMigrationError(RuntimeError):
    pass


class UserDataError(UserMigrationError):
    pass


class UserTablesNotEmptyError(UserMigrationError):
    pass


class UserVerificationError(UserMigrationError):
    pass


@dataclass(frozen=True)
class StopReference:
    route_stop_id: int
    name: str
    latitude: Decimal
    longitude: Decimal
    scheduled_time: time | None
    boarding_allowed: bool
    alighting_allowed: bool
    is_default_dropoff: bool
    source_kind: str


@dataclass(frozen=True)
class RouteReference:
    route_id: int
    region: str
    name: str
    trip_type: str
    stops: tuple[StopReference, ...]


@dataclass(frozen=True)
class FavoritePlan:
    kakao_user_id: int
    route_id: int
    route_name: str
    trip_type: str
    boarding: StopReference
    alighting: StopReference
    notify_enabled: bool
    notify_min: int


@dataclass(frozen=True)
class UserPlan:
    kakao_user_id: int
    role: str
    access_token: str | None
    refresh_token: str | None
    exclude_holidays: bool
    active_days: tuple[int, ...]
    favorites: tuple[FavoritePlan, ...]
    legacy: bool


@dataclass(frozen=True)
class MigrationPlan:
    users: tuple[UserPlan, ...]
    source_entries: int
    skipped_guest_entries: int

    @property
    def favorites(self) -> tuple[FavoritePlan, ...]:
        return tuple(favorite for user in self.users for favorite in user.favorites)

    @property
    def legacy_users(self) -> int:
        return sum(user.legacy for user in self.users)

    @property
    def access_token_users(self) -> int:
        return sum(bool(user.access_token) for user in self.users)

    @property
    def refresh_token_users(self) -> int:
        return sum(bool(user.refresh_token) for user in self.users)

    @property
    def active_days(self) -> int:
        return sum(len(user.active_days) for user in self.users)

    @property
    def admin_users(self) -> int:
        return sum(user.role == "admin" for user in self.users)

    @property
    def default_dropoff_favorites(self) -> int:
        return sum(favorite.alighting.is_default_dropoff for favorite in self.favorites)

    @property
    def dropoff_only_favorites(self) -> int:
        return sum("(하차만)" in favorite.alighting.name for favorite in self.favorites)


def load_user_rows(path: Path = USER_SETTINGS_FILE) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UserDataError(f"Could not parse user JSON ({type(exc).__name__}).") from None
    if not isinstance(value, dict):
        raise UserDataError("User JSON must contain a top-level object.")
    return value


def _kakao_user_id(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise UserDataError("A Kakao user ID is invalid.") from None
    if str(parsed) != str(value).strip() or parsed <= 0 or parsed > 9223372036854775807:
        raise UserDataError("A Kakao user ID is invalid.")
    return parsed


def convert_weekdays(values: object) -> tuple[int, ...]:
    if not isinstance(values, list):
        raise UserDataError("active_days must be a list.")
    converted = []
    for value in values:
        if value not in WEEKDAY_MAP:
            raise UserDataError("active_days contains an unsupported weekday.")
        weekday = WEEKDAY_MAP[value]
        if weekday not in converted:
            converted.append(weekday)
    return tuple(converted)


def _coordinate(value: object) -> Decimal:
    try:
        coordinate = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise UserDataError("A favorite coordinate is invalid.") from None
    if not coordinate.is_finite():
        raise UserDataError("A favorite coordinate is invalid.")
    quantized = coordinate.quantize(Decimal("0.000001"))
    if coordinate != quantized:
        raise UserDataError("A favorite coordinate exceeds database precision.")
    return quantized


def _scheduled_time(value: object, field: str) -> time | None:
    if not isinstance(value, str):
        raise UserDataError(f"{field} must be text.")
    cleaned = value.strip()
    if cleaned in NO_TIME_VALUES:
        return None
    try:
        parsed = time.fromisoformat(cleaned)
    except ValueError:
        raise UserDataError(f"{field} is not a valid HH:MM time.") from None
    if parsed.second or parsed.microsecond or len(cleaned) != 5:
        raise UserDataError(f"{field} is not a valid HH:MM time.")
    return parsed


def _trip_type(value: object) -> str:
    if not isinstance(value, str):
        raise UserDataError("A favorite trip_type is missing.")
    morning = "출근" in value
    evening = "퇴근" in value
    if morning == evening:
        raise UserDataError("A favorite trip_type is invalid.")
    return "morning" if morning else "evening"


def _token(value: object, field: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise UserDataError(f"{field} must be text or NULL.")
    return value


def _normalized_entry(value: object) -> tuple[dict, bool]:
    if isinstance(value, list):
        return {
            "settings": value,
            "access_token": None,
            "refresh_token": None,
            "notification_config": {
                "active_days": list(DEFAULT_ACTIVE_DAYS),
                "exclude_holidays": True,
            },
        }, True
    if not isinstance(value, Mapping):
        raise UserDataError("A user entry must be an object or legacy list.")
    entry = dict(value)
    entry.setdefault("settings", [])
    entry.setdefault("access_token", None)
    entry.setdefault("refresh_token", None)
    entry.setdefault("notification_config", {
        "active_days": list(DEFAULT_ACTIVE_DAYS),
        "exclude_holidays": True,
    })
    return entry, False


def load_route_references(connection=None) -> dict[tuple[str, str], RouteReference]:
    owns_connection = connection is None
    manager = database_connection() if owns_connection else None
    if owns_connection:
        connection = manager.__enter__()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT r.id, rg.code, r.name, r.trip_type, rs.id, s.name, s.latitude, "
                "s.longitude, rs.scheduled_time, rs.boarding_allowed, rs.alighting_allowed, "
                "rs.is_default_dropoff, rs.source_kind FROM routes r "
                "JOIN regions rg ON rg.id=r.region_id "
                "JOIN route_stops rs ON rs.route_id=r.id "
                "JOIN stops s ON s.id=rs.stop_id ORDER BY r.id, rs.stop_order"
            )
            rows = cursor.fetchall()
    finally:
        if owns_connection:
            manager.__exit__(None, None, None)

    grouped: dict[tuple[str, str], list[StopReference]] = {}
    route_info: dict[tuple[str, str], tuple[int, str]] = {}
    for row in rows:
        route_id, region, route_name, trip_type = row[:4]
        key = (region, route_name)
        route_info[key] = (route_id, trip_type)
        grouped.setdefault(key, []).append(StopReference(
            route_stop_id=row[4], name=row[5], latitude=row[6], longitude=row[7],
            scheduled_time=row[8], boarding_allowed=row[9], alighting_allowed=row[10],
            is_default_dropoff=row[11], source_kind=row[12],
        ))
    return {
        key: RouteReference(route_info[key][0], key[0], key[1], route_info[key][1], tuple(stops))
        for key, stops in grouped.items()
    }


def _match_stop(
    route: RouteReference,
    *,
    name: object,
    latitude: object,
    longitude: object,
    boarding: bool,
) -> StopReference:
    if not isinstance(name, str) or not name:
        raise UserDataError("A favorite stop name is missing.")
    lat = _coordinate(latitude)
    lon = _coordinate(longitude)
    matches = [
        stop for stop in route.stops
        if stop.name == name and stop.latitude == lat and stop.longitude == lon
    ]
    if len(matches) != 1:
        raise UserDataError("A favorite stop could not be mapped uniquely by route, name, and coordinates.")
    stop = matches[0]
    if boarding and not stop.boarding_allowed:
        raise UserDataError("A favorite boarding stop does not allow boarding.")
    if not boarding and not stop.alighting_allowed:
        raise UserDataError("A favorite alighting stop does not allow alighting.")
    return stop


def _favorite_plan(
    kakao_user_id: int,
    item: object,
    route_references: Mapping[tuple[str, str], RouteReference],
) -> FavoritePlan:
    if not isinstance(item, Mapping):
        raise UserDataError("A favorite must be an object.")
    region = item.get("region", "gyeonggi")
    route_name = item.get("route_name")
    route = route_references.get((region, route_name))
    if route is None:
        raise UserDataError("A favorite route could not be mapped.")
    trip_type = _trip_type(item.get("trip_type"))
    if trip_type != route.trip_type:
        raise UserDataError("A favorite trip_type does not match its route.")
    boarding = _match_stop(
        route, name=item.get("board_stop"), latitude=item.get("board_lat"),
        longitude=item.get("board_lon"), boarding=True,
    )
    alighting = _match_stop(
        route, name=item.get("arrive_stop"), latitude=item.get("arrive_lat"),
        longitude=item.get("arrive_lon"), boarding=False,
    )
    if alighting.name == DEFAULT_DROPOFF and not (
        alighting.is_default_dropoff and alighting.source_kind == "system_default"
    ):
        raise UserDataError("The default destination is not mapped to its system route-stop.")
    if "(하차만)" in alighting.name and (alighting.boarding_allowed or not alighting.alighting_allowed):
        raise UserDataError("A dropoff-only favorite has invalid route-stop permissions.")

    board_time = _scheduled_time(item.get("board_time", ""), "board_time")
    if board_time != boarding.scheduled_time:
        raise UserDataError("A favorite boarding time does not match its route-stop.")
    arrive_time = _scheduled_time(item.get("arrive_time", ""), "arrive_time")
    if trip_type == "morning" and arrive_time != alighting.scheduled_time:
        raise UserDataError("A favorite arrival time does not match its route-stop.")

    notify_enabled = item.get("notify_enabled", False)
    if not isinstance(notify_enabled, bool):
        raise UserDataError("notify_enabled must be boolean.")
    notify_min = item.get("notify_min", 10)
    if isinstance(notify_min, bool) or not isinstance(notify_min, int) or not 1 <= notify_min <= 180:
        raise UserDataError("notify_min must be an integer from 1 to 180.")
    return FavoritePlan(
        kakao_user_id, route.route_id, route.name, trip_type, boarding, alighting,
        notify_enabled, notify_min,
    )


def build_migration_plan(
    rows: Mapping,
    route_references: Mapping[tuple[str, str], RouteReference],
) -> MigrationPlan:
    users = []
    seen_ids = set()
    skipped_guest_entries = 0
    for raw_user_id, raw_entry in rows.items():
        if raw_user_id == GUEST_USER_KEY:
            if not isinstance(raw_entry, list) or raw_entry:
                raise UserDataError("The guest placeholder unexpectedly contains user data.")
            skipped_guest_entries += 1
            continue
        kakao_user_id = _kakao_user_id(raw_user_id)
        if kakao_user_id in seen_ids:
            raise UserDataError("Duplicate Kakao user ID after normalization.")
        seen_ids.add(kakao_user_id)
        entry, legacy = _normalized_entry(raw_entry)
        settings = entry.get("settings")
        if not isinstance(settings, list):
            raise UserDataError("settings must be a list.")
        config = entry.get("notification_config")
        if not isinstance(config, Mapping):
            raise UserDataError("notification_config must be an object.")
        exclude_holidays = config.get("exclude_holidays", True)
        if not isinstance(exclude_holidays, bool):
            raise UserDataError("exclude_holidays must be boolean.")
        active_days = convert_weekdays(config.get("active_days", list(DEFAULT_ACTIVE_DAYS)))
        favorites = tuple(
            _favorite_plan(kakao_user_id, item, route_references) for item in settings
        )
        if len({(f.route_id, f.boarding.route_stop_id, f.alighting.route_stop_id) for f in favorites}) != len(favorites):
            raise UserDataError("A user has duplicate favorite selections.")
        users.append(UserPlan(
            kakao_user_id=kakao_user_id,
            role="admin" if kakao_user_id in ADMIN_KAKAO_USER_IDS else "user",
            access_token=_token(entry.get("access_token"), "access_token"),
            refresh_token=_token(entry.get("refresh_token"), "refresh_token"),
            exclude_holidays=exclude_holidays,
            active_days=active_days,
            favorites=favorites,
            legacy=legacy,
        ))
    return MigrationPlan(tuple(users), len(rows), skipped_guest_entries)


USER_TABLES = (
    "users", "kakao_credentials", "notification_settings", "notification_active_days",
    "favorites", "favorite_notifications",
)


def ensure_user_tables_empty(counts: Mapping[str, int]) -> None:
    nonempty = {name: count for name, count in counts.items() if count}
    if nonempty:
        summary = ", ".join(f"{name}={count}" for name, count in sorted(nonempty.items()))
        raise UserTablesNotEmptyError(f"User migration stopped; target tables are not empty ({summary}).")


def _user_table_counts(cursor) -> dict[str, int]:
    cursor.execute("SELECT " + ", ".join(f"(SELECT count(*) FROM {table})" for table in USER_TABLES))
    return dict(zip(USER_TABLES, cursor.fetchone()))


def insert_plan(connection, plan: MigrationPlan, key: bytes) -> None:
    with connection.cursor() as cursor:
        ensure_user_tables_empty(_user_table_counts(cursor))
        for user in plan.users:
            cursor.execute(
                "INSERT INTO users (kakao_user_id, nickname, role, enabled, last_login_at) "
                "VALUES (%s, NULL, %s, TRUE, NULL) RETURNING id",
                (user.kakao_user_id, user.role),
            )
            user_id = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO kakao_credentials "
                "(user_id, access_token_ciphertext, refresh_token_ciphertext, "
                "access_token_expires_at, refresh_token_expires_at, token_updated_at) "
                "VALUES (%s, %s, %s, NULL, NULL, NULL)",
                (user_id, encrypt_token(user.access_token, key), encrypt_token(user.refresh_token, key)),
            )
            cursor.execute(
                "INSERT INTO notification_settings (user_id, exclude_holidays, timezone) "
                "VALUES (%s, %s, %s)",
                (user_id, user.exclude_holidays, DEFAULT_TIMEZONE),
            )
            for weekday in user.active_days:
                cursor.execute(
                    "INSERT INTO notification_active_days (user_id, weekday) VALUES (%s, %s)",
                    (user_id, weekday),
                )
            for favorite in user.favorites:
                cursor.execute(
                    "INSERT INTO favorites "
                    "(user_id, route_id, boarding_route_stop_id, alighting_route_stop_id) "
                    "VALUES (%s, %s, %s, %s) RETURNING id",
                    (user_id, favorite.route_id, favorite.boarding.route_stop_id,
                     favorite.alighting.route_stop_id),
                )
                favorite_id = cursor.fetchone()[0]
                cursor.execute(
                    "INSERT INTO favorite_notifications (favorite_id, enabled, lead_minutes) "
                    "VALUES (%s, %s, %s)",
                    (favorite_id, favorite.notify_enabled, favorite.notify_min),
                )


def migrate_plan(
    plan: MigrationPlan,
    key: str | bytes | None = None,
    transaction_factory: Callable = database_transaction,
) -> None:
    validated_key = validate_encryption_key(key)
    with transaction_factory() as connection:
        insert_plan(connection, plan, validated_key)


def verify_plan(plan: MigrationPlan, key: str | bytes | None = None) -> dict[str, int | bool]:
    validated_key = validate_encryption_key(key)
    with database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT u.id, u.kakao_user_id, u.role, u.enabled, u.last_login_at, "
                "kc.access_token_ciphertext, kc.refresh_token_ciphertext, "
                "kc.access_token_expires_at, kc.refresh_token_expires_at, kc.token_updated_at, "
                "ns.exclude_holidays, ns.timezone FROM users u "
                "JOIN kakao_credentials kc ON kc.user_id=u.id "
                "JOIN notification_settings ns ON ns.user_id=u.id"
            )
            user_rows = cursor.fetchall()
            cursor.execute(
                "SELECT u.kakao_user_id, nad.weekday FROM notification_active_days nad "
                "JOIN users u ON u.id=nad.user_id ORDER BY u.kakao_user_id, nad.weekday"
            )
            day_rows = cursor.fetchall()
            cursor.execute(
                "SELECT u.kakao_user_id, f.route_id, f.boarding_route_stop_id, "
                "f.alighting_route_stop_id, r.trip_type, bs.latitude, bs.longitude, "
                "als.latitude, als.longitude, brs.scheduled_time, ars.scheduled_time, "
                "fn.enabled, fn.lead_minutes FROM favorites f "
                "JOIN users u ON u.id=f.user_id JOIN routes r ON r.id=f.route_id "
                "JOIN route_stops brs ON brs.id=f.boarding_route_stop_id AND brs.route_id=f.route_id "
                "JOIN route_stops ars ON ars.id=f.alighting_route_stop_id AND ars.route_id=f.route_id "
                "JOIN stops bs ON bs.id=brs.stop_id JOIN stops als ON als.id=ars.stop_id "
                "JOIN favorite_notifications fn ON fn.favorite_id=f.id"
            )
            favorite_rows = cursor.fetchall()
            cursor.execute("SELECT count(*) FROM push_subscriptions")
            push_count = cursor.fetchone()[0]
            cursor.execute(
                "SELECT count(*) FROM favorites f LEFT JOIN users u ON u.id=f.user_id "
                "LEFT JOIN routes r ON r.id=f.route_id "
                "LEFT JOIN route_stops b ON b.id=f.boarding_route_stop_id AND b.route_id=f.route_id "
                "LEFT JOIN route_stops a ON a.id=f.alighting_route_stop_id AND a.route_id=f.route_id "
                "WHERE u.id IS NULL OR r.id IS NULL OR b.id IS NULL OR a.id IS NULL"
            )
            broken_references = cursor.fetchone()[0]

    expected_users = {user.kakao_user_id: user for user in plan.users}
    if len(user_rows) != len(expected_users):
        raise UserVerificationError("User count does not match the migration plan.")
    token_matches = True
    actual_ids = set()
    for row in user_rows:
        (_, kakao_id, role, enabled, last_login, access_cipher, refresh_cipher,
         access_expiry, refresh_expiry, token_updated, exclude_holidays, timezone) = row
        user = expected_users.get(kakao_id)
        if user is None:
            raise UserVerificationError("An unexpected user exists in the database.")
        actual_ids.add(kakao_id)
        if (role, enabled, last_login, exclude_holidays, timezone) != (
            user.role, True, None, user.exclude_holidays, DEFAULT_TIMEZONE,
        ):
            raise UserVerificationError("User role or notification settings do not match.")
        if any(value is not None for value in (access_expiry, refresh_expiry, token_updated)):
            raise UserVerificationError("Unknown token timestamps were populated.")
        if user.access_token and (
            access_cipher is None or bytes(access_cipher) == user.access_token.encode("utf-8")
        ):
            raise UserVerificationError("An access token was stored without encryption.")
        if user.refresh_token and (
            refresh_cipher is None or bytes(refresh_cipher) == user.refresh_token.encode("utf-8")
        ):
            raise UserVerificationError("A refresh token was stored without encryption.")
        if decrypt_token(access_cipher, validated_key) != user.access_token:
            token_matches = False
        if decrypt_token(refresh_cipher, validated_key) != user.refresh_token:
            token_matches = False
    if actual_ids != set(expected_users) or not token_matches:
        raise UserVerificationError("User IDs or decrypted credentials do not match.")

    actual_days: dict[int, list[int]] = {}
    for kakao_id, weekday in day_rows:
        actual_days.setdefault(kakao_id, []).append(weekday)
    expected_days = {user.kakao_user_id: list(user.active_days) for user in plan.users}
    if actual_days != expected_days:
        raise UserVerificationError("Notification active days do not match.")

    expected_favorites = {
        (favorite.kakao_user_id, favorite.route_id, favorite.boarding.route_stop_id,
         favorite.alighting.route_stop_id): favorite
        for favorite in plan.favorites
    }
    if len(favorite_rows) != len(expected_favorites):
        raise UserVerificationError("Favorite count does not match.")
    for row in favorite_rows:
        key_tuple = row[:4]
        favorite = expected_favorites.get(key_tuple)
        if favorite is None:
            raise UserVerificationError("An unexpected favorite exists in the database.")
        actual = row[4:]
        expected = (
            favorite.trip_type,
            favorite.boarding.latitude, favorite.boarding.longitude,
            favorite.alighting.latitude, favorite.alighting.longitude,
            favorite.boarding.scheduled_time, favorite.alighting.scheduled_time,
            favorite.notify_enabled, favorite.notify_min,
        )
        if actual != expected:
            raise UserVerificationError("Favorite route-stop or notification values do not match.")
    if broken_references:
        raise UserVerificationError("Broken favorite references were found.")
    if push_count:
        raise UserVerificationError("Push subscriptions changed during user migration.")

    return {
        "users": len(user_rows),
        "credentials": len(user_rows),
        "notification_settings": len(user_rows),
        "active_days": len(day_rows),
        "favorites": len(favorite_rows),
        "favorite_notifications": len(favorite_rows),
        "token_decrypt_match": token_matches,
        "push_subscriptions": push_count,
    }


def plan_summary(plan: MigrationPlan) -> list[str]:
    return [
        f"Source entries: {plan.source_entries}",
        f"Users: {len(plan.users)}",
        f"Legacy users: {plan.legacy_users}",
        f"Skipped empty guest placeholders: {plan.skipped_guest_entries}",
        f"Admin roles: {plan.admin_users}",
        f"Users with access token: {plan.access_token_users}",
        f"Users with refresh token: {plan.refresh_token_users}",
        f"Notification settings: {len(plan.users)}",
        f"Active days: {plan.active_days}",
        f"Favorites: {len(plan.favorites)}",
        f"Route mappings: {len(plan.favorites)} succeeded, 0 failed",
        f"Boarding stop mappings: {len(plan.favorites)} succeeded, 0 failed",
        f"Alighting stop mappings: {len(plan.favorites)} succeeded, 0 failed",
        f"Default dropoff mappings: {plan.default_dropoff_favorites}",
        f"Dropoff-only mappings: {plan.dropoff_only_favorites}",
        f"Enabled favorite notifications: {sum(f.notify_enabled for f in plan.favorites)}",
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate ShuttleAI user JSON to PostgreSQL.")
    parser.add_argument("command", choices=("dry-run", "migrate", "verify"))
    args = parser.parse_args()
    try:
        routes = load_route_references()
        plan = build_migration_plan(load_user_rows(), routes)
        for line in plan_summary(plan):
            print(line)
        if args.command == "dry-run":
            print("Dry-run: OK (database unchanged; credentials not encrypted)")
            return 0
        validate_encryption_key()
        print("Token encryption key: configured and valid")
        if args.command == "migrate":
            migrate_plan(plan)
            print("User migration: OK")
        report = verify_plan(plan)
        print("User verification: OK")
        print("Verified counts: " + ", ".join(f"{name}={value}" for name, value in report.items()))
        return 0
    except (UserMigrationError, TokenCryptoError) as exc:
        print(f"User migration error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
