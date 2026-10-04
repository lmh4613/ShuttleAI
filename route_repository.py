"""Read-only Aiven repository for route data used by the Streamlit UI."""

from __future__ import annotations

import logging
import hashlib
import re
from collections import OrderedDict
from collections.abc import Callable
from datetime import time
from decimal import Decimal, InvalidOperation

from database import database_connection, database_transaction


logger = logging.getLogger(__name__)


class RouteRepositoryError(RuntimeError):
    """Raised when route data cannot be loaded without exposing DB details."""


class RouteValidationError(RouteRepositoryError):
    """Raised before mutation when incoming route rows are invalid."""


class RouteConflictError(RouteRepositoryError):
    """Raised when reconcile would break an existing reference."""

    def __init__(self, conflicts):
        self.conflicts = tuple(conflicts)
        super().__init__("참조 중인 노선 또는 정류장이 변경 대상에 포함되어 저장할 수 없습니다.")


class StaleRouteSnapshotError(RouteRepositoryError):
    """Raised when Admin data changed after the preview was generated."""


TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
ROUTE_NAME_PATTERN = re.compile(r"^\s*\((출근|퇴근)\)\s*(.+?)\s*$")
VEHICLE_ID_PATTERN = re.compile(r"^(.*?)(\d+호)$")
DEFAULT_DROPOFF = "판교 제2테크노밸리"
DEFAULT_DROPOFF_REGION = "gyeonggi"
DEFAULT_DROPOFF_LATITUDE = Decimal("37.412605")
DEFAULT_DROPOFF_LONGITUDE = Decimal("127.095703")


def normalize_stop_name_for_matching(stop_name):
    """Normalize only unambiguous spacing differences for stop-name comparison."""
    normalized = re.sub(r"\s+", " ", str(stop_name or "").strip())
    normalized = normalized.replace("맞은 편", "맞은편")
    return re.sub(r"\s+\(", "(", normalized)
DEFAULT_DROPOFF_GRID = (62, 123)
ALLOWED_REGIONS = {"gyeonggi", "seoul"}


ROUTES_FOR_UI_SQL = """
SELECT rg.code, r.name, r.trip_type,
       rs.stop_order, s.name, rs.scheduled_time,
       s.latitude, s.longitude, s.grid_x, s.grid_y, s.geocode_status,
       rs.boarding_allowed, rs.alighting_allowed,
       rs.is_default_dropoff, rs.source_kind
  FROM routes r
  JOIN regions rg ON rg.id=r.region_id
  JOIN route_stops rs ON rs.route_id=r.id
  JOIN stops s ON s.id=rs.stop_id
 WHERE r.active=TRUE AND rs.active=TRUE AND s.active=TRUE
 ORDER BY rg.code, r.id, rs.stop_order
"""


REGION_STATE_SQL = """
SELECT rg.id, r.id, r.name, r.trip_type, r.active, r.updated_at,
       rs.id, rs.stop_order, rs.scheduled_time, rs.boarding_allowed,
       rs.alighting_allowed, rs.is_default_dropoff, rs.source_kind,
       rs.active, rs.inactive_reason, rs.updated_at,
       s.id, s.name, s.latitude, s.longitude, s.grid_x, s.grid_y,
       s.geocode_status, s.active, s.updated_at,
       (SELECT count(*) FROM favorites f WHERE f.route_id=r.id AND f.active=TRUE),
       (SELECT count(*) FROM favorites f
         WHERE f.active=TRUE AND (
               f.boarding_route_stop_id=rs.id OR f.alighting_route_stop_id=rs.id))
  FROM regions rg
  JOIN routes r ON r.region_id=rg.id
  JOIN route_stops rs ON rs.route_id=r.id
  JOIN stops s ON s.id=rs.stop_id
 WHERE rg.code=%s
 ORDER BY r.id, rs.stop_order
"""


def _time_text(value) -> str:
    return value.strftime("%H:%M") if value is not None else ""


def _ui_record(row) -> dict:
    return {
        "region": row[0],
        "route_name": row[1],
        "trip_type": row[2],
        "stop_order": row[3],
        "stop_name": row[4],
        "arrival_time": _time_text(row[5]),
        "lat": float(row[6]),
        "lon": float(row[7]),
        "nx": row[8],
        "ny": row[9],
        "status": row[10],
        "boarding_allowed": row[11],
        "alighting_allowed": row[12],
        "is_default_dropoff": row[13],
        "source_kind": row[14],
    }


def load_routes_for_ui(
    *,
    connection_factory: Callable = database_connection,
) -> list[dict]:
    """Return active routes in the flat shape expected by the existing UI."""
    try:
        with connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(ROUTES_FOR_UI_SQL)
                rows = cursor.fetchall()
        return [_ui_record(row) for row in rows]
    except Exception as exc:
        logger.warning("Route query failed type=%s", type(exc).__name__)
        raise RouteRepositoryError(
            "노선 정보를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요."
        ) from None


def normalize_base_route_name_for_identity(value: object) -> str:
    """Normalize formatting-only route spacing for identity comparisons."""
    normalized = " ".join(str(value or "").split())
    return re.sub(r"\s*/\s*", "/", normalized).strip()


def route_logical_identity(route_name: object) -> tuple[str, str, str]:
    """Return a whitespace-normalized identity without changing its display name."""
    if not isinstance(route_name, str) or not route_name.strip():
        raise RouteValidationError("노선명이 비어 있습니다.")
    match = ROUTE_NAME_PATTERN.fullmatch(route_name)
    if match is None:
        raise RouteValidationError(f"출근/퇴근 구분을 확인할 수 없는 노선이 있습니다: {route_name}")
    trip_type = "morning" if match.group(1) == "출근" else "evening"
    route_body = " ".join(match.group(2).split())
    vehicle_match = VEHICLE_ID_PATTERN.fullmatch(route_body)
    if vehicle_match:
        base_route_name = normalize_base_route_name_for_identity(vehicle_match.group(1))
        vehicle_id = vehicle_match.group(2)
    else:
        base_route_name = normalize_base_route_name_for_identity(route_body)
        vehicle_id = ""
    if not base_route_name:
        raise RouteValidationError(f"기본 노선명이 비어 있습니다: {route_name}")
    return trip_type, base_route_name, vehicle_id


def _trip_type(route_name: object) -> str:
    return route_logical_identity(route_name)[0]


def _scheduled_time(value: object) -> time | None:
    if value is None:
        return None
    if isinstance(value, time):
        return value.replace(second=0, microsecond=0)
    if not isinstance(value, str):
        raise RouteValidationError("정류장 시간은 HH:MM 형식이어야 합니다.")
    cleaned = value.strip()
    if cleaned in {"", "-"}:
        return None
    if not TIME_PATTERN.fullmatch(cleaned):
        raise RouteValidationError(f"정류장 시간이 올바르지 않습니다: {cleaned}")
    return time.fromisoformat(cleaned)


def _coordinate(value: object, *, latitude: bool) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise RouteValidationError("정류장 좌표가 숫자가 아닙니다.") from None
    limit = Decimal("90") if latitude else Decimal("180")
    if not result.is_finite() or not -limit <= result <= limit:
        raise RouteValidationError("정류장 좌표가 허용 범위를 벗어났습니다.")
    return result.quantize(Decimal("0.000001"))


def _grid(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise RouteValidationError(f"{name} 격자값이 올바르지 않습니다.")
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise RouteValidationError(f"{name} 격자값이 올바르지 않습니다.") from None
    if result != value or not -32768 <= result <= 32767:
        raise RouteValidationError(f"{name} 격자값이 허용 범위를 벗어났습니다.")
    return result


def validate_route_document_rows(rows, target_region: str) -> list[dict]:
    """Normalize geocoded document rows without touching the database."""
    if target_region not in ALLOWED_REGIONS:
        raise RouteValidationError("지원하지 않는 지역입니다.")
    if not isinstance(rows, list) or not rows:
        raise RouteValidationError("저장할 노선 데이터가 없습니다.")

    normalized = []
    seen_routes = set()
    closed_routes = set()
    current_route = None
    stop_names = set()
    route_order = 0
    for row_number, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise RouteValidationError(f"{row_number}번째 노선 행이 올바르지 않습니다.")
        row_region = row.get("region", target_region)
        if row_region != target_region:
            raise RouteValidationError("선택한 지역과 다른 지역 데이터가 포함되어 있습니다.")
        route_name = str(row.get("route_name", "")).strip()
        stop_name = str(row.get("stop_name", "")).strip()
        trip_type = _trip_type(route_name)
        if not stop_name:
            raise RouteValidationError(f"{row_number}번째 정류장명이 비어 있습니다.")
        if stop_name == DEFAULT_DROPOFF:
            raise RouteValidationError("판교 기본 목적지는 문서 정류장으로 직접 저장할 수 없습니다.")
        if route_name != current_route:
            if current_route is not None:
                closed_routes.add(current_route)
            if route_name in closed_routes:
                raise RouteValidationError(f"같은 노선의 행은 연속되어야 합니다: {route_name}")
            current_route = route_name
            seen_routes.add(route_name)
            stop_names = set()
            route_order = 0
        if stop_name in stop_names:
            raise RouteValidationError(f"한 노선에 같은 정류장명이 중복되었습니다: {route_name} / {stop_name}")
        stop_names.add(stop_name)
        route_order += 1
        scheduled = _scheduled_time(row.get("arrival_time"))
        if trip_type == "evening" and route_order > 1:
            scheduled = None
        dropoff_only = "(하차만)" in stop_name
        if trip_type == "morning":
            boarding_allowed = not dropoff_only
            alighting_allowed = dropoff_only
        else:
            boarding_allowed = route_order == 1
            alighting_allowed = True
        if boarding_allowed and scheduled is None:
            raise RouteValidationError(
                f"탑승 가능한 정류장에는 시간이 필요합니다: {route_name} / {stop_name}"
            )
        status = row.get("status")
        if status is not None and not isinstance(status, str):
            raise RouteValidationError("좌표 상태값은 문자열이어야 합니다.")
        normalized.append({
            "region": target_region,
            "route_name": route_name,
            "trip_type": trip_type,
            "stop_order": route_order,
            "stop_name": stop_name,
            "scheduled_time": scheduled,
            "latitude": _coordinate(row.get("lat"), latitude=True),
            "longitude": _coordinate(row.get("lon"), latitude=False),
            "grid_x": _grid(row.get("nx"), "X"),
            "grid_y": _grid(row.get("ny"), "Y"),
            "geocode_status": status,
            "boarding_allowed": boarding_allowed,
            "alighting_allowed": alighting_allowed,
            "is_default_dropoff": False,
            "source_kind": "document",
            "identity_key": row.get("_identity_key"),
        })
    return normalized


def _load_region_state(cursor, region: str, *, lock: bool = False) -> dict:
    sql = REGION_STATE_SQL + (" FOR UPDATE OF r, rs, s" if lock else "")
    cursor.execute(sql, (region,))
    rows = cursor.fetchall()
    routes = OrderedDict()
    region_id = rows[0][0] if rows else None
    if region_id is None:
        cursor.execute("SELECT id FROM regions WHERE code=%s", (region,))
        found = cursor.fetchone()
        if found is None:
            raise RouteValidationError("지역 DB 정보를 찾을 수 없습니다.")
        region_id = found[0]
    for row in rows:
        route = routes.setdefault(row[2], {
            "region_id": row[0], "id": row[1], "name": row[2], "trip_type": row[3],
            "active": row[4], "updated_at": row[5], "favorite_count": row[25],
            "stops": [],
        })
        route["stops"].append({
            "id": row[6], "stop_order": row[7], "scheduled_time": row[8],
            "boarding_allowed": row[9], "alighting_allowed": row[10],
            "is_default_dropoff": row[11], "source_kind": row[12],
            "active": row[13], "inactive_reason": row[14], "updated_at": row[15],
            "stop_id": row[16], "stop_name": row[17],
            "latitude": row[18], "longitude": row[19], "grid_x": row[20],
            "grid_y": row[21], "geocode_status": row[22], "stop_active": row[23],
            "stop_updated_at": row[24], "favorite_count": row[26],
        })
    return {"region": region, "region_id": region_id, "routes": routes, "raw_rows": rows}


def _state_snapshot(state: dict) -> str:
    values = []
    for route in state["routes"].values():
        values.append((route["id"], route["name"], route["trip_type"], route["active"],
                       str(route["updated_at"])))
        for stop in route["stops"]:
            values.append((stop["id"], stop["stop_id"], stop["stop_order"],
                           str(stop["scheduled_time"]), str(stop["updated_at"]),
                           str(stop["stop_updated_at"]), stop["stop_active"],
                           stop.get("active", True), stop.get("inactive_reason")))
    return hashlib.sha256(repr(values).encode("utf-8")).hexdigest()


def _group_rows(rows: list[dict]) -> OrderedDict:
    grouped = OrderedDict()
    for row in rows:
        identity = route_logical_identity(row["route_name"])
        group = grouped.setdefault(identity, {"name": row["route_name"], "stops": []})
        if group["name"] != row["route_name"]:
            raise RouteValidationError(
                "같은 논리 노선에 서로 다른 표시 이름이 포함되어 있습니다."
            )
        group["stops"].append(row)
    return grouped


def _physical_key(row: dict):
    return row["stop_name"], row["latitude"], row["longitude"]


def _preview_value(value):
    if isinstance(value, time):
        return _time_text(value)
    if isinstance(value, Decimal):
        return str(value)
    return value


def _change_detail(route_name, stop_name, change_type, field, current, proposed):
    return {
        "route_name": route_name,
        "stop_name": stop_name,
        "change_type": change_type,
        "field": field,
        "current": _preview_value(current),
        "proposed": _preview_value(proposed),
    }


CHANGE_CANDIDATE_FIELDS = (
    "stop_name", "scheduled_time", "boarding_allowed", "alighting_allowed",
)
CHANGE_DECISIONS = {"APPLY", "KEEP_EXISTING"}


def _candidate_id(route_id, route_stop_id):
    return f"route:{route_id}:stop:{route_stop_id}"


def _build_reconcile_plan(normalized, state, identity_map=None, change_decisions=None) -> dict:
    identity_map = identity_map or {}
    change_decisions = change_decisions or {}
    desired_routes = _group_rows(normalized)
    current_routes = state["routes"]
    conflicts = []
    details = []
    change_candidates = []
    unresolved_candidates = []
    route_plans = []
    counts = {
        "routes_added": 0, "routes_updated": 0, "routes_deactivated": 0,
        "stops_added": 0, "stops_updated": 0, "stops_removed": 0,
    }

    current_by_identity = {}
    for current in current_routes.values():
        identity = route_logical_identity(current["name"])
        if identity in current_by_identity:
            raise RouteValidationError("DB에 중복된 논리 노선이 있습니다.")
        current_by_identity[identity] = current

    for route_identity, desired_group in desired_routes.items():
        desired_display_name = desired_group["name"]
        desired_stops = desired_group["stops"]
        current = current_by_identity.get(route_identity)
        route_name = current["name"] if current is not None else desired_display_name
        trip_type = desired_stops[0]["trip_type"]
        if current is not None and current["trip_type"] != trip_type:
            conflicts.append(f"노선 유형 변경: {route_name}")
            continue
        if current is None:
            counts["routes_added"] += 1
            details.append(_change_detail(
                route_name, None, "ROUTE_ADDED", "route_name", None, route_name
            ))
        existing_document = [] if current is None else [
            item for item in current["stops"]
            if item["source_kind"] == "document" and item.get("active", True)
        ]
        existing_defaults = [] if current is None else [
            item for item in current["stops"]
            if item["is_default_dropoff"] and item.get("active", True)
        ]
        by_id = {item["id"]: item for item in existing_document}
        matched_by_desired = {}
        used = set()

        # Hidden identities from the editable Admin grid are authoritative.
        for desired_index, desired in enumerate(desired_stops):
            identity = identity_map.get(desired.get("identity_key"), {})
            route_stop_id = identity.get("route_stop_id")
            if route_stop_id is None or current is None:
                continue
            candidate = by_id.get(route_stop_id)
            if candidate is None or candidate["id"] in used:
                conflicts.append(
                    f"정류장 식별정보 불일치: {route_name} / {desired['stop_name']}"
                )
                continue
            matched_by_desired[desired_index] = candidate
            used.add(candidate["id"])

        # Safely normalized names identify a stop without fuzzy matching.
        for desired_index, desired in enumerate(desired_stops):
            if desired_index in matched_by_desired:
                continue
            desired_name_key = normalize_stop_name_for_matching(desired["stop_name"])
            matches = [item for item in existing_document
                       if item["id"] not in used and
                       normalize_stop_name_for_matching(item["stop_name"]) == desired_name_key]
            if len(matches) == 1:
                matched_by_desired[desired_index] = matches[0]
                used.add(matches[0]["id"])
            elif len(matches) > 1:
                conflicts.append(f"정류장명 매칭 모호: {route_name} / {desired['stop_name']}")

        moved = [
            (desired_stops[index], matched)
            for index, matched in matched_by_desired.items()
            if matched["stop_order"] != desired_stops[index]["stop_order"]
        ]
        unmatched_desired = [index for index in range(len(desired_stops))
                             if index not in matched_by_desired]
        unmatched_current = [item for item in existing_document if item["id"] not in used]
        structure_conflict = bool(moved and (unmatched_desired or unmatched_current))
        if structure_conflict:
            conflicts.append(f"ROUTE_STRUCTURE_CONFLICT: {route_name}")
            details.append(_change_detail(
                route_name, None, "STRUCTURE_CHANGE", "stop_order",
                [item["stop_name"] for item in existing_document],
                [item["stop_name"] for item in desired_stops],
            ))
        else:
            # A remaining row at the same logical position is a selectable change candidate.
            current_by_order = {item["stop_order"]: item for item in unmatched_current}
            for desired_index in list(unmatched_desired):
                desired = desired_stops[desired_index]
                candidate = current_by_order.get(desired["stop_order"])
                if candidate is None or candidate["id"] in used:
                    continue
                matched_by_desired[desired_index] = candidate
                used.add(candidate["id"])

        stop_plans = []
        route_changed = current is not None and not current["active"]
        for desired_index, original_desired in enumerate(desired_stops):
            desired = dict(original_desired)
            matched = matched_by_desired.get(desired_index)
            if matched is None:
                counts["stops_added"] += 1
                route_changed = current is not None
                details.append(_change_detail(
                    route_name, desired["stop_name"], "STOP_ADDED", "stop_name",
                    None, desired["stop_name"],
                ))
            else:
                # Ordinary import changes never replace coordinates; use the existing physical stop.
                desired.update({
                    "latitude": matched["latitude"], "longitude": matched["longitude"],
                    "grid_x": matched["grid_x"], "grid_y": matched["grid_y"],
                    "geocode_status": matched["geocode_status"],
                })
                # Formatting-only variants are comparison-equivalent. Preserve the DB display name.
                if normalize_stop_name_for_matching(matched["stop_name"]) == \
                        normalize_stop_name_for_matching(desired["stop_name"]):
                    desired["stop_name"] = matched["stop_name"]
                changed_fields = [
                    (field, matched[field], desired[field])
                    for field in CHANGE_CANDIDATE_FIELDS
                    if matched[field] != desired[field]
                ]
                if changed_fields:
                    candidate_id = _candidate_id(current["id"], matched["id"])
                    decision = change_decisions.get(candidate_id)
                    if decision not in CHANGE_DECISIONS:
                        decision = None
                        unresolved_candidates.append(candidate_id)
                    candidate = {
                        "candidate_id": candidate_id, "route_name": route_name,
                        "stop_order": desired["stop_order"],
                        "current_stop_name": matched["stop_name"],
                        "proposed_stop_name": desired["stop_name"],
                        "fields": [{
                            "field": field, "current": _preview_value(current_value),
                            "proposed": _preview_value(proposed_value),
                        } for field, current_value, proposed_value in changed_fields],
                        "decision": decision,
                    }
                    change_candidates.append(candidate)
                    if decision in {None, "APPLY"}:
                        counts["stops_updated"] += 1
                        route_changed = True
                    for field, current_value, proposed_value in changed_fields:
                        details.append(_change_detail(
                            route_name, desired["stop_name"], "CHANGE_CANDIDATE", field,
                            current_value, proposed_value,
                        ))
                    if decision != "APPLY":
                        for field, current_value, _proposed_value in changed_fields:
                            desired[field] = current_value
                if matched["stop_order"] != desired["stop_order"]:
                    route_changed = True
                    details.append(_change_detail(
                        route_name, desired["stop_name"], "STOP_MOVED", "stop_order",
                        matched["stop_order"], desired["stop_order"],
                    ))
            stop_plans.append({"desired": desired, "current": matched})

        removed = [item for item in existing_document if item["id"] not in used]
        for item in removed:
            counts["stops_removed"] += 1
            route_changed = True
            details.append(_change_detail(
                route_name, item["stop_name"], "STOP_DEACTIVATED", "active", True, False,
            ))
        if trip_type == "morning" and len(existing_defaults) > 1:
            conflicts.append(f"판교 기본 목적지 중복: {route_name}")
        if trip_type == "evening" and existing_defaults:
            conflicts.append(f"퇴근 노선의 판교 기본 목적지 오류: {route_name}")
        if trip_type == "morning" and not existing_defaults:
            counts["stops_added"] += 1
            route_changed = current is not None
            details.append(_change_detail(
                route_name, DEFAULT_DROPOFF, "STOP_ADDED", "system_default",
                None, True,
            ))
        elif trip_type == "morning" and existing_defaults:
            default = existing_defaults[0]
            if any((
                default["stop_order"] != len(desired_stops) + 1,
                default["scheduled_time"] is not None,
                default["boarding_allowed"],
                not default["alighting_allowed"],
                default["source_kind"] != "system_default",
                default["stop_name"] != DEFAULT_DROPOFF,
                default["latitude"] != DEFAULT_DROPOFF_LATITUDE,
                default["longitude"] != DEFAULT_DROPOFF_LONGITUDE,
            )):
                counts["stops_updated"] += 1
                route_changed = True
                details.append(_change_detail(
                    route_name, DEFAULT_DROPOFF, "STOP_UPDATED", "system_default",
                    "invalid", "canonical",
                ))
        if route_changed:
            counts["routes_updated"] += 1
        route_plans.append({
            "name": route_name, "trip_type": trip_type, "current": current,
            "stops": stop_plans, "removed": removed,
            "default": existing_defaults[0] if existing_defaults else None,
            "changed": route_changed,
        })

    for route_name, current in current_routes.items():
        identity = route_logical_identity(route_name)
        if current["active"] and identity not in desired_routes:
            counts["routes_deactivated"] += 1
            details.append(_change_detail(
                route_name, None, "ROUTE_DEACTIVATED", "active", True, False
            ))

    return {
        "region": state["region"], "snapshot": _state_snapshot(state),
        "counts": counts, "conflicts": conflicts, "details": details,
        "change_candidates": change_candidates,
        "unresolved_candidates": unresolved_candidates, "routes": route_plans,
        "deactivate": [current for name, current in current_routes.items()
                       if current["active"]
                       and route_logical_identity(name) not in desired_routes],
    }


def _public_preview(plan: dict) -> dict:
    return {
        "region": plan["region"], "snapshot": plan["snapshot"],
        **plan["counts"], "conflicts": list(plan["conflicts"]),
        "details": list(plan["details"]),
        "change_candidates": list(plan["change_candidates"]),
        "unresolved_candidates": list(plan["unresolved_candidates"]),
    }


def preview_region_reconcile(
    target_region: str,
    rows: list[dict],
    *,
    identity_map: dict | None = None,
    change_decisions: dict | None = None,
    connection_factory: Callable = database_connection,
) -> dict:
    normalized = validate_route_document_rows(rows, target_region)
    try:
        with connection_factory() as connection:
            with connection.cursor() as cursor:
                state = _load_region_state(cursor, target_region)
        return _public_preview(_build_reconcile_plan(
            normalized, state, identity_map, change_decisions
        ))
    except RouteRepositoryError:
        raise
    except Exception as exc:
        logger.warning("Route preview failed type=%s", type(exc).__name__)
        raise RouteRepositoryError("노선 변경 내용을 확인하지 못했습니다.") from None


def _find_or_create_stop(cursor, region_id, desired, current=None, orphan_ids=None):
    target = (
        desired["stop_name"], desired["latitude"], desired["longitude"],
        desired["grid_x"], desired["grid_y"], desired["geocode_status"],
    )
    if current is not None:
        cursor.execute("SELECT id FROM route_stops WHERE stop_id=%s FOR UPDATE", (current["stop_id"],))
        usages = cursor.fetchall()
        current_values = (
            current["stop_name"], current["latitude"], current["longitude"],
            current["grid_x"], current["grid_y"], current["geocode_status"],
        )
        if len(usages) == 1:
            if current_values != target or not current["stop_active"]:
                cursor.execute(
                    "UPDATE stops SET name=%s, latitude=%s, longitude=%s, grid_x=%s, "
                    "grid_y=%s, geocode_status=%s, active=TRUE WHERE id=%s",
                    (*target, current["stop_id"]),
                )
            return current["stop_id"]
        if current_values == target and current["stop_active"]:
            return current["stop_id"]

    cursor.execute(
        "SELECT id FROM stops WHERE region_id=%s AND name=%s AND latitude=%s "
        "AND longitude=%s ORDER BY active DESC, id FOR UPDATE",
        (region_id, desired["stop_name"], desired["latitude"], desired["longitude"]),
    )
    matches = cursor.fetchall()
    if len(matches) > 1:
        raise RouteConflictError([f"물리 정류장 중복: {desired['stop_name']}"])
    if matches:
        stop_id = matches[0][0]
        cursor.execute(
            "UPDATE stops SET grid_x=%s, grid_y=%s, geocode_status=%s, active=TRUE WHERE id=%s",
            (desired["grid_x"], desired["grid_y"], desired["geocode_status"], stop_id),
        )
    else:
        cursor.execute(
            "INSERT INTO stops (region_id, name, latitude, longitude, grid_x, grid_y, "
            "geocode_status, active) VALUES (%s,%s,%s,%s,%s,%s,%s,TRUE) RETURNING id",
            (region_id, *target),
        )
        stop_id = cursor.fetchone()[0]
    if current is not None and stop_id != current["stop_id"] and orphan_ids is not None:
        orphan_ids.add(current["stop_id"])
    return stop_id


def _default_stop_id(cursor):
    cursor.execute("SELECT id FROM regions WHERE code=%s", (DEFAULT_DROPOFF_REGION,))
    region = cursor.fetchone()
    if region is None:
        raise RouteValidationError("판교 기본 목적지 지역을 찾을 수 없습니다.")
    desired = {
        "stop_name": DEFAULT_DROPOFF, "latitude": DEFAULT_DROPOFF_LATITUDE,
        "longitude": DEFAULT_DROPOFF_LONGITUDE, "grid_x": DEFAULT_DROPOFF_GRID[0],
        "grid_y": DEFAULT_DROPOFF_GRID[1], "geocode_status": "system_default",
    }
    return _find_or_create_stop(cursor, region[0], desired)


def _apply_reconcile_plan(cursor, plan: dict, state: dict) -> dict:
    orphan_ids = set()
    for route in plan["deactivate"]:
        cursor.execute(
            "UPDATE routes SET active=FALSE, inactive_reason='ROUTE_REMOVED' WHERE id=%s",
            (route["id"],),
        )
        cursor.execute(
            "UPDATE favorites SET active=FALSE, inactive_reason='ROUTE_REMOVED' "
            "WHERE route_id=%s AND active=TRUE RETURNING id",
            (route["id"],),
        )
        favorite_ids = [row[0] for row in cursor.fetchall()]
        if favorite_ids:
            cursor.execute(
                "UPDATE favorite_notifications SET enabled=FALSE WHERE favorite_id=ANY(%s)",
                (favorite_ids,),
            )

    for route_plan in plan["routes"]:
        current_route = route_plan["current"]
        if current_route is not None and not route_plan["changed"]:
            continue
        if current_route is None:
            cursor.execute(
                "INSERT INTO routes (region_id, name, trip_type, active) "
                "VALUES (%s,%s,%s,TRUE) RETURNING id",
                (state["region_id"], route_plan["name"], route_plan["trip_type"]),
            )
            route_id = cursor.fetchone()[0]
        else:
            route_id = current_route["id"]
            cursor.execute(
                "UPDATE routes SET active=TRUE, inactive_reason=NULL WHERE id=%s", (route_id,)
            )
            cursor.execute(
                "UPDATE route_stops SET stop_order=stop_order+10000 "
                "WHERE route_id=%s AND active=TRUE",
                (route_id,),
            )

        for removed in route_plan["removed"]:
            cursor.execute(
                "UPDATE route_stops SET active=FALSE, inactive_reason='STOP_REMOVED', "
                "stop_order=%s WHERE id=%s",
                (removed["stop_order"], removed["id"]),
            )
            cursor.execute(
                "UPDATE favorites SET active=FALSE, inactive_reason='STOP_REMOVED' "
                "WHERE active=TRUE AND "
                "(boarding_route_stop_id=%s OR alighting_route_stop_id=%s) RETURNING id",
                (removed["id"], removed["id"]),
            )
            favorite_ids = [row[0] for row in cursor.fetchall()]
            if favorite_ids:
                cursor.execute(
                    "UPDATE favorite_notifications SET enabled=FALSE WHERE favorite_id=ANY(%s)",
                    (favorite_ids,),
                )
            orphan_ids.add(removed["stop_id"])

        for stop_plan in route_plan["stops"]:
            desired = stop_plan["desired"]
            current = stop_plan["current"]
            stop_id = _find_or_create_stop(
                cursor, state["region_id"], desired, current=current, orphan_ids=orphan_ids
            )
            if current is None:
                cursor.execute(
                    "INSERT INTO route_stops (route_id, stop_id, stop_order, scheduled_time, "
                    "boarding_allowed, alighting_allowed, is_default_dropoff, source_kind, active) "
                    "VALUES (%s,%s,%s,%s,%s,%s,FALSE,'document',TRUE)",
                    (route_id, stop_id, desired["stop_order"], desired["scheduled_time"],
                     desired["boarding_allowed"], desired["alighting_allowed"]),
                )
            else:
                cursor.execute(
                    "UPDATE route_stops SET stop_id=%s, stop_order=%s, scheduled_time=%s, "
                    "boarding_allowed=%s, alighting_allowed=%s, is_default_dropoff=FALSE, "
                    "source_kind='document', active=TRUE, inactive_reason=NULL WHERE id=%s",
                    (stop_id, desired["stop_order"], desired["scheduled_time"],
                     desired["boarding_allowed"], desired["alighting_allowed"], current["id"]),
                )

        if route_plan["trip_type"] == "morning":
            default = route_plan["default"]
            default_order = len(route_plan["stops"]) + 1
            if default is None:
                cursor.execute(
                    "INSERT INTO route_stops (route_id, stop_id, stop_order, scheduled_time, "
                    "boarding_allowed, alighting_allowed, is_default_dropoff, source_kind, active) "
                    "VALUES (%s,%s,%s,NULL,FALSE,TRUE,TRUE,'system_default',TRUE)",
                    (route_id, _default_stop_id(cursor), default_order),
                )
            else:
                default_stop_id = _default_stop_id(cursor)
                cursor.execute(
                    "UPDATE route_stops SET stop_id=%s, stop_order=%s, scheduled_time=NULL, "
                    "boarding_allowed=FALSE, alighting_allowed=TRUE, "
                    "is_default_dropoff=TRUE, source_kind='system_default', active=TRUE, "
                    "inactive_reason=NULL WHERE id=%s",
                    (default_stop_id, default_order, default["id"]),
                )
                if default_stop_id != default["stop_id"]:
                    orphan_ids.add(default["stop_id"])

        cursor.execute(
            "SELECT count(*), count(*) FILTER (WHERE is_default_dropoff), "
            "count(*) FILTER (WHERE boarding_allowed AND scheduled_time IS NULL) "
            "FROM route_stops WHERE route_id=%s AND active=TRUE",
            (route_id,),
        )
        total, defaults, missing_times = cursor.fetchone()
        expected_defaults = 1 if route_plan["trip_type"] == "morning" else 0
        if total != len(route_plan["stops"]) + expected_defaults or defaults != expected_defaults or missing_times:
            raise RouteConflictError([f"노선 최종 검증 실패: {route_plan['name']}"])

    for stop_id in orphan_ids:
        cursor.execute(
            "SELECT count(*) FROM route_stops WHERE stop_id=%s AND active=TRUE", (stop_id,)
        )
        if cursor.fetchone()[0] == 0:
            cursor.execute("UPDATE stops SET active=FALSE WHERE id=%s", (stop_id,))
    return dict(plan["counts"])


def reconcile_region_routes(
    target_region: str,
    rows: list[dict],
    *,
    expected_snapshot: str,
    identity_map: dict | None = None,
    change_decisions: dict | None = None,
    transaction_factory: Callable = database_transaction,
) -> dict:
    normalized = validate_route_document_rows(rows, target_region)
    try:
        with transaction_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"shuttleai_routes:{target_region}",),
                )
                state = _load_region_state(cursor, target_region, lock=True)
                if _state_snapshot(state) != expected_snapshot:
                    raise StaleRouteSnapshotError(
                        "노선 데이터가 미리보기 이후 변경되었습니다. 새로고침 후 다시 확인해 주세요."
                    )
                plan = _build_reconcile_plan(
                    normalized, state, identity_map, change_decisions
                )
                if plan["conflicts"]:
                    raise RouteConflictError(plan["conflicts"])
                if plan["unresolved_candidates"]:
                    raise RouteConflictError(["모든 변경 후보에 적용 여부를 선택해 주세요."])
                return _apply_reconcile_plan(cursor, plan, state)
    except RouteRepositoryError:
        raise
    except Exception as exc:
        logger.warning("Route reconcile failed type=%s", type(exc).__name__)
        raise RouteRepositoryError("노선 정보를 저장하지 못했습니다.") from None


def load_admin_route_snapshot(
    *, connection_factory: Callable = database_connection,
) -> dict:
    """Load editable document rows plus hidden DB identities and region snapshots."""
    try:
        rows = []
        identity_map = {}
        snapshots = {}
        with connection_factory() as connection:
            with connection.cursor() as cursor:
                for region in sorted(ALLOWED_REGIONS):
                    state = _load_region_state(cursor, region)
                    snapshots[region] = _state_snapshot(state)
                    for route in state["routes"].values():
                        if not route["active"]:
                            continue
                        for item in route["stops"]:
                            if item["source_kind"] != "document" or not item.get("active", True):
                                continue
                            key = f"route-stop:{item['id']}"
                            rows.append({
                                "route_name": route["name"], "stop_name": item["stop_name"],
                                "arrival_time": _time_text(item["scheduled_time"]), "region": region,
                                "lat": float(item["latitude"]), "lon": float(item["longitude"]),
                                "nx": item["grid_x"], "ny": item["grid_y"],
                                "status": item["geocode_status"], "_identity_key": key,
                                "_stop_order": item["stop_order"],
                            })
                            identity_map[key] = {
                                "route_id": route["id"], "route_stop_id": item["id"],
                                "stop_id": item["stop_id"],
                                "version": hashlib.sha256(repr((
                                    item["id"], item["stop_id"], str(item["updated_at"]),
                                    str(item["stop_updated_at"]),
                                )).encode("utf-8")).hexdigest(),
                            }
        return {"rows": rows, "identity_map": identity_map, "snapshots": snapshots}
    except RouteRepositoryError:
        raise
    except Exception as exc:
        logger.warning("Admin route snapshot failed type=%s", type(exc).__name__)
        raise RouteRepositoryError("관리자 노선 정보를 불러오지 못했습니다.") from None


def reconcile_admin_route_edits(
    rows: list[dict],
    *,
    identity_map: dict,
    expected_snapshots: dict,
    transaction_factory: Callable = database_transaction,
) -> dict:
    by_region = {region: [] for region in ALLOWED_REGIONS}
    for row in rows:
        region = row.get("region")
        if region not in by_region:
            raise RouteValidationError("지원하지 않는 지역이 포함되어 있습니다.")
        by_region[region].append(row)
    normalized = {
        region: validate_route_document_rows(region_rows, region)
        for region, region_rows in by_region.items()
    }
    try:
        with transaction_factory() as connection:
            with connection.cursor() as cursor:
                states = {}
                plans = {}
                for region in sorted(ALLOWED_REGIONS):
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock(hashtext(%s))",
                        (f"shuttleai_routes:{region}",),
                    )
                    states[region] = _load_region_state(cursor, region, lock=True)
                    if _state_snapshot(states[region]) != expected_snapshots.get(region):
                        raise StaleRouteSnapshotError(
                            "노선 데이터가 화면을 연 이후 변경되었습니다. 새로고침 후 다시 시도해 주세요."
                        )
                    draft = _build_reconcile_plan(
                        normalized[region], states[region], identity_map
                    )
                    decisions = {
                        item["candidate_id"]: "APPLY"
                        for item in draft["change_candidates"]
                    }
                    plans[region] = _build_reconcile_plan(
                        normalized[region], states[region], identity_map, decisions
                    )
                conflicts = [item for plan in plans.values() for item in plan["conflicts"]]
                if conflicts:
                    raise RouteConflictError(conflicts)
                results = {
                    region: _apply_reconcile_plan(cursor, plans[region], states[region])
                    for region in sorted(ALLOWED_REGIONS)
                }
                return results
    except RouteRepositoryError:
        raise
    except Exception as exc:
        logger.warning("Admin route reconcile failed type=%s", type(exc).__name__)
        raise RouteRepositoryError("관리자 노선 정보를 저장하지 못했습니다.") from None


def update_route_stop_coordinates(
    route_stop_id: int,
    *,
    latitude,
    longitude,
    grid_x,
    grid_y,
    geocode_status: str,
    expected_version: str,
    transaction_factory: Callable = database_transaction,
) -> dict:
    desired = {
        "latitude": _coordinate(latitude, latitude=True),
        "longitude": _coordinate(longitude, latitude=False),
        "grid_x": _grid(grid_x, "X"), "grid_y": _grid(grid_y, "Y"),
        "geocode_status": geocode_status,
    }
    try:
        with transaction_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT rs.id, rs.stop_id, rs.updated_at, s.name, s.latitude, s.longitude, "
                    "s.grid_x, s.grid_y, s.geocode_status, s.active, s.updated_at, s.region_id, "
                    "rg.code FROM route_stops rs JOIN stops s ON s.id=rs.stop_id "
                    "JOIN regions rg ON rg.id=s.region_id WHERE rs.id=%s FOR UPDATE OF rs, s",
                    (route_stop_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise RouteValidationError("수정할 정류장을 찾을 수 없습니다.")
                version = hashlib.sha256(repr((row[0], row[1], str(row[2]), str(row[10]))).encode("utf-8")).hexdigest()
                if version != expected_version:
                    raise StaleRouteSnapshotError(
                        "정류장 정보가 변경되었습니다. 새로고침 후 다시 시도해 주세요."
                    )
                desired["stop_name"] = row[3]
                current = {
                    "stop_id": row[1], "stop_name": row[3], "latitude": row[4],
                    "longitude": row[5], "grid_x": row[6], "grid_y": row[7],
                    "geocode_status": row[8], "stop_active": row[9],
                }
                stop_id = _find_or_create_stop(cursor, row[11], desired, current=current)
                if stop_id != row[1]:
                    cursor.execute("UPDATE route_stops SET stop_id=%s WHERE id=%s", (stop_id, row[0]))
                    cursor.execute("SELECT count(*) FROM route_stops WHERE stop_id=%s", (row[1],))
                    if cursor.fetchone()[0] == 0:
                        cursor.execute("UPDATE stops SET active=FALSE WHERE id=%s", (row[1],))
                return {"route_stop_id": row[0], "stop_id": stop_id, "shared_split": stop_id != row[1]}
    except RouteRepositoryError:
        raise
    except Exception as exc:
        logger.warning("Route coordinate update failed type=%s", type(exc).__name__)
        raise RouteRepositoryError("정류장 좌표를 저장하지 못했습니다.") from None
