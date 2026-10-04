import os
import json
import io
import re
import logging
import streamlit as st
from dotenv import load_dotenv
from google import genai
from google.genai.errors import ClientError
from google.genai import types
from pptx import Presentation
from pdf2image import convert_from_bytes
from pdf_route_canonical import (
    PdfCanonicalError,
    build_pdf_canonical_index,
    canonicalize_pdf_rows,
)

load_dotenv()

logger = logging.getLogger(__name__)
DEFAULT_DOCUMENT_MODEL = "gemini-3.6-flash"
PARSER_METADATA_FIELDS = {
    "source_section", "is_authoritative", "vehicle_id", "stop_order",
    "_canonical_boarding_allowed", "_canonical_alighting_allowed",
}


def get_document_model():
    model = os.getenv("GEMINI_DOCUMENT_MODEL", "").strip()
    if not model:
        try:
            model = str(st.secrets.get("GEMINI_DOCUMENT_MODEL", "")).strip()
        except Exception:
            model = ""
    return model or DEFAULT_DOCUMENT_MODEL


def _safe_client_error_message(error):
    message = " ".join(str(getattr(error, "message", "Gemini request failed.")).split())
    message = re.sub(
        r"(?i)(api[_-]?key|key|token|secret|password)(\s*[=:]\s*)[^\s&,]+",
        r"\1\2[REDACTED]",
        message,
    )
    return message[:500]


def extract_authoritative_route_rows(payload, *, strip_metadata=True):
    """Select validated integrated-table rows and optionally strip parser metadata."""
    if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
        raise ValueError("Gemini 노선 분석 결과의 문서 구조가 올바르지 않습니다.")
    authoritative = []
    seen = set()
    for row in payload["rows"]:
        if not isinstance(row, dict):
            raise ValueError("Gemini 노선 분석 결과에 잘못된 행이 포함되어 있습니다.")
        if row.get("source_section") != "integrated_table" or row.get("is_authoritative") is not True:
            continue
        route_name = str(row.get("route_name", "")).strip()
        stop_name = str(row.get("stop_name", "")).strip()
        vehicle_id = str(row.get("vehicle_id", "") or "").strip()
        try:
            stop_order = int(row.get("stop_order"))
        except (TypeError, ValueError):
            raise ValueError("통합 시간표 정류장 순서가 올바르지 않습니다.") from None
        if stop_order < 1:
            raise ValueError("통합 시간표 정류장 순서가 올바르지 않습니다.")
        if vehicle_id and vehicle_id not in route_name:
            raise ValueError("호차 정보가 노선명에 반영되지 않았습니다.")
        key = (route_name, vehicle_id, stop_order)
        if key in seen:
            raise ValueError("통합 시간표에 중복된 노선 정류장이 포함되어 있습니다.")
        seen.add(key)
        validated = dict(row)
        validated["stop_order"] = stop_order
        if strip_metadata:
            validated = {
                field: value for field, value in validated.items()
                if field not in PARSER_METADATA_FIELDS
            }
        authoritative.append(validated)
    if not authoritative:
        raise ValueError("통합 시간표에서 노선 정보를 찾지 못했습니다.")
    return authoritative


def _strip_parser_metadata(rows):
    return [
        {
            field: value for field, value in row.items()
            if field not in PARSER_METADATA_FIELDS
        }
        for row in rows
    ]

def get_gemini_client():
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        try:
            api_key = st.secrets.get("GEMINI_API_KEY")
        except Exception:
            api_key = None
            
    if not api_key:
        raise ValueError(".env 파일이나 Streamlit Secrets에 GEMINI_API_KEY가 설정되어 있지 않습니다.")
    return genai.Client(api_key=api_key)

def parse_shuttle_document(uploaded_file):
    try:
        uploaded_file.seek(0)
    except (AttributeError, OSError, ValueError):
        pass

    client = get_gemini_client()
    file_name = uploaded_file.name.lower()
    file_type = os.path.splitext(file_name)[1].lstrip(".") or "unknown"
    logger.info("Route document parsing started type=%s", file_type)
    
    prompt = """
    너는 셔틀버스 노선도 분석 전문가야. 제공된 파일/이미지 내용을 분석해서 노선 및 정류장 정보를 정확하게 추출해 줘.
    
    [문서 구조와 출처 우선순위]
    1. 문서 앞부분의 출근/퇴근 전체 통합 시간표가 노선 데이터의 authoritative source다.
       `route_name`, 차량/호차, `stop_name`, 정류장 순서, 시간, 승차 여부와 명시적인 `(하차만)` 여부는 전체 통합 시간표에서 추출할 것.
    2. 통합 시간표 뒤의 `(출근) 노선명 → 판교IT캠퍼스` 또는 `(퇴근) 판교IT캠퍼스 → 노선명` 개별 시간표는 앞의 같은 노선을 반복한 확인 자료다.
       반복된 정류장을 새로운 노선이나 새로운 정류장으로 다시 출력하지 말고, 각 route/stop 조합을 정확히 한 번만 출력할 것.
    3. 지도/사진/위치 안내 페이지는 통합 시간표 정류장의 주소와 위치를 확인하는 보조자료로만 사용할 것.
       상세 페이지만 보고 새로운 승차 또는 하차 정류장을 만들지 말 것.
    4. 상세 페이지의 `도착지`, `최종 도착지`로 표시된 판교 내부 하차 위치는 route stop으로 출력하지 말 것.
       예를 들어 `코오롱린든그로브 버스정류장`, `판교글로벌비즈센터 앞 버스정류장`, `판교제2테크노밸리 버스정류장~전방 횡단보도까지`는 출력하지 말 것.
       최종 목적지 `판교 제2테크노밸리`는 앱이 system_default로 별도 처리하므로 문서 분석 결과에 추가하지 말 것.
    5. 응답의 각 행에 내부 검증용 `source_section`, `is_authoritative`, `vehicle_id`, `stop_order`를 포함할 것.
       전체 통합 시간표에서 추출한 행만 `source_section`을 정확히 `integrated_table`, `is_authoritative`를 `true`로 설정하여 `rows`에 넣을 것.
       개별 노선 반복표, 지도/사진, 상세 도착지에서만 발견한 정보는 `rows`에 넣지 말 것.

    [노선 및 시간 규칙]
    1. 동일한 시간대나 목적지라도 (1호), (2호), (3호) 또는 A, B, C 등 세부 노선이나 차량 번호가 나뉘어져 있다면, 이를 절대 합치지 말고 각각 독립된 노선으로 분리해서 추출할 것.
    2. `route_name`에는 반드시 세부 노선 번호나 이름까지 포함할 것 (예: "(출근)망포/영통 1호", "(퇴근)병점/서천").
    3. 각 노선 및 정류장의 위치가 서울 지역(예: 강남, 사당 등)인지 경기 지역(예: 수원, 판교, 동탄, 용인 등)인지 판단하여 `region` 필드에 정확히 "seoul" 또는 "gyeonggi"를 입력할 것.
    4. 문서 상단의 전체 노선표와 하단의 노선별 상세/사진 영역에서 정류장명이 다르면, `stop_name`은 반드시 상단 전체 노선표의 표기를 우선할 것.
       하단 상세/사진 영역은 위치, 주소, 좌표를 판단하는 보조정보로만 사용할 것.
       정류장명은 원문을 그대로 전사하고 약어를 확장하거나 축약하지 말며, 글자를 임의로 추가하거나 제거하지 말 것.
       예를 들어 `SKT`를 `SK`로 바꾸거나 `대학병원`을 `대학교병원`으로 바꾸지 말 것.
    5. 출근 일반 승차 정류장은 통합 시간표에 표시된 각 정류장의 시간을 `arrival_time`에 그대로 유지할 것. 시간이 없는 값을 추론하지 말 것.
    6. 출근 통합 시간표에 명시된 `(하차만)` 정류장은 탑승 정류장이 아니지만 실제 시간이 있는 노선 중간 하차 정류장이다.
       `(하차만)` 표시와 통합 시간표의 시간을 모두 그대로 유지하고, 시간이 있다는 이유로 일반 승차 정류장으로 바꾸거나 시간을 삭제하지 말 것.
    7. 퇴근 표의 병합된 출발시간은 노선 단위 출발시간이며 첫 번째 승차 정류장에만 적용할 것.
       이후 하차 정류장에는 출발시간을 복사하지 말고 `arrival_time`을 빈 문자열로 반환할 것. 차량/호차별 출발시간도 각 독립 노선의 첫 승차 정류장에만 적용할 것.
    8. 호차가 구분된 노선은 `vehicle_id`에 `1호`, `2호`처럼 원문의 호차를 넣고, 같은 값을 `route_name`에도 반드시 포함할 것.
       호차 구분이 없는 노선은 `vehicle_id`를 빈 문자열로 반환할 것.
       출근 통합표에서 하나의 기본 노선 아래 `1호`, `2호`, `3호`, `4호`처럼 호차별 시간 열이 있으면 각 호차를 반드시 별도 row로 출력할 것.
       이때 각 row의 `vehicle_id`에는 해당 열의 호차를 원문 그대로 넣고, 여러 호차 열이 있는 노선에서는 `vehicle_id`를 절대 빈 문자열로 반환하지 말 것.
       예를 들어 정류장 1에 `1호 06:20`, `2호 06:30`, `3호 미정차`가 있으면 1호와 2호 row만 만들고 3호 row는 만들지 말 것.
    9. `stop_order`는 호차별 route stop 순서를 1부터 기록할 것.
       출근은 통합표의 정류장 번호를 그대로 사용하고, 해당 호차의 `미정차` 행은 출력하지 않되 이후 정류장 번호를 당겨 바꾸지 말 것.
       퇴근은 첫 승차 정류장을 1, 하차 목록의 첫 정류장을 2, 두 번째를 3 순서로 기록할 것.
    10. 반드시 통합 시간표에 등장하는 순서대로 아래 JSON 객체 형식으로만 응답해야 해. 마크다운(` ```json `) 표기 없이 pure JSON 문자열만 출력해.
    
    [응답 데이터 형식]
    {
      "rows": [
        {
          "source_section": "integrated_table",
          "is_authoritative": true,
          "vehicle_id": "1호",
          "stop_order": 1,
          "route_name": "(출근)망포/영통 1호",
          "stop_name": "정자역 3번출구",
          "arrival_time": "08:15",
          "region": "gyeonggi",
          "address": "위치 설명 또는 주소"
        }
      ]
    }
    """

    contents = []

    canonical_index = None
    if file_name.endswith('.pdf'):
        pdf_bytes = uploaded_file.read()
        try:
            canonical_index = build_pdf_canonical_index(pdf_bytes)
        except PdfCanonicalError as exc:
            logger.exception(
                "[ROUTE_IMPORT_ERROR] stage=pdf_canonical_index "
                "type=%s message=%s",
                type(exc).__name__, str(exc),
            )
            raise
        images = convert_from_bytes(pdf_bytes)
        for img in images:
            img_byte_arr = io.BytesIO()
            img.save(img_byte_arr, format='JPEG', quality=85)
            contents.append(
                types.Part.from_bytes(
                    data=img_byte_arr.getvalue(),
                    mime_type="image/jpeg"
                )
            )
        contents.append(prompt)

    elif file_name.endswith('.pptx'):
        prs = Presentation(uploaded_file)
        text_content = []
        for slide in prs.slides:
            for shape in slide.shapes:
                if shape.has_text_frame:
                    text_content.append(shape.text_frame.text)
                if shape.has_table:
                    for row in shape.table.rows:
                        text_content.append(" | ".join([cell.text.strip() for cell in row.cells]))
        
        extracted_text = "\n".join(text_content)
        contents = [f"PPTX extracted content:\n{extracted_text}", prompt]
    
    else:
        raise ValueError("지원하지 않는 파일 형식입니다. (PDF, PPTX만 가능)")

    try:
        response = client.models.generate_content(
            model=get_document_model(),
            contents=contents
        )
    except ClientError as exc:
        logger.warning(
            "Route document Gemini request failed code=%s status=%s message=%s",
            getattr(exc, "code", None),
            getattr(exc, "status", None),
            _safe_client_error_message(exc),
        )
        raise
    
    raw_text = response.text.strip()
    if "```" in raw_text:
        raw_text = re.sub(r'```(?:json)?\s*', '', raw_text)
        raw_text = raw_text.replace('```', '').strip()
    
    payload = json.loads(raw_text)
    if canonical_index is not None:
        result = extract_authoritative_route_rows(payload, strip_metadata=False)
        try:
            result = canonicalize_pdf_rows(result, canonical_index)
        except PdfCanonicalError as exc:
            logger.exception(
                "[ROUTE_IMPORT_ERROR] stage=pdf_canonical_match "
                "type=%s message=%s",
                type(exc).__name__, str(exc),
            )
            raise
        result = _strip_parser_metadata(result)
    else:
        result = extract_authoritative_route_rows(payload)
    logger.info("Route document parsing completed rows=%s", len(result) if isinstance(result, list) else 0)
    return result
