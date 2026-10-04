import io
import re

import pdfplumber

from route_repository import normalize_base_route_name_for_identity


class PdfCanonicalError(ValueError):
    pass


TRIP_LABELS = {"출근": "morning", "퇴근": "evening"}
VEHICLE_TIME_PATTERN = re.compile(r"\((\d+호)\)\s*(미정차|\d{2}:\d{2})")
TIME_PATTERN = re.compile(r"\b\d{2}:\d{2}\b")
NUMBERED_STOP_PATTERN = re.compile(r"^(\d+)\.\s*(.+)$")
HEADING_PATTERN = re.compile(r"^\((출근|퇴근)\).+시간표$")


def _text(value):
    return " ".join(str(value or "").split())


def _heading(page):
    for line in (page.extract_text() or "").splitlines()[:8]:
        normalized = _text(line)
        match = HEADING_PATTERN.match(normalized)
        if match:
            return TRIP_LABELS[match.group(1)], normalized
    return None


def _integrated_pages(pages):
    headings = [_heading(page) for page in pages]
    result = {}
    for trip_type in ("morning", "evening"):
        start = next((index for index, item in enumerate(headings)
                      if item and item[0] == trip_type), None)
        if start is None:
            raise PdfCanonicalError(f"PDF에서 {trip_type} 통합 시간표를 찾지 못했습니다.")
        title = headings[start][1]
        selected = []
        index = start
        while index < len(pages) and headings[index] == (trip_type, title):
            selected.append(pages[index])
            index += 1
        if not selected:
            raise PdfCanonicalError(f"PDF의 {trip_type} 통합 시간표 범위가 올바르지 않습니다.")
        result[trip_type] = selected
    return result


def _table(page, trip_type, page_number=None):
    candidates = []
    for table in page.extract_tables() or []:
        if not table:
            continue
        header = [_text(cell) for cell in table[0]]
        joined = "|".join(header)
        required = ("노선", "출발시간", "정류장(승차)")
        if not all(value in joined for value in required):
            continue
        if trip_type == "evening" and "정류장(하차)" not in joined:
            continue
        if trip_type == "morning" and "정류장(하차)" in joined:
            continue
        candidates.append(table)
    if len(candidates) != 1:
        raise PdfCanonicalError(
            f"PDF {trip_type} 통합 시간표의 표 구조가 올바르지 않습니다. "
            f"context=page={page_number or 'unknown'} candidate_tables={len(candidates)}"
        )
    return candidates[0]


def _column(header, label):
    matches = [index for index, cell in enumerate(header) if label in _text(cell)]
    if len(matches) != 1:
        raise PdfCanonicalError(f"PDF 통합 시간표의 {label} 열을 식별하지 못했습니다.")
    return matches[0]


def _numbered_stop(value):
    match = NUMBERED_STOP_PATTERN.match(_text(value))
    if not match:
        raise PdfCanonicalError("PDF 통합 시간표의 정류장 순서를 식별하지 못했습니다.")
    return int(match.group(1)), match.group(2)


def _numbered_stops(value):
    stops = []
    current_order = None
    current_parts = []
    for raw_line in str(value or "").splitlines():
        line = _text(raw_line)
        if not line:
            continue
        match = NUMBERED_STOP_PATTERN.match(line)
        if match:
            if current_order is not None:
                stops.append((current_order, _text(" ".join(current_parts))))
            current_order = int(match.group(1))
            current_parts = [match.group(2)]
        elif current_order is not None:
            current_parts.append(line)
    if current_order is not None:
        stops.append((current_order, _text(" ".join(current_parts))))
    if not stops or [order for order, _ in stops] != list(range(1, len(stops) + 1)):
        raise PdfCanonicalError("PDF 퇴근 하차 정류장 순서가 올바르지 않습니다.")
    return stops


def _times(values):
    joined = "\n".join(str(value or "") for value in values)
    vehicle_times = VEHICLE_TIME_PATTERN.findall(joined)
    if vehicle_times:
        return [(vehicle, value) for vehicle, value in vehicle_times if value != "미정차"]
    match = TIME_PATTERN.search(joined)
    if match:
        return [("", match.group(0))]
    raise PdfCanonicalError("PDF 통합 시간표의 시간을 식별하지 못했습니다.")


def _add(index, key, value):
    index.setdefault(key, []).append(value)


def _morning_rows(table, index, page_number=None):
    header = table[0]
    route_col = _column(header, "노선")
    stop_col = _column(header, "정류장(승차)")
    current_route = ""
    for row_number, row in enumerate(table[1:], start=2):
        if not row or not any(_text(cell) for cell in row):
            continue
        try:
            route = _text(row[route_col])
            if route:
                current_route = route
            if not current_route:
                raise PdfCanonicalError("PDF 통합 시간표의 노선명을 식별하지 못했습니다.")
            stop_order, stop_name = _numbered_stop(row[stop_col])
            dropoff_only = "(하차만)" in stop_name
            for vehicle_id, scheduled_time in _times(row[route_col + 1:stop_col]):
                key = (
                    "morning",
                    normalize_base_route_name_for_identity(current_route),
                    vehicle_id,
                    stop_order,
                )
                _add(index, key, {
                    "stop_name": stop_name,
                    "scheduled_time": scheduled_time,
                    "boarding_allowed": not dropoff_only,
                    "alighting_allowed": dropoff_only,
                })
        except PdfCanonicalError as exc:
            raise PdfCanonicalError(
                f"{exc} context=page={page_number or 'unknown'} row={row_number} "
                f"trip_type=morning route={current_route or '-'}"
            ) from exc


def _evening_rows(table, index, page_number=None):
    header = table[0]
    route_col = _column(header, "노선")
    departure_col = _column(header, "출발시간")
    boarding_col = _column(header, "정류장(승차)")
    alighting_col = _column(header, "정류장(하차)")
    current_route = ""
    for row_number, row in enumerate(table[1:], start=2):
        if not row or not any(_text(cell) for cell in row):
            continue
        try:
            route = _text(row[route_col])
            if route:
                current_route = route
            if not current_route:
                raise PdfCanonicalError("PDF 통합 시간표의 노선명을 식별하지 못했습니다.")
            boarding_stop = _text(row[boarding_col])
            if not boarding_stop:
                raise PdfCanonicalError("PDF 퇴근 통합 시간표의 승차 정류장이 비어 있습니다.")
            alighting_stops = _numbered_stops(row[alighting_col])
            for vehicle_id, scheduled_time in _times([row[departure_col]]):
                route_identity = normalize_base_route_name_for_identity(current_route)
                _add(index, ("evening", route_identity, vehicle_id, 1), {
                    "stop_name": boarding_stop,
                    "scheduled_time": scheduled_time,
                    "boarding_allowed": True,
                    "alighting_allowed": True,
                })
                for pdf_order, stop_name in alighting_stops:
                    _add(index, ("evening", route_identity, vehicle_id, pdf_order + 1), {
                        "stop_name": stop_name,
                        "scheduled_time": None,
                        "boarding_allowed": False,
                        "alighting_allowed": True,
                    })
        except PdfCanonicalError as exc:
            raise PdfCanonicalError(
                f"{exc} context=page={page_number or 'unknown'} row={row_number} "
                f"trip_type=evening route={current_route or '-'}"
            ) from exc


def build_canonical_index_from_pages(pages):
    integrated = _integrated_pages(pages)
    index = {}
    page_numbers = {id(page): number for number, page in enumerate(pages, start=1)}
    for trip_type, selected_pages in integrated.items():
        for page in selected_pages:
            page_number = page_numbers.get(id(page))
            table = _table(page, trip_type, page_number)
            if trip_type == "morning":
                _morning_rows(table, index, page_number)
            else:
                _evening_rows(table, index, page_number)
    return index


def build_pdf_canonical_index(pdf_bytes):
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as document:
            return build_canonical_index_from_pages(document.pages)
    except PdfCanonicalError:
        raise
    except Exception as exc:
        raise PdfCanonicalError("PDF 통합 시간표를 읽지 못했습니다.") from exc


def _trip_and_base_route(route_name, vehicle_id):
    match = re.match(r"^\s*\((출근|퇴근)\)\s*(.+?)\s*$", str(route_name or ""))
    if not match:
        raise PdfCanonicalError(
            f"Gemini 노선명에서 출퇴근 구분을 확인하지 못했습니다. "
            f"context=route={str(route_name or '')[:120]} vehicle_id={vehicle_id or '-'}"
        )
    trip_type = TRIP_LABELS[match.group(1)]
    base_route = _text(match.group(2))
    if vehicle_id:
        if not base_route.endswith(vehicle_id):
            raise PdfCanonicalError(
                f"Gemini 호차 정보가 노선명에 반영되지 않았습니다. "
                f"context=route={base_route[:120]} vehicle_id={vehicle_id}"
            )
        base_route = base_route[:-len(vehicle_id)].strip()
    return trip_type, normalize_base_route_name_for_identity(base_route)


def _validate_morning_vehicle_ids(rows, canonical_index):
    """Reject ambiguous missing vehicle ids before structural key matching."""
    route_vehicles = {}
    for trip_type, base_route, vehicle_id, _stop_order in canonical_index:
        if trip_type == "morning" and vehicle_id:
            route_vehicles.setdefault((trip_type, base_route), set()).add(vehicle_id)
    for row in rows:
        vehicle_id = _text(row.get("vehicle_id"))
        if vehicle_id:
            continue
        try:
            stop_order = int(row.get("stop_order"))
            trip_type, base_route = _trip_and_base_route(row.get("route_name"), vehicle_id)
        except (PdfCanonicalError, TypeError, ValueError):
            continue
        vehicles = sorted(route_vehicles.get((trip_type, base_route), set()))
        if trip_type == "morning" and len(vehicles) > 1:
            raise PdfCanonicalError(
                "Missing vehicle_id for multi-vehicle morning route. "
                f"context=trip_type={trip_type} route={base_route} "
                f"stop_order={stop_order} canonical_vehicle_ids={vehicles!r}"
            )


def canonicalize_pdf_rows(rows, canonical_index):
    _validate_morning_vehicle_ids(rows, canonical_index)
    canonicalized = []
    matched_keys = set()
    for row in rows:
        vehicle_id = _text(row.get("vehicle_id"))
        try:
            stop_order = int(row.get("stop_order"))
        except (TypeError, ValueError):
            raise PdfCanonicalError(
                "Gemini 정류장 순서가 올바르지 않습니다. "
                f"context=route={str(row.get('route_name') or '')[:120]} "
                f"vehicle_id={vehicle_id or '-'} stop_order={row.get('stop_order')!r}"
            ) from None
        if stop_order < 1:
            raise PdfCanonicalError(
                "Gemini 정류장 순서가 올바르지 않습니다. "
                f"context=route={str(row.get('route_name') or '')[:120]} "
                f"vehicle_id={vehicle_id or '-'} stop_order={stop_order}"
            )
        trip_type, base_route = _trip_and_base_route(row.get("route_name"), vehicle_id)
        key = (trip_type, base_route, vehicle_id, stop_order)
        if key in matched_keys:
            raise PdfCanonicalError(
                f"Gemini 노선 구조에 중복된 정류장 순서가 있습니다. context=key={key!r}"
            )
        candidates = canonical_index.get(key, [])
        if len(candidates) != 1:
            raise PdfCanonicalError(
                "PDF 통합 시간표와 Gemini 노선 구조를 유일하게 연결하지 못했습니다. "
                f"context=key={key!r} canonical_candidates={len(candidates)}"
            )
        canonical = candidates[0]
        result = dict(row)
        result["stop_name"] = canonical["stop_name"]
        result["arrival_time"] = canonical["scheduled_time"] or ""
        result["_canonical_boarding_allowed"] = canonical["boarding_allowed"]
        result["_canonical_alighting_allowed"] = canonical["alighting_allowed"]
        canonicalized.append(result)
        matched_keys.add(key)
    if matched_keys != set(canonical_index):
        missing_keys = sorted(set(canonical_index) - matched_keys)
        raise PdfCanonicalError(
            "PDF 통합 시간표와 Gemini 노선 구조가 일치하지 않습니다. "
            f"context=missing_key_count={len(missing_keys)} "
            f"missing_key_examples={missing_keys[:3]!r}"
        )
    return canonicalized
