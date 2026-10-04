import os
from collections import Counter

import pytest

from database import database_connection
from migrate_routes_to_db import build_migration_plan, load_route_rows
from route_repository import load_routes_for_ui
from route_repository import preview_region_reconcile
import weather_api


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_AIVEN_INTEGRATION_TESTS") != "1" or not os.getenv("AIVEN_DATABASE_URL"),
    reason="Set RUN_AIVEN_INTEGRATION_TESTS=1 with AIVEN_DATABASE_URL for Aiven tests.",
)


def test_aiven_ui_routes_match_json_plan_read_only():
    plan = build_migration_plan(load_route_rows())
    records = load_routes_for_ui()

    assert len(records) == 249
    assert len({(row["region"], row["route_name"]) for row in records}) == 42
    with database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM stops WHERE active=TRUE")
            assert cursor.fetchone()[0] == 162
    assert Counter(row["trip_type"] for row in records if row["stop_order"] == 1) == {
        "morning": 22, "evening": 20,
    }
    assert sum(row["source_kind"] == "system_default" for row in records) == 22
    assert sum("(하차만)" in row["stop_name"] for row in records) == 13

    document_records = [row for row in records if row["source_kind"] == "document"]
    assert len(document_records) == plan.source_rows == 227
    expected = []
    stop_details = {
        stop.key: (stop.grid_x, stop.grid_y, stop.geocode_status) for stop in plan.stops
    }
    route_types = {route.key: route.trip_type for route in plan.routes}
    for item in plan.route_stops:
        if not item.from_json:
            continue
        grid_x, grid_y, status = stop_details[item.stop_key]
        expected.append({
            "region": item.route_key[0], "route_name": item.route_key[1],
            "trip_type": route_types[item.route_key], "stop_order": item.stop_order,
            "stop_name": item.stop_key[1],
            "arrival_time": item.scheduled_time.strftime("%H:%M") if item.scheduled_time else "",
            "lat": float(item.stop_key[2]), "lon": float(item.stop_key[3]),
            "nx": grid_x, "ny": grid_y, "status": status,
            "boarding_allowed": item.boarding_allowed,
            "alighting_allowed": item.alighting_allowed,
            "is_default_dropoff": item.is_default_dropoff,
            "source_kind": item.source_kind,
        })

    key = lambda row: (row["region"], row["route_name"], row["stop_order"])
    assert sorted(document_records, key=key) == sorted(expected, key=key)


def test_aiven_seoul_frozen_import_only_normalizes_evening_followup_times(monkeypatch):
    source = [row for row in load_route_rows() if row.get("region", "gyeonggi") == "seoul"]
    parsed = [{
        "route_name": row["route_name"], "stop_name": row["stop_name"],
        "arrival_time": row["arrival_time"], "region": row["region"],
    } for row in source]
    monkeypatch.setattr(
        weather_api, "get_coordinates_by_gemini",
        lambda _name: pytest.fail("unchanged Aiven rows must not be geocoded"),
    )
    prepared = weather_api.prepare_routes_with_sequential_geocoding(
        parsed, target_region="seoul", existing_rows=source,
    )

    preview = preview_region_reconcile("seoul", prepared)

    assert {key: preview[key] for key in (
        "routes_added", "routes_updated", "routes_deactivated",
        "stops_added", "stops_updated", "stops_removed",
    )} == {
        "routes_added": 0, "routes_updated": 7, "routes_deactivated": 0,
        "stops_added": 0, "stops_updated": 31, "stops_removed": 0,
    }
    assert preview["conflicts"] == []
    assert len(preview["details"]) == 31
    assert {item["field"] for item in preview["details"]} == {"scheduled_time"}
