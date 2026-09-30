"""Explicit routes_db.json to PostgreSQL data migration and verification."""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

from psycopg.types.json import Jsonb

from database import database_connection, database_transaction, safe_database_target


ROUTES_FILE = Path(__file__).parent / "routes_db.json"
ALLOWED_REGIONS = {"gyeonggi", "seoul"}
DEFAULT_DROPOFF = "판교 제2테크노밸리"
DEFAULT_DROPOFF_REGION = "gyeonggi"
DEFAULT_DROPOFF_LATITUDE = Decimal("37.412605")
DEFAULT_DROPOFF_LONGITUDE = Decimal("127.095703")
DEFAULT_DROPOFF_GRID = (62, 123)
NO_TIME_VALUES = {"", "-"}
TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class RouteMigrationError(RuntimeError):
    pass


class RouteDataError(RouteMigrationError):
    pass


class RouteTablesNotEmptyError(RouteMigrationError):
    pass


class RouteVerificationError(RouteMigrationError):
    pass


StopKey = tuple[str, str, Decimal, Decimal]
RouteKey = tuple[str, str]


@dataclass(frozen=True)
class RoutePlan:
    key: RouteKey
    trip_type: str


@dataclass(frozen=True)
class StopPlan:
    key: StopKey
    grid_x: int
    grid_y: int
    geocode_status: str | None
    metadata: dict


@dataclass(frozen=True)
class RouteStopPlan:
    route_key: RouteKey
    stop_key: StopKey
    stop_order: int
    scheduled_time: time | None
    boarding_allowed: bool
    alighting_allowed: bool
    is_default_dropoff: bool
    source_kind: str
    from_json: bool


@dataclass(frozen=True)
class MigrationPlan:
    routes: tuple[RoutePlan, ...]
    stops: tuple[StopPlan, ...]
    route_stops: tuple[RouteStopPlan, ...]
    source_rows: int
    dropoff_only_rows: int
    blank_time_rows: int
    coordinate_conflict_names: int

    @property
    def morning_routes(self) -> int:
        return sum(route.trip_type == "morning" for route in self.routes)

    @property
    def evening_routes(self) -> int:
        return sum(route.trip_type == "evening" for route in self.routes)

    @property
    def migrated_route_stops(self) -> int:
        return sum(item.from_json for item in self.route_stops)

    @property
    def system_default_route_stops(self) -> int:
        return sum(not item.from_json for item in self.route_stops)


def load_route_rows(path: Path = ROUTES_FILE) -> list[dict]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RouteDataError(f"Could not parse route JSON ({type(exc).__name__}).") from None
    if not isinstance(value, list):
        raise RouteDataError("Route JSON must contain a top-level array.")
    return value


def route_trip_type(route_name: object) -> str:
    if not isinstance(route_name, str) or not route_name.strip():
        raise RouteDataError("A route name is missing.")
    morning = "출근" in route_name
    evening = "퇴근" in route_name
    if morning == evening:
        raise RouteDataError(f"Cannot determine trip type for route: {route_name}")
    return "morning" if morning else "evening"


def parse_scheduled_time(value: object) -> time | None:
    if not isinstance(value, str):
        raise RouteDataError("arrival_time must be a string.")
    cleaned = value.strip()
    if cleaned in NO_TIME_VALUES:
        return None
    if not TIME_PATTERN.fullmatch(cleaned):
        raise RouteDataError(f"Invalid arrival_time: {cleaned}")
    return time.fromisoformat(cleaned)


def _coordinate(value: object, *, latitude: bool) -> Decimal:
    try:
        coordinate = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise RouteDataError("A route coordinate is not numeric.") from None
    limit = Decimal("90") if latitude else Decimal("180")
    if not coordinate.is_finite() or not -limit <= coordinate <= limit:
        raise RouteDataError("A route coordinate is outside the valid range.")
    quantized = coordinate.quantize(Decimal("0.000001"))
    if coordinate != quantized:
        raise RouteDataError("A route coordinate exceeds the schema's six-decimal precision.")
    return quantized


def _smallint(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise RouteDataError(f"{field} must be an integer.")
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise RouteDataError(f"{field} must be an integer.") from None
    if result != value or not -32768 <= result <= 32767:
        raise RouteDataError(f"{field} is outside the SMALLINT range.")
    return result


def _stop_key(row: Mapping) -> StopKey:
    region = row.get("region")
    name = row.get("stop_name")
    if region not in ALLOWED_REGIONS:
        raise RouteDataError(f"Unsupported region: {region}")
    if not isinstance(name, str) or not name.strip():
        raise RouteDataError("A stop name is missing.")
    return (
        region,
        name,
        _coordinate(row.get("lat"), latitude=True),
        _coordinate(row.get("lon"), latitude=False),
    )


def build_migration_plan(rows: list[dict]) -> MigrationPlan:
    routes: dict[RouteKey, RoutePlan] = {}
    stops: dict[StopKey, StopPlan] = {}
    route_stops_by_route: dict[RouteKey, list[RouteStopPlan]] = {}
    dropoff_only_rows = 0
    blank_time_rows = 0
    name_coordinates: dict[tuple[str, str], set[tuple[Decimal, Decimal]]] = {}

    for row_number, row in enumerate(rows, start=1):
        if not isinstance(row, Mapping):
            raise RouteDataError(f"Route row {row_number} is not an object.")
        region = row.get("region")
        route_name = row.get("route_name")
        if region not in ALLOWED_REGIONS:
            raise RouteDataError(f"Unsupported region at row {row_number}: {region}")
        trip_type = route_trip_type(route_name)
        route_key = (region, route_name)
        existing_route = routes.setdefault(route_key, RoutePlan(route_key, trip_type))
        if existing_route.trip_type != trip_type:
            raise RouteDataError(f"Inconsistent trip type for route: {route_name}")

        stop_key = _stop_key(row)
        grid_x = _smallint(row.get("nx"), "nx")
        grid_y = _smallint(row.get("ny"), "ny")
        status = row.get("status")
        if status is not None and not isinstance(status, str):
            raise RouteDataError(f"status must be text at row {row_number}.")
        stop = StopPlan(stop_key, grid_x, grid_y, status, {})
        existing_stop = stops.setdefault(stop_key, stop)
        if existing_stop != stop:
            raise RouteDataError(
                f"Conflicting nx/ny/status for stop identity at row {row_number}."
            )
        name_coordinates.setdefault(stop_key[:2], set()).add(stop_key[2:])

        scheduled_time = parse_scheduled_time(row.get("arrival_time"))
        if scheduled_time is None:
            blank_time_rows += 1
        stop_name = stop_key[1]
        dropoff_only = "(하차만)" in stop_name
        if dropoff_only:
            dropoff_only_rows += 1
        items = route_stops_by_route.setdefault(route_key, [])
        stop_order = len(items) + 1
        if trip_type == "morning":
            boarding_allowed = not dropoff_only
            alighting_allowed = dropoff_only
        else:
            boarding_allowed = stop_order == 1
            alighting_allowed = True
        items.append(RouteStopPlan(
            route_key, stop_key, stop_order, scheduled_time,
            boarding_allowed, alighting_allowed, False, "document", True,
        ))

    default_key: StopKey = (
        DEFAULT_DROPOFF_REGION,
        DEFAULT_DROPOFF,
        DEFAULT_DROPOFF_LATITUDE,
        DEFAULT_DROPOFF_LONGITUDE,
    )
    for route in routes.values():
        if route.trip_type != "morning":
            continue
        items = route_stops_by_route[route.key]
        existing_indexes = [index for index, item in enumerate(items) if item.stop_key[1] == DEFAULT_DROPOFF]
        if len(existing_indexes) > 1:
            raise RouteDataError(f"Multiple default destinations in route: {route.key[1]}")
        if existing_indexes:
            index = existing_indexes[0]
            existing = items[index]
            if existing.scheduled_time is not None:
                raise RouteDataError(
                    f"Existing default destination has a schedule in route: {route.key[1]}"
                )
            items[index] = replace(
                existing,
                boarding_allowed=False,
                alighting_allowed=True,
                is_default_dropoff=True,
                source_kind="system_default",
            )
            continue
        stops.setdefault(default_key, StopPlan(
            default_key,
            DEFAULT_DROPOFF_GRID[0],
            DEFAULT_DROPOFF_GRID[1],
            "system_default",
            {"origin": "app_default_destination"},
        ))
        items.append(RouteStopPlan(
            route.key, default_key, len(items) + 1, None,
            False, True, True, "system_default", False,
        ))

    route_stops = tuple(
        item for route in routes.values() for item in route_stops_by_route[route.key]
    )
    return MigrationPlan(
        routes=tuple(routes.values()),
        stops=tuple(stops.values()),
        route_stops=route_stops,
        source_rows=len(rows),
        dropoff_only_rows=dropoff_only_rows,
        blank_time_rows=blank_time_rows,
        coordinate_conflict_names=sum(len(values) > 1 for values in name_coordinates.values()),
    )


def ensure_route_tables_empty(counts: Mapping[str, int]) -> None:
    nonempty = {name: count for name, count in counts.items() if count}
    if nonempty:
        summary = ", ".join(f"{name}={count}" for name, count in sorted(nonempty.items()))
        raise RouteTablesNotEmptyError(f"Route migration stopped; target tables are not empty ({summary}).")


def _route_table_counts(cursor) -> dict[str, int]:
    cursor.execute(
        "SELECT (SELECT count(*) FROM routes), (SELECT count(*) FROM stops), "
        "(SELECT count(*) FROM route_stops)"
    )
    values = cursor.fetchone()
    return dict(zip(("routes", "stops", "route_stops"), values))


def insert_plan(connection, plan: MigrationPlan) -> None:
    with connection.cursor() as cursor:
        ensure_route_tables_empty(_route_table_counts(cursor))
        cursor.execute("SELECT code, id FROM regions")
        region_ids = dict(cursor.fetchall())
        required_regions = {route.key[0] for route in plan.routes} | {
            stop.key[0] for stop in plan.stops
        }
        missing_regions = required_regions - set(region_ids)
        if missing_regions:
            raise RouteMigrationError("Required regions are missing from the database.")

        route_ids = {}
        for route in plan.routes:
            cursor.execute(
                "INSERT INTO routes (region_id, name, trip_type, active) "
                "VALUES (%s, %s, %s, TRUE) RETURNING id",
                (region_ids[route.key[0]], route.key[1], route.trip_type),
            )
            route_ids[route.key] = cursor.fetchone()[0]

        stop_ids = {}
        for stop in plan.stops:
            region, name, latitude, longitude = stop.key
            cursor.execute(
                "INSERT INTO stops "
                "(region_id, name, latitude, longitude, grid_x, grid_y, geocode_status, active, metadata) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE, %s) RETURNING id",
                (region_ids[region], name, latitude, longitude, stop.grid_x, stop.grid_y,
                 stop.geocode_status, Jsonb(stop.metadata)),
            )
            stop_ids[stop.key] = cursor.fetchone()[0]

        for item in plan.route_stops:
            cursor.execute(
                "INSERT INTO route_stops "
                "(route_id, stop_id, stop_order, scheduled_time, boarding_allowed, "
                "alighting_allowed, is_default_dropoff, source_kind) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (route_ids[item.route_key], stop_ids[item.stop_key], item.stop_order,
                 item.scheduled_time, item.boarding_allowed, item.alighting_allowed,
                 item.is_default_dropoff, item.source_kind),
            )


def migrate_plan(
    plan: MigrationPlan,
    transaction_factory: Callable = database_transaction,
) -> None:
    with transaction_factory() as connection:
        insert_plan(connection, plan)


def _actual_route_data(connection):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT rg.code, r.name, r.trip_type, rs.stop_order, s.name, s.latitude, "
            "s.longitude, s.grid_x, s.grid_y, s.geocode_status, rs.scheduled_time, "
            "rs.boarding_allowed, rs.alighting_allowed, rs.is_default_dropoff, rs.source_kind "
            "FROM route_stops rs JOIN routes r ON r.id = rs.route_id "
            "JOIN regions rg ON rg.id = r.region_id JOIN stops s ON s.id = rs.stop_id "
            "ORDER BY rg.code, r.name, rs.stop_order"
        )
        route_stop_rows = cursor.fetchall()
        cursor.execute(
            "SELECT rg.code, s.name, s.latitude, s.longitude, s.grid_x, s.grid_y, "
            "s.geocode_status FROM stops s JOIN regions rg ON rg.id = s.region_id"
        )
        stop_rows = cursor.fetchall()
        cursor.execute("SELECT rg.code, r.name, r.trip_type FROM routes r JOIN regions rg ON rg.id = r.region_id")
        route_rows = cursor.fetchall()
        cursor.execute(
            "SELECT count(*) FROM route_stops rs LEFT JOIN routes r ON r.id = rs.route_id "
            "LEFT JOIN stops s ON s.id = rs.stop_id WHERE r.id IS NULL OR s.id IS NULL"
        )
        broken_references = cursor.fetchone()[0]
    return route_rows, stop_rows, route_stop_rows, broken_references


def verify_plan(plan: MigrationPlan) -> dict[str, int]:
    with database_connection() as connection:
        route_rows, stop_rows, actual_rows, broken_references = _actual_route_data(connection)

    expected_routes = {(item.key[0], item.key[1], item.trip_type) for item in plan.routes}
    if set(route_rows) != expected_routes:
        raise RouteVerificationError("Route rows do not match the JSON plan.")

    expected_stops = {
        (item.key[0], item.key[1], item.key[2], item.key[3], item.grid_x, item.grid_y,
         item.geocode_status)
        for item in plan.stops
    }
    if set(stop_rows) != expected_stops or len(stop_rows) != len(expected_stops):
        raise RouteVerificationError("Physical stop rows do not match the migration plan.")

    route_types = {route.key: route.trip_type for route in plan.routes}
    stop_details = {
        stop.key: (stop.grid_x, stop.grid_y, stop.geocode_status) for stop in plan.stops
    }
    expected_by_route = {}
    for item in plan.route_stops:
        grid_x, grid_y, status = stop_details[item.stop_key]
        expected_by_route.setdefault(
            (item.route_key[0], item.route_key[1], route_types[item.route_key]), []
        ).append((
            item.stop_order, item.stop_key[1], item.stop_key[2], item.stop_key[3],
            grid_x, grid_y, status, item.scheduled_time, item.boarding_allowed,
            item.alighting_allowed, item.is_default_dropoff, item.source_kind,
        ))
    actual_by_route = {}
    for row in actual_rows:
        actual_by_route.setdefault(row[:3], []).append(row[3:])
    if actual_by_route != expected_by_route:
        raise RouteVerificationError("Route-stop order or values do not match the migration plan.")
    if broken_references:
        raise RouteVerificationError("Broken route-stop references were found.")

    expected_dropoff_only = sum(
        item.from_json and "(하차만)" in item.stop_key[1]
        and not item.boarding_allowed and item.alighting_allowed
        for item in plan.route_stops
    )
    if expected_dropoff_only != plan.dropoff_only_rows:
        raise RouteVerificationError("Dropoff-only conversion is incomplete.")
    default_count = sum(item.is_default_dropoff for item in plan.route_stops)
    if default_count != plan.morning_routes:
        raise RouteVerificationError("Default destination configuration is incomplete.")
    return {
        "routes": len(route_rows),
        "stops": len(stop_rows),
        "route_stops": len(actual_rows),
        "dropoff_only": expected_dropoff_only,
        "default_dropoffs": default_count,
    }


def plan_summary(plan: MigrationPlan) -> list[str]:
    return [
        f"JSON rows: {plan.source_rows}",
        f"Routes: {len(plan.routes)} (morning={plan.morning_routes}, evening={plan.evening_routes})",
        f"Physical stops: {len(plan.stops)}",
        f"JSON route-stops: {plan.migrated_route_stops}",
        f"System-default route-stops: {plan.system_default_route_stops}",
        f"Final route-stops: {len(plan.route_stops)}",
        f"Dropoff-only rows: {plan.dropoff_only_rows}",
        f"Rows without time: {plan.blank_time_rows}",
        f"Same region/name with different coordinates: {plan.coordinate_conflict_names}",
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate ShuttleAI route JSON to PostgreSQL.")
    parser.add_argument("command", choices=("dry-run", "migrate", "verify"))
    args = parser.parse_args()
    try:
        plan = build_migration_plan(load_route_rows())
        for line in plan_summary(plan):
            print(line)
        if args.command == "dry-run":
            print("Dry-run: OK (database unchanged)")
            return 0
        host, database = safe_database_target()
        print(f"Database target: host={host}, database={database}")
        if args.command == "migrate":
            migrate_plan(plan)
            print("Route migration: OK")
        report = verify_plan(plan)
        print("Route verification: OK")
        print("Verified counts: " + ", ".join(f"{key}={value}" for key, value in report.items()))
        return 0
    except RouteMigrationError as exc:
        print(f"Route migration error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
