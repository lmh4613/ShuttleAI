from contextlib import contextmanager
from datetime import time
from decimal import Decimal

import pytest

from user_settings_repository import (
    DuplicateFavoriteError,
    FavoriteMappingError,
    InvalidSettingsError,
    PermissionDeniedError,
    UserNotFoundError,
    create_favorite,
    delete_favorite,
    get_global_notification_policy,
    get_notification_settings,
    get_user_dashboard_data,
    list_favorites,
    update_favorite_notification,
    update_global_notification_policy,
    update_notification_settings,
)


class ScriptedCursor:
    def __init__(self, steps):
        self.steps = list(steps)
        self.current = None
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        self.calls.append(("execute", sql, params))
        marker, result = self.steps.pop(0)
        assert marker in " ".join(sql.split())
        self.current = result

    def executemany(self, sql, params):
        params = list(params)
        self.calls.append(("executemany", sql, params))
        marker, result = self.steps.pop(0)
        assert marker in " ".join(sql.split())
        self.current = result

    def fetchone(self):
        if isinstance(self.current, list):
            return self.current[0] if self.current else None
        return self.current

    def fetchall(self):
        return list(self.current or [])


class ScriptedConnection:
    def __init__(self, steps):
        self.cursor_value = ScriptedCursor(steps)

    def cursor(self):
        return self.cursor_value


def factory(steps):
    connection = ScriptedConnection(steps)

    @contextmanager
    def context():
        yield connection

    return context, connection


def user_step(role="user"):
    return ("SELECT id, role FROM users", (11, role))


def route_stop(stop_id, order, *, boarding=True, alighting=True,
               default=False, source="document", scheduled=time(7, 10)):
    return (stop_id, order, scheduled, boarding, alighting, default, source)


def selection(**changes):
    value = {
        "region": "seoul", "route_name": "출근 테스트", "trip_type": "출근길",
        "board_stop": "탑승 A", "board_lat": 37.1, "board_lon": 127.1,
        "arrive_stop": "판교 제2테크노밸리",
        "arrive_lat": 37.412605, "arrive_lon": 127.095703,
    }
    value.update(changes)
    return value


def create_steps(*, route=(22, "morning"), board=None, arrive=None,
                 first=None, inserted=(44,)):
    steps = [
        user_step(),
        ("SELECT r.id, r.trip_type", route),
        ("FROM route_stops rs JOIN stops", [board or route_stop(31, 1, alighting=False)]),
        ("FROM route_stops rs JOIN stops", [arrive or route_stop(
            32, 99, boarding=False, default=True, source="system_default", scheduled=None
        )]),
    ]
    if route and route[1] == "evening":
        steps.append(("ORDER BY stop_order LIMIT 1", first or (31,)))
    steps.extend([
        ("INSERT INTO favorites", inserted),
        ("INSERT INTO favorite_notifications", None),
    ])
    return steps


def test_user_lookup_and_notification_settings_read():
    context, _connection = factory([
        user_step("admin"),
        ("FROM notification_settings", (True, "Asia/Seoul", "PUSH")),
        ("FROM notification_active_days", [(0,), (2,), (4,)]),
        ("FROM push_subscriptions", (2,)),
    ])
    result = get_notification_settings(123, connection_factory=context)
    assert result == {
        "user_id": 11, "role": "admin", "exclude_holidays": True,
        "timezone": "Asia/Seoul", "delivery_channel": "PUSH",
        "active_days": [0, 2, 4], "active_push_devices": 2,
    }


def test_unknown_user_is_not_created():
    context, connection = factory([("SELECT id, role FROM users", None)])
    with pytest.raises(UserNotFoundError):
        get_notification_settings(999, connection_factory=context)
    assert all("INSERT INTO users" not in call[1] for call in connection.cursor_value.calls)


def test_database_failure_returns_safe_error_without_secret(caplog):
    secret = "postgres-secret-value"

    @contextmanager
    def broken_connection():
        raise RuntimeError(secret)
        yield

    from user_settings_repository import UserSettingsError
    with pytest.raises(UserSettingsError, match="잠시 후") as error:
        get_notification_settings(123, connection_factory=broken_connection)
    assert secret not in str(error.value)
    assert secret not in caplog.text


def test_notification_settings_update_is_one_transaction_and_accepts_empty_days():
    context, connection = factory([
        user_step(),
        ("UPDATE notification_settings", None),
        ("DELETE FROM notification_active_days", None),
        ("INSERT INTO notification_active_days", None),
    ])
    update_notification_settings(
        123, active_days=[], exclude_holidays=False, delivery_channel="PUSH",
        transaction_factory=context,
    )
    assert connection.cursor_value.calls[-1][0] == "executemany"
    assert connection.cursor_value.calls[-1][2] == []


@pytest.mark.parametrize("days,channel", [([7], "PUSH"), ([True], "PUSH"), ([0], "EMAIL")])
def test_notification_settings_reject_invalid_values(days, channel):
    with pytest.raises(InvalidSettingsError):
        update_notification_settings(
            123, active_days=days, exclude_holidays=True, delivery_channel=channel
        )


def test_favorite_list_maps_database_fields_without_guessing_arrival_time():
    row = (
        44, "seoul", "출근 테스트", "morning", "탑승 A", time(7, 10),
        Decimal("37.100000"), Decimal("127.100000"), "판교 제2테크노밸리", None,
        Decimal("37.412605"), Decimal("127.095703"), False, 10,
    )
    context, _connection = factory([user_step(), ("FROM favorites f", [row])])
    result = list_favorites(123, connection_factory=context)
    assert result[0]["favorite_id"] == 44
    assert result[0]["trip_type"] == "출근길"
    assert result[0]["board_time"] == "07:10"
    assert result[0]["arrive_time"] == "-"


def test_dashboard_read_uses_one_connection_and_two_selects():
    favorite = (
        44, "seoul", "출근 테스트", "morning", "탑승 A", time(7, 10),
        Decimal("37.100000"), Decimal("127.100000"), "판교 제2테크노밸리", None,
        Decimal("37.412605"), Decimal("127.095703"), True, 20,
    )
    context, connection = factory([
        ("FROM users u JOIN notification_settings", (
            11, "user", True, "Asia/Seoul", "PUSH", [0, 2, 4], 2,
        )),
        ("FROM favorites f JOIN routes", [favorite]),
    ])
    result = get_user_dashboard_data(123, connection_factory=context)
    assert result["kakao_user_id"] == 123
    assert result["internal_user_id"] == 11
    assert result["notification_settings"]["active_days"] == [0, 2, 4]
    assert result["notification_settings"]["active_push_devices"] == 2
    assert result["favorites"][0]["notify_min"] == 20
    execute_calls = [call for call in connection.cursor_value.calls if call[0] == "execute"]
    assert len(execute_calls) == 2


def test_create_favorite_resolves_exact_route_stops_and_default_dropoff():
    context, connection = factory(create_steps())
    assert create_favorite(123, selection(), transaction_factory=context) == 44
    resolve_calls = [call for call in connection.cursor_value.calls if "FROM route_stops rs JOIN stops" in call[1]]
    assert resolve_calls[0][2][1:] == ("탑승 A", Decimal("37.100000"), Decimal("127.100000"))
    assert resolve_calls[1][2][1:] == (
        "판교 제2테크노밸리", Decimal("37.412605"), Decimal("127.095703")
    )


def test_create_favorite_supports_dropoff_only_destination():
    destination = route_stop(33, 4, boarding=False, alighting=True)
    context, _connection = factory(create_steps(arrive=destination))
    chosen = selection(
        arrive_stop="D (하차만)", arrive_lat=37.2, arrive_lon=127.2
    )
    assert create_favorite(123, chosen, transaction_factory=context) == 44


def test_same_name_stops_are_resolved_with_exact_coordinates():
    context, connection = factory(create_steps())
    chosen = selection(board_stop="공통 정류장", board_lat=37.1, board_lon=127.1)
    assert create_favorite(123, chosen, transaction_factory=context) == 44
    board_query = next(
        call for call in connection.cursor_value.calls
        if "FROM route_stops rs JOIN stops" in call[1]
    )
    assert board_query[2] == (
        22, "공통 정류장", Decimal("37.100000"), Decimal("127.100000")
    )


def test_evening_first_route_stop_is_accepted():
    context, _connection = factory(create_steps(
        route=(22, "evening"), board=route_stop(31, 1),
        arrive=route_stop(33, 5), first=(31,),
    ))
    assert create_favorite(
        123,
        selection(route_name="(퇴근) 테스트", trip_type="퇴근길",
                  arrive_stop="하차 A", arrive_lat=37.2, arrive_lon=127.2),
        transaction_factory=context,
    ) == 44


def test_evening_requires_actual_first_route_stop():
    steps = create_steps(
        route=(22, "evening"), board=route_stop(31, 2),
        arrive=route_stop(33, 5), first=(30,),
    )
    context, _connection = factory(steps)
    with pytest.raises(FavoriteMappingError, match="첫 정류장"):
        create_favorite(
            123,
            selection(route_name="(퇴근) 테스트", trip_type="퇴근길",
                      arrive_stop="하차 A", arrive_lat=37.2, arrive_lon=127.2),
            transaction_factory=context,
        )


@pytest.mark.parametrize(
    "steps,error",
    [
        ([user_step(), ("SELECT r.id, r.trip_type", None)], "노선"),
        (create_steps(board=None)[:2] + [("FROM route_stops rs JOIN stops", [])], "정류장"),
    ],
)
def test_create_favorite_rejects_missing_route_or_stop(steps, error):
    context, _connection = factory(steps)
    with pytest.raises(FavoriteMappingError, match=error):
        create_favorite(123, selection(), transaction_factory=context)


def test_create_favorite_rejects_coordinate_precision_mismatch_before_insert():
    context, _connection = factory([user_step(), ("SELECT r.id, r.trip_type", (22, "morning"))])
    with pytest.raises(FavoriteMappingError, match="좌표"):
        create_favorite(
            123, selection(board_lat="37.1234567"), transaction_factory=context
        )


def test_duplicate_favorite_does_not_create_notification_row():
    steps = create_steps(inserted=None)[:-1]
    context, connection = factory(steps)
    with pytest.raises(DuplicateFavoriteError):
        create_favorite(123, selection(), transaction_factory=context)
    assert all("INSERT INTO favorite_notifications" not in call[1] for call in connection.cursor_value.calls)


def test_delete_and_notification_update_are_owner_scoped():
    delete_context, delete_connection = factory([user_step(), ("DELETE FROM favorites", None)])
    assert not delete_favorite(123, 44, transaction_factory=delete_context)
    assert delete_connection.cursor_value.calls[-1][2] == (44, 11)

    update_context, update_connection = factory([
        user_step(), ("UPDATE favorite_notifications", (44,))
    ])
    assert update_favorite_notification(
        123, 44, enabled=True, lead_minutes=30, transaction_factory=update_context
    )
    assert update_connection.cursor_value.calls[-1][2] == (True, 30, 44, 11)

    owned_context, _owned_connection = factory([
        user_step(), ("DELETE FROM favorites", (44,))
    ])
    assert delete_favorite(123, 44, transaction_factory=owned_context)


@pytest.mark.parametrize("lead", [1, 180])
def test_favorite_notification_accepts_database_bounds(lead):
    context, _connection = factory([
        user_step(), ("UPDATE favorite_notifications", (44,))
    ])
    assert update_favorite_notification(
        123, 44, enabled=True, lead_minutes=lead, transaction_factory=context
    )


@pytest.mark.parametrize("lead", [0, 181, True])
def test_favorite_notification_enforces_database_bounds(lead):
    with pytest.raises(InvalidSettingsError):
        update_favorite_notification(123, 44, enabled=True, lead_minutes=lead)


def test_global_policy_read_and_admin_authorization():
    read_context, _connection = factory([
        ("FROM service_settings WHERE singleton=TRUE", ("AUTO",))
    ])
    assert get_global_notification_policy(connection_factory=read_context) == "AUTO"

    denied_context, denied_connection = factory([user_step("user")])
    with pytest.raises(PermissionDeniedError):
        update_global_notification_policy(123, "PUSH", transaction_factory=denied_context)
    assert len(denied_connection.cursor_value.calls) == 1

    allowed_context, allowed_connection = factory([
        user_step("admin"), ("UPDATE service_settings", (True,))
    ])
    update_global_notification_policy(123, "KAKAO", transaction_factory=allowed_context)
    assert allowed_connection.cursor_value.calls[-1][2] == ("KAKAO",)
