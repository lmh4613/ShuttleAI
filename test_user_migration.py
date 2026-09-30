from contextlib import contextmanager
from datetime import time
from decimal import Decimal

import pytest
from cryptography.fernet import Fernet

import migrate_users_to_db as migration
from token_crypto import encrypt_token


def stop(stop_id, name, lat, lon, scheduled, boarding, alighting, *, default=False, source="document"):
    return migration.StopReference(
        stop_id, name, Decimal(lat), Decimal(lon),
        time.fromisoformat(scheduled) if scheduled else None,
        boarding, alighting, default, source,
    )


def references():
    morning = migration.RouteReference(
        10, "gyeonggi", "(출근) 테스트", "morning", (
            stop(101, "공통", "37.100000", "127.100000", "07:00", True, False),
            stop(102, "공통", "37.200000", "127.200000", "07:10", True, False),
            stop(103, "중간 (하차만)", "37.300000", "127.300000", "07:20", False, True),
            stop(104, migration.DEFAULT_DROPOFF, "37.412605", "127.095703", None, False, True,
                 default=True, source="system_default"),
        ),
    )
    evening = migration.RouteReference(
        20, "gyeonggi", "(퇴근) 테스트", "evening", (
            stop(201, "회사", "37.400000", "127.400000", "18:00", True, True),
            stop(202, "집", "37.500000", "127.500000", None, False, True),
        ),
    )
    return {(morning.region, morning.name): morning, (evening.region, evening.name): evening}


def favorite(*, arrive=migration.DEFAULT_DROPOFF, arrive_lat=37.412605,
             arrive_lon=127.095703, arrive_time="-", notify_min=10):
    return {
        "region": "gyeonggi", "route_name": "(출근) 테스트",
        "board_stop": "공통", "arrive_stop": arrive,
        "trip_type": "출근길", "board_time": "07:00", "arrive_time": arrive_time,
        "board_lat": 37.1, "board_lon": 127.1,
        "arrive_lat": arrive_lat, "arrive_lon": arrive_lon,
        "notify_enabled": True, "notify_min": notify_min,
    }


def user_entry(settings=None):
    return {
        "access_token": "test-access", "refresh_token": "test-refresh",
        "notification_config": {
            "active_days": ["월", "화", "월", "일"], "exclude_holidays": False,
        },
        "settings": settings if settings is not None else [favorite()],
    }


def test_legacy_normalization_weekdays_and_admin_role_mapping():
    rows = {
        str(next(iter(migration.ADMIN_KAKAO_USER_IDS))): user_entry(),
        "123": [],
    }
    plan = migration.build_migration_plan(rows, references())
    admin = next(user for user in plan.users if user.role == "admin")
    legacy = next(user for user in plan.users if user.legacy)
    assert admin.active_days == (0, 1, 6)
    assert legacy.active_days == (0, 1, 2, 3, 4)
    assert legacy.access_token is None and legacy.refresh_token is None
    assert plan.legacy_users == 1


def test_empty_guest_placeholder_is_not_invented_as_a_kakao_user():
    plan = migration.build_migration_plan({"guest": [], "123": user_entry([])}, references())
    assert plan.source_entries == 2
    assert len(plan.users) == 1
    assert plan.skipped_guest_entries == 1
    with pytest.raises(migration.UserDataError, match="guest placeholder"):
        migration.build_migration_plan({"guest": [favorite()]}, references())


def test_route_mapping_uses_coordinates_for_same_name_and_system_default():
    plan = migration.build_migration_plan({"123": user_entry()}, references())
    mapped = plan.favorites[0]
    assert mapped.boarding.route_stop_id == 101
    assert mapped.alighting.route_stop_id == 104
    assert mapped.alighting.is_default_dropoff
    assert mapped.alighting.source_kind == "system_default"


def test_dropoff_only_mapping_preserves_permissions():
    item = favorite(
        arrive="중간 (하차만)", arrive_lat=37.3, arrive_lon=127.3, arrive_time="07:20"
    )
    plan = migration.build_migration_plan({"123": user_entry([item])}, references())
    mapped = plan.favorites[0].alighting
    assert not mapped.boarding_allowed and mapped.alighting_allowed


def test_invalid_notification_lead_and_coordinate_mismatch_are_rejected():
    with pytest.raises(migration.UserDataError, match="notify_min"):
        migration.build_migration_plan({"123": user_entry([favorite(notify_min=0)])}, references())
    bad = favorite()
    bad["board_lat"] = 37.2
    bad["board_lon"] = 127.2
    bad["board_time"] = "07:00"
    with pytest.raises(migration.UserDataError, match="boarding time"):
        migration.build_migration_plan({"123": user_entry([bad])}, references())


def test_invalid_weekday_and_duplicate_migration_protection():
    with pytest.raises(migration.UserDataError, match="weekday"):
        migration.convert_weekdays(["월", "holiday"])
    migration.ensure_user_tables_empty({name: 0 for name in migration.USER_TABLES})
    with pytest.raises(migration.UserTablesNotEmptyError, match="not empty"):
        migration.ensure_user_tables_empty({**{name: 0 for name in migration.USER_TABLES}, "users": 1})


def test_user_migration_uses_one_transaction_and_propagates_for_rollback(monkeypatch):
    key = Fernet.generate_key()
    state = {"entered": False, "rolled_back": False}

    @contextmanager
    def transaction():
        state["entered"] = True
        try:
            yield object()
        except Exception:
            state["rolled_back"] = True
            raise

    monkeypatch.setattr(migration, "insert_plan", lambda *_args: (_ for _ in ()).throw(RuntimeError("fail")))
    with pytest.raises(RuntimeError, match="fail"):
        migration.migrate_plan(object(), key, transaction_factory=transaction)
    assert state == {"entered": True, "rolled_back": True}


def test_verify_logic_compares_tokens_settings_favorites_and_push(monkeypatch):
    key = Fernet.generate_key()
    plan = migration.build_migration_plan({"123": user_entry()}, references())
    user = plan.users[0]
    fav = plan.favorites[0]
    results = [
        [(1, 123, "user", True, None, encrypt_token(user.access_token, key),
          encrypt_token(user.refresh_token, key), None, None, None, False, "Asia/Seoul")],
        [(123, 0), (123, 1), (123, 6)],
        [(123, fav.route_id, fav.boarding.route_stop_id, fav.alighting.route_stop_id,
          fav.trip_type, fav.boarding.latitude, fav.boarding.longitude,
          fav.alighting.latitude, fav.alighting.longitude, fav.boarding.scheduled_time,
          fav.alighting.scheduled_time, True, 10)],
    ]
    singles = [(0,), (0,)]

    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def execute(self, _sql): pass
        def fetchall(self): return results.pop(0)
        def fetchone(self): return singles.pop(0)

    class Connection:
        def cursor(self): return Cursor()

    @contextmanager
    def connection():
        yield Connection()

    monkeypatch.setattr(migration, "database_connection", connection)
    report = migration.verify_plan(plan, key)
    assert report["token_decrypt_match"] is True
    assert report["users"] == 1 and report["favorites"] == 1
    assert report["push_subscriptions"] == 0
