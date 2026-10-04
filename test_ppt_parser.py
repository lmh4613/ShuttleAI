import io
import logging
from unittest.mock import Mock

import pytest
from google.genai.errors import ClientError

import ppt_parser
import weather_api


class UploadedPptx(io.BytesIO):
    name = "routes.pptx"


def _parser_client(text='''{"rows":[{
    "source_section":"integrated_table","is_authoritative":true,"vehicle_id":"","stop_order":1,
    "route_name":"(출근) A","stop_name":"정류장 A","arrival_time":"07:10",
    "region":"gyeonggi","address":"주소"
}]}'''):
    client = Mock()
    client.models.generate_content.return_value = Mock(text=text)
    return client


def test_document_model_uses_verified_default_and_environment_override(monkeypatch):
    monkeypatch.delenv("GEMINI_DOCUMENT_MODEL", raising=False)
    monkeypatch.setattr(ppt_parser.st, "secrets", {})
    assert ppt_parser.get_document_model() == "gemini-3.6-flash"

    monkeypatch.setenv("GEMINI_DOCUMENT_MODEL", "configured-model")
    assert ppt_parser.get_document_model() == "configured-model"


def test_pptx_parser_rewinds_file_and_preserves_result_structure(monkeypatch):
    uploaded = UploadedPptx(b"placeholder")
    uploaded.seek(len(uploaded.getvalue()))
    client = _parser_client()
    observed = {}

    def presentation(file_object):
        observed["position"] = file_object.tell()
        return Mock(slides=[])

    monkeypatch.setattr(ppt_parser, "get_gemini_client", lambda: client)
    monkeypatch.setattr(ppt_parser, "Presentation", presentation)
    monkeypatch.setenv("GEMINI_DOCUMENT_MODEL", "gemini-3.6-flash")

    result = ppt_parser.parse_shuttle_document(uploaded)

    assert observed["position"] == 0
    assert result == [{
        "route_name": "(출근) A", "stop_name": "정류장 A",
        "arrival_time": "07:10", "region": "gyeonggi", "address": "주소",
    }]
    assert client.models.generate_content.call_args.kwargs["model"] == "gemini-3.6-flash"
    prompt = client.models.generate_content.call_args.kwargs["contents"][-1]
    assert "상단 전체 노선표의 표기를 우선" in prompt
    assert "하단 상세/사진 영역은 위치, 주소, 좌표를 판단하는 보조정보" in prompt
    assert "약어를 확장하거나 축약하지 말며" in prompt
    assert "`SKT`를 `SK`로 바꾸거나" in prompt
    assert "`대학병원`을 `대학교병원`으로 바꾸지 말 것" in prompt
    assert "전체 통합 시간표가 노선 데이터의 authoritative source" in prompt
    assert "각 route/stop 조합을 정확히 한 번만 출력" in prompt
    assert "코오롱린든그로브 버스정류장" in prompt
    assert "판교글로벌비즈센터 앞 버스정류장" in prompt
    assert "앱이 system_default로 별도 처리" in prompt
    assert "`(하차만)` 표시와 통합 시간표의 시간을 모두 그대로 유지" in prompt
    assert "이후 하차 정류장에는 출발시간을 복사하지 말고" in prompt
    assert "`source_section`, `is_authoritative`, `vehicle_id`, `stop_order`" in prompt
    assert "같은 값을 `route_name`에도 반드시 포함" in prompt
    assert "호차별 시간 열이 있으면 각 호차를 반드시 별도 row로 출력" in prompt
    assert "여러 호차 열이 있는 노선에서는 `vehicle_id`를 절대 빈 문자열" in prompt
    assert "`3호 미정차`" in prompt


def test_parser_selects_integrated_table_even_when_detail_rows_come_first(monkeypatch):
    client = _parser_client(text='''{"rows":[
      {"source_section":"route_detail","is_authoritative":false,"vehicle_id":"","stop_order":1,"route_name":"(출근) 미사/상일동","stop_name":"정류장 A","arrival_time":"06:25","region":"seoul","address":"상세 사진"},
      {"source_section":"integrated_table","is_authoritative":true,"vehicle_id":"","stop_order":1,"route_name":"(출근) 미사/상일동","stop_name":"정류장 A","arrival_time":"06:20","region":"seoul","address":"통합표"},
      {"source_section":"integrated_table","is_authoritative":true,"vehicle_id":"","stop_order":2,"route_name":"(출근) 미사/상일동","stop_name":"정류장 B","arrival_time":"06:26","region":"seoul","address":"통합표"}
    ]}''')
    monkeypatch.setattr(ppt_parser, "get_gemini_client", lambda: client)
    monkeypatch.setattr(ppt_parser, "Presentation", lambda _file: Mock(slides=[]))

    result = ppt_parser.parse_shuttle_document(UploadedPptx(b"placeholder"))

    assert [(row["stop_name"], row["address"]) for row in result] == [
        ("정류장 A", "통합표"), ("정류장 B", "통합표"),
    ]


def test_parser_prompt_excludes_detail_only_pangyo_destinations_from_geocoding(monkeypatch):
    client = _parser_client(text='''{"rows":[
      {"source_section":"integrated_table","is_authoritative":true,"vehicle_id":"","stop_order":1,"route_name":"(출근) 미사/상일동","stop_name":"미사역","arrival_time":"06:26","region":"seoul","address":"주소"},
      {"source_section":"photo_detail","is_authoritative":false,"vehicle_id":"","stop_order":2,"route_name":"(출근) 미사/상일동","stop_name":"처음 보는 판교 내부 목적지","arrival_time":"","region":"seoul","address":"상세 도착지"},
      {"source_section":"route_detail","is_authoritative":false,"vehicle_id":"1호","stop_order":2,"route_name":"(출근) 서울시청 1호","stop_name":"또 다른 판교 상세 위치","arrival_time":"","region":"seoul","address":"상세 도착지"}
    ]}''')
    geocoded = []
    monkeypatch.setattr(ppt_parser, "get_gemini_client", lambda: client)
    monkeypatch.setattr(ppt_parser, "Presentation", lambda _file: Mock(slides=[]))
    monkeypatch.setattr(
        weather_api, "get_coordinates_by_gemini",
        lambda name: geocoded.append(name) or (37.1, 127.1),
    )

    result = ppt_parser.parse_shuttle_document(UploadedPptx(b"placeholder"))
    prepared = weather_api.prepare_routes_with_sequential_geocoding(
        result, target_region="seoul",
    )
    prompt = client.models.generate_content.call_args.kwargs["contents"][-1]

    assert [row["stop_name"] for row in result] == ["미사역"]
    assert [row["stop_name"] for row in prepared] == ["미사역"]
    assert geocoded == ["미사역"]
    assert "route stop으로 출력하지 말 것" in prompt
    assert "개별 노선 반복표, 지도/사진, 상세 도착지" in prompt


def test_duplicate_authoritative_rows_are_rejected_instead_of_first_wins(monkeypatch):
    client = _parser_client(text='''{"rows":[
      {"source_section":"integrated_table","is_authoritative":true,"vehicle_id":"","stop_order":1,"route_name":"(출근) 경기 A","stop_name":"정류장 A","arrival_time":"06:20","region":"gyeonggi"},
      {"source_section":"integrated_table","is_authoritative":true,"vehicle_id":"","stop_order":1,"route_name":"(출근) 경기 A","stop_name":"정류장 B","arrival_time":"06:25","region":"gyeonggi"}
    ]}''')
    monkeypatch.setattr(ppt_parser, "get_gemini_client", lambda: client)
    monkeypatch.setattr(ppt_parser, "Presentation", lambda _file: Mock(slides=[]))

    with pytest.raises(ValueError, match="중복된 노선 정류장"):
        ppt_parser.parse_shuttle_document(UploadedPptx(b"placeholder"))


def test_vehicle_identifier_must_be_present_in_route_name(monkeypatch):
    client = _parser_client(text='''{"rows":[
      {"source_section":"integrated_table","is_authoritative":true,"vehicle_id":"1호","stop_order":1,"route_name":"(출근) 서울시청","stop_name":"정류장 A","arrival_time":"06:45","region":"seoul"}
    ]}''')
    monkeypatch.setattr(ppt_parser, "get_gemini_client", lambda: client)
    monkeypatch.setattr(ppt_parser, "Presentation", lambda _file: Mock(slides=[]))

    with pytest.raises(ValueError, match="호차 정보가 노선명"):
        ppt_parser.parse_shuttle_document(UploadedPptx(b"placeholder"))


def test_vehicle_routes_are_kept_separate_and_metadata_is_not_exposed(monkeypatch):
    client = _parser_client(text='''{"rows":[
      {"source_section":"integrated_table","is_authoritative":true,"vehicle_id":"1호","stop_order":1,"route_name":"(출근) 서울시청 1호","stop_name":"정류장 A","arrival_time":"06:45","region":"seoul"},
      {"source_section":"integrated_table","is_authoritative":true,"vehicle_id":"2호","stop_order":1,"route_name":"(출근) 서울시청 2호","stop_name":"정류장 A","arrival_time":"06:55","region":"seoul"}
    ]}''')
    monkeypatch.setattr(ppt_parser, "get_gemini_client", lambda: client)
    monkeypatch.setattr(ppt_parser, "Presentation", lambda _file: Mock(slides=[]))

    result = ppt_parser.parse_shuttle_document(UploadedPptx(b"placeholder"))

    assert [row["route_name"] for row in result] == [
        "(출근) 서울시청 1호", "(출근) 서울시청 2호",
    ]
    assert all(not (ppt_parser.PARSER_METADATA_FIELDS & row.keys()) for row in result)


def test_pdf_parser_rewinds_file_before_conversion(monkeypatch):
    uploaded = UploadedPptx(b"%PDF-placeholder")
    uploaded.name = "routes.pdf"
    uploaded.seek(len(uploaded.getvalue()))
    client = _parser_client()
    observed = {}
    image = Mock()

    def convert(data):
        observed["data"] = data
        return [image]

    monkeypatch.setattr(ppt_parser, "get_gemini_client", lambda: client)
    monkeypatch.setattr(
        ppt_parser,
        "build_pdf_canonical_index",
        lambda _data: {
            ("morning", "A", "", 1): [{
                "stop_name": "정류장 A",
                "scheduled_time": "07:10",
                "boarding_allowed": True,
                "alighting_allowed": False,
            }]
        },
    )
    monkeypatch.setattr(ppt_parser, "convert_from_bytes", convert)
    monkeypatch.setattr(ppt_parser.types.Part, "from_bytes", lambda **_kwargs: "image-part")

    ppt_parser.parse_shuttle_document(uploaded)

    assert observed["data"] == b"%PDF-placeholder"
    image.save.assert_called_once()


def test_pdf_canonical_error_logs_stage_message_and_traceback(monkeypatch, caplog):
    uploaded = UploadedPptx(b"%PDF-placeholder")
    uploaded.name = "routes.pdf"
    monkeypatch.setattr(ppt_parser, "get_gemini_client", lambda: _parser_client())
    monkeypatch.setattr(
        ppt_parser,
        "build_pdf_canonical_index",
        lambda _data: (_ for _ in ()).throw(
            ppt_parser.PdfCanonicalError(
                "PDF morning 통합 시간표 오류 context=page=3 row=4"
            )
        ),
    )

    with caplog.at_level(logging.ERROR, logger="ppt_parser"):
        with pytest.raises(ppt_parser.PdfCanonicalError):
            ppt_parser.parse_shuttle_document(uploaded)

    assert "stage=pdf_canonical_index" in caplog.text
    assert "type=PdfCanonicalError" in caplog.text
    assert "context=page=3 row=4" in caplog.text
    assert any(record.exc_info is not None for record in caplog.records)


def test_client_error_logs_safe_fields_without_secret(monkeypatch, caplog):
    client = _parser_client()
    error = ClientError(429, {
        "error": {
            "code": 429,
            "status": "RESOURCE_EXHAUSTED",
            "message": "quota exhausted api_key=top-secret-value",
        }
    })
    client.models.generate_content.side_effect = error
    monkeypatch.setattr(ppt_parser, "get_gemini_client", lambda: client)
    monkeypatch.setattr(ppt_parser, "Presentation", lambda _file: Mock(slides=[]))

    with caplog.at_level(logging.WARNING, logger="ppt_parser"):
        with pytest.raises(ClientError):
            ppt_parser.parse_shuttle_document(UploadedPptx(b"placeholder"))

    assert "code=429" in caplog.text
    assert "status=RESOURCE_EXHAUSTED" in caplog.text
    assert "[REDACTED]" in caplog.text
    assert "top-secret-value" not in caplog.text
