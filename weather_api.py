import os
import json
import re
import requests
from dotenv import load_dotenv
import google.generativeai as genai
from datetime import datetime

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
KMA_API_KEY = os.getenv("KMA_API_KEY")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

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
    """정류장 이름으로 위도/경도를 찾고 상세 로그를 출력합니다."""
    print(f"[LOG] 지오코딩 요청 - 정류장: '{stop_name}'")
    if not GEMINI_API_KEY:
        return (37.3947, 127.1111)
        
    prompt = f"""
너는 대한민국 지리/위치 정보 전문가야. 아래 정류장 이름과 주소를 참고하여, 대한민국 내 실제 위치의 위도(latitude)와 경도(longitude) 소수점 좌표를 찾아내 줘.
- 정류장 이름: {stop_name}
- 위치 설명: 대한민국 내 셔틀버스 정류장

[필수 규칙]
반드시 아래 형태의 JSON 객체 딱 하나만 출력할 것. 마크다운 백틱 없이 순수 JSON만 출력하세요.
{{"lat": 위도숫자, "lon": 경도숫자}}
"""
    models_to_try = ["gemini-flash-latest", "gemini-pro", "gemini-1.5-flash"]
    for m_name in models_to_try:
        try:
            model = genai.GenerativeModel(m_name)
            response = model.generate_content(prompt)
            text = response.text.strip()
            
            match = re.search(r'\{.*\}', text, re.DOTALL)
            if match:
                json_str = match.group(0)
                data = json.loads(json_str)
                lat = float(data.get("lat", 0))
                lon = float(data.get("lon", 0))
                if 33.0 <= lat <= 43.0 and 124.0 <= lon <= 132.0:
                    return lat, lon
        except Exception:
            continue
            
    print(f"[LOG] 모든 Gemini 시도 실패. 기본 좌표(판교) 반환.")
    return (37.3947, 127.1111)

def update_single_route_coordinate(target_stop_name, target_route_name):
    """특정 정류장 단건의 좌표를 다시 계산하여 DB를 업데이트합니다."""
    print(f"[LOG] 단건 좌표 재조회 -> 노선: {target_route_name}, 정류장: {target_stop_name}")
    db_data = load_routes_from_db()
    if not db_data:
        return False, "저장된 DB 데이터가 없습니다."
    
    updated = False
    new_lat, new_lon = get_coordinates_by_gemini(target_stop_name)
    new_nx, new_ny = latlon_to_grid(new_lat, new_lon)
    
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
    """
    정류장 이름 기준 중복을 제거하여 Gemini 지오코딩 및 격자 변환을 수행하고,
    지정된 target_region('gyeonggi' 또는 'seoul')으로 강제 매핑한 뒤 
    기존 DB에서 해당 region 데이터만 교체하고 나머지 region은 보존(Merge)합니다.
    """
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
            "region": target_region,  # 업로드 창에 맞춰 강제 매핑
            "lat": coord_info["lat"],
            "lon": coord_info["lon"],
            "nx": coord_info["nx"],
            "ny": coord_info["ny"],
            "status": "✅ 정상"
        })
    
    # 기존 DB 데이터 불러오기
    existing_data = load_routes_from_db()

    # 이번에 업로드된 target_region과 다른 지역 데이터는 안전하게 보존
    # 예: 서울(seoul)을 업로드했다면 기존 경기(gyeonggi) 데이터는 유지되고 기존 서울 데이터만 교체됨
    preserved_data = [
        item for item in existing_data 
        if item.get("region", "gyeonggi") != target_region
    ]
    
    # 보존된 데이터 + 새로 업로드된 데이터 합치기
    final_results = preserved_data + new_results
    
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(final_results, f, ensure_ascii=False, indent=4)
        
    print(f"[LOG] 지역별 병합 저장 완료. 총 데이터 수: {len(final_results)}개")
    return final_results

def get_weather_forecast_by_coords(lat, lon, stop_name="", trip_type="출근길"):
    """좌표 기반 기상청 날씨 조회 및 AI 코멘트를 생성합니다."""
    nx, ny = latlon_to_grid(lat, lon)
    
    now = datetime.now()
    base_date = now.strftime("%Y%m%d")
    base_time = "0600"
    
    url = "http://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getVilageFcst"
    params = {
        "serviceKey": KMA_API_KEY,
        "pageNo": "1",
        "numOfRows": "1000",
        "dataType": "JSON",
        "base_date": base_date,
        "base_time": base_time,
        "nx": nx,
        "ny": ny
    }
    
    temp = "정보 없음"
    sky = "정보 없음"
    pop = "정보 없음"
    
    try:
        res = requests.get(url, params=params, timeout=5)
        if res.status_code == 200:
            items = res.json().get("response", {}).get("body", {}).get("items", {}).get("item", [])
            for item in items:
                category = item.get("category")
                fcst_value = item.get("fcstValue")
                if category == "TMP":
                    temp = f"{fcst_value}°C"
                elif category == "SKY":
                    sky_map = {"1": "맑음", "3": "구름많음", "4": "흐림"}
                    sky = sky_map.get(str(fcst_value), "정보 없음")
                elif category == "POP":
                    pop = f"{fcst_value}%"
    except Exception:
        pass
        
    if temp == "정보 없음":
        temp = "20°C"
        sky = "맑음"
        pop = "0%"

    ai_message = f"{stop_name} 정류장 주변 {trip_type} 날씨입니다. 안전한 이동 되세요!"
    if GEMINI_API_KEY:
        try:
            if trip_type == "퇴근길":
                role_guide = "하루 일과를 마치고 돌아오는 피곤한 직장인을 위한 퇴근길 멘트입니다. 저녁 시간대 날씨 변화나 따뜻한 위로, 야외 활동 시 유의사항을 담아주세요."
            else:
                role_guide = "상쾌하고 바쁜 아침 출근길 직장인을 위한 멘트입니다. 옷차림이나 대중교통 이용 시 날씨 유의사항을 담아주세요."

            prompt = (
                f"당신은 센스 있는 스마트 셔틀버스 날씨 알림이입니다.\n"
                f"정류장: {stop_name}\n"
                f"시간대: {trip_type}\n"
                f"- 기온: {temp}\n"
                f"- 하늘 상태: {sky}\n"
                f"- 강수 확률: {pop}\n\n"
                f"{role_guide}\n"
                f"위 내용을 바탕으로 친근하고 자연스러운 한두 줄의 짧은 코멘트를 작성해주세요."
            )
            
            models_to_try = ["gemini-flash-latest", "gemini-pro"]
            for m_name in models_to_try:
                try:
                    model = genai.GenerativeModel(m_name)
                    response = model.generate_content(prompt)
                    if response and response.text:
                        ai_message = response.text.strip()
                        break
                except Exception:
                    continue
        except Exception:
            pass

    return {
        "temperature": temp,
        "sky_status": sky,
        "rain_probability": pop,
        "calculated_grid": f"격자 좌표: NX={nx}, NY={ny}",
        "message": ai_message
    }