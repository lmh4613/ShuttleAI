import pytest
from pathlib import Path

from pdf_route_canonical import (
    PdfCanonicalError,
    build_pdf_canonical_index,
    build_canonical_index_from_pages,
    canonicalize_pdf_rows,
)


class FakePage:
    def __init__(self, heading, table):
        self.heading = heading
        self.table = table

    def extract_text(self):
        return self.heading

    def extract_tables(self):
        return [self.table]


MORNING_HEADER = [["노선", "출발시간", None, "정류장(승차)"]]
EVENING_HEADER = [["노선", "출발시간", "정류장(승차)", "정류장(하차)"]]


def _pages():
    morning = MORNING_HEADER + [
        ["서울시청", "(1호) 06:45", "(2호) 06:55", "1. 서울역"],
        [None, "(1호) 06:54", "(2호) 미정차", "2. 순천향대학병원 앞"],
        [None, "(1호) 07:10", "(2호) 07:20", "3. 화랑공원 (하차만)"],
    ]
    detail = MORNING_HEADER + [["서울시청", "(1호) 09:99", None, "1. 상세 페이지 정류장"]]
    evening = EVENING_HEADER + [[
        "서울시청",
        "(1호) 17:30\n(2호) 17:35",
        "경기기업성장센터 앞",
        "1. 정류장 A\n2. 정류장 B\n3. SKT타워 앞",
    ]]
    return [
        FakePage("(출근) 서울 → 판교IT캠퍼스 시간표", morning),
        FakePage("(출근) 서울시청 → 판교IT캠퍼스", detail),
        FakePage("(퇴근) 판교IT캠퍼스 → 서울 시간표", evening),
        FakePage("(퇴근) 판교IT캠퍼스 → 서울시청", EVENING_HEADER + [[
            "서울시청", "19:00", "상세 승차지", "1. 상세 하차지",
        ]]),
    ]


def _row(route_name, vehicle_id, stop_order, stop_name="Gemini 이름", arrival_time="00:00"):
    return {
        "source_section": "integrated_table",
        "is_authoritative": True,
        "vehicle_id": vehicle_id,
        "stop_order": stop_order,
        "route_name": route_name,
        "stop_name": stop_name,
        "arrival_time": arrival_time,
        "region": "seoul",
    }


def test_morning_index_preserves_names_times_roles_and_vehicle_specific_non_stop():
    index = build_canonical_index_from_pages(_pages())

    assert index[("morning", "서울시청", "1호", 2)] == [{
        "stop_name": "순천향대학병원 앞",
        "scheduled_time": "06:54",
        "boarding_allowed": True,
        "alighting_allowed": False,
    }]
    assert ("morning", "서울시청", "2호", 2) not in index
    assert index[("morning", "서울시청", "2호", 3)][0]["scheduled_time"] == "07:20"
    assert index[("morning", "서울시청", "1호", 3)][0]["boarding_allowed"] is False
    assert index[("morning", "서울시청", "1호", 3)][0]["alighting_allowed"] is True


def test_multi_vehicle_morning_columns_create_separate_keys_and_skip_non_stop():
    morning = [["노선", "출발시간", None, None, None, "정류장(승차)"], [
        "수원역/수원시청", "(1호) 06:20", "(2호) 06:30",
        "(3호) 미정차", "(4호) 06:40", "1. 수원역",
    ]]
    pages = [
        FakePage("(출근) 경기 → 판교IT캠퍼스 시간표", morning),
        _pages()[2],
    ]

    index = build_canonical_index_from_pages(pages)

    assert ("morning", "수원역/수원시청", "1호", 1) in index
    assert ("morning", "수원역/수원시청", "2호", 1) in index
    assert ("morning", "수원역/수원시청", "4호", 1) in index
    assert ("morning", "수원역/수원시청", "3호", 1) not in index


def test_multi_vehicle_morning_route_requires_explicit_vehicle_id():
    index = {
        ("morning", "수원역/수원시청", vehicle, 1): [{
            "stop_name": "수원역", "scheduled_time": scheduled,
            "boarding_allowed": True, "alighting_allowed": False,
        }]
        for vehicle, scheduled in (("1호", "06:20"), ("2호", "06:30"))
    }
    missing_vehicle = _row("(출근) 수원역/수원시청", "", 1)

    with pytest.raises(PdfCanonicalError) as caught:
        canonicalize_pdf_rows([missing_vehicle], index)

    message = str(caught.value)
    assert "Missing vehicle_id for multi-vehicle morning route" in message
    assert "route=수원역/수원시청" in message
    assert "stop_order=1" in message


def test_multi_vehicle_morning_route_matches_explicit_vehicle_key():
    key = ("morning", "수원역/수원시청", "1호", 1)
    index = {key: [{
        "stop_name": "수원역", "scheduled_time": "06:20",
        "boarding_allowed": True, "alighting_allowed": False,
    }]}
    row = _row("(출근) 수원역/수원시청 1호", "1호", 1)

    assert canonicalize_pdf_rows([row], index)[0]["vehicle_id"] == "1호"


def test_single_morning_route_matches_despite_slash_spacing():
    key = ("morning", "수원역/수원시청", "", 1)
    index = {key: [{
        "stop_name": "수원역", "scheduled_time": "06:25",
        "boarding_allowed": True, "alighting_allowed": False,
    }]}
    row = _row("(출근) 수원역/ 수원시청", "", 1)

    result = canonicalize_pdf_rows([row], index)

    assert result[0]["stop_name"] == "수원역"


def test_canonical_index_normalizes_slash_spacing_only():
    pages = _pages()
    pages[0].table[1][0] = "목동 / 신도림"

    index = build_canonical_index_from_pages(pages)

    assert ("morning", "목동/신도림", "1호", 1) in index
    assert all(key[1] != "목동 / 신도림" for key in index)


def test_local_gyeonggi_pdf_all_canonical_keys_match_mock_authoritative_rows():
    pdf_path = Path("upload/3. (운행정보) 경기.pdf")
    if not pdf_path.exists():
        pytest.skip("local Gyeonggi PDF fixture is unavailable")
    index = build_pdf_canonical_index(pdf_path.read_bytes())
    rows = []
    for (trip_type, base_route, vehicle_id, stop_order) in index:
        trip_label = "출근" if trip_type == "morning" else "퇴근"
        route_name = f"({trip_label}) {base_route}"
        if vehicle_id:
            route_name += f" {vehicle_id}"
        rows.append(_row(route_name, vehicle_id, stop_order))

    result = canonicalize_pdf_rows(rows, index)

    assert len(index) == 157
    assert len(result) == 157


def test_evening_order_shifts_alighting_numbers_and_only_boarding_has_time():
    index = build_canonical_index_from_pages(_pages())

    first = index[("evening", "서울시청", "1호", 1)][0]
    assert first["stop_name"] == "경기기업성장센터 앞"
    assert first["scheduled_time"] == "17:30"
    assert first["boarding_allowed"] is True
    assert index[("evening", "서울시청", "1호", 2)][0]["stop_name"] == "정류장 A"
    assert index[("evening", "서울시청", "1호", 4)][0] == {
        "stop_name": "SKT타워 앞",
        "scheduled_time": None,
        "boarding_allowed": False,
        "alighting_allowed": True,
    }


def test_detail_pages_are_not_part_of_canonical_index():
    index = build_canonical_index_from_pages(_pages())

    assert all(
        "상세" not in value["stop_name"]
        for values in index.values()
        for value in values
    )


@pytest.mark.parametrize(
    ("gemini_name", "order", "expected"),
    [
        ("순천향대학교병원 앞", 2, "순천향대학병원 앞"),
        ("SK타워 앞", 4, "SKT타워 앞"),
        ("문자열이 전혀 다른 이름", 1, "서울역"),
    ],
)
def test_structural_key_uses_pdf_name_without_fuzzy_matching(gemini_name, order, expected):
    full_index = build_canonical_index_from_pages(_pages())
    if order == 4:
        row = _row("(퇴근) 서울시청 1호", "1호", order, gemini_name)
        key = ("evening", "서울시청", "1호", order)
    else:
        row = _row("(출근) 서울시청 1호", "1호", order, gemini_name)
        key = ("morning", "서울시청", "1호", order)

    result = canonicalize_pdf_rows([row], {key: full_index[key]})

    assert result[0]["stop_name"] == expected


def test_canonical_matching_preserves_parser_metadata_and_overrides_time():
    full_index = build_canonical_index_from_pages(_pages())
    key = ("morning", "서울시청", "1호", 2)
    row = _row("(출근) 서울시청 1호", "1호", 2, arrival_time="09:99")

    result = canonicalize_pdf_rows([row], {key: full_index[key]})[0]

    assert result["source_section"] == "integrated_table"
    assert result["is_authoritative"] is True
    assert result["vehicle_id"] == "1호"
    assert result["stop_order"] == 2
    assert result["arrival_time"] == "06:54"


def test_missing_or_ambiguous_structural_key_is_blocked():
    index = build_canonical_index_from_pages(_pages())
    missing = _row("(출근) 서울시청 1호", "1호", 99)
    with pytest.raises(PdfCanonicalError, match="canonical_candidates=0"):
        canonicalize_pdf_rows([missing], index)

    key = ("morning", "서울시청", "1호", 1)
    index = {key: index[key] + [dict(index[key][0])]}
    with pytest.raises(PdfCanonicalError, match="canonical_candidates=2"):
        canonicalize_pdf_rows([_row("(출근) 서울시청 1호", "1호", 1)], index)


def test_structure_mismatch_reports_missing_canonical_key_context():
    index = build_canonical_index_from_pages(_pages())
    one_key = next(iter(index))
    only_row = _row(
        f"({'출근' if one_key[0] == 'morning' else '퇴근'}) {one_key[1]}"
        f"{' ' + one_key[2] if one_key[2] else ''}",
        one_key[2], one_key[3],
    )

    with pytest.raises(PdfCanonicalError) as caught:
        canonicalize_pdf_rows([only_row], index)

    message = str(caught.value)
    assert "missing_key_count=" in message
    assert "missing_key_examples=" in message


def test_missing_integrated_section_or_wrong_header_is_blocked():
    with pytest.raises(PdfCanonicalError, match="evening 통합 시간표"):
        build_canonical_index_from_pages(_pages()[:2])

    pages = _pages()
    pages[0] = FakePage(
        "(출근) 서울 → 판교IT캠퍼스 시간표",
        [["노선", "시간", "정류장"]],
    )
    with pytest.raises(PdfCanonicalError, match="표 구조"):
        build_canonical_index_from_pages(pages)
