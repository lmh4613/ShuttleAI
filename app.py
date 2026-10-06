import streamlit as st
import pandas as pd
import requests
import os
import json
import re
import logging
from urllib.parse import urlencode
from demo_support import (prepare_login, consume_login, api_request,
                          deliver_message, message_succeeded, SELECTION_KEYS)
from datetime import datetime
import weather_api
import ppt_parser
from pdf_route_canonical import PdfCanonicalError
from mobile_ui import inject_mobile_styles
from notification_message import format_weather_notification
from push_subscription_store import (
    OWNERSHIP_CURRENT,
    PushSubscriptionError,
    mark_subscription_expired,
)
from web_push import load_web_push_config, send_web_push
from web_push_ui import can_send_weather_push, render_web_push_poc
from user_screen_cache import (
    get_user_route_data,
    get_user_screen_data,
    invalidate_user_route_data,
    invalidate_user_screen_data,
)
from notification_repository import (
    NotificationRepositoryError,
    load_kakao_credentials,
    save_kakao_credentials,
)
from notification_worker import start_notification_worker
from route_repository import (
    RouteConflictError,
    RouteRepositoryError,
    RouteValidationError,
    StaleRouteSnapshotError,
    load_admin_route_snapshot,
    preview_region_reconcile,
    reconcile_admin_route_edits,
    reconcile_region_routes,
    update_route_stop_coordinates,
)
from user_settings_repository import (
    DuplicateFavoriteError,
    UserSettingsError,
    create_favorite,
    delete_favorite,
    get_global_notification_policy,
    update_favorite_notification,
    update_global_notification_policy,
    update_notification_settings,
)
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

st.set_page_config(page_title="AI 셔틀버스 날씨 알림", page_icon="🚌", layout="wide")
inject_mobile_styles()

def get_env_variable(var_name, default=""):
    """환경 변수를 os.getenv에서 먼저 찾고, 없으면 st.secrets에서 가져옵니다."""
    val = os.getenv(var_name)
    if not val:
        try:
            val = st.secrets.get(var_name)
        except Exception:
            val = None
    return val if val is not None else default

# 카카오 API 설정 정보 (로컬 .env 및 Streamlit Cloud st.secrets 호환)
KAKAO_CLIENT_ID = get_env_variable("KAKAO_CLIENT_ID", "")
KAKAO_CLIENT_SECRET = get_env_variable("KAKAO_CLIENT_SECRET", "")
KAKAO_REDIRECT_URI = get_env_variable("KAKAO_REDIRECT_URI", "http://localhost:8501")
WEB_PUSH_CONFIG = load_web_push_config(get_env_variable)

USER_SETTINGS_FILE = "user_settings.json"

def load_user_settings():
    if os.path.exists(USER_SETTINGS_FILE):
        try:
            with open(USER_SETTINGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def get_user_data(user_id):
    all_data = load_user_settings()
    user_entry = all_data.get(str(user_id), {})
    if isinstance(user_entry, list):
        user_entry = {}
    return {
        "access_token": user_entry.get("access_token", ""),
        "refresh_token": user_entry.get("refresh_token", ""),
    }


def save_user_data(user_id, access_token=None, refresh_token=None):
    all_data = load_user_settings()
    raw_entry = all_data.get(str(user_id), {})
    # Preserve legacy migration input, but never read or mutate its favorite/settings fields.
    user_entry = dict(raw_entry) if isinstance(raw_entry, dict) else {}
    if access_token is not None:
        user_entry["access_token"] = access_token
    if refresh_token is not None:
        user_entry["refresh_token"] = refresh_token
    all_data[str(user_id)] = user_entry
    with open(USER_SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(all_data, f, ensure_ascii=False, indent=4)

def refresh_kakao_token(refresh_token):
    token_url = "https://kauth.kakao.com/oauth/token"
    payload = {
        "grant_type": "refresh_token",
        "client_id": KAKAO_CLIENT_ID,
        "refresh_token": refresh_token
    }
    if KAKAO_CLIENT_SECRET:
        payload["client_secret"] = KAKAO_CLIENT_SECRET
    status, data = api_request("POST", token_url, data=payload)
    if status == 200:
        return data.get("access_token"), data.get("refresh_token")
    return None, None

def send_kakao_memo(access_token, title, description):
    talk_url = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
    headers = {"Authorization": f"Bearer {access_token}"}
    template_object = {
        "object_type": "text",
        "text": f"[AI 셔틀버스 탑승·하차 통합 안내]\n\n{title}\n\n{description}",
        "link": {"web_url": KAKAO_REDIRECT_URI, "mobile_web_url": KAKAO_REDIRECT_URI},
        "button_title": "앱 열기"
    }
    payload = {"template_object": json.dumps(template_object)}
    return api_request("POST", talk_url, headers=headers, data=payload)


def send_user_memo(user_id, title, description, scheduled=False):
    user = get_user_data(user_id)
    try:
        return deliver_message(
            user.get("access_token"), user.get("refresh_token"),
            lambda token: send_kakao_memo(token, title, description),
            refresh_kakao_token,
            lambda at, rt: save_user_data(user_id, access_token=at, refresh_token=rt),
            retry_transient=scheduled,
        )
    except OSError:
        logger.exception("Could not persist refreshed Kakao credentials")
        return 0, {"error": "save_failed"}

# 대한민국 공휴일 목록 조회 (Nager.Date API 활용, 1일 캐싱)
@st.cache_data(ttl=86400)
def get_kr_holidays(year):
    url = f"https://date.nager.at/api/v3/PublicHolidays/{year}/KR"
    try:
        res = requests.get(url, timeout=3)
        if res.status_code == 200:
            return [h.get("date") for h in res.json()]
    except Exception:
        pass
    return []

def is_today_holiday():
    now = datetime.now()
    holidays = get_kr_holidays(now.year)
    return now.strftime("%Y-%m-%d") in holidays

# 온도 문자열에서 숫자만 추출하는 헬퍼 함수
def parse_temp(temp_str):
    if not temp_str:
        return 0.0
    match = re.search(r'[-+]?(?:\d+(?:\.\d+)?|\.\d+)', str(temp_str))
    return float(match.group()) if match else 0.0

# 탑승·하차 날씨 비교에 따른 통합 AI 멘트 생성 헬퍼 함수
def get_integrated_ai_message(wb, wa, board_name, arrive_name, trip_type='출근길'):
    from weather_comment_ai import generate_weather_advice
    return generate_weather_advice(wb, wa, trip_type)['text']


def route_stop_options(stops, is_leave):
    """Preserve route order; only marked morning stops are extra destinations."""
    if stops and isinstance(stops[0], dict):
        if "boarding_allowed" in stops[0]:
            rows = stops
            if is_leave:
                boarding = [row["stop_name"] for row in rows if row.get("boarding_allowed")]
                arrival = [row["stop_name"] for row in rows if row.get("alighting_allowed")]
                return boarding or ["정류장 없음"], arrival or ["정류장 없음"]
            boarding = [row["stop_name"] for row in rows if row.get("boarding_allowed")]
            arrival_rows = [row for row in rows if row.get("alighting_allowed")]
            defaults = [row["stop_name"] for row in arrival_rows if row.get("is_default_dropoff")]
            marked = [row["stop_name"] for row in arrival_rows
                      if not row.get("is_default_dropoff") and "(하차만)" in row["stop_name"]]
            arrival = list(dict.fromkeys(defaults + marked))
            return boarding or ["정류장 없음"], arrival or ["판교 제2테크노밸리"]
        stops = [row.get("stop_name") for row in stops]
    if is_leave:
        return stops[:1] or ["정류장 없음"], stops or ["정류장 없음"]
    boarding = [stop for stop in stops if "(하차만)" not in stop]
    arrival = list(dict.fromkeys(["판교 제2테크노밸리"] + [stop for stop in stops if "(하차만)" in stop]))
    return boarding or ["정류장 없음"], arrival


def non_searching_list_select(label, options, *, key):
    """Render a keyboard-free list selector for mobile route changes."""
    choices = list(options) or ["노선 없음"]
    current = st.session_state.get(key)
    if current not in choices:
        st.session_state[key] = choices[0]
    return st.radio(label, choices, key=key)


def render_reconcile_preview(preview, *, region):
    labels = [
        ("추가 노선", "routes_added"), ("수정 노선", "routes_updated"),
        ("비활성화 예정 노선", "routes_deactivated"),
        ("추가 정류장", "stops_added"), ("수정 정류장", "stops_updated"),
        ("제거 예정 정류장", "stops_removed"),
    ]
    columns = st.columns(3)
    for index, (label, key) in enumerate(labels):
        columns[index % 3].metric(label, preview.get(key, 0))
    if preview.get("conflicts"):
        st.error("노선 구조를 자동으로 확정할 수 없어 저장할 수 없습니다.")
        for conflict in preview["conflicts"]:
            st.caption(f"• {conflict}")

    details = preview.get("details", [])
    if details:
        with st.expander(f"상세 변경 내역 ({len(details)}건)", expanded=False):
            with st.container(height=320):
                for detail in details:
                    current = "-" if detail.get("current") is None else str(detail["current"])
                    proposed = "-" if detail.get("proposed") is None else str(detail["proposed"])
                    st.markdown(
                        f"**{detail.get('route_name', '')}** · "
                        f"{detail.get('stop_name') or '노선'} · `{detail.get('change_type', '')}`"
                    )
                    st.caption(
                        f"{detail.get('field', '')}: {current} → {proposed}"
                    )
    decisions = {}
    candidates = preview.get("change_candidates", [])
    if candidates:
        st.markdown("**관리자 선택이 필요한 변경 후보**")
        for candidate in candidates:
            with st.container(border=True):
                st.markdown(
                    f"**{candidate['route_name']} / {candidate['stop_order']}번**"
                )
                for field in candidate["fields"]:
                    st.caption(
                        f"{field['field']}: {field['current']} → {field['proposed']}"
                    )
                choice = st.selectbox(
                    "처리 방법",
                    ["", "APPLY", "KEEP_EXISTING"],
                    format_func=lambda value: {
                        "": "선택하세요", "APPLY": "변경 적용",
                        "KEEP_EXISTING": "기존 값 유지",
                    }[value],
                    key=f"route_change_{region}_{candidate['candidate_id']}",
                )
                if choice:
                    decisions[candidate["candidate_id"]] = choice
    return decisions


def render_route_import(region, label, uploader_key, existing_rows):
    pending_key = f"route_import_pending_{region}"
    success_key = f"route_import_success_{region}"
    if st.session_state.pop(success_key, False):
        st.success(f"{label} 노선이 Aiven에 저장되었습니다.")
    uploaded = st.file_uploader(
        f"{label} 셔틀 노선 파일 선택 (PDF, PPTX)",
        type=["pdf", "pptx"], key=uploader_key,
    )
    if uploaded and st.button(f"{label} 데이터 분석 및 미리보기", type="primary",
                              key=f"preview_route_import_{region}"):
        stage = "document_parse"
        try:
            with st.spinner(f"Gemini AI가 {label} 노선 문서를 분석 중입니다..."):
                parsed = ppt_parser.parse_shuttle_document(uploaded)
                stage = "coordinate_prepare"
                bar = st.progress(0)
                txt = st.empty()
                logger.info("Route geocoding started region=%s", region)
                rows = weather_api.prepare_routes_with_sequential_geocoding(
                    parsed, target_region=region,
                    existing_rows=existing_rows,
                    progress_callback=lambda c, t, s: (
                        bar.progress(c / t), txt.text(f"지오코딩 중... ({c}/{t}): {s}")
                    ),
                )
                stage = "reconcile_preview"
                logger.info("Route reconcile preview started region=%s", region)
                preview = preview_region_reconcile(region, rows)
            st.session_state[pending_key] = {"rows": rows, "preview": preview}
        except RouteRepositoryError as exc:
            st.session_state.pop(pending_key, None)
            st.error(str(exc))
        except PdfCanonicalError as exc:
            logger.exception(
                "[ROUTE_IMPORT_ERROR] stage=%s region=%s type=%s message=%s",
                stage, region, type(exc).__name__, str(exc),
            )
            st.session_state.pop(pending_key, None)
            st.error("노선 문서를 분석하지 못했습니다. 파일과 API 연결 상태를 확인해 주세요.")
        except weather_api.GeocodingError:
            st.session_state.pop(pending_key, None)
            st.error("정류장 좌표를 확인하지 못했습니다. 잠시 후 다시 시도해 주세요.")
        except Exception as exc:
            logger.warning(
                "[ROUTE_IMPORT_ERROR] stage=%s region=%s type=%s",
                stage, region, type(exc).__name__,
            )
            st.session_state.pop(pending_key, None)
            st.error("노선 문서를 분석하지 못했습니다. 파일과 API 연결 상태를 확인해 주세요.")

    pending = st.session_state.get(pending_key)
    if pending:
        st.markdown("**Aiven 반영 예정 변경사항**")
        decisions = render_reconcile_preview(pending["preview"], region=region)
        candidates = pending["preview"].get("change_candidates", [])
        unresolved = len(decisions) != len(candidates)
        if unresolved:
            st.info("모든 변경 후보에 대해 적용 또는 기존 값 유지를 선택해 주세요.")
        if st.button(
            f"{label} 데이터 Aiven에 저장", type="primary",
            key=f"save_route_import_{region}",
            disabled=bool(pending["preview"].get("conflicts")) or unresolved,
        ):
            try:
                reconcile_region_routes(
                    region, pending["rows"],
                    expected_snapshot=pending["preview"]["snapshot"],
                    change_decisions=decisions,
                )
                invalidate_user_route_data(st.session_state)
                st.session_state.pop(pending_key, None)
                st.session_state[success_key] = True
                st.rerun()
            except (RouteConflictError, StaleRouteSnapshotError) as exc:
                st.error(str(exc))
            except RouteRepositoryError as exc:
                st.error(str(exc))

def send_worker_kakao(target, title, description):
    """Deliver through Kakao only when the resolved channel is Kakao."""
    try:
        access_token, refresh_token = load_kakao_credentials(target.user_id)
        if not access_token and not refresh_token:
            return False
        status, data = deliver_message(
            access_token, refresh_token,
            lambda token: send_kakao_memo(token, title, description),
            refresh_kakao_token,
            lambda new_access, new_refresh: save_kakao_credentials(
                target.user_id, new_access, new_refresh or refresh_token
            ),
            retry_transient=True,
        )
        return message_succeeded(status, data)
    except NotificationRepositoryError:
        return False


def worker_holiday_checker(service_date):
    """Existing failure policy: an unavailable holiday API returns no holidays."""
    return service_date.isoformat() in get_kr_holidays(service_date.year)

@st.cache_resource
def start_background_scheduler():
    started = start_notification_worker(
        config=WEB_PUSH_CONFIG,
        holiday_checker=worker_holiday_checker,
        kakao_sender=send_worker_kakao,
    )
    if not started:
        logger.info("Notification worker disabled or already running")
    return started

start_background_scheduler()

# 세션 상태 초기화
if "user_info" not in st.session_state:
    st.session_state["user_info"] = None
if "is_admin" not in st.session_state:
    st.session_state["is_admin"] = False

# OAuth callbacks create a new Streamlit session; restore only validated selections.
st.session_state.setdefault("preview_user", False)
query_params = st.query_params
if ("code" in query_params or "error" in query_params) and st.session_state["user_info"] is None:
    selection = consume_login(query_params.get("state", ""))
    auth_code = query_params.get("code")
    denied = "error" in query_params
    st.query_params.clear()
    if selection is None:
        st.session_state["login_error"] = "로그인 요청이 만료되었거나 유효하지 않습니다. 카카오 로그인 버튼으로 다시 시도해 주세요."
    else:
        st.session_state.update(selection)
        st.session_state["restored_selection"] = selection
        if denied:
            st.session_state["login_error"] = "카카오 로그인이 취소되었습니다. 동의 후 다시 로그인할 수 있습니다."
        else:
            payload = {"grant_type": "authorization_code", "client_id": KAKAO_CLIENT_ID,
                       "redirect_uri": KAKAO_REDIRECT_URI, "code": auth_code}
            if KAKAO_CLIENT_SECRET:
                payload["client_secret"] = KAKAO_CLIENT_SECRET
            status, token_data = api_request("POST", "https://kauth.kakao.com/oauth/token", data=payload)
            access_token = token_data.get("access_token")
            if status == 200 and access_token:
                user_status, user_data = api_request("GET", "https://kapi.kakao.com/v2/user/me",
                    headers={"Authorization": f"Bearer {access_token}"})
                if user_status == 200 and user_data.get("id") is not None:
                    kakao_id = user_data["id"]
                    refresh_token = token_data.get("refresh_token") or get_user_data(kakao_id).get("refresh_token", "")
                    try:
                        save_user_data(kakao_id, access_token=access_token, refresh_token=refresh_token)
                    except OSError:
                        logger.exception("Could not save login data")
                        st.session_state["login_error"] = "로그인 정보를 저장하지 못했습니다. 파일 접근 상태를 확인한 뒤 다시 시도해 주세요."
                    else:
                        st.session_state["user_info"] = {
                            "id": kakao_id, "nickname": user_data.get("properties", {}).get("nickname", "사용자"),
                            "access_token": access_token, "refresh_token": refresh_token}
                        st.session_state["is_admin"] = kakao_id in [5070327065]
                        st.session_state["preview_user"] = False
                        invalidate_user_screen_data(st.session_state)
                        st.session_state.pop("login_error", None)
                else:
                    st.session_state["login_error"] = "카카오 사용자 정보를 가져오지 못했습니다. 잠시 후 다시 로그인해 주세요."
            else:
                st.session_state["login_error"] = "카카오 로그인에 연결하지 못했습니다. 인터넷 연결을 확인하고 다시 로그인해 주세요."
    st.rerun()

# Load the route source of truth once per Streamlit session.
route_load_error = None
try:
    db_data = get_user_route_data(st.session_state)
except RouteRepositoryError as exc:
    db_data = []
    route_load_error = str(exc)

# Restore only existing choices, before widgets are constructed.
restored = st.session_state.pop("restored_selection", None)
if restored:
    rows = db_data
    rows = [r for r in rows if r.get("region", "gyeonggi") == restored.get("user_reg")]
    if not rows:
        restored = {}
    elif restored.get("user_rt") not in {r.get("route_name") for r in rows}:
        restored = {"user_reg": restored["user_reg"]}
    else:
        stops = {r.get("stop_name") for r in rows if r.get("route_name") == restored["user_rt"]}
        restored = {k: v for k, v in restored.items()
                    if k not in ("user_board_st", "user_arrive_st") or v in stops}
    for key in ("user_reg", "user_rt", "user_board_st", "user_arrive_st"):
        st.session_state.pop(key, None)
    st.session_state.update(restored)

def logout_user_session():
    invalidate_user_screen_data(st.session_state)
    st.session_state["user_info"] = None
    st.session_state["is_admin"] = False
    st.session_state["preview_user"] = False
    st.query_params.clear()
    st.rerun()


login_links = []
# 사이드바: 로그인 및 관리자 인증
with st.sidebar:
    st.subheader("🔐 사용자 인증")
    if st.session_state["user_info"] is None and not st.session_state["is_admin"]:
        st.info("💡 카카오 로그인을 통해 즐겨찾기 및 알림 기능을 이용하세요.")
        login_links.append(st.empty())
        if st.session_state.get("login_error"):
            st.error(st.session_state["login_error"])
        st.divider()
        with st.expander("👑 관리자 로그인"):
            admin_pw = st.text_input("관리자 비밀번호", type="password")
            if st.button("로그인", width='stretch'):
                if admin_pw == "admin1234":
                    st.session_state["is_admin"] = True
                    st.success("관리자 로그인 성공!")
                    st.rerun()
                else:
                    st.error("비밀번호가 틀렸습니다.")
    else:
        if st.session_state["user_info"]:
            st.success(f"👋 **{st.session_state['user_info']['nickname']}**님 환영합니다!")
        if st.session_state["is_admin"]:
            if st.session_state["preview_user"]:
                st.info("일반 사용자 미리보기 중 · 실제 관리자 권한은 유지됩니다.")
                if st.button("관리자 모드로 돌아가기", width="stretch"):
                    st.session_state["restored_selection"] = {
                        k: st.session_state[k] for k in SELECTION_KEYS if k in st.session_state}
                    st.session_state["preview_user"] = False
                    st.rerun()
            else:
                st.markdown("👑 **관리자 권한 활성화됨**")
                if st.button("일반 사용자 모드로 보기", width="stretch"):
                    st.session_state["restored_selection"] = {
                        k: st.session_state[k] for k in SELECTION_KEYS if k in st.session_state}
                    st.session_state["preview_user"] = True
                    st.rerun()
            if st.session_state["user_info"] is None:
                st.info("즐겨찾기와 메시지 시연에는 카카오 로그인이 필요합니다.")
                login_links.append(st.empty())
        if st.button("로그아웃", key="sidebar_logout", width='stretch'):
            logout_user_session()

st.title("🚌 AI 셔틀버스 날씨 알림 (탑승·하차 통합 안내)")

with st.container(key="mobile_auth"):
    if st.session_state["user_info"] is None:
        st.caption("카카오 로그인 후 즐겨찾기와 알림 기능을 이용할 수 있습니다.")
        login_links.append(st.empty())
    else:
        st.caption(f"👤 {st.session_state['user_info']['nickname']}님 로그인 중")
        if st.button("로그아웃", key="mobile_logout", width="stretch"):
            logout_user_session()

if route_load_error:
    st.error(route_load_error)
sorted_db_data = sorted(
    db_data,
    key=lambda x: x.get('region', 'gyeonggi')
)
admin_snapshot = {"rows": [], "identity_map": {}, "snapshots": {}}
admin_route_error = None
if st.session_state["is_admin"] and not st.session_state["preview_user"]:
    try:
        admin_snapshot = load_admin_route_snapshot()
    except RouteRepositoryError as exc:
        admin_route_error = str(exc)
admin_db_data = admin_snapshot["rows"]

if st.session_state["is_admin"] and not st.session_state["preview_user"]:
    tab1, tab2 = st.tabs(["👑 [어드민] 노선 관리 및 그리드 편집", "🌤️ 셔틀버스 탑승·하차 통합 날씨 및 즐겨찾기"], default="🌤️ 셔틀버스 탑승·하차 통합 날씨 및 즐겨찾기")
else:
    tab1 = None
    tab2 = st.tabs(["🌤️ 셔틀버스 탑승·하차 통합 날씨 및 즐겨찾기"])[0]

if tab1 is None:
    from weather_advice_preview import reset_preview
    from notification_history_ui import reset_notification_history
    reset_preview()
    reset_notification_history()

if tab1 is not None:
    with tab1:
        st.header("📋 지역별 셔틀버스 노선 문서 업로드 및 관리")
        if admin_route_error:
            st.error(admin_route_error)
        st.subheader("🔔 전역 알림 채널 정책")
        try:
            global_channel_policy = get_global_notification_policy()
            admin_actor_id = (
                st.session_state["user_info"].get("id")
                if st.session_state["user_info"] else None
            )
            global_policy_options = ["AUTO", "PUSH", "KAKAO"]
            selected_global_policy = st.selectbox(
                "전체 서비스 알림 채널",
                global_policy_options,
                index=global_policy_options.index(global_channel_policy),
                format_func=lambda value: {
                    "AUTO": "AUTO - 사용자 설정 사용",
                    "PUSH": "PUSH - 전체 Web Push 강제",
                    "KAKAO": "KAKAO - 전체 카카오톡 강제",
                }[value],
                key="admin_global_notification_policy",
            )
            if admin_actor_id is None:
                st.caption("전역 정책 변경에는 DB에 등록된 카카오 관리자 로그인이 필요합니다.")
            if st.button(
                "💾 전역 알림 정책 저장", key="save_global_notification_policy",
                disabled=admin_actor_id is None,
            ):
                update_global_notification_policy(
                    admin_actor_id, selected_global_policy
                )
                st.success("전역 알림 정책이 저장되었습니다.")
                st.rerun()
        except UserSettingsError as exc:
            st.error(str(exc))
        st.divider()
        up_tab_gy, up_tab_se = st.tabs(["🟢 경기 지역 업로드", "🔵 서울 지역 업로드"])
        
        with up_tab_gy:
            render_route_import("gyeonggi", "경기", "up_gy", admin_db_data)

        with up_tab_se:
            render_route_import("seoul", "서울", "up_se", admin_db_data)

        st.divider()
        st.subheader("📍 특정 정류장 좌표 단건 재계산")
        if admin_db_data:
            c1, c2, c3 = st.columns(3)
            with c1:
                admin_regions = sorted(list(set(i.get('region', 'gyeonggi') for i in admin_db_data)))
                reg_map_admin = {"gyeonggi": "경기", "seoul": "서울"}
                sel_admin_region = st.selectbox("지역 선택", admin_regions, format_func=lambda x: reg_map_admin.get(x, x), key="admin_reg")
            
            reg_filtered_admin = [i for i in admin_db_data if i.get('region', 'gyeonggi') == sel_admin_region]
            
            with c2:
                admin_routes = list(dict.fromkeys(i.get('route_name') for i in reg_filtered_admin if i.get('route_name')))
                sel_rt = st.selectbox("노선 선택", admin_routes if admin_routes else ["노선 없음"], key="admin_rt")
            
            route_filtered_admin = [i for i in reg_filtered_admin if i.get('route_name') == sel_rt]
            
            with c3:
                admin_stops = [i.get('stop_name') for i in route_filtered_admin]
                sel_st = st.selectbox("정류장 선택", admin_stops if admin_stops else ["정류장 없음"], key="admin_st")
            selected_admin_row = next(
                (row for row in route_filtered_admin if row.get("stop_name") == sel_st), None
            )
            
            if st.button("🎯 선택 정류장 좌표 재계산 및 갱신", type="primary"):
                with st.spinner("카카오 지도 API 및 Gemini 격자 변환 처리 중..."):
                    try:
                        if selected_admin_row is None:
                            raise RouteValidationError("수정할 정류장을 찾을 수 없습니다.")
                        identity = admin_snapshot["identity_map"].get(
                            selected_admin_row.get("_identity_key"), {}
                        )
                        if not identity:
                            raise RouteValidationError("정류장 식별정보를 찾을 수 없습니다.")
                        new_lat, new_lon = weather_api.get_coordinates_by_gemini(sel_st)
                        if new_lat == 37.3947 and new_lon == 127.1111:
                            raise RouteValidationError(
                                "좌표를 확인하지 못해 기본 좌표가 반환되었습니다. 저장을 취소했습니다."
                            )
                        new_nx, new_ny = weather_api.latlon_to_grid(new_lat, new_lon)
                        update_route_stop_coordinates(
                            identity["route_stop_id"], latitude=new_lat, longitude=new_lon,
                            grid_x=new_nx, grid_y=new_ny,
                            geocode_status="✅ 정상 (단건 재조회)",
                            expected_version=identity["version"],
                        )
                    except RouteRepositoryError as exc:
                        st.error(str(exc))
                    except Exception as exc:
                        logger.warning("Route coordinate refresh failed type=%s", type(exc).__name__)
                        st.error("정류장 좌표를 갱신하지 못했습니다.")
                    else:
                        invalidate_user_route_data(st.session_state)
                        st.toast("정류장 좌표가 성공적으로 갱신되었습니다!", icon="🎯")
                        st.rerun()

        st.divider()
        st.subheader("📝 노선 및 정류장 데이터 직접 편집 그리드")
        if admin_db_data:
            df_routes = pd.DataFrame(admin_db_data)
            edited_df = st.data_editor(
                df_routes, num_rows="dynamic", width='stretch',
                key="route_grid_editor", height=400,
                column_config={"_identity_key": None, "_stop_order": None},
            )
            if st.button("💾 그리드 변경사항 저장", type="primary", width='stretch'):
                try:
                    updated_records = edited_df.to_dict(orient="records")
                    reconcile_admin_route_edits(
                        updated_records,
                        identity_map=admin_snapshot["identity_map"],
                        expected_snapshots=admin_snapshot["snapshots"],
                    )
                    invalidate_user_route_data(st.session_state)
                    st.toast("노선 변경사항이 Aiven에 저장되었습니다.", icon="✅")
                    st.rerun()
                except RouteRepositoryError as exc:
                    st.error(str(exc))

        from weather_advice_preview import render_admin_preview
        render_admin_preview(st.session_state['is_admin'], st.session_state['preview_user'])
        from notification_history_ui import render_admin_notification_history
        render_admin_notification_history(
            st.session_state['is_admin'], st.session_state['preview_user']
        )

main_tab_target = tab2 if tab1 is not None else tab2
with main_tab_target:
    st.subheader("🔄 셔틀버스 탑승지 & 하차지 통합 날씨 안내")
    st.write("선택하신 노선의 **탑승 정류장**과 **하차(도착) 정류장**의 날씨를 동시에 조회하여 출퇴근 준비를 완벽하게 도와드립니다.")
    
    if sorted_db_data:
        col_r1, col_r2 = st.columns(2)
        with col_r1:
            regions = sorted(list(set(i.get('region', 'gyeonggi') for i in sorted_db_data)))
            reg_map = {"gyeonggi": "경기", "seoul": "서울"}
            sel_region = st.selectbox("지역 선택", regions, format_func=lambda x: reg_map.get(x, x), key="user_reg")
        
        reg_filtered = [i for i in sorted_db_data if i.get('region', 'gyeonggi') == sel_region]
        routes = list(dict.fromkeys(i.get('route_name') for i in reg_filtered if i.get('route_name')))
        
        with col_r2:
            sel_route = non_searching_list_select("노선 선택", routes, key="user_rt")
        
        route_stops = [i for i in reg_filtered if i.get('route_name') == sel_route]
        stops = [i.get('stop_name') for i in route_stops] if route_stops else []
        
        is_leave = "퇴근" in str(sel_route)
        trip_type = "퇴근길" if is_leave else "출근길"
        boarding_options, arrival_options = route_stop_options(route_stops, is_leave)

        # Reset before creating stop widgets, even when routes share stop names.
        # On the first render, preserve any selections restored by OAuth.
        route_context = (sel_region, sel_route)
        previous_route = st.session_state.get("_stop_route_context", route_context)
        if previous_route != route_context:
            st.session_state["user_board_st"] = boarding_options[0]
            st.session_state["user_arrive_st"] = arrival_options[-1] if is_leave else arrival_options[0]
            st.session_state["user_arrive_st_fixed"] = "판교 제2테크노밸리"
        st.session_state["_stop_route_context"] = route_context
        
        st.markdown("---")
        col_s1, col_s2 = st.columns(2)
        with col_s1:
            st.markdown("🟢 **[1] 내 탑승 정류장 선택**")
            if is_leave:
                first_stop = stops[0] if stops else "정류장 없음"
                st.session_state["user_board_st"] = first_stop
                sel_board_stop = st.selectbox("탑승 정류장", [first_stop], key="user_board_st", disabled=True)
            else:
                if st.session_state.get("user_board_st") not in boarding_options:
                    st.session_state.pop("user_board_st", None)
                sel_board_stop = st.selectbox("탑승 정류장", boarding_options, key="user_board_st")
        
        with col_s2:
            st.markdown("🔴 **[2] 내 하차(도착) 정류장 선택**")
            if is_leave:
                if st.session_state.get("user_arrive_st") not in stops:
                    st.session_state.pop("user_arrive_st", None)
                default_arrive_idx = 0 if "user_arrive_st" in st.session_state else max(len(stops) - 1, 0)
                sel_arrive_stop = st.selectbox("하차 정류장 (도착지)", stops if stops else ["정류장 없음"], index=default_arrive_idx, key="user_arrive_st")
            else:
                if st.session_state.get("user_arrive_st_fixed") not in arrival_options:
                    st.session_state.pop("user_arrive_st_fixed", None)
                sel_arrive_stop = st.selectbox("하차 정류장 (도착지)", arrival_options, key="user_arrive_st_fixed")
        
        board_row = next((i for i in route_stops if i.get('stop_name') == sel_board_stop), {})
        
        if is_leave or sel_arrive_stop != "판교 제2테크노밸리":
            arrive_row = next((i for i in route_stops if i.get('stop_name') == sel_arrive_stop), {})
            arrive_lat = float(arrive_row.get('lat', 37.3947))
            arrive_lon = float(arrive_row.get('lon', 127.1111))
        else:
            PANGYO_2ND_LAT = 37.412605
            PANGYO_2ND_LON = 127.095703
            arrive_row = next((i for i in route_stops if i.get('is_default_dropoff')), None) or {
                'stop_name': "판교 제2테크노밸리",
                'lat': PANGYO_2ND_LAT,
                'lon': PANGYO_2ND_LON
            }
            arrive_lat = float(arrive_row.get('lat', PANGYO_2ND_LAT))
            arrive_lon = float(arrive_row.get('lon', PANGYO_2ND_LON))
        
        board_lat = float(board_row.get('lat', 37.3947))
        board_lon = float(board_row.get('lon', 127.1111))
        boarding_target = weather_api.resolve_boarding_datetime(board_row.get('arrival_time'))
        weather_selection_key = (sel_region, sel_route, sel_board_stop, sel_arrive_stop,
                                 boarding_target.isoformat() if boarding_target else None)
        
        st.markdown("")
        if st.button("🔍 탑승·하차 통합 날씨 조회", type="primary", width='stretch'):
            with st.spinner("탑승지와 하차지의 기상청 날씨 및 AI 통합 코멘트 생성 중..."):
                w_board = weather_api.get_weather_forecast_by_coords(board_lat, board_lon, stop_name=sel_board_stop, trip_type=trip_type, target_datetime=boarding_target, location="boarding")
                w_arrive = weather_api.get_weather_forecast_by_coords(arrive_lat, arrive_lon, stop_name=sel_arrive_stop, trip_type=trip_type, target_datetime=boarding_target, location="destination")
            st.session_state['w_board'] = w_board
            st.session_state['w_arrive'] = w_arrive
            st.session_state['integrated_stop_key'] = weather_selection_key
            st.session_state['integrated_comment'] = get_integrated_ai_message(
                w_board, w_arrive, sel_board_stop, sel_arrive_stop, trip_type
            )
            st.session_state['integrated_comment_key'] = weather_selection_key
        
        user_id = st.session_state["user_info"]["id"] if st.session_state["user_info"] else None
        user_settings = []
        notification_settings = None
        user_settings_error = None
        if user_id is not None:
            try:
                dashboard_data = get_user_screen_data(st.session_state, user_id)
                notification_settings = dashboard_data["notification_settings"]
                user_settings = dashboard_data["favorites"]
            except (UserSettingsError, ValueError) as exc:
                user_settings_error = str(exc)
                st.error(user_settings_error)
        
        if 'w_board' in st.session_state and 'w_arrive' in st.session_state and st.session_state.get('integrated_stop_key') == weather_selection_key:
            wb = st.session_state['w_board']
            wa = st.session_state['w_arrive']
            
            if not wb.get("available", True) or not wa.get("available", True):
                st.warning("일부 정류장의 날씨 정보를 불러오지 못했습니다. 잠시 후 다시 조회해 주세요.")
            with st.container(key="mobile_weather_result"):
                st.divider()
                st.markdown(f"### 📊 [{sel_route}] 탑승·하차 날씨 비교 리포트")

                res_col1, res_col2 = st.columns(2)
                with res_col1:
                    with st.container(border=True):
                        st.markdown(f"#### 🟢 탑승지: {sel_board_stop}")
                        st.caption(f"탑승 예정 시간: {boarding_target:%Y-%m-%d %H:%M}" if boarding_target else "등록된 탑승시간 없음")
                        m1, m2 = st.columns(2)
                        m1.metric("기온", wb["temperature"])
                        m2.metric("하늘상태", wb["sky_status"])

                with res_col2:
                    with st.container(border=True):
                        st.markdown(f"#### 🔴 하차지: {sel_arrive_stop}")
                        if not is_leave:
                            st.caption("최종 목적지" if sel_arrive_stop == "판교 제2테크노밸리" else "선택한 하차지")
                        m3, m4 = st.columns(2)
                        m3.metric("기온", wa["temperature"])
                        m4.metric("하늘상태", wa["sky_status"])
            
            st.markdown("")
            if st.session_state.get('integrated_comment_key') == weather_selection_key:
                integrated_comment = st.session_state.get('integrated_comment')
            else:
                integrated_comment = get_integrated_ai_message(
                    wb, wa, sel_board_stop, sel_arrive_stop, trip_type
                )
                st.session_state['integrated_comment'] = integrated_comment
                st.session_state['integrated_comment_key'] = weather_selection_key
            boarding_label = f" · {boarding_target:%m/%d %H:%M} 탑승 예정" if boarding_target else ""
            st.info(f"🤖 **통합 AI 코멘트{boarding_label}**\n\n{integrated_comment}")

        result_matches_selection = bool(
            st.session_state.get('integrated_stop_key') == weather_selection_key
            and st.session_state.get('integrated_comment_key') == weather_selection_key
            and st.session_state.get('w_board')
            and st.session_state.get('w_arrive')
            and st.session_state.get('integrated_comment')
        )
        weather_message = None
        if result_matches_selection:
            weather_message = format_weather_notification(
                route_name=sel_route,
                trip_type=trip_type,
                boarding_stop=sel_board_stop,
                boarding_time=board_row.get('arrival_time'),
                boarding_weather=st.session_state['w_board'],
                destination_stop=sel_arrive_stop,
                destination_time=(None if is_leave else arrive_row.get('arrival_time')),
                destination_weather=st.session_state['w_arrive'],
                comment=st.session_state['integrated_comment'],
            )

        with st.container(key="mobile_favorite_actions"):
            action_favorite, action_kakao, action_push = st.columns(3)
            with action_favorite:
                if st.session_state["user_info"]:
                    can_change_db_settings = not st.session_state["preview_user"] and not user_settings_error
                    if st.button("⭐ 통합 즐겨찾기 추가", width='stretch',
                                 disabled=not can_change_db_settings):
                        new_item = {
                            "region": sel_region,
                            "route_name": sel_route,
                            "board_stop": sel_board_stop,
                            "arrive_stop": sel_arrive_stop,
                            "trip_type": trip_type,
                            "board_time": board_row.get('arrival_time', ''),
                            "arrive_time": "-" if is_leave else arrive_row.get('arrival_time', '-'),
                            "board_lat": board_lat,
                            "board_lon": board_lon,
                            "arrive_lat": arrive_lat,
                            "arrive_lon": arrive_lon,
                        }
                        try:
                            create_favorite(user_id, new_item)
                            invalidate_user_screen_data(st.session_state, user_id)
                            st.success("통합 즐겨찾기에 추가되었습니다!")
                            st.rerun()
                        except DuplicateFavoriteError as exc:
                            st.warning(str(exc))
                        except UserSettingsError as exc:
                            st.error(str(exc))
                    if st.session_state["preview_user"]:
                        st.caption("일반 사용자 미리보기에서는 즐겨찾기를 변경할 수 없습니다.")
                else:
                    st.button("⭐ 즐겨찾기 (로그인필요)", width='stretch', disabled=True)

            with action_kakao:
                kakao_enabled = bool(st.session_state["user_info"] and weather_message)
                if st.button(
                    "💬 카카오톡으로 보내기",
                    width='stretch',
                    disabled=not kakao_enabled,
                ):
                    code, res = send_user_memo(
                        user_id, weather_message.title, weather_message.body
                    )
                    if message_succeeded(code, res):
                        st.success("카카오톡 통합 전송 완료!")
                        st.toast("카카오톡 나에게 톡메시지가 전송되었습니다.", icon="💬")
                    else:
                        st.error("카카오톡 전송에 실패했습니다. 잠시 후 다시 시도해 주세요. 계속 실패하면 로그아웃 후 카카오 로그인으로 다시 연결해 주세요.")

            with action_push:
                push_enabled = can_send_weather_push(
                    st.session_state,
                    logged_in=st.session_state["user_info"] is not None,
                    preview_user=st.session_state["preview_user"],
                    result_matches=result_matches_selection,
                )
                if st.button(
                    "🔔 Push로 보내기",
                    width='stretch',
                    disabled=not push_enabled,
                ):
                    subscription = st.session_state["web_push_subscription"]

                    def expire_current_subscription():
                        try:
                            mark_subscription_expired(user_id, subscription)
                        except PushSubscriptionError:
                            logger.warning("Expired current browser Push cleanup failed")
                        st.session_state["web_push_db_ownership"] = "none"
                        invalidate_user_screen_data(st.session_state, user_id)

                    ok, message = send_web_push(
                        subscription,
                        WEB_PUSH_CONFIG,
                        weather_message.title,
                        weather_message.body,
                        st.session_state.get("web_push_click_url", ""),
                        expired_handler=expire_current_subscription,
                    )
                    if ok:
                        st.success(message)
                    else:
                        st.error(message)
                if st.session_state["user_info"] and not st.session_state["preview_user"]:
                    if st.session_state.get("web_push_db_ownership") != OWNERSHIP_CURRENT:
                        st.caption("이 기기 알림을 먼저 등록해 주세요.")
                    elif not result_matches_selection:
                        st.caption("현재 선택으로 날씨를 조회한 뒤 전송할 수 있습니다.")

        st.divider()
        if st.session_state["user_info"]:
            with st.expander("⚙️ 자동 알림 공통 조건 설정", expanded=True):
                current_active_days = []
                current_exclude_holidays = True
                selected_channel = "KAKAO"
                settings_widget_owner = "unavailable"
                if notification_settings is None:
                    st.warning("알림 설정을 불러오지 못해 변경할 수 없습니다.")
                else:
                    settings_widget_owner = notification_settings["user_id"]
                    current_active_days = notification_settings["active_days"]
                    current_exclude_holidays = notification_settings["exclude_holidays"]
                    current_channel = notification_settings["delivery_channel"]
                    st.markdown("**알림 받는 방법**")
                    selected_channel = st.radio(
                        "알림 채널",
                        ["PUSH", "KAKAO"],
                        index=0 if current_channel == "PUSH" else 1,
                        format_func=lambda value: "Web Push" if value == "PUSH" else "카카오톡",
                        horizontal=True,
                        key=f"user_delivery_channel_{notification_settings['user_id']}",
                        disabled=st.session_state["preview_user"],
                    )
                    if selected_channel == "PUSH" and notification_settings["active_push_devices"] == 0:
                        st.info("Web Push를 사용하려면 알림을 받을 기기를 먼저 등록해 주세요.")

                col_cfg1, col_cfg2 = st.columns([3, 2])
                with col_cfg1:
                    st.markdown("**알림 발송 요일 선택**")
                    all_days = ["월", "화", "수", "목", "금", "토", "일"]
                    selected_days = []
                    with st.container(key="mobile_weekdays"):
                        day_cols = st.columns(7)
                        for idx, day in enumerate(all_days):
                            with day_cols[idx]:
                                if st.checkbox(
                                    day, value=(idx in current_active_days),
                                    key=f"day_chk_{settings_widget_owner}_{day}",
                                    disabled=st.session_state["preview_user"] or notification_settings is None,
                                ):
                                    selected_days.append(idx)
                with col_cfg2:
                    st.markdown("**휴일 설정**")
                    exclude_hols = st.checkbox(
                        "대한민국 공휴일 자동 제외",
                        value=current_exclude_holidays if notification_settings else True,
                        key=f"exclude_hols_chk_{settings_widget_owner}",
                        disabled=st.session_state["preview_user"] or notification_settings is None,
                    )
                
                if st.button(
                    "💾 공통 알림 조건 저장", type="primary", width='stretch',
                    disabled=st.session_state["preview_user"] or notification_settings is None,
                ):
                    try:
                        update_notification_settings(
                            user_id, active_days=selected_days,
                            exclude_holidays=exclude_hols,
                            delivery_channel=selected_channel,
                        )
                        invalidate_user_screen_data(st.session_state, user_id)
                        st.success("알림 조건이 저장되었습니다!")
                        st.rerun()
                    except UserSettingsError as exc:
                        st.error(str(exc))

            st.markdown("")
        if st.session_state["user_info"] and not st.session_state["preview_user"]:
            render_web_push_poc(
                WEB_PUSH_CONFIG,
                kakao_user_id=st.session_state["user_info"]["id"],
                on_data_changed=lambda: invalidate_user_screen_data(
                    st.session_state, st.session_state["user_info"]["id"]
                ),
                active_device_count=(
                    notification_settings["active_push_devices"]
                    if notification_settings is not None else None
                ),
            )
        elif st.session_state["preview_user"]:
            st.info("일반 사용자 미리보기에서는 기기 알림을 등록할 수 없습니다.")
        else:
            render_web_push_poc(WEB_PUSH_CONFIG)

        st.markdown("")
        st.subheader("⭐ 내 통합 즐겨찾기 및 알림 설정 목록")
        if st.session_state["user_info"]:
            if user_settings:
                for item in user_settings:
                    favorite_id = item["favorite_id"]
                    with st.container(border=True, key=f"mobile_favorite_{favorite_id}"):
                        cols_fav = st.columns([4, 2, 1])
                        with cols_fav[0]:
                            is_item_leave = "퇴근" in str(item.get('trip_type', ''))
                            arr_time_str = "" if is_item_leave else (f" ({item.get('arrive_time')})" if item.get('arrive_time') and item.get('arrive_time') != '-' else "")
                            st.markdown(
                                f"**[{item.get('trip_type')}]** [{reg_map.get(item.get('region'), '경기')}] **{item.get('route_name')}**<br>"
                                f"🟢 탑승: {item.get('board_stop')} ({item.get('board_time')}) ➔ 🔴 하차: **{item.get('arrive_stop')}**{arr_time_str}",
                                unsafe_allow_html=True
                            )
                        with cols_fav[1]:
                            notify_enabled = item.get("notify_enabled", False)
                            notify_min = item.get("notify_min", 10)
                            
                            new_notify_enabled = st.checkbox(
                                "🔔 알림 받기", value=notify_enabled,
                                key=f"notif_chk_{favorite_id}",
                                disabled=st.session_state["preview_user"],
                            )
                            
                            options_min = [10, 20, 30, 40, 50, 60]
                            current_idx = options_min.index(notify_min) if notify_min in options_min else 0
                            new_notify_min = st.selectbox(
                                "알림 시점", options_min, index=current_idx,
                                format_func=lambda x: f"출발 {x}분 전",
                                key=f"notif_min_{favorite_id}",
                                disabled=st.session_state["preview_user"],
                            )
                            
                            if st.button(
                                "💾 설정 저장", key=f"save_notif_{favorite_id}", width='stretch',
                                disabled=st.session_state["preview_user"],
                            ):
                                try:
                                    if not update_favorite_notification(
                                        user_id, favorite_id, enabled=new_notify_enabled,
                                        lead_minutes=new_notify_min,
                                    ):
                                        raise UserSettingsError("즐겨찾기 알림 설정을 찾을 수 없습니다.")
                                    invalidate_user_screen_data(st.session_state, user_id)
                                    st.success("알림 설정이 저장되었습니다!")
                                    st.rerun()
                                except UserSettingsError as exc:
                                    st.error(str(exc))
                        with cols_fav[2]:
                            st.write("")
                            if st.button(
                                "🗑️ 삭제", key=f"del_fav_{favorite_id}", width='stretch',
                                disabled=st.session_state["preview_user"],
                            ):
                                try:
                                    if not delete_favorite(user_id, favorite_id):
                                        raise UserSettingsError("삭제할 즐겨찾기를 찾을 수 없습니다.")
                                    invalidate_user_screen_data(st.session_state, user_id)
                                    st.success("삭제되었습니다.")
                                    st.rerun()
                                except UserSettingsError as exc:
                                    st.error(str(exc))
            elif user_settings_error is None:
                st.info("등록된 통합 즐겨찾기가 없습니다. 자주 이용하는 출퇴근 구간을 추가해 보세요!")
        else:
            st.info("🔒 카카오 로그인 후 나만의 즐겨찾기 및 알림 설정 목록을 확인하실 수 있습니다.")
    else:
        st.info("등록된 노선 데이터가 없습니다.")

# Render after the selectors, so OAuth always snapshots the current choices.
if login_links:
    state = prepare_login(st.session_state.get("oauth_state"), st.session_state)
    st.session_state["oauth_state"] = state
    login_url = "https://kauth.kakao.com/oauth/authorize?" + urlencode({
        "client_id": KAKAO_CLIENT_ID, "redirect_uri": KAKAO_REDIRECT_URI,
        "response_type": "code", "state": state,
    })
    login_html = f'<a href="{login_url}" target="_self" style="display:block;text-align:center;background:#FEE500;color:#000;padding:10px;border-radius:5px;text-decoration:none;font-weight:bold">💬 카카오계정으로 로그인</a>'
    for login_link in login_links:
        login_link.markdown(login_html, unsafe_allow_html=True)
