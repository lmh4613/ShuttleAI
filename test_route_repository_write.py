from collections import OrderedDict
from contextlib import contextmanager
from datetime import time
from decimal import Decimal
import hashlib
import json
from pathlib import Path

import pytest

import route_repository as repository
import weather_api


def document_row(route="(출근) A", stop="정류장 A", arrival="07:10", **changes):
    row = {
        "region": "gyeonggi", "route_name": route, "stop_name": stop,
        "arrival_time": arrival, "lat": 37.1, "lon": 127.1,
        "nx": 60, "ny": 120, "status": "ok",
    }
    row.update(changes)
    return row


def current_stop(stop_id=201, route_stop_id=101, name="정류장 A", order=1,
                 scheduled=time(7, 10), lat="37.100000", lon="127.100000",
                 boarding=True, alighting=False, default=False, source="document",
                 favorite_count=0):
    return {
        "id": route_stop_id, "stop_order": order, "scheduled_time": scheduled,
        "boarding_allowed": boarding, "alighting_allowed": alighting,
        "is_default_dropoff": default, "source_kind": source, "updated_at": "rs-v1",
        "stop_id": stop_id, "stop_name": name, "latitude": Decimal(lat),
        "longitude": Decimal(lon), "grid_x": 60, "grid_y": 120,
        "geocode_status": "ok", "stop_active": True, "stop_updated_at": "s-v1",
        "favorite_count": favorite_count, "active": True, "inactive_reason": None,
    }


def current_route(name="(출근) A", *, route_id=11, active=True, favorite_count=0,
                  stops=None, trip="morning"):
    if stops is None:
        stops = [
            current_stop(),
            current_stop(202, 102, "판교 제2테크노밸리", 2, None,
                         "37.412605", "127.095703", False, True, True,
                         "system_default"),
        ]
    return {
        "region_id": 1, "id": route_id, "name": name, "trip_type": trip,
        "active": active, "updated_at": "r-v1", "favorite_count": favorite_count,
        "stops": stops,
    }


def state(*routes):
    return {
        "region": "gyeonggi", "region_id": 1,
        "routes": OrderedDict((route["name"], route) for route in routes),
        "raw_rows": [],
    }


def test_validation_derives_morning_dropoff_and_evening_first_stop_rules():
    morning = repository.validate_route_document_rows([
        document_row(),
        document_row(stop="중간 (하차만)", arrival=""),
    ], "gyeonggi")
    assert morning[0]["boarding_allowed"] and not morning[0]["alighting_allowed"]
    assert not morning[1]["boarding_allowed"] and morning[1]["alighting_allowed"]

    evening = repository.validate_route_document_rows([
        document_row("(퇴근) A", "회사", "17:30"),
        document_row("(퇴근) A", "집", ""),
    ], "gyeonggi")
    assert evening[0]["boarding_allowed"] and evening[0]["scheduled_time"] == time(17, 30)
    assert not evening[1]["boarding_allowed"] and evening[1]["alighting_allowed"]


@pytest.mark.parametrize("route_name, expected", [
    ("(출근)미사/상일동", ("morning", "미사/상일동", "")),
    ("(퇴근) 노원", ("evening", "노원", "")),
    ("(출근)서울시청 1호", ("morning", "서울시청", "1호")),
    (" (출근)  목동/신도림   2호 ", ("morning", "목동/신도림", "2호")),
    ("(출근) 수원역/ 수원시청", ("morning", "수원역/수원시청", "")),
    ("(출근) 수원역 / 수원시청", ("morning", "수원역/수원시청", "")),
    ("(출근) 목동 / 신도림", ("morning", "목동/신도림", "")),
])
def test_route_logical_identity_ignores_only_safe_whitespace(route_name, expected):
    assert repository.route_logical_identity(route_name) == expected


def test_route_logical_identity_keeps_meaningful_names_and_vehicles_distinct():
    assert repository.route_logical_identity("(출근) 서울시청") != \
        repository.route_logical_identity("(출근) 서울시청A")
    assert repository.route_logical_identity("(출근) 서울시청 1호") != \
        repository.route_logical_identity("(출근) 서울시청 2호")
    assert repository.route_logical_identity("(출근) 수원역/수원시청") != \
        repository.route_logical_identity("(출근) 수원역/수원시청A")
    assert repository.route_logical_identity("(출근) 목동/신도림") != \
        repository.route_logical_identity("(출근) 목동/영등포")


@pytest.mark.parametrize("left, right", [
    ("정류장(북판교 방면)", "정류장 (북판교 방면)"),
    ("정류장(북판교 방면)", "정류장  (북판교 방면)"),
    ("KOICA 후문 맞은 편", "KOICA 후문 맞은편"),
])
def test_stop_name_matching_normalizes_only_safe_spacing(left, right):
    assert repository.normalize_stop_name_for_matching(left) == \
        repository.normalize_stop_name_for_matching(right)


@pytest.mark.parametrize("left, right", [
    ("SK타워 앞", "SKT타워 앞"),
    ("순천향대학병원", "순천향대학교병원"),
    ("과천역 6번 출구", "과천역 7번 출구"),
    ("영등포역 버스정류장(중)", "영등포역 중앙차로 버스정류장"),
])
def test_stop_name_matching_preserves_meaningful_differences(left, right):
    assert repository.normalize_stop_name_for_matching(left) != \
        repository.normalize_stop_name_for_matching(right)


@pytest.mark.parametrize("rows, message", [
    ([document_row(), document_row()], "중복"),
    ([document_row(arrival="")], "시간"),
    ([document_row(route="노선 A")], "출근/퇴근"),
    ([document_row(stop="판교 제2테크노밸리")], "기본 목적지"),
    ([document_row(lat=999)], "좌표"),
])
def test_validation_rejects_unsafe_rows(rows, message):
    with pytest.raises(repository.RouteValidationError, match=message):
        repository.validate_route_document_rows(rows, "gyeonggi")


def test_plan_preserves_route_and_route_stop_ids_for_time_order_and_coordinate_changes():
    route = current_route(stops=[
        current_stop(route_stop_id=101, name="정류장 A", order=1),
        current_stop(stop_id=203, route_stop_id=103, name="정류장 B", order=2,
                     scheduled=time(7, 20)),
        current_stop(202, 102, "판교 제2테크노밸리", 3, None,
                     "37.412605", "127.095703", False, True, True,
                     "system_default"),
    ])
    rows = repository.validate_route_document_rows([
        document_row(stop="정류장 B", arrival="07:25", lat=37.2),
        document_row(stop="정류장 A", arrival="07:10"),
    ], "gyeonggi")

    plan = repository._build_reconcile_plan(rows, state(route))

    assert plan["routes"][0]["current"]["id"] == 11
    matched = {item["desired"]["stop_name"]: item["current"]["id"]
               for item in plan["routes"][0]["stops"]}
    assert matched == {"정류장 B": 103, "정류장 A": 101}
    assert plan["counts"]["stops_updated"] == 1
    assert plan["routes"][0]["default"]["id"] == 102


def test_unchanged_route_is_not_mutated():
    normalized = repository.validate_route_document_rows([document_row()], "gyeonggi")
    current = state(current_route())
    plan = repository._build_reconcile_plan(normalized, current)
    connection = ResultConnection([])

    result = repository._apply_reconcile_plan(connection.cursor_value, plan, current)

    assert result == {
        "routes_added": 0, "routes_updated": 0, "routes_deactivated": 0,
        "stops_added": 0, "stops_updated": 0, "stops_removed": 0,
    }
    assert connection.cursor_value.calls == []


@pytest.mark.parametrize("db_name,pdf_name,trip", [
    ("(출근) 미사/상일동", "(출근)미사/상일동", "morning"),
    ("(퇴근) 노원", "(퇴근)노원", "evening"),
    ("(출근) 서울시청 1호", "(출근)서울시청 1호", "morning"),
    ("(출근) 수원역/수원시청", "(출근) 수원역/ 수원시청", "morning"),
])
def test_route_whitespace_difference_preserves_existing_route_and_stops(db_name, pdf_name, trip):
    stops = [current_stop(name="정류장 A")]
    if trip == "morning":
        stops.append(current_stop(
            202, 102, "판교 제2테크노밸리", 2, None,
            "37.412605", "127.095703", False, True, True, "system_default",
        ))
    route = current_route(name=db_name, trip=trip, stops=stops)
    normalized = repository.validate_route_document_rows([
        document_row(route=pdf_name),
    ], "gyeonggi")

    plan = repository._build_reconcile_plan(normalized, state(route))

    assert plan["counts"]["routes_added"] == 0
    assert plan["counts"]["routes_deactivated"] == 0
    assert plan["counts"]["stops_added"] == 0
    assert plan["routes"][0]["current"]["id"] == 11
    assert plan["routes"][0]["name"] == db_name
    assert plan["routes"][0]["stops"][0]["current"]["id"] == 101
    if trip == "morning":
        assert plan["routes"][0]["default"]["id"] == 102


def test_vehicle_routes_remain_separate_while_marker_whitespace_is_ignored():
    route_1 = current_route(name="(출근) 서울시청 1호", route_id=11)
    route_2 = current_route(
        name="(출근) 서울시청 2호", route_id=12,
        stops=[current_stop(211, 111), current_stop(
            212, 112, "판교 제2테크노밸리", 2, None,
            "37.412605", "127.095703", False, True, True, "system_default",
        )],
    )
    normalized = repository.validate_route_document_rows([
        document_row(route="(출근)서울시청 1호"),
        document_row(route="(출근)서울시청 2호"),
    ], "gyeonggi")

    plan = repository._build_reconcile_plan(normalized, state(route_1, route_2))

    assert [item["current"]["id"] for item in plan["routes"]] == [11, 12]
    assert plan["counts"]["routes_added"] == 0
    assert plan["counts"]["routes_deactivated"] == 0


def test_meaningful_route_name_change_is_not_auto_merged():
    route = current_route(name="(출근) 서울시청")
    normalized = repository.validate_route_document_rows([
        document_row(route="(출근) 서울시청A"),
    ], "gyeonggi")

    plan = repository._build_reconcile_plan(normalized, state(route))

    assert plan["counts"]["routes_added"] == 1
    assert plan["counts"]["routes_deactivated"] == 1


def test_existing_stop_reuses_coordinates_without_geocoding_and_preview_is_noop(monkeypatch):
    monkeypatch.setattr(
        weather_api, "get_coordinates_by_gemini",
        lambda _name: pytest.fail("existing stop must not be geocoded"),
    )
    stop_name = "순천향대학병원 앞"
    existing = [{
        "region": "gyeonggi", "route_name": "(출근) A", "stop_name": stop_name,
        "arrival_time": "07:10", "lat": 37.1, "lon": 127.1,
        "nx": 60, "ny": 120, "status": "ok",
    }]
    prepared = weather_api.prepare_routes_with_sequential_geocoding(
        [{"route_name": "(출근) A", "stop_name": stop_name, "arrival_time": "07:10"}],
        existing_rows=existing,
    )

    route = current_route(stops=[
        current_stop(name=stop_name),
        current_stop(202, 102, "판교 제2테크노밸리", 2, None,
                     "37.412605", "127.095703", False, True, True,
                     "system_default"),
    ])
    plan = repository._build_reconcile_plan(
        repository.validate_route_document_rows(prepared, "gyeonggi"),
        state(route),
    )

    assert prepared == existing
    assert plan["counts"] == {
        "routes_added": 0, "routes_updated": 0, "routes_deactivated": 0,
        "stops_added": 0, "stops_updated": 0, "stops_removed": 0,
    }
    assert plan["details"] == []


def test_logical_route_spacing_reuses_existing_stop_coordinates(monkeypatch):
    monkeypatch.setattr(
        weather_api, "get_coordinates_by_gemini",
        lambda _name: pytest.fail("logical route match must reuse coordinates"),
    )
    existing = [{
        "region": "seoul", "route_name": "(퇴근) 미사/상일동", "stop_order": 1,
        "stop_name": "고덕역 4번 출구 앞 버스정류장", "arrival_time": "17:30",
        "lat": 37.1, "lon": 127.1, "nx": 60, "ny": 120, "status": "ok",
        "source_kind": "document",
    }]

    prepared = weather_api.prepare_routes_with_sequential_geocoding([{
        "route_name": "(퇴근)미사/상일동",
        "stop_name": "고덕역 4번 출구 앞 버스정류장", "arrival_time": "17:30",
    }], target_region="seoul", existing_rows=existing)

    assert prepared[0]["lat"] == 37.1
    assert prepared[0]["lon"] == 127.1


def test_slash_spacing_reuses_existing_stop_coordinates(monkeypatch):
    monkeypatch.setattr(
        weather_api, "get_coordinates_by_gemini",
        lambda _name: pytest.fail("slash spacing must reuse coordinates"),
    )
    existing = [{
        "region": "gyeonggi", "route_name": "(출근) 수원역/수원시청",
        "stop_order": 1, "stop_name": "수원역", "arrival_time": "06:25",
        "lat": 37.2, "lon": 127.0, "nx": 60, "ny": 121, "status": "ok",
        "source_kind": "document",
    }]

    prepared = weather_api.prepare_routes_with_sequential_geocoding([{
        "route_name": "(출근) 수원역/ 수원시청",
        "stop_name": "수원역", "arrival_time": "06:25",
    }], target_region="gyeonggi", existing_rows=existing)

    assert prepared[0]["lat"] == 37.2
    assert prepared[0]["lon"] == 127.0


def test_frozen_seoul_rows_are_reused_without_geocoding(monkeypatch):
    rows = [row for row in json.loads(Path("routes_db.json").read_text(encoding="utf-8-sig"))
            if row.get("region", "gyeonggi") == "seoul"]
    parsed = [{
        "route_name": row["route_name"], "stop_name": row["stop_name"],
        "arrival_time": row["arrival_time"], "region": row["region"],
    } for row in rows]
    monkeypatch.setattr(
        weather_api, "get_coordinates_by_gemini",
        lambda _name: pytest.fail("unchanged frozen rows must not be geocoded"),
    )

    prepared = weather_api.prepare_routes_with_sequential_geocoding(
        parsed, target_region="seoul", existing_rows=rows,
    )

    route_orders = {}
    for current, proposed in zip(rows, prepared):
        route = current["route_name"]
        route_orders[route] = route_orders.get(route, 0) + 1
        assert {key: proposed[key] for key in ("stop_name", "lat", "lon", "nx", "ny", "status")} == {
            key: current[key] for key in ("stop_name", "lat", "lon", "nx", "ny", "status")
        }
        expected_time = "" if "퇴근" in route and route_orders[route] > 1 else current["arrival_time"]
        assert proposed["arrival_time"] == expected_time


def test_frozen_seoul_preview_ignores_parenthesis_spacing_but_keeps_skt_changes():
    frozen = [row for row in json.loads(Path("routes_db.json").read_text(encoding="utf-8-sig"))
              if row.get("region", "gyeonggi") == "seoul"]
    desired_rows = []
    for row in frozen:
        desired = dict(row, route_name=row["route_name"].replace(") ", ")", 1))
        if row["route_name"] in {"(퇴근) 서울시청 1호", "(퇴근) 서울시청 2호"} and \
                row["stop_name"] == "SK타워 앞":
            desired["stop_name"] = "SKT타워 앞"
        if row["route_name"] == "(퇴근) 과천/사당" and \
                row["stop_name"].endswith("버스정류장(북판교 방면)"):
            desired["stop_name"] = row["stop_name"].replace(
                "버스정류장(", "버스정류장 (", 1,
            )
        desired_rows.append(desired)
    normalized = repository.validate_route_document_rows(desired_rows, "seoul")
    grouped = OrderedDict()
    for row in frozen:
        grouped.setdefault(row["route_name"], []).append(row)
    routes = []
    next_route_stop_id = 1000
    next_stop_id = 2000
    for route_id, (route_name, rows) in enumerate(grouped.items(), start=100):
        trip = "evening" if "퇴근" in route_name else "morning"
        stops = []
        for order, row in enumerate(rows, start=1):
            scheduled = time.fromisoformat(row["arrival_time"]) if row["arrival_time"] else None
            if trip == "evening" and order > 1:
                scheduled = None
            dropoff_only = "(하차만)" in row["stop_name"]
            stops.append(current_stop(
                next_stop_id, next_route_stop_id, row["stop_name"], order, scheduled,
                str(row["lat"]), str(row["lon"]),
                (not dropoff_only) if trip == "morning" else order == 1,
                dropoff_only if trip == "morning" else True,
            ))
            next_route_stop_id += 1
            next_stop_id += 1
        if trip == "morning":
            stops.append(current_stop(
                next_stop_id, next_route_stop_id, "판교 제2테크노밸리", len(stops) + 1,
                None, "37.412605", "127.095703", False, True, True, "system_default",
            ))
            next_route_stop_id += 1
            next_stop_id += 1
        routes.append(current_route(
            name=route_name, route_id=route_id, trip=trip, stops=stops,
        ))

    plan = repository._build_reconcile_plan(normalized, state(*routes))

    assert len(routes) == 15
    assert plan["counts"]["routes_added"] == 0
    assert plan["counts"]["routes_deactivated"] == 0
    assert plan["counts"]["stops_added"] == 0
    assert plan["counts"]["stops_removed"] == 0
    assert [(item["route_name"], item["current_stop_name"], item["proposed_stop_name"])
            for item in plan["change_candidates"]] == [
        ("(퇴근) 서울시청 1호", "SK타워 앞", "SKT타워 앞"),
        ("(퇴근) 서울시청 2호", "SK타워 앞", "SKT타워 앞"),
    ]


def test_parenthesis_spacing_is_noop_and_preserves_existing_display_name():
    existing_name = "정류장(북판교 방면)"
    route = current_route(
        name="(퇴근) A", trip="evening",
        stops=[current_stop(name=existing_name, scheduled=time(17, 30),
                            boarding=True, alighting=True)],
    )
    desired = repository.validate_route_document_rows([
        document_row("(퇴근) A", "정류장  (북판교 방면)", "17:30"),
    ], "gyeonggi")

    plan = repository._build_reconcile_plan(desired, state(route))

    assert plan["change_candidates"] == []
    assert plan["counts"]["routes_updated"] == 0
    assert plan["routes"][0]["stops"][0]["desired"]["stop_name"] == existing_name


def test_new_stop_is_geocoded_and_previewed_as_added(monkeypatch):
    calls = []
    monkeypatch.setattr(
        weather_api, "get_coordinates_by_gemini",
        lambda name: calls.append(name) or (37.2, 127.2),
    )
    existing = [{
        "region": "gyeonggi", "route_name": "(출근) A", "stop_name": "정류장 A",
        "arrival_time": "07:10", "lat": 37.1, "lon": 127.1,
        "nx": 60, "ny": 120, "status": "ok",
    }]
    prepared = weather_api.prepare_routes_with_sequential_geocoding([
        {"route_name": "(출근) A", "stop_name": "정류장 A", "arrival_time": "07:10"},
        {"route_name": "(출근) A", "stop_name": "신규 정류장", "arrival_time": "07:20"},
    ], existing_rows=existing)
    plan = repository._build_reconcile_plan(
        repository.validate_route_document_rows(prepared, "gyeonggi"),
        state(current_route()),
    )

    assert calls == ["신규 정류장"]
    assert plan["counts"]["stops_added"] == 1
    assert any(item["change_type"] == "STOP_ADDED" and
               item["stop_name"] == "신규 정류장" for item in plan["details"])


def test_unambiguous_spacing_variant_reuses_coordinates_and_preserves_parser_name(monkeypatch):
    monkeypatch.setattr(
        weather_api, "get_coordinates_by_gemini",
        lambda _name: pytest.fail("spacing-only variant must not be geocoded"),
    )
    existing = [{
        "region": "gyeonggi", "route_name": "(퇴근) A",
        "stop_name": "KOICA 후문 맞은 편", "arrival_time": "17:30",
        "lat": 37.1, "lon": 127.1, "nx": 60, "ny": 120, "status": "ok",
    }]

    prepared = weather_api.prepare_routes_with_sequential_geocoding(
        [{"route_name": "(퇴근) A", "stop_name": "  KOICA  후문 맞은편 ",
          "arrival_time": "17:30"}],
        existing_rows=existing,
    )

    assert prepared[0]["stop_name"] == "KOICA  후문 맞은편"
    assert prepared[0]["lat"] == 37.1


@pytest.mark.parametrize("old_name, new_name", [
    ("SK타워 앞", "SKT타워 앞"),
    ("순천향대학병원 앞", "순천향대학교병원 앞"),
])
def test_meaningful_parser_name_change_reuses_position_coordinates_and_is_blocked(
    monkeypatch, old_name, new_name,
):
    calls = []
    monkeypatch.setattr(
        weather_api, "get_coordinates_by_gemini",
        lambda name: calls.append(name) or (37.2, 127.2),
    )
    existing = [{
        "region": "gyeonggi", "route_name": "(출근) A", "stop_name": old_name,
        "_stop_order": 1, "source_kind": "document",
        "arrival_time": "07:10", "lat": 37.1, "lon": 127.1,
        "nx": 60, "ny": 120, "status": "ok",
    }]
    prepared = weather_api.prepare_routes_with_sequential_geocoding(
        [{"route_name": "(출근) A", "stop_name": new_name, "arrival_time": "07:10"}],
        existing_rows=existing,
    )
    route = current_route(stops=[
        current_stop(name=old_name),
        current_stop(202, 102, "판교 제2테크노밸리", 2, None,
                     "37.412605", "127.095703", False, True, True,
                     "system_default"),
    ])
    plan = repository._build_reconcile_plan(
        repository.validate_route_document_rows(prepared, "gyeonggi"),
        state(route),
    )

    assert calls == []
    assert prepared[0]["stop_name"] == new_name
    assert prepared[0]["lat"] == 37.1
    assert len(plan["change_candidates"]) == 1
    assert plan["change_candidates"][0]["fields"][0]["current"] == old_name
    assert plan["change_candidates"][0]["fields"][0]["proposed"] == new_name
    assert plan["unresolved_candidates"]


def test_shared_physical_stop_name_changes_reuse_coordinates_for_both_routes(monkeypatch):
    calls = []
    monkeypatch.setattr(
        weather_api, "get_coordinates_by_gemini",
        lambda name: calls.append(name) or (37.2, 127.2),
    )
    existing = [{
        "region": "seoul", "route_name": f"(퇴근) 서울시청 {vehicle}",
        "_stop_order": 1, "stop_name": "SK타워 앞", "arrival_time": departure,
        "lat": 37.5664, "lon": 126.985, "nx": 60, "ny": 127,
        "status": "ok", "source_kind": "document",
    } for vehicle, departure in (("1호", "17:30"), ("2호", "17:35"))]
    parsed = [{
        "route_name": f"(퇴근) 서울시청 {vehicle}",
        "stop_name": "SKT타워 앞", "arrival_time": departure,
    } for vehicle, departure in (("1호", "17:30"), ("2호", "17:35"))]

    prepared = weather_api.prepare_routes_with_sequential_geocoding(
        parsed, target_region="seoul", existing_rows=existing,
    )
    route_1 = current_route(
        name="(퇴근) 서울시청 1호", route_id=11, trip="evening",
        stops=[current_stop(152, 231, "SK타워 앞", 1, time(17, 30),
                            "37.566400", "126.985000", True, True)],
    )
    route_2 = current_route(
        name="(퇴근) 서울시청 2호", route_id=12, trip="evening",
        stops=[current_stop(152, 236, "SK타워 앞", 1, time(17, 35),
                            "37.566400", "126.985000", True, True)],
    )
    plan = repository._build_reconcile_plan(
        repository.validate_route_document_rows(prepared, "seoul"),
        state(route_1, route_2),
    )

    assert calls == []
    assert [(row["lat"], row["lon"]) for row in prepared] == [
        (37.5664, 126.985), (37.5664, 126.985),
    ]
    assert len(plan["change_candidates"]) == 2
    assert {candidate["proposed_stop_name"] for candidate in plan["change_candidates"]} == {
        "SKT타워 앞"
    }


def test_existing_stop_with_missing_coordinates_is_geocoded(monkeypatch):
    calls = []
    monkeypatch.setattr(
        weather_api, "get_coordinates_by_gemini",
        lambda name: calls.append(name) or (37.2, 127.2),
    )
    existing = [{
        "region": "gyeonggi", "route_name": "(출근) A", "_stop_order": 1,
        "stop_name": "정류장 A", "arrival_time": "07:10",
        "lat": None, "lon": None, "nx": None, "ny": None, "status": "missing",
        "source_kind": "document",
    }]

    prepared = weather_api.prepare_routes_with_sequential_geocoding([{
        "route_name": "(출근) A", "stop_name": "정류장 A", "arrival_time": "07:10",
    }], existing_rows=existing)

    assert calls == ["정류장 A"]
    assert (prepared[0]["lat"], prepared[0]["lon"]) == (37.2, 127.2)


def test_seoul_fixture_route_spacing_and_name_candidates_do_not_regeocode(monkeypatch):
    frozen = [row for row in json.loads(Path("routes_db.json").read_text(encoding="utf-8-sig"))
              if row.get("region", "gyeonggi") == "seoul"]
    route_orders = {}
    existing = []
    parsed = []
    for row in frozen:
        route_name = row["route_name"]
        route_orders[route_name] = route_orders.get(route_name, 0) + 1
        existing.append(dict(
            row, _stop_order=route_orders[route_name], source_kind="document",
        ))
        parsed_stop_name = row["stop_name"]
        if route_name in {"(퇴근) 서울시청 1호", "(퇴근) 서울시청 2호"} and \
                parsed_stop_name == "SK타워 앞":
            parsed_stop_name = "SKT타워 앞"
        parsed.append({
            "route_name": route_name.replace(") ", ")", 1),
            "stop_name": parsed_stop_name, "arrival_time": row["arrival_time"],
        })
    monkeypatch.setattr(
        weather_api, "get_coordinates_by_gemini",
        lambda _name: pytest.fail("existing Seoul stops must not be re-geocoded"),
    )

    prepared = weather_api.prepare_routes_with_sequential_geocoding(
        parsed, target_region="seoul", existing_rows=existing,
    )

    assert len(prepared) == len(parsed)
    assert [row["stop_name"] for row in prepared].count("SKT타워 앞") == 2


def test_admin_snapshot_keeps_hidden_route_stop_order(monkeypatch):
    gyeonggi_state = state(current_route())
    seoul_state = {"region": "seoul", "region_id": 2, "routes": OrderedDict(), "raw_rows": []}
    monkeypatch.setattr(
        repository, "_load_region_state",
        lambda _cursor, region: gyeonggi_state if region == "gyeonggi" else seoul_state,
    )

    @contextmanager
    def connection_factory():
        yield ResultConnection([])

    snapshot = repository.load_admin_route_snapshot(connection_factory=connection_factory)

    assert snapshot["rows"][0]["_stop_order"] == 1


def test_evening_time_normalization_keeps_only_first_departure(monkeypatch):
    monkeypatch.setattr(
        weather_api, "get_coordinates_by_gemini", lambda _name: (37.1, 127.1)
    )
    prepared = weather_api.prepare_routes_with_sequential_geocoding([
        {"route_name": "(퇴근) A", "stop_name": "회사", "arrival_time": "17:30"},
        {"route_name": "(퇴근) A", "stop_name": "중간", "arrival_time": "17:30"},
        {"route_name": "(퇴근) A", "stop_name": "종점", "arrival_time": "17:30"},
    ])
    normalized = repository.validate_route_document_rows(prepared, "gyeonggi")

    assert [row["arrival_time"] for row in prepared] == ["17:30", "", ""]
    assert [row["scheduled_time"] for row in normalized] == [time(17, 30), None, None]
    assert normalized[0]["boarding_allowed"] is True
    assert all(row["boarding_allowed"] is False for row in normalized[1:])


def test_morning_times_are_preserved_for_all_stops(monkeypatch):
    monkeypatch.setattr(
        weather_api, "get_coordinates_by_gemini", lambda _name: (37.1, 127.1)
    )
    prepared = weather_api.prepare_routes_with_sequential_geocoding([
        {"route_name": "(출근) A", "stop_name": "첫 정류장", "arrival_time": "07:10"},
        {"route_name": "(출근) A", "stop_name": "두 번째 정류장", "arrival_time": "07:20"},
    ])
    normalized = repository.validate_route_document_rows(prepared, "gyeonggi")

    assert [row["arrival_time"] for row in prepared] == ["07:10", "07:20"]
    assert [row["scheduled_time"] for row in normalized] == [time(7, 10), time(7, 20)]


def test_morning_dropoff_only_keeps_pdf_time_and_permissions():
    normalized = repository.validate_route_document_rows([
        document_row("(출근) 경기 A", "일반 정류장", "06:30"),
        document_row("(출근) 경기 A", "화랑공원남편 버스정류장 (하차만)", "07:15"),
        document_row("(출근) 경기 B", "다른 일반 정류장", "06:40"),
        document_row("(출근) 경기 B", "화랑공원남편 버스정류장 (하차만)", "07:25"),
    ], "gyeonggi")

    dropoffs = [row for row in normalized if "(하차만)" in row["stop_name"]]
    assert [row["scheduled_time"] for row in dropoffs] == [time(7, 15), time(7, 25)]
    assert all(row["boarding_allowed"] is False for row in dropoffs)
    assert all(row["alighting_allowed"] is True for row in dropoffs)


def test_evening_vehicle_departures_apply_only_to_each_first_boarding_stop():
    normalized = repository.validate_route_document_rows([
        document_row("(퇴근) 서울시청 1호", "회사", "17:30", region="seoul"),
        document_row("(퇴근) 서울시청 1호", "SKT타워 앞", "17:30", region="seoul"),
        document_row("(퇴근) 서울시청 2호", "회사", "17:35", region="seoul"),
        document_row("(퇴근) 서울시청 2호", "SKT타워 앞", "17:35", region="seoul"),
    ], "seoul")

    assert [row["scheduled_time"] for row in normalized] == [
        time(17, 30), None, time(17, 35), None,
    ]
    assert [row["boarding_allowed"] for row in normalized] == [True, False, True, False]


def test_repository_boundary_clears_evening_followup_time_but_keeps_first():
    normalized = repository.validate_route_document_rows([
        document_row("(퇴근) A", "회사", "17:30"),
        document_row("(퇴근) A", "도착", "17:30"),
    ], "gyeonggi")

    assert normalized[0]["scheduled_time"] == time(17, 30)
    assert normalized[1]["scheduled_time"] is None


@pytest.mark.parametrize("old_name, new_name", [
    ("SK타워 앞", "SKT타워 앞"),
    ("영등포역 버스정류장(중)", "영등포역 중앙차로 버스정류장"),
])
def test_meaningful_name_change_is_selectable_change_candidate(old_name, new_name):
    route = current_route(stops=[
        current_stop(name=old_name),
        current_stop(202, 102, "판교 제2테크노밸리", 2, None,
                     "37.412605", "127.095703", False, True, True,
                     "system_default"),
    ])
    desired = repository.validate_route_document_rows([
        document_row(stop=new_name, lat=37.2, lon=127.2),
    ], "gyeonggi")

    plan = repository._build_reconcile_plan(desired, state(route))

    assert len(plan["change_candidates"]) == 1
    fields = plan["change_candidates"][0]["fields"]
    assert fields == [{"field": "stop_name", "current": old_name, "proposed": new_name}]
    assert plan["unresolved_candidates"]
    assert plan["conflicts"] == []


def test_time_only_change_reuses_coordinates_and_reports_only_time(monkeypatch):
    monkeypatch.setattr(
        weather_api, "get_coordinates_by_gemini",
        lambda _name: pytest.fail("time-only change must not geocode"),
    )
    existing = [{
        "region": "gyeonggi", "route_name": "(출근) A", "stop_name": "정류장 A",
        "arrival_time": "07:10", "lat": 37.1, "lon": 127.1,
        "nx": 60, "ny": 120, "status": "ok",
    }]
    prepared = weather_api.prepare_routes_with_sequential_geocoding(
        [{"route_name": "(출근) A", "stop_name": "정류장 A", "arrival_time": "07:15"}],
        existing_rows=existing,
    )
    plan = repository._build_reconcile_plan(
        repository.validate_route_document_rows(prepared, "gyeonggi"),
        state(current_route()),
    )

    assert plan["counts"]["stops_updated"] == 1
    stop_details = [item for item in plan["details"] if item["change_type"] == "CHANGE_CANDIDATE"]
    assert [item["field"] for item in stop_details] == ["scheduled_time"]
    assert stop_details[0]["current"] == "07:10"
    assert stop_details[0]["proposed"] == "07:15"


def test_plan_adds_new_route_stop_and_default_without_merging_different_coordinates():
    rows = repository.validate_route_document_rows([
        document_row("(출근) 신규", "공통", lat=37.1),
        document_row("(출근) 신규", "공통 다른 위치", arrival="07:20", lat=37.2),
    ], "gyeonggi")

    plan = repository._build_reconcile_plan(rows, state())

    assert plan["counts"] == {
        "routes_added": 1, "routes_updated": 0, "routes_deactivated": 0,
        "stops_added": 3, "stops_updated": 0, "stops_removed": 0,
    }
    assert all(item["current"] is None for item in plan["routes"][0]["stops"])


def test_plan_repairs_noncanonical_system_default_without_replacing_its_route_stop():
    wrong_default = current_stop(
        299, 102, "잘못된 기본 목적지", 2, None,
        "37.500000", "127.200000", False, True, True, "system_default",
    )
    rows = repository.validate_route_document_rows([document_row()], "gyeonggi")

    plan = repository._build_reconcile_plan(
        rows, state(current_route(stops=[current_stop(), wrong_default]))
    )

    assert plan["counts"]["stops_updated"] == 1
    assert plan["routes"][0]["default"]["id"] == 102
    assert plan["routes"][0]["changed"] is True


def test_plan_soft_deactivates_referenced_stop_and_route_removal():
    kept_stop = current_stop(name="유지 정류장")
    referenced_stop = current_stop(203, 103, "삭제 정류장", 2, time(7, 20),
                                   favorite_count=1)
    default = current_stop(202, 102, "판교 제2테크노밸리", 3, None,
                           "37.412605", "127.095703", False, True, True,
                           "system_default")
    rows = repository.validate_route_document_rows([
        document_row(stop="유지 정류장"),
    ], "gyeonggi")
    stop_plan = repository._build_reconcile_plan(
        rows, state(current_route(stops=[kept_stop, referenced_stop, default]))
    )
    assert stop_plan["conflicts"] == []
    assert stop_plan["counts"]["stops_removed"] == 1

    route_plan = repository._build_reconcile_plan(
        repository.validate_route_document_rows([
            document_row("(출근) B", "B 정류장"),
        ], "gyeonggi"),
        state(current_route(favorite_count=1)),
    )
    assert route_plan["conflicts"] == []
    assert route_plan["counts"]["routes_deactivated"] == 1


def test_unreferenced_missing_route_is_soft_deactivated():
    rows = repository.validate_route_document_rows([
        document_row("(출근) B", "B 정류장"),
    ], "gyeonggi")
    plan = repository._build_reconcile_plan(rows, state(current_route()))
    assert plan["counts"]["routes_deactivated"] == 1
    assert plan["deactivate"][0]["id"] == 11


def test_unreferenced_route_rename_is_new_route_plus_deactivation():
    row = document_row("(출근) 새 이름", "정류장 A", _identity_key="old-stop")
    normalized = repository.validate_route_document_rows([row], "gyeonggi")
    plan = repository._build_reconcile_plan(
        normalized, state(current_route()),
        {"old-stop": {"route_id": 11, "route_stop_id": 101, "stop_id": 201}},
    )
    assert plan["conflicts"] == []
    assert plan["counts"]["routes_added"] == 1
    assert plan["counts"]["routes_deactivated"] == 1


def test_same_identity_groups_name_time_and_permission_changes():
    route = current_route(stops=[
        current_stop(name="기존 정류장"),
        current_stop(202, 102, "판교 제2테크노밸리", 2, None,
                     "37.412605", "127.095703", False, True, True,
                     "system_default"),
    ])
    desired = repository.validate_route_document_rows([
        document_row(stop="새 정류장 (하차만)", arrival="07:20", lat=37.9, lon=127.9),
    ], "gyeonggi")

    plan = repository._build_reconcile_plan(desired, state(route))

    assert len(plan["change_candidates"]) == 1
    candidate = plan["change_candidates"][0]
    assert {field["field"] for field in candidate["fields"]} == {
        "stop_name", "scheduled_time", "boarding_allowed", "alighting_allowed",
    }
    effective = plan["routes"][0]["stops"][0]["desired"]
    assert effective["latitude"] == Decimal("37.100000")
    assert effective["longitude"] == Decimal("127.100000")


def test_apply_and_keep_existing_preserve_route_stop_identity():
    route = current_route()
    desired = repository.validate_route_document_rows([
        document_row(stop="새 이름", arrival="07:20", lat=37.9, lon=127.9),
    ], "gyeonggi")
    draft = repository._build_reconcile_plan(desired, state(route))
    candidate_id = draft["change_candidates"][0]["candidate_id"]

    applied = repository._build_reconcile_plan(
        desired, state(route), change_decisions={candidate_id: "APPLY"},
    )
    kept = repository._build_reconcile_plan(
        desired, state(route), change_decisions={candidate_id: "KEEP_EXISTING"},
    )

    applied_stop = applied["routes"][0]["stops"][0]
    kept_stop = kept["routes"][0]["stops"][0]
    assert applied_stop["current"]["id"] == kept_stop["current"]["id"] == 101
    assert applied_stop["desired"]["stop_name"] == "새 이름"
    assert applied_stop["desired"]["scheduled_time"] == time(7, 20)
    assert kept_stop["desired"]["stop_name"] == "정류장 A"
    assert kept_stop["desired"]["scheduled_time"] == time(7, 10)
    assert not kept["unresolved_candidates"]


def test_keep_existing_does_not_block_another_approved_change():
    route = current_route(stops=[
        current_stop(201, 101, "A", 1),
        current_stop(202, 102, "B", 2, scheduled=time(7, 20)),
        current_stop(203, 103, "판교 제2테크노밸리", 3, None,
                     "37.412605", "127.095703", False, True, True,
                     "system_default"),
    ])
    desired = repository.validate_route_document_rows([
        document_row(stop="A 새 이름"), document_row(stop="B", arrival="07:25"),
    ], "gyeonggi")
    draft = repository._build_reconcile_plan(desired, state(route))
    decisions = {
        candidate["candidate_id"]: (
            "KEEP_EXISTING" if candidate["current_stop_name"] == "A" else "APPLY"
        )
        for candidate in draft["change_candidates"]
    }

    plan = repository._build_reconcile_plan(
        desired, state(route), change_decisions=decisions,
    )

    assert not plan["unresolved_candidates"] and not plan["conflicts"]
    effective = {item["current"]["id"]: item["desired"] for item in plan["routes"][0]["stops"]}
    assert effective[101]["stop_name"] == "A"
    assert effective[102]["scheduled_time"] == time(7, 25)
    assert plan["counts"]["stops_updated"] == 1


def test_append_is_added_but_middle_insertion_is_structure_conflict():
    route = current_route(stops=[
        current_stop(201, 101, "A", 1),
        current_stop(202, 102, "B", 2, scheduled=time(7, 20)),
        current_stop(203, 103, "C", 3, scheduled=time(7, 30)),
        current_stop(204, 104, "판교 제2테크노밸리", 4, None,
                     "37.412605", "127.095703", False, True, True,
                     "system_default"),
    ])
    appended = repository.validate_route_document_rows([
        document_row(stop="A"), document_row(stop="B", arrival="07:20"),
        document_row(stop="C", arrival="07:30"), document_row(stop="D", arrival="07:40"),
    ], "gyeonggi")
    inserted = repository.validate_route_document_rows([
        document_row(stop="A"), document_row(stop="X", arrival="07:15"),
        document_row(stop="B", arrival="07:20"), document_row(stop="C", arrival="07:30"),
    ], "gyeonggi")

    append_plan = repository._build_reconcile_plan(appended, state(route))
    insert_plan = repository._build_reconcile_plan(inserted, state(route))

    assert append_plan["counts"]["stops_added"] == 1
    assert not append_plan["conflicts"]
    assert any("ROUTE_STRUCTURE_CONFLICT" in item for item in insert_plan["conflicts"])
    assert any(item["change_type"] == "STRUCTURE_CHANGE" for item in insert_plan["details"])


def test_unambiguous_pure_reorder_preserves_existing_route_stop_ids():
    route = current_route(stops=[
        current_stop(201, 101, "A", 1),
        current_stop(202, 102, "B", 2, scheduled=time(7, 20)),
        current_stop(203, 103, "판교 제2테크노밸리", 3, None,
                     "37.412605", "127.095703", False, True, True,
                     "system_default"),
    ])
    desired = repository.validate_route_document_rows([
        document_row(stop="B", arrival="07:20"), document_row(stop="A"),
    ], "gyeonggi")

    plan = repository._build_reconcile_plan(desired, state(route))

    assert not plan["conflicts"]
    assert [(item["desired"]["stop_name"], item["current"]["id"])
            for item in plan["routes"][0]["stops"]] == [("B", 102), ("A", 101)]
    assert sum(item["change_type"] == "STOP_MOVED" for item in plan["details"]) == 2


class ResultCursor:
    def __init__(self, results):
        self.results = list(results)
        self.current = None
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))
        self.current = self.results.pop(0) if self.results else None

    def fetchone(self):
        if isinstance(self.current, list):
            return self.current[0] if self.current else None
        return self.current

    def fetchall(self):
        return list(self.current or [])


class ResultConnection:
    def __init__(self, results):
        self.cursor_value = ResultCursor(results)

    def cursor(self):
        return self.cursor_value


def test_route_deactivation_disables_favorites_and_notifications_without_delete():
    plan = {
        "deactivate": [current_route()], "routes": [],
        "counts": {"routes_added": 0, "routes_updated": 0, "routes_deactivated": 1,
                   "stops_added": 0, "stops_updated": 0, "stops_removed": 0},
    }
    connection = ResultConnection([None, [(44,)], None])

    repository._apply_reconcile_plan(connection.cursor_value, plan, state(current_route()))

    calls = connection.cursor_value.calls
    assert any("UPDATE routes SET active=FALSE" in sql for sql, _ in calls)
    assert any("UPDATE favorites SET active=FALSE" in sql for sql, _ in calls)
    assert any("UPDATE favorite_notifications SET enabled=FALSE" in sql for sql, _ in calls)
    assert not any("DELETE FROM favorites" in sql or "DELETE FROM routes" in sql
                   for sql, _ in calls)


def test_stop_removal_soft_deactivates_reference_and_preserves_history_tables(monkeypatch):
    route = current_route(stops=[
        current_stop(201, 101, "A", 1),
        current_stop(202, 102, "B", 2, scheduled=time(7, 20), favorite_count=1),
        current_stop(203, 103, "판교 제2테크노밸리", 3, None,
                     "37.412605", "127.095703", False, True, True,
                     "system_default"),
    ])
    normalized = repository.validate_route_document_rows([document_row(stop="A")], "gyeonggi")
    plan = repository._build_reconcile_plan(normalized, state(route))
    connection = ResultConnection([
        None, None, None, [(44,)], None, None, None, (2, 1, 0), (0,), None,
    ])
    monkeypatch.setattr(repository, "_find_or_create_stop", lambda *_a, **_k: 201)
    monkeypatch.setattr(repository, "_default_stop_id", lambda _cursor: 203)

    repository._apply_reconcile_plan(connection.cursor_value, plan, state(route))

    calls = connection.cursor_value.calls
    assert any("UPDATE route_stops SET active=FALSE" in sql for sql, _ in calls)
    assert any("inactive_reason='STOP_REMOVED'" in sql for sql, _ in calls)
    assert any("UPDATE favorites SET active=FALSE" in sql for sql, _ in calls)
    assert any("UPDATE favorite_notifications SET enabled=FALSE" in sql for sql, _ in calls)
    combined = " ".join(sql for sql, _ in calls)
    assert "notification_runs" not in combined and "notification_deliveries" not in combined
    assert "DELETE FROM route_stops" not in combined and "DELETE FROM favorites" not in combined


def test_shared_stop_coordinate_change_splits_physical_stop():
    rs_updated = "rs-v1"
    stop_updated = "s-v1"
    version = hashlib.sha256(repr((101, 201, rs_updated, stop_updated)).encode("utf-8")).hexdigest()
    row = (101, 201, rs_updated, "공통", Decimal("37.100000"), Decimal("127.100000"),
           60, 120, "ok", True, stop_updated, 1, "gyeonggi")
    connection = ResultConnection([
        row,
        [(101,), (999,)],
        [],
        (301,),
        None,
        (1,),
    ])

    @contextmanager
    def transaction():
        yield connection

    result = repository.update_route_stop_coordinates(
        101, latitude=37.2, longitude=127.2, grid_x=61, grid_y=121,
        geocode_status="updated", expected_version=version,
        transaction_factory=transaction,
    )

    assert result == {"route_stop_id": 101, "stop_id": 301, "shared_split": True}
    assert any("UPDATE route_stops SET stop_id" in sql for sql, _ in connection.cursor_value.calls)
    assert not any("UPDATE stops SET active=FALSE" in sql for sql, _ in connection.cursor_value.calls)


def test_stale_snapshot_rolls_back_before_mutation(monkeypatch):
    transaction_state = {"rolled_back": False}
    connection = ResultConnection([None])

    @contextmanager
    def transaction():
        try:
            yield connection
        except Exception:
            transaction_state["rolled_back"] = True
            raise

    monkeypatch.setattr(repository, "_load_region_state", lambda *_args, **_kwargs: state())
    monkeypatch.setattr(repository, "_state_snapshot", lambda _state: "new-version")
    mutate = monkeypatch.setattr(
        repository, "_apply_reconcile_plan",
        lambda *_args: pytest.fail("mutation must not run"),
    )

    with pytest.raises(repository.StaleRouteSnapshotError):
        repository.reconcile_region_routes(
            "gyeonggi", [document_row()], expected_snapshot="old-version",
            transaction_factory=transaction,
        )
    assert transaction_state["rolled_back"]


def test_reconcile_error_does_not_expose_database_details(monkeypatch):
    @contextmanager
    def transaction():
        raise RuntimeError("password=real-secret")
        yield

    with pytest.raises(repository.RouteRepositoryError) as caught:
        repository.reconcile_region_routes(
            "gyeonggi", [document_row()], expected_snapshot="snapshot",
            transaction_factory=transaction,
        )
    assert "real-secret" not in str(caught.value)
