import os
import json
import re
import requests
from dotenv import load_dotenv
from google import genai
from datetime import datetime, timedelta
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

def get_weather_forecast_by_coords(lat, lon, stop_name="", trip_type="출근길"):
    nx, ny = latlon_to_grid(lat, lon)
    
    now = datetime.now()
    target_time = now - timedelta(minutes=45)
    base_date = target_time.strftime("%Y%m%d")
    hour = target_time.hour
    
    base_hours = [2, 5, 8, 11, 14, 17, 20, 23]
    valid_hour = 2
    if hour < 2:
        target_time = now - timedelta(days=1)
        base_date = target_time.strftime("%Y%m%d")
        valid_hour = 23
    else:
        for h in base_hours:
            if hour >= h:
                valid_hour = h
                
    base_time = f"{valid_hour:02d}00"
    
    url = "http://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getVilageFcst"
    decoded_service_key = urllib.parse.unquote(KMA_API_KEY) if KMA_API_KEY else ""

    params = {
        "serviceKey": decoded_service_key,
        "pageNo": "1",
        "numOfRows": "1000",
        "dataType": "JSON",
        "base_date": base_date,
        "base_time": base_time,
        "nx": nx,
        "ny": ny
    }
    
    print(f"[LOG KMA] 날씨 요청 시작 -> 정류장: {stop_name}, 좌표: ({lat}, {lon}) -> 격자: ({nx}, {ny})")
    print(f"[LOG KMA] API 발표 기준일시(base): {base_date} {base_time}")
    
    temp = "정보 없음"
    sky = "정보 없음"
    pop = "정보 없음"
    
    try:
        res = requests.get(url, params=params, timeout=5)
        if res.status_code == 200:
            res_json = res.json()
            response_body = res_json.get("response", {}).get("body", {})
            items = response_body.get("items", {}).get("item", [])
            
            # 시간별 데이터 딕셔너리로 구조화
            forecast_map = {}
            for item in items:
                f_date = item.get("fcstDate")
                f_time = item.get("fcstTime")
                category = item.get("category")
                value = item.get("fcstValue")
                
                key = (f_date, f_time)
                if key not in forecast_map:
                    forecast_map[key] = {}
                forecast_map[key][category] = value
            
            # 현재 시각 기준 매칭 키 생성 (정각 단위)
            target_fcst_date = now.strftime("%Y%m%d")
            target_fcst_time = f"{now.hour:02d}00"
            target_key = (target_fcst_date, target_fcst_time)
            
            print(f"[LOG KMA] 찾고자 하는 목표 예보 시각: {target_fcst_date} {target_fcst_time}")
            
            matched_data = None
            matched_time_str = ""
            
            if target_key in forecast_map:
                matched_data = forecast_map[target_key]
                matched_time_str = f"{target_fcst_date} {target_fcst_time}"
            else:
                # 정확히 일치하는 시간이 없으면 이후 시간대 중 가장 가까운 시간 탐색
                sorted_keys = sorted(forecast_map.keys())
                for k in sorted_keys:
                    if k[0] == target_fcst_date and k[1] >= target_fcst_time:
                        matched_data = forecast_map[k]
                        matched_time_str = f"{k[0]} {k[1]}"
                        break
                # 오늘 남은 시간 데이터가 없다면 오늘의 마지막 데이터 사용
                if not matched_data and sorted_keys:
                    today_keys = [k for k in sorted_keys if k[0] == target_fcst_date]
                    if today_keys:
                        matched_data = forecast_map[today_keys[-1]]
                        matched_time_str = f"{today_keys[-1][0]} {today_keys[-1][1]}"
            
            if matched_data:
                print(f"[LOG KMA] 최종 매칭된 예보 시각: {matched_time_str}")
                if "TMP" in matched_data:
                    temp = f"{matched_data['TMP']}°C"
                if "SKY" in matched_data:
                    sky_map = {"1": "맑음", "3": "구름많음", "4": "흐림"}
                    sky = sky_map.get(str(matched_data['SKY']), "정보 없음")
                if "POP" in matched_data:
                    pop = f"{matched_data['POP']}%"
        else:
            print(f"[LOG ERROR KMA] API 호출 실패 (Status Code: {res.status_code})")
    except Exception as e:
        print(f"[LOG ERROR KMA] 요청 중 예외 발생: {type(e).__name__} - {str(e)}")
        
    print(f"[LOG KMA] 파싱 결과 -> 기온: {temp}, 하늘: {sky}, 강수확률: {pop}")
    
    if temp == "정보 없음":
        logger.warning("Weather unavailable for grid %s,%s", nx, ny)
        return {
            "available": False,
            "temperature": "정보 없음", "sky_status": "정보 없음",
            "rain_probability": "정보 없음",
            "calculated_grid": f"격자 좌표: NX={nx}, NY={ny}",
            "message": "날씨 정보를 불러오지 못했습니다. 잠시 후 다시 조회해 주세요.",
        }

    ai_message = f"{stop_name} 정류장 주변 {trip_type} 날씨입니다. 안전한 이동 되세요!"
    if GEMINI_API_KEY and client:
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
                f"위 내용을 바탕으로 친근하고 자연스러운 한두 줄의 짧은 코멘트를 작성해주세요. 사용자에게 보낼 단 하나의 자연스럽고 센스 있는 AI 코멘트 문장만 생성해."
            )
            
            models_to_try = ["gemini-3.6-flash"]
            for m_name in models_to_try:
                try:
                    response = client.models.generate_content(
                        model=m_name,
                        contents=prompt
                    )
                    if response and response.text:
                        ai_message = response.text.strip()
                        break
                except Exception as exc:
                    logger.warning("Weather comment generation failed: %s", type(exc).__name__)
                    continue
        except Exception:
            pass

    return {
        "available": True,
        "temperature": temp,
        "sky_status": sky,
        "rain_probability": pop,
        "calculated_grid": f"격자 좌표: NX={nx}, NY={ny}",
        "message": ai_message
    }
