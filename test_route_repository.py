from contextlib import contextmanager
from datetime import time
from decimal import Decimal

import pytest

from route_repository import RouteRepositoryError, load_routes_for_ui


class Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.sql = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql):
        self.sql = sql

    def fetchall(self):
        return self.rows


class Connection:
    def __init__(self, rows):
        self.cursor_value = Cursor(rows)

    def cursor(self):
        return self.cursor_value


def factory(rows):
    connection = Connection(rows)

    @contextmanager
    def context():
        yield connection

    return context, connection


def db_row(*, route="(출근) A", trip="morning", order=1, stop="정류장",
           scheduled=time(7, 5), lat="37.100000", lon="127.100000",
           boarding=True, alighting=False, default=False, source="document"):
    return (
        "gyeonggi", route, trip, order, stop, scheduled,
        Decimal(lat), Decimal(lon), 60, 120, "ok",
        boarding, alighting, default, source,
    )


def test_flat_ui_conversion_preserves_order_time_permissions_and_default():
    rows = [
        db_row(),
        db_row(order=2, stop="중간 (하차만)", scheduled=None,
               boarding=False, alighting=True),
        db_row(order=3, stop="판교 제2테크노밸리", scheduled=None,
               lat="37.412605", lon="127.095703", boarding=False,
               alighting=True, default=True, source="system_default"),
        db_row(route="(퇴근) B", trip="evening", stop="퇴근 출발",
               scheduled=time(17, 30), boarding=True, alighting=True),
    ]
    context, connection = factory(rows)

    result = load_routes_for_ui(connection_factory=context)

    assert [item["stop_name"] for item in result] == [
        "정류장", "중간 (하차만)", "판교 제2테크노밸리", "퇴근 출발"
    ]
    assert result[0] == {
        "region": "gyeonggi", "route_name": "(출근) A", "trip_type": "morning",
        "stop_order": 1, "stop_name": "정류장", "arrival_time": "07:05",
        "lat": 37.1, "lon": 127.1, "nx": 60, "ny": 120, "status": "ok",
        "boarding_allowed": True, "alighting_allowed": False,
        "is_default_dropoff": False, "source_kind": "document",
    }
    assert result[1]["arrival_time"] == ""
    assert not result[1]["boarding_allowed"] and result[1]["alighting_allowed"]
    assert result[2]["is_default_dropoff"]
    assert result[2]["source_kind"] == "system_default"
    assert result[3]["trip_type"] == "evening"
    assert "ORDER BY rg.code, r.id, rs.stop_order" in connection.cursor_value.sql


def test_same_name_with_different_coordinates_remains_two_rows():
    rows = [
        db_row(route="(출근) A", stop="공통", lat="37.100000"),
        db_row(route="(출근) B", stop="공통", lat="37.200000"),
    ]
    context, _connection = factory(rows)

    result = load_routes_for_ui(connection_factory=context)

    assert len(result) == 2
    assert [item["lat"] for item in result] == [37.1, 37.2]


def test_database_failure_is_safe_and_has_no_fallback():
    @contextmanager
    def failing_connection():
        raise RuntimeError("sensitive connection details")
        yield

    with pytest.raises(RouteRepositoryError) as caught:
        load_routes_for_ui(connection_factory=failing_connection)

    assert "secret" not in str(caught.value)
    assert "노선 정보를 불러오지 못했습니다" in str(caught.value)
