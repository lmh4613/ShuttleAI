import streamlit as st
import pandas as pd
import requests
import os
import json
import re
import threading
import time
from datetime import datetime, timedelta
import weather_api
import ppt_parser
from dotenv import load_dotenv

load_dotenv()

st.set_page_config(page_title="AI 셔틀버스 날씨 알림", page_icon="🚌", layout="wide")

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
        # 구버전 리스트 형식 호환 및 기본 설정 추가
        return {
            "settings": user_entry, 
            "access_token": "", 
            "refresh_token": "",
            "notification_config": {
                "active_days": ["월", "화", "수", "목", "금"],
                "exclude_holidays": True
            }
        }
    if "notification_config" not in user_entry:
        user_entry["notification_config"] = {
            "active_days": ["월", "화", "수", "목", "금"],
            "exclude_holidays": True
        }
    return user_entry

def save_user_data(user_id, settings_list=None, access_token=None, refresh_token=None, notification_config=None):
    all_data = load_user_settings()
    user_entry = get_user_data(user_id)
    if settings_list is not None:
        user_entry["settings"] = settings_list
    if access_token is not None:
        user_entry["access_token"] = access_token
    if refresh_token is not None:
        user_entry["refresh_token"] = refresh_token
    if notification_config is not None:
        user_entry["notification_config"] = notification_config
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
    res = requests.post(token_url, data=payload)
    if res.status_code == 200:
        data = res.json()
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
    response = requests.post(talk_url, headers=headers, data=payload)
    return response.status_code, response.json()

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
    match = re.search(r'[-+]?\d*\.\d+|\d+', str(temp_str))
    return float(match.group()) if match else 0.0

# 탑승·하차 날씨 비교에 따른 통합 AI 멘트 생성 헬퍼 함수
def get_integrated_ai_message(wb, wa, board_name, arrive_name):
    t1, t2 = wb.get("temperature", ""), wa.get("temperature", "")
    s1, s2 = wb.get("sky_status", ""), wa.get("sky_status", "")
    msg1, msg2 = wb.get("message", ""), wa.get("message", "")
    
    temp1 = parse_temp(t1)
    temp2 = parse_temp(t2)
    temp_diff = abs(temp1 - temp2)
    
    is_similar_temp = temp_diff <= 2
    is_same_sky = (s1 == s2)
    
    if is_similar_temp and is_same_sky:
        return f"탑승지({board_name})와 하차지({arrive_name})의 날씨({s1})가 같고 기온 차이도 적어(탑승지 {t1}, 하차지 {t2}) 비슷한 기상 환경입니다. {msg1}"
    else:
        diff_desc = []
        if not is_similar_temp:
            if temp1 > temp2:
                diff_desc.append(f"하차지 기온({t2})이 탑승지({t1})보다 약 {temp_diff:.0f}°C 낮습니다.")
            else:
                diff_desc.append(f"하차지 기온({t2})이 탑승지({t1})보다 약 {temp_diff:.0f}°C 높습니다.")
        if not is_same_sky:
            diff_desc.append(f"하늘 상태가 탑승지({s1})와 하차지({s2})로 다릅니다.")
        
        rain_keywords = ["비", "강수", "우산", "눈"]
        has_rain = any(k in s1 or k in s2 or k in msg1 or k in msg2 for k in rain_keywords)
        umbrella_tip = " 하차지에 비나 눈 소식이 있으니 우산을 챙기세요!" if has_rain else ""
        
        diff_text = " ".join(diff_desc)
        return (
            f"탑승지와 하차지의 기상 환경에 차이가 있습니다. {diff_text}{umbrella_tip}\n\n"
            f"• **{board_name}** ({t1}, {s1}): {msg1}\n"
            f"• **{arrive_name}** ({t2}, {s2}): {msg2}"
        )

# 백그라운드 자동 알림 스케줄러 워커
def notification_background_worker():
    sent_cache = {}
    weekday_map = {0: "월", 1: "화", 2: "수", 3: "목", 4: "금", 5: "토", 6: "일"}
    
    while True:
        try:
            now = datetime.now()
            current_date_str = now.strftime("%Y-%m-%d")
            current_time_str = now.strftime("%H:%M")
            current_weekday = weekday_map.get(now.weekday())
            
            all_data = load_user_settings()
            for uid, entry in all_data.items():
                if not isinstance(entry, dict):
                    continue
                
                notif_config = entry.get("notification_config", {"active_days": ["월", "화", "수", "목", "금"], "exclude_holidays": True})
                active_days = notif_config.get("active_days", ["월", "화", "수", "목", "금"])
                exclude_holidays = notif_config.get("exclude_holidays", True)
                
                if current_weekday not in active_days:
                    continue
                if exclude_holidays and is_today_holiday():
                    continue
                
                settings = entry.get("settings", [])
                access_token = entry.get("access_token")
                refresh_token = entry.get("refresh_token")
                
                for item in settings:
                    if not item.get("notify_enabled", False):
                        continue
                    board_time = item.get("board_time")
                    if not board_time or board_time == "-":
                        continue
                    notify_min = int(item.get("notify_min", 10))
                    
                    try:
                        bt_dt = datetime.strptime(board_time, "%H:%M")
                        target_dt = datetime(now.year, now.month, now.day, bt_dt.hour, bt_dt.minute) - timedelta(minutes=notify_min)
                        target_time_str = target_dt.strftime("%H:%M")
                        
                        cache_key = f"{uid}_{item.get('route_name')}_{item.get('board_stop')}_{current_date_str}"
                        if current_time_str == target_time_str and cache_key not in sent_cache:
                            sent_cache[cache_key] = True
                            
                            if not access_token and refresh_token:
                                new_at, new_rt = refresh_kakao_token(refresh_token)
                                if new_at:
                                    access_token = new_at
                                    if new_rt:
                                        refresh_token = new_rt
                                    save_user_data(uid, None, access_token=access_token, refresh_token=refresh_token)
                            
                            if access_token:
                                b_lat = float(item.get('board_lat', 37.3947))
                                b_lon = float(item.get('board_lon', 127.1111))
                                a_lat = float(item.get('arrive_lat', 37.3947))
                                a_lon = float(item.get('arrive_lon', 127.1111))
                                b_name = item.get('board_stop')
                                a_name = item.get('arrive_stop')
                                t_type = item.get('trip_type', '출근길')
                                r_name = item.get('route_name')
                                
                                wb = weather_api.get_weather_forecast_by_coords(b_lat, b_lon, stop_name=b_name, trip_type=t_type)
                                wa = weather_api.get_weather_forecast_by_coords(a_lat, a_lon, stop_name=a_name, trip_type=t_type)
                                
                                integrated_ai_text = get_integrated_ai_message(wb, wa, b_name, a_name)
                                is_l = "퇴근" in str(r_name)
                                arrive_time_str = "" if is_l else (f" ({item.get('arrive_time')})" if item.get('arrive_time') and item.get('arrive_time') != '-' else "")
                                
                                desc = (
                                    f"🚍 [셔틀 출발 {notify_min}분 전 알림]\n\n"
                                    f"노선: {r_name} ({t_type})\n\n"
                                    f"🟢 [탑승] {b_name} ({item.get('board_time')})\n"
                                    f"• 기온: {wb['temperature']} | 상태: {wb['sky_status']}\n\n"
                                    f"🔴 [하차] {a_name}{arrive_time_str}\n"
                                    f"• 기온: {wa['temperature']} | 상태: {wa['sky_status']}\n\n"
                                    f"🤖 **[AI 코멘트]**\n{integrated_ai_text}"
                                )
                                send_kakao_memo(access_token, f"[{r_name}] 탑승·하차 날씨 알림", desc)
                    except Exception as ex:
                        print(f"Notification error: {ex}")
        except Exception as e:
            print(f"Background worker loop error: {e}")
        time.sleep(30)

@st.cache_resource
def start_background_scheduler():
    t = threading.Thread(target=notification_background_worker, daemon=True)
    t.start()
    return True

start_background_scheduler()

# 세션 상태 초기화
if "user_info" not in st.session_state:
    st.session_state["user_info"] = None
if "is_admin" not in st.session_state:
    st.session_state["is_admin"] = False

# 카카오 로그인 인가 코드 처리
query_params = st.query_params
if "code" in query_params and st.session_state["user_info"] is None:
    auth_code = query_params["code"]
    token_url = "https://kauth.kakao.com/oauth/token"
    payload = {
        "grant_type": "authorization_code",
        "client_id": KAKAO_CLIENT_ID,
        "redirect_uri": KAKAO_REDIRECT_URI,
        "code": auth_code
    }
    if KAKAO_CLIENT_SECRET:
        payload["client_secret"] = KAKAO_CLIENT_SECRET
    
    res = requests.post(token_url, data=payload)
    if res.status_code == 200:
        token_data = res.json()
        access_token = token_data.get("access_token")
        refresh_token = token_data.get("refresh_token")
        
        user_info_url = "https://kapi.kakao.com/v2/user/me"
        headers = {"Authorization": f"Bearer {access_token}"}
        user_res = requests.get(user_info_url, headers=headers)
        
        if user_res.status_code == 200:
            user_data = user_res.json()
            kakao_id = user_data.get("id")
            nickname = user_data.get("properties", {}).get("nickname", "사용자")
            
            st.session_state["user_info"] = {
                "id": kakao_id,
                "nickname": nickname,
                "access_token": access_token,
                "refresh_token": refresh_token
            }
            
            save_user_data(kakao_id, access_token=access_token, refresh_token=refresh_token)
            
            ADMIN_KAKAO_IDS = [5070327065]
            if kakao_id in ADMIN_KAKAO_IDS:
                st.session_state["is_admin"] = True
            
            st.query_params.clear()
            st.rerun()

# 사이드바: 로그인 및 관리자 인증
with st.sidebar:
    st.subheader("🔐 사용자 인증")
    if st.session_state["user_info"] is None and not st.session_state["is_admin"]:
        st.info("💡 카카오 로그인을 통해 즐겨찾기 및 알림 기능을 이용하세요.")
        kakao_login_url = f"https://kauth.kakao.com/oauth/authorize?client_id={KAKAO_CLIENT_ID}&redirect_uri={KAKAO_REDIRECT_URI}&response_type=code"
        st.markdown(f"""
        <a href="{kakao_login_url}" target="_self" style="display: block; text-align: center; background-color: #FEE500; color: #000000; padding: 10px; border-radius: 5px; text-decoration: none; font-weight: bold; margin-bottom: 10px;">
            💬 카카오계정으로 로그인
        </a>
        """, unsafe_allow_html=True)
        
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
            st.markdown("👑 **관리자 권한 활성화됨**")
        if st.button("로그아웃", width='stretch'):
            st.session_state["user_info"] = None
            st.session_state["is_admin"] = False
            st.query_params.clear()
            st.rerun()

st.title("🚌 AI 셔틀버스 날씨 알림 (탑승·하차 통합 안내)")

db_data = weather_api.load_routes_from_db()
sorted_db_data = sorted(
    db_data,
    key=lambda x: x.get('region', 'gyeonggi')
)

if st.session_state["is_admin"]:
    tab1, tab2 = st.tabs(["👑 [어드민] 노선 관리 및 그리드 편집", "🌤️ 셔틀버스 탑승·하차 통합 날씨 및 즐겨찾기"])
else:
    tab1 = None
    tab2 = st.tabs(["🌤️ 셔틀버스 탑승·하차 통합 날씨 및 즐겨찾기"])[0]

if tab1 is not None:
    with tab1:
        st.header("📋 지역별 셔틀버스 노선 문서 업로드 및 관리")
        up_tab_gy, up_tab_se = st.tabs(["🟢 경기 지역 업로드", "🔵 서울 지역 업로드"])
        
        with up_tab_gy:
            file_gy = st.file_uploader("경기 셔틀 노선 파일 선택 (PDF, PPTX)", type=["pdf", "pptx"], key="up_gy")
            if file_gy and st.button("경기 데이터 일괄 반영", type="primary"):
                try:
                    with st.spinner("Gemini AI가 경기 노선 문서를 분석 중입니다..."):
                        parsed = ppt_parser.parse_shuttle_document(file_gy)
                        bar = st.progress(0)
                        txt = st.empty()
                        weather_api.save_routes_with_sequential_geocoding(
                            parsed, target_region="gyeonggi", 
                            progress_callback=lambda c, t, s: (bar.progress(c/t), txt.text(f"지오코딩 중... ({c}/{t}): {s}"))
                        )
                    st.success("경기 노선 반영 완료!")
                    st.rerun()
                except Exception as e:
                    st.error(f"오류 발생: {e}")

        with up_tab_se:
            file_se = st.file_uploader("서울 셔틀 노선 파일 선택 (PDF, PPTX)", type=["pdf", "pptx"], key="up_se")
            if file_se and st.button("서울 데이터 일괄 반영", type="primary"):
                try:
                    with st.spinner("Gemini AI가 서울 노선 문서를 분석 중입니다..."):
                        parsed = ppt_parser.parse_shuttle_document(file_se)
                        bar = st.progress(0)
                        txt = st.empty()
                        weather_api.save_routes_with_sequential_geocoding(
                            parsed, target_region="seoul", 
                            progress_callback=lambda c, t, s: (bar.progress(c/t), txt.text(f"지오코딩 중... ({c}/{t}): {s}"))
                        )
                    st.success("서울 노선 반영 완료!")
                    st.rerun()
                except Exception as e:
                    st.error(f"오류 발생: {e}")

        st.divider()
        st.subheader("📍 특정 정류장 좌표 단건 재계산")
        if sorted_db_data:
            c1, c2, c3 = st.columns(3)
            with c1:
                admin_regions = sorted(list(set(i.get('region', 'gyeonggi') for i in sorted_db_data)))
                reg_map_admin = {"gyeonggi": "경기", "seoul": "서울"}
                sel_admin_region = st.selectbox("지역 선택", admin_regions, format_func=lambda x: reg_map_admin.get(x, x), key="admin_reg")
            
            reg_filtered_admin = [i for i in sorted_db_data if i.get('region', 'gyeonggi') == sel_admin_region]
            
            with c2:
                admin_routes = list(dict.fromkeys(i.get('route_name') for i in reg_filtered_admin if i.get('route_name')))
                sel_rt = st.selectbox("노선 선택", admin_routes if admin_routes else ["노선 없음"], key="admin_rt")
            
            route_filtered_admin = [i for i in reg_filtered_admin if i.get('route_name') == sel_rt]
            
            with c3:
                admin_stops = [i.get('stop_name') for i in route_filtered_admin]
                sel_st = st.selectbox("정류장 선택", admin_stops if admin_stops else ["정류장 없음"], key="admin_st")
            
            if st.button("🎯 선택 정류장 좌표 재계산 및 갱신", type="primary"):
                with st.spinner("카카오 지도 API 및 Gemini 격자 변환 처리 중..."):
                    print(f"[LOG] 단건 좌표 재계산 요청 시작 -> 노선: {sel_rt}, 정류장: {sel_st}")
                    try:
                        succ, msg = weather_api.update_single_route_coordinate(sel_st, sel_rt)
                        print(f"[LOG] 단건 좌표 재계산 응답 결과 -> 성공 여부: {succ}, 메시지: {msg}")
                    except Exception as e:
                        succ = False
                        msg = f"예외 발생 (Exception): {str(e)}"
                        print(f"[LOG ERROR] update_single_route_coordinate 실행 중 예외 발생: {e}")
                
                if succ:
                    st.success(f"✅ 수정 완료: {msg}")
                    st.toast("정류장 좌표가 성공적으로 재계산 및 수정되었습니다!", icon="🎯")
                    st.rerun()
                else:
                    st.error(f"❌ 수정 실패: {msg}")
                    with st.expander("🔍 Gemini 호출 및 지오코딩 실패 원인 상세 확인"):
                        st.markdown(f"- **대상 노선:** `{sel_rt}`")
                        st.markdown(f"- **대상 정류장:** `{sel_st}`")
                        st.markdown(f"- **반환된 메시지/에러:** `{msg}`")
                        st.info("💡 **확인 사항:** `weather_api.py` 내부의 Gemini API 호출 함수에서 API Key 인증 오류, 할당량 초과, 또는 모델명 설정 문제로 인해 예외가 발생하고 기본값(판교)으로 빠지고 있는지 확인이 필요합니다.")                    

        st.divider()
        st.subheader("📝 노선 및 정류장 데이터 직접 편집 그리드")
        if sorted_db_data:
            df_routes = pd.DataFrame(sorted_db_data)
            edited_df = st.data_editor(df_routes, num_rows="dynamic", width='stretch', key="route_grid_editor", height=400)
            if st.button("💾 그리드 변경사항 저장", type="primary", width='stretch'):
                try:
                    updated_records = edited_df.to_dict(orient="records")
                    if hasattr(weather_api, 'save_all_routes'):
                        weather_api.save_all_routes(updated_records)
                    else:
                        db_file = getattr(weather_api, 'DB_FILE', 'routes_db.json')
                        with open(db_file, "w", encoding="utf-8") as f:
                            json.dump(updated_records, f, ensure_ascii=False, indent=4)
                    st.success("✅ 변경사항이 저장되었습니다!")
                    st.rerun()
                except Exception as e:
                    st.error(f"❌ 저장 오류: {e}")

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
            sel_route = st.selectbox("노선 선택", routes if routes else ["노선 없음"], key="user_rt")
        
        route_stops = [i for i in reg_filtered if i.get('route_name') == sel_route]
        stops = [i.get('stop_name') for i in route_stops] if route_stops else []
        
        is_leave = "퇴근" in str(sel_route)
        trip_type = "퇴근길" if is_leave else "출근길"
        
        st.markdown("---")
        col_s1, col_s2 = st.columns(2)
        with col_s1:
            st.markdown("🟢 **[1] 내 탑승 정류장 선택**")
            sel_board_stop = st.selectbox("탑승 정류장", stops if stops else ["정류장 없음"], key="user_board_st")
        
        with col_s2:
            st.markdown("🔴 **[2] 내 하차(도착) 정류장 선택**")
            if is_leave:
                default_arrive_idx = len(stops) - 1 if len(stops) > 1 else 0
                sel_arrive_stop = st.selectbox("하차 정류장 (도착지)", stops if stops else ["정류장 없음"], index=default_arrive_idx, key="user_arrive_st")
            else:
                sel_arrive_stop = st.selectbox("하차 정류장 (도착지)", ["판교 제2테크노밸리"], key="user_arrive_st_fixed")
        
        board_row = next((i for i in route_stops if i.get('stop_name') == sel_board_stop), {})
        
        if is_leave:
            arrive_row = next((i for i in route_stops if i.get('stop_name') == sel_arrive_stop), {})
            arrive_lat = float(arrive_row.get('lat', 37.3947))
            arrive_lon = float(arrive_row.get('lon', 127.1111))
        else:
            PANGYO_2ND_LAT = 37.412605
            PANGYO_2ND_LON = 127.095703
            arrive_row = {
                'stop_name': "판교 제2테크노밸리",
                'lat': PANGYO_2ND_LAT,
                'lon': PANGYO_2ND_LON
            }
            arrive_lat = PANGYO_2ND_LAT
            arrive_lon = PANGYO_2ND_LON
        
        board_lat = float(board_row.get('lat', 37.3947))
        board_lon = float(board_row.get('lon', 127.1111))
        
        st.markdown("")
        b1, b2, b3 = st.columns(3)
        with b1:
            if st.button("🔍 탑승·하차 통합 날씨 조회", type="primary", width='stretch'):
                with st.spinner("탑승지와 하차지의 기상청 날씨 및 AI 통합 코멘트 생성 중..."):
                    w_board = weather_api.get_weather_forecast_by_coords(board_lat, board_lon, stop_name=sel_board_stop, trip_type=trip_type)
                    w_arrive = weather_api.get_weather_forecast_by_coords(arrive_lat, arrive_lon, stop_name=sel_arrive_stop, trip_type=trip_type)
                st.session_state['w_board'] = w_board
                st.session_state['w_arrive'] = w_arrive
                st.session_state['integrated_stop_key'] = f"{sel_route}_{sel_board_stop}_{sel_arrive_stop}"
        
        user_id = st.session_state["user_info"]["id"] if st.session_state["user_info"] else "guest"
        user_data_obj = get_user_data(user_id)
        user_settings = user_data_obj.get("settings", [])
        
        with b2:
            if st.session_state["user_info"]:
                if st.button("⭐ 통합 즐겨찾기 추가", width='stretch'):
                    new_item = {
                        "region": sel_region,
                        "route_name": sel_route,
                        "board_stop": sel_board_stop,
                        "arrive_stop": sel_arrive_stop,
                        "trip_type": trip_type,
                        "board_time": board_row.get('arrival_time', '08:00'),
                        "arrive_time": "-" if is_leave else arrive_row.get('arrival_time', '-'),
                        "board_lat": board_lat,
                        "board_lon": board_lon,
                        "arrive_lat": arrive_lat,
                        "arrive_lon": arrive_lon,
                        "notify_enabled": False,
                        "notify_min": 10
                    }
                    keys = [f"{i.get('route_name')}_{i.get('board_stop')}_{i.get('arrive_stop')}" for i in user_settings]
                    if f"{sel_route}_{sel_board_stop}_{sel_arrive_stop}" not in keys:
                        user_settings.append(new_item)
                        save_user_data(user_id, settings_list=user_settings)
                        st.success("통합 즐겨찾기에 추가되었습니다!")
                        st.rerun()
                    else:
                        st.warning("이미 등록된 구간입니다.")
            else:
                st.button("⭐ 즐겨찾기 (로그인필요)", width='stretch', disabled=True)

        with b3:
            if st.session_state["user_info"]:
                if st.button("💬 카카오톡 통합 날씨 전송", width='stretch'):
                    wb = st.session_state.get('w_board')
                    wa = st.session_state.get('w_arrive')
                    if not wb or st.session_state.get('integrated_stop_key') != f"{sel_route}_{sel_board_stop}_{sel_arrive_stop}":
                        wb = weather_api.get_weather_forecast_by_coords(board_lat, board_lon, stop_name=sel_board_stop, trip_type=trip_type)
                        wa = weather_api.get_weather_forecast_by_coords(arrive_lat, arrive_lon, stop_name=sel_arrive_stop, trip_type=trip_type)
                    
                    token = st.session_state["user_info"]["access_token"]
                    integrated_ai_text = get_integrated_ai_message(wb, wa, sel_board_stop, sel_arrive_stop)
                    arrive_time_str = "" if is_leave else (f" ({arrive_row.get('arrival_time', '-')})" if arrive_row.get('arrival_time') else "")
                    
                    desc = (
                        f"🚍 노선: {sel_route} ({trip_type})\n\n"
                        f"🟢 [탑승] {sel_board_stop} ({board_row.get('arrival_time', '-')})\n"
                        f"• 기온: {wb['temperature']} | 상태: {wb['sky_status']}\n\n"
                        f"🔴 [하차] {sel_arrive_stop}{arrive_time_str}\n"
                        f"• 기온: {wa['temperature']} | 상태: {wa['sky_status']}\n\n"
                        f"🤖 **[AI 코멘트]**\n{integrated_ai_text}"
                    )
                    code, res = send_kakao_memo(token, f"[{sel_route}] 탑승·하차 날씨 안내", desc)
                    if code == 200: 
                        st.success("카카오톡 통합 전송 완료!")
                        st.toast("카카오톡 나에게 톡메시지가 전송되었습니다.", icon="💬")
                    else: 
                        st.error(f"전송 실패 ({code})")
            else:
                st.button("💬 카카오톡 (로그인필요)", width='stretch', disabled=True)

        if 'w_board' in st.session_state and 'w_arrive' in st.session_state and st.session_state.get('integrated_stop_key') == f"{sel_route}_{sel_board_stop}_{sel_arrive_stop}":
            wb = st.session_state['w_board']
            wa = st.session_state['w_arrive']
            
            st.divider()
            st.markdown(f"### 📊 [{sel_route}] 탑승·하차 날씨 비교 리포트")
            
            res_col1, res_col2 = st.columns(2)
            with res_col1:
                st.markdown(f"#### 🟢 탑승지: {sel_board_stop}")
                st.caption(f"예정 시간: {board_row.get('arrival_time', '-')}")
                m1, m2 = st.columns(2)
                m1.metric("기온", wb["temperature"])
                m2.metric("하늘상태", wb["sky_status"])
            
            with res_col2:
                st.markdown(f"#### 🔴 하차지: {sel_arrive_stop}")
                if not is_leave:
                    st.caption("최종 목적지")
                m3, m4 = st.columns(2)
                m3.metric("기온", wa["temperature"])
                m4.metric("하늘상태", wa["sky_status"])
            
            st.markdown("")
            integrated_comment = get_integrated_ai_message(wb, wa, sel_board_stop, sel_arrive_stop)
            st.info(f"🤖 **통합 AI 코멘트**\n\n{integrated_comment}")

        st.divider()
        st.subheader("⭐ 내 통합 즐겨찾기 및 알림 설정 목록")
        if st.session_state["user_info"]:
            with st.expander("⚙️ 자동 알림 공통 조건 설정 (발송 요일 및 공휴일)", expanded=True):
                notif_config = user_data_obj.get("notification_config", {"active_days": ["월", "화", "수", "목", "금"], "exclude_holidays": True})
                current_active_days = notif_config.get("active_days", ["월", "화", "수", "목", "금"])
                current_exclude_holidays = notif_config.get("exclude_holidays", True)
                
                col_cfg1, col_cfg2 = st.columns([3, 2])
                with col_cfg1:
                    st.markdown("**알림 발송 요일 선택**")
                    all_days = ["월", "화", "수", "목", "금", "토", "일"]
                    selected_days = []
                    day_cols = st.columns(7)
                    for idx, day in enumerate(all_days):
                        with day_cols[idx]:
                            if st.checkbox(day, value=(day in current_active_days), key=f"day_chk_{day}"):
                                selected_days.append(day)
                with col_cfg2:
                    st.markdown("**휴일 설정**")
                    exclude_hols = st.checkbox("대한민국 공휴일 자동 제외", value=current_exclude_holidays, key="exclude_hols_chk")
                
                if st.button("💾 공통 알림 조건 저장", type="primary", width='stretch'):
                    new_config = {
                        "active_days": selected_days,
                        "exclude_holidays": exclude_hols
                    }
                    save_user_data(user_id, notification_config=new_config)
                    st.success("알림 조건이 저장되었습니다!")
                    st.rerun()

            st.markdown("")
            if user_settings:
                for idx, item in enumerate(user_settings):
                    with st.container(border=True):
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
                            
                            new_notify_enabled = st.checkbox("🔔 알림 받기", value=notify_enabled, key=f"notif_chk_{idx}")
                            
                            options_min = [10, 20, 30, 40, 50, 60]
                            current_idx = options_min.index(notify_min) if notify_min in options_min else 0
                            new_notify_min = st.selectbox("알림 시점", options_min, index=current_idx, format_func=lambda x: f"출발 {x}분 전", key=f"notif_min_{idx}")
                            
                            if st.button("💾 설정 저장", key=f"save_notif_{idx}", width='stretch'):
                                item["notify_enabled"] = new_notify_enabled
                                item["notify_min"] = new_notify_min
                                save_user_data(user_id, settings_list=user_settings)
                                st.success("알림 설정이 저장되었습니다!")
                                st.rerun()
                        with cols_fav[2]:
                            st.write("")
                            if st.button("🗑️ 삭제", key=f"del_fav_{idx}", width='stretch'):
                                user_settings.pop(idx)
                                save_user_data(user_id, settings_list=user_settings)
                                st.success("삭제되었습니다.")
                                st.rerun()
            else:
                st.info("등록된 통합 즐겨찾기가 없습니다. 자주 이용하는 출퇴근 구간을 추가해 보세요!")
        else:
            st.info("🔒 카카오 로그인 후 나만의 즐겨찾기 및 알림 설정 목록을 확인하실 수 있습니다.")
    else:
        st.info("등록된 노선 데이터가 없습니다.")