from contextlib import contextmanager
from decimal import Decimal

import pytest

import migrate_routes_to_db as migration


def row(route, stop, *, region="gyeonggi", arrival="07:10", lat=37.1, lon=127.1,
        nx=60, ny=120, status="ok"):
    return {
        "region": region, "route_name": route, "stop_name": stop,
        "arrival_time": arrival, "lat": lat, "lon": lon,
        "nx": nx, "ny": ny, "status": status,
    }


def test_route_grouping_trip_types_and_stop_order():
    plan = migration.build_migration_plan([
        row("(출근) A", "첫 정류장", arrival="07:10"),
        row("(출근) A", "두 번째", arrival="07:20"),
        row("(퇴근) B", "회사", arrival="18:00"),
        row("(퇴근) B", "집", arrival=""),
    ])

    assert [(route.key[1], route.trip_type) for route in plan.routes] == [
        ("(출근) A", "morning"), ("(퇴근) B", "evening")]
    morning = [item for item in plan.route_stops if item.route_key[1] == "(출근) A"]
    evening = [item for item in plan.route_stops if item.route_key[1] == "(퇴근) B"]
    assert [item.stop_order for item in morning] == [1, 2, 3]
    assert [item.stop_order for item in evening] == [1, 2]
    assert evening[0].boarding_allowed and evening[0].alighting_allowed
    assert not evening[1].boarding_allowed and evening[1].alighting_allowed


def test_stop_identity_shares_exact_coordinates_and_splits_different_coordinates():
    plan = migration.build_migration_plan([
        row("(출근) A", "공통", lat=37.1, lon=127.1),
        row("(출근) B", "공통", lat=37.1, lon=127.1),
        row("(출근) C", "공통", lat=37.2, lon=127.1),
    ])
    common = [stop for stop in plan.stops if stop.key[1] == "공통"]
    assert len(common) == 2
    assert plan.coordinate_conflict_names == 1


def test_valid_empty_and_invalid_time_handling():
    assert migration.parse_scheduled_time("17:45").isoformat(timespec="minutes") == "17:45"
    assert migration.parse_scheduled_time("") is None
    assert migration.parse_scheduled_time("-") is None
    for invalid in ("24:00", "7:10", "17:60", "unknown"):
        with pytest.raises(migration.RouteDataError, match="Invalid arrival_time"):
            migration.parse_scheduled_time(invalid)


def test_dropoff_only_conversion_and_original_name_preserved():
    plan = migration.build_migration_plan([
        row("(출근) A", "일반 정류장"),
        row("(출근) A", "중간 정류장 (하차만)"),
    ])
    marked = next(item for item in plan.route_stops if "(하차만)" in item.stop_key[1])
    normal = next(item for item in plan.route_stops if item.stop_key[1] == "일반 정류장")
    assert marked.stop_key[1] == "중간 정류장 (하차만)"
    assert not marked.boarding_allowed and marked.alighting_allowed
    assert normal.boarding_allowed and not normal.alighting_allowed


def test_default_dropoff_plan_is_shared_and_has_no_invented_time():
    plan = migration.build_migration_plan([
        row("(출근) A", "A"),
        row("(출근) B", "B", region="seoul"),
        row("(퇴근) C", "C", arrival="18:00"),
    ])
    defaults = [item for item in plan.route_stops if item.is_default_dropoff]
    default_stops = [stop for stop in plan.stops if stop.key[1] == migration.DEFAULT_DROPOFF]
    assert len(defaults) == 2
    assert len(default_stops) == 1
    assert len({item.stop_key for item in defaults}) == 1
    assert all(item.scheduled_time is None for item in defaults)
    assert all(item.source_kind == "system_default" for item in defaults)
    assert all(not item.boarding_allowed and item.alighting_allowed for item in defaults)


def test_invalid_direction_coordinate_and_conflicting_stop_data_are_rejected():
    with pytest.raises(migration.RouteDataError, match="trip type"):
        migration.build_migration_plan([row("A", "stop")])
    with pytest.raises(migration.RouteDataError, match="coordinate"):
        migration.build_migration_plan([row("(출근) A", "stop", lat=None)])
    with pytest.raises(migration.RouteDataError, match="Conflicting"):
        migration.build_migration_plan([
            row("(출근) A", "same", nx=60),
            row("(출근) B", "same", nx=61),
        ])


def test_nonempty_tables_stop_duplicate_migration():
    migration.ensure_route_tables_empty({"routes": 0, "stops": 0, "route_stops": 0})
    with pytest.raises(migration.RouteTablesNotEmptyError, match="not empty"):
        migration.ensure_route_tables_empty({"routes": 1, "stops": 0, "route_stops": 0})


def test_migration_uses_transaction_boundary_and_propagates_for_rollback(monkeypatch):
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
        migration.migrate_plan(object(), transaction_factory=transaction)
    assert state == {"entered": True, "rolled_back": True}


def test_real_json_plan_matches_values_derived_from_source():
    rows = migration.load_route_rows()
    plan = migration.build_migration_plan(rows)
    route_keys = {(row["region"], row["route_name"]) for row in rows}
    morning = sum("출근" in route_name for _, route_name in route_keys)
    evening = sum("퇴근" in route_name for _, route_name in route_keys)
    dropoff_only = sum("(하차만)" in row["stop_name"] for row in rows)
    empty_times = sum(row["arrival_time"].strip() in migration.NO_TIME_VALUES for row in rows)

    assert plan.source_rows == len(rows)
    assert len(plan.routes) == len(route_keys)
    assert (plan.morning_routes, plan.evening_routes) == (morning, evening)
    assert plan.dropoff_only_rows == dropoff_only
    assert plan.blank_time_rows == empty_times
    assert plan.migrated_route_stops == len(rows)
    assert plan.system_default_route_stops == morning
    assert all(stop.key[2] == stop.key[2].quantize(Decimal("0.000001")) for stop in plan.stops)
