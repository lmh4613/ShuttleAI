import os
import json
import re
import requests
from dotenv import load_dotenv
from google import genai
from datetime import datetime, timedelta, timezone
import urllib.parse
import logging

logger = logging.getLogger(__name__)

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
KMA_API_KEY = os.getenv("KMA_API_KEY")

client = None
if GEMINI_API_KEY:
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
    except Exception as e:
        print(f"[LOG ERROR] google.genai 클라이언트 초기화 실패: {e}")

DB_FILE = "routes_db.json"

def latlon_to_grid(lat, lon):
    """위경도 좌표를 기상청 격자 좌표(NX, NY)로 변환합니다."""
    import math
    RE = 6371.00877  
    GRID = 5.0       
    SLAT1 = 30.0     
    SLAT2 = 60.0     
    OLON = 126.0     
    OLAT = 38.0      
    XO = 43          
    YO = 136         

    DEGRAD = math.pi / 180.0
    re = RE / GRID
    slat1 = SLAT1 * DEGRAD
    slat2 = SLAT2 * DEGRAD
    olon = OLON * DEGRAD
    olat = OLAT * DEGRAD

    sn = math.tan(math.pi * 0.25 + slat2 * 0.5) / math.tan(math.pi * 0.25 + slat1 * 0.5)
    sn = math.log(math.cos(slat1) / math.cos(slat2)) / math.log(sn)
    sf = math.tan(math.pi * 0.25 + slat1 * 0.5)
    sf = math.pow(sf, sn) * math.cos(slat1) / sn
    ro = math.tan(math.pi * 0.25 + olat * 0.5)
    ro = re * sf / math.pow(ro, sn)

    ra = math.tan(math.pi * 0.25 + (lat) * DEGRAD * 0.5)
    ra = re * sf / math.pow(ra, sn)
    theta = lon * DEGRAD - olon
    if theta > math.pi:
        theta -= 2.0 * math.pi
    if theta < -math.pi:
        theta += 2.0 * math.pi
    theta *= sn
    x = math.floor(ra * math.sin(theta) + XO + 0.5)
    y = math.floor(ro - ra * math.cos(theta) + YO + 0.5)
    return int(x), int(y)

def load_routes_from_db():
    """저장된 노선 및 정류장 위치 DB를 불러옵니다."""
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[LOG] DB 로드 중 오류 발생: {e}")
            return []
    return []

def get_coordinates_by_gemini(stop_name):
    """정류장 이름으로 위도/경도를 찾고 최신 google.genai 패키지와 상세 에러 로그를 출력합니다."""
    print(f"[LOG] 지오코딩 요청 - 정류장: '{stop_name}'")
    if not GEMINI_API_KEY or not client:
        print("[LOG ERROR] GEMINI_API_KEY가 설정되어 있지 않거나 클라이언트가 초기화되지 않았습니다.")
        return (37.3947, 127.1111)
        
    prompt = f"""
너는 대한민국 지리/위치 정보 전문가야. 아래 정류장 이름과 주소를 참고하여, 대한민국 내 실제 위치의 위도(latitude)와 경도(longitude) 소수점 좌표를 찾아내 줘.
- 정류장 이름: {stop_name}
- 위치 설명: 대한민국 내 셔틀버스 정류장

[필수 규칙]
반드시 아래 형태의 JSON 객체 딱 하나만 출력할 것. 마크다운 백틱 없이 순수 JSON만 출력하세요.
{{"lat": 위도숫자, "lon": 경도숫자}}
"""
    models_to_try = ["gemini-3.6-flash"]
    
    for m_name in models_to_try:
        try:
            print(f"[LOG] 모델 시도 중: {m_name}")
            response = client.models.generate_content(
                model=m_name,
                contents=prompt,
            )
            text = response.text.strip()
            print(f"[LOG] 모델({m_name}) 응답 수신 완료. 원본 텍스트: {text}")
            
            match = re.search(r'\{.*\}', text, re.DOTALL)
            if match:
                json_str = match.group(0)
                data = json.loads(json_str)
                lat = float(data.get("lat", 0))
                lon = float(data.get("lon", 0))
                
                print(f"[LOG] 파싱된 좌표 -> 위도: {lat}, 경도: {lon}")
                if 33.0 <= lat <= 43.0 and 124.0 <= lon <= 132.0:
                    print(f"[LOG] 유효 범위 내 좌표 확인 완료!")
                    return lat, lon
                else:
                    print(f"[LOG WARNING] 좌표가 대한민국 유효 범위를 벗어났습니다. (Lat: {lat}, Lon: {lon})")
            else:
                print(f"[LOG WARNING] 응답 텍스트에서 JSON 형식을 찾지 못했습니다.")
                
        except Exception as e:
            print(f"[LOG ERROR] 모델({m_name}) 호출/파싱 중 예외 발생: {type(e).__name__} - {str(e)}")
            continue
            
    print(f"[LOG] 모든 Gemini 시도 실패. 기본 좌표(판교) 반환.")
    return (37.3947, 127.1111)

def update_single_route_coordinate(target_stop_name, target_route_name):
    """특정 정류장 단건의 좌표를 다시 계산하여 DB를 업데이트합니다."""
    print(f"[LOG] 단건 좌표 재조회 -> 노선: {target_route_name}, 정류장: {target_stop_name}")
    db_data = load_routes_from_db()
    if not db_data:
        return False, "저장된 DB 데이터가 없습니다."
    
    new_lat, new_lon = get_coordinates_by_gemini(target_stop_name)
    
    if new_lat == 37.3947 and new_lon == 127.1111:
        return False, f"'{target_stop_name}' 지오코딩(Gemini) 실패로 인해 기본 좌표(판교)가 반환되어 업데이트가 취소되었습니다."
    
    new_nx, new_ny = latlon_to_grid(new_lat, new_lon)
    
    updated = False
    for item in db_data:
        if item.get("stop_name") == target_stop_name and item.get("route_name") == target_route_name:
            item["lat"] = new_lat
            item["lon"] = new_lon
            item["nx"] = new_nx
            item["ny"] = new_ny
            item["status"] = "✅ 정상 (단건 재조회)"
            updated = True
            
    if updated:
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(db_data, f, ensure_ascii=False, indent=4)
        print(f"[LOG] 단건 DB 업데이트 성공.")
        return True, f"'{target_stop_name}' 정류장 좌표가 성공적으로 재조회되었습니다! (위도: {new_lat}, 경도: {new_lon})"
    else:
        return False, "해당 정류장을 DB에서 찾지 못했습니다."

def save_routes_with_sequential_geocoding(parsed_data, target_region="gyeonggi", progress_callback=None):
    print(f"[LOG] 일괄 지오코딩 시작 [대상 지역: {target_region}] - 총 파싱 아이템 수: {len(parsed_data)}")
    
    unique_stops = {}
    for item in parsed_data:
        stop_name = item.get("stop_name", "").strip()
        if stop_name and stop_name not in unique_stops:
            unique_stops[stop_name] = True

    unique_stop_names = list(unique_stops.keys())
    total_unique = len(unique_stop_names)
    print(f"[LOG] 고유 정류장 수: {total_unique}개 (중복 제거됨)")
    
    calculated_coords = {}
    for idx, stop_name in enumerate(unique_stop_names):
        if progress_callback:
            progress_callback(idx + 1, total_unique, stop_name)
            
        lat, lon = get_coordinates_by_gemini(stop_name)
        nx, ny = latlon_to_grid(lat, lon)
        
        calculated_coords[stop_name] = {
            "lat": lat,
            "lon": lon,
            "nx": nx,
            "ny": ny
        }

    new_results = []
    for item in parsed_data:
        stop_name = item.get("stop_name", "").strip()
        coord_info = calculated_coords.get(stop_name, {"lat": 37.3947, "lon": 127.1111, "nx": 60, "ny": 127})
        
        new_results.append({
            "route_name": item.get("route_name", "기본 노선"),
            "stop_name": stop_name,
            "arrival_time": item.get("arrival_time", "08:00"),
            "region": target_region,  
            "lat": coord_info["lat"],
            "lon": coord_info["lon"],
            "nx": coord_info["nx"],
            "ny": coord_info["ny"],
            "status": "✅ 정상"
        })
    
    existing_data = load_routes_from_db()

    preserved_data = [
        item for item in existing_data 
        if item.get("region", "gyeonggi") != target_region
    ]
    
    final_results = preserved_data + new_results
    
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(final_results, f, ensure_ascii=False, indent=4)
        
    print(f"[LOG] 지역별 병합 저장 완료. 총 데이터 수: {len(final_results)}개")
    return final_results

# All timetable/API times are Korea Standard Time, including on Windows.
KST = timezone(timedelta(hours=9))


def korea_now():
    return datetime.now(KST)


def as_kst(value):
    return value.replace(tzinfo=KST) if value.tzinfo is None else value.astimezone(KST)


def resolve_boarding_datetime(board_time, now=None):
    """Next occurrence of an existing HH:MM timetable entry; never invent a time.

    A time in the current minute still means today's departure. Past times mean
    tomorrow, not the next working day (the route data has no service calendar).
    """
    now = as_kst(now or korea_now())
    if not isinstance(board_time, str) or not re.fullmatch(r"\s*\d{1,2}\s*:\s*\d{2}\s*", board_time):
        return None
    try:
        hour, minute = map(int, board_time.split(":"))
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    except ValueError:
        return None
    if target < now.replace(second=0, microsecond=0):
        target += timedelta(days=1)
    return target


def forecast_base(now, source):
    now = as_kst(now)
    if source == "UltraSrtFcst":
        # Official getUltraSrtFcst guide: HH:30 production, available HH:45.
        base = now.replace(minute=30, second=0, microsecond=0)
        return base if now.minute >= 45 else base - timedelta(hours=1)
    # Official getVilageFcst guide: 02/05/.../23, available ten minutes later.
    available = now - timedelta(minutes=10)
    base = available.replace(minute=0, second=0, microsecond=0)
    while base.hour not in (2, 5, 8, 11, 14, 17, 20, 23):
        base -= timedelta(hours=1)
    return base


def _request_forecast(source, base, nx, ny):
    params = {
        "serviceKey": urllib.parse.unquote(KMA_API_KEY or ""),
        "pageNo": "1", "numOfRows": "1000", "dataType": "JSON",
        "base_date": base.strftime("%Y%m%d"), "base_time": base.strftime("%H%M"),
        "nx": nx, "ny": ny,
    }
    try:
        res = requests.get(
            f"https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/get{source}",
            params=params, timeout=8)
        if res.status_code != 200:
            print(f"[WEATHER] source={source} HTTP={res.status_code}")
            return {}
        response = res.json().get("response", {})
        code = response.get("header", {}).get("resultCode")
        if code != "00":
            # Do not print response bodies or exceptions containing serviceKey URLs.
            print(f"[WEATHER] source={source} invalid_or_error_response")
            return {}
        items = response.get("body", {}).get("items", {}).get("item", [])
        forecast = {}
        for item in items:
            try:
                stamp = datetime.strptime(item["fcstDate"] + item["fcstTime"], "%Y%m%d%H%M").replace(tzinfo=KST)
                forecast.setdefault(stamp, {})[item["category"]] = item["fcstValue"]
            except (KeyError, TypeError, ValueError):
                continue
        return forecast
    except Exception as exc:
        print(f"[WEATHER] source={source} request_failed={type(exc).__name__}")
        return {}


def _number(value):
    try:
        number = float(value)
        return number if -100 < number < 1000 else None
    except (TypeError, ValueError):
        return None


def _has_precipitation(values):
    pty = _number(values.get("PTY"))
    if pty in (1, 2, 3, 4, 5, 6, 7):
        return True
    for key in ("RN1", "PCP"):
        amount = str(values.get(key, ""))
        if amount in ("", "강수없음", "없음", "정보 없음"):
            continue
        match = re.search(r"[-+]?\d+(?:\.\d+)?", amount)
        if match and float(match.group()) > 0:
            return True
    pop = _number(values.get("POP"))
    # Probability alone is a possibility, never a claim that precipitation occurs.
    return pop is not None and 60 <= pop <= 100


def precipitation_note(forecast, target_hour, source, boarding_target=None):
    end = (boarding_target or target_hour) + timedelta(hours=2) if source == "UltraSrtFcst" else target_hour
    window = [{"time": stamp.strftime("%Y-%m-%d %H:%M"),
               **{k: row[k] for k in ("PTY", "RN1", "POP", "PCP") if k in row}}
              for stamp, row in sorted(forecast.items()) if target_hour <= stamp <= end]
    if _has_precipitation(forecast.get(target_hour, {})):
        note = "탑승 시간대 강수 가능성이 있으니 우산을 챙기세요."
    elif any(_has_precipitation(row) for stamp, row in forecast.items() if target_hour < stamp <= end):
        note = "탑승 후 1~2시간 이내 강수 가능성이 있으니 우산을 챙기는 것이 좋겠습니다."
    else:
        note = ""
    return note, window


def _unavailable(message, target=None):
    return {"available": False, "temperature": "정보 없음", "sky_status": "정보 없음",
            "rain_probability": "정보 없음", "message": message,
            "target_time": target.isoformat() if target else None, "precipitation_note": ""}


def get_weather_forecast_by_coords(lat, lon, stop_name="", trip_type="출근길",
                                   target_datetime=None, location="boarding", now=None):
    now = as_kst(now or korea_now())
    if not isinstance(target_datetime, datetime):
        return _unavailable("탑승 예정시간이 등록되어 있지 않아 날씨 정보를 불러오지 못했습니다.")
    target = as_kst(target_datetime)
    target_hour = (target + timedelta(minutes=30)).replace(minute=0, second=0, microsecond=0)
    try:
        nx, ny = latlon_to_grid(float(lat), float(lon))
    except (TypeError, ValueError, OverflowError):
        return _unavailable("정류장 위치를 확인할 수 없어 날씨 정보를 불러오지 못했습니다.", target)
    source = "UltraSrtFcst" if timedelta(minutes=-1) < target - now <= timedelta(hours=4) else "VilageFcst"
    base = forecast_base(now, source)
    forecast = _request_forecast(source, base, nx, ny)

    def usable(rows, kind):
        row = rows.get(target_hour, {})
        return _number(row.get("T1H" if kind == "UltraSrtFcst" else "TMP")) is not None and str(row.get("SKY")) in ("1", "3", "4")

    if source == "UltraSrtFcst" and not usable(forecast, source):
        print("[WEATHER] UltraSrtFcst target unavailable -> fallback to VilageFcst")
        source = "VilageFcst"
        base = forecast_base(now, source)
        forecast = _request_forecast(source, base, nx, ny)
    if source == "VilageFcst" and not usable(forecast, source):
        # A just-published forecast can start after the current boarding hour.
        # Try only the immediately preceding release; never substitute another hour.
        base -= timedelta(hours=3)
        forecast = _request_forecast(source, base, nx, ny)
    if not usable(forecast, source):
        print(f"[WEATHER] location={location} target={target:%Y-%m-%d %H:%M} unavailable")
        return _unavailable("탑승 예정시간의 날씨 정보를 불러오지 못했습니다. 잠시 후 다시 조회해 주세요.", target)
    row = forecast[target_hour]
    temp = f"{_number(row.get('T1H' if source == 'UltraSrtFcst' else 'TMP')):g}°C"
    sky = {"1": "맑음", "3": "구름많음", "4": "흐림"}[str(row["SKY"])]
    pty = _number(row.get("PTY"))
    sky = {1: "비", 2: "비/눈", 3: "눈", 4: "소나기", 5: "빗방울", 6: "빗방울/눈날림", 7: "눈날림"}.get(pty, sky)
    pop_value = _number(row.get("POP"))
    pop = f"{pop_value:g}%" if pop_value is not None and 0 <= pop_value <= 100 else "정보 없음"
    rain_note, rain_window = precipitation_note(forecast, target_hour, source, target)
    print(f"[WEATHER] location={location} target={target:%Y-%m-%d %H:%M} source={source} "
          f"base={base:%Y-%m-%d %H:%M} selected={target_hour:%Y-%m-%d %H:%M} "
          f"temperature={temp} precipitation_slots={len(rain_window)}")
    # Preserve actual precipitation fields for deterministic preparation advice.
    wet_slots = [slot for slot in rain_window if _has_precipitation(slot)]
    selected_time = target_hour.strftime("%Y-%m-%d %H:%M")
    direct = lambda slot: _has_precipitation({k: v for k, v in slot.items() if k != "POP"})
    if any(slot['time'] == selected_time and direct(slot) for slot in wet_slots):
        rain_status = "boarding"
    elif any(slot['time'] != selected_time and direct(slot) for slot in wet_slots):
        rain_status = "during_trip"
    elif wet_slots:
        rain_status = "possible"
    else:
        rain_status = "none"
    relevant = [slot for slot in wet_slots if direct(slot) and
                (slot['time'] == selected_time if rain_status == 'boarding' else slot['time'] != selected_time)]
    kinds = set()
    for slot in relevant:
        pty_value = _number(slot.get('PTY'))
        if pty_value in (2, 6):
            kinds.update(('rain', 'snow'))
        elif pty_value in (3, 7):
            kinds.add('snow')
        elif pty_value in (1, 4, 5) or _has_precipitation({'RN1': slot.get('RN1')}):
            kinds.add('rain')
        else:
            kinds.add('unknown')
    rain_type = ('mixed' if {'rain', 'snow'} <= kinds else
                 next(iter(kinds)) if len(kinds) == 1 else 'unknown')
    if rain_status == 'possible':
        rain_type = 'rain'  # POP-only wording stays explicitly probabilistic.
    # Weather commentary no longer needs a model call; coordinate lookup still uses Gemini.
    ai_message = ""
    return {"available": True, "temperature": temp, "sky_status": sky, "rain_probability": pop,
            "calculated_grid": f"격자 좌표: NX={nx}, NY={ny}", "message": ai_message,
            "target_time": target.isoformat(), "precipitation_note": rain_note,
            "precipitation_status": rain_status, "precipitation_window": rain_window,
            "precipitation_type": rain_type,
            "precipitation_certainty": "forecast" if relevant else "possible"}
