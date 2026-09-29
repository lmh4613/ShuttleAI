"""Gemini wording only. Python remains the source of weather decisions and fallback."""
from collections import OrderedDict
import json
import logging
import re
import threading
import time

from weather_advice import assess_weather, render_advice

MODEL = 'gemini-3.6-flash'  # Reuse the model already used by weather_api.
PROMPT_VERSION = 2
SUCCESS_TTL = 5 * 60 * 60
FAILURE_TTL = 60
MAX_CACHE = 256
logger = logging.getLogger(__name__)
_cache = OrderedDict()
_lock = threading.RLock()


def get_client():
    # Reuse existing .env/key/client management without changing geocoding calls.
    import weather_api
    return weather_api.client


def comment_payload(situation, trip_type):
    """One semantic input contract shared by the prompt and production key."""
    fields = ('precipitation_status', 'precipitation_type', 'precipitation_certainty',
              'destination_precipitation', 'thermal_feel', 'meaningful_temperature_change',
              'temperature_direction', 'sky_summary', 'unavailable')
    assessment = {field: situation[field] for field in fields}
    if assessment['precipitation_status'] == 'none':
        assessment.update(precipitation_type='unknown', precipitation_certainty='possible',
                          destination_precipitation=False)
    if not assessment['meaningful_temperature_change']:
        assessment['temperature_direction'] = ''
    return {'trip_type': '퇴근길' if trip_type == '퇴근길' else '출근길',
            'assessment': assessment,
            'recommended_clothing': {'cold': '따뜻한 외투', 'cool': '얇은 겉옷',
                                     'mild': '편안한 옷차림', 'hot': '가볍고 시원한 옷차림'}.get(situation['thermal_feel'], '정보 없음')}


def comment_cache_key(situation, trip_type):
    return json.dumps([PROMPT_VERSION, MODEL, comment_payload(situation, trip_type)],
                      sort_keys=True, ensure_ascii=False)


def build_prompt(situation, trip_type):
    payload = comment_payload(situation, trip_type)
    return '''당신은 출퇴근 기상 안내 문장을 다듬는 편집자입니다. 날씨 판단은 이미 Python이 끝냈습니다.
아래 JSON 판정값을 변경하거나 원시 날씨를 다시 추정하지 마세요.
자연스러운 한국어 1~2문장, 최대 220자로 출퇴근 상황과 실제 준비 행동을 안내하세요.
짧고 자연스러운 기상 안내 방송 문체로 쓰고 과도한 수식어나 감정 표현을 피하세요.
'상쾌하게', '안전하게', '만약의 경우를 대비해', '꼭' 같은 불필요한 강조는 가급적 생략하세요.
'편안한 옷차림으로 편안하게'처럼 같은 의미의 표현을 반복하지 마세요.
'~하시기 바랍니다'보다 '~하세요', '~챙기세요', '~준비하세요'를 우선하세요.
출근길은 필요한 준비 행동을 짧게 안내하고, 퇴근길은 필요할 때만 '오늘 하루도 수고 많으셨습니다' 같은 인사를 사용하세요.
출근길/아침/하루를 시작하는 길, 퇴근길/귀갓길/편안한 귀가 등 맥락을 활용하되 인사를 강제하지 마세요.
정확한 기온 숫자, 정류장명, '탑승 예정 시간대에는', '기온 차이가 거의 없습니다'를 반복하지 마세요.
우선순위는 강수 > 의미 있는 기온 변화 > 복장 > 하늘상태입니다.
meaningful_temperature_change=false면 지역 간 기온차를 강조하지 마세요.
precipitation_type: rain=비, snow=눈, mixed=비와 눈 혼합, unknown=종류 미상의 강수입니다.
precipitation_certainty=forecast이면 '비/눈/강수가 예상됩니다'처럼 예보형으로 표현하세요.
possible이면 '내릴 가능성이 있습니다'를 사용하세요. 가능성을 확정 예보로, 명확한 예보를 가능성으로 바꾸지 마세요.
precipitation_status=none이면 강수나 우산 안내를 만들지 마세요.
boarding은 출발 시점, during_trip은 탑승 후 최대 2시간 범위의 예보입니다.
destination_precipitation=true이면 하차 지역 예보일 뿐 도착 시각 예보가 아닙니다.
정확한 도착/하차 시각, 이동시간, 전달되지 않은 강수시간을 추정하지 마세요.
두 지역은 같은 탑승시각 기준 예보이며 '이동하면서 기온이 오른다' 같은 시간 변화를 만들지 마세요.
바람/풍속/습도/건조/체감온도/날씨 원인/미세먼지 등 전달되지 않은 사실을 추가하지 마세요.
'현재 비가 오고 있습니다', '지금' 등 조회 시점의 실황 표현은 금지합니다.
sky_summary가 비어 있으면 하늘상태를 추정하지 마세요. 비/눈 종류도 주어진 값만 사용하세요.
출력은 마크다운 없이 {"comment": "최종 안내"} JSON 객체 하나만 반환하세요.
판정 입력:
''' + json.dumps(payload, ensure_ascii=False, sort_keys=True)


# Match precipitation nouns, not syllables in unrelated words such as 준비/대비.
RAIN_WORD = r'(?<![가-힣])비(?=가|는|도|를|와|나|의|에|로|만|\s|$)|빗방울|소나기|강우|우천'
SNOW_WORD = r'(?<![가-힣])눈(?=이|은|도|을|과|이나|의|에|으로|만|발|날림|\s|$)|강설'
PRECIPITATION_WORD = rf'{RAIN_WORD}|{SNOW_WORD}|진눈깨비|강수'
DESTINATION = r'하차\s*지역|하차지|도착지|목적지'
BOARDING_TIME = r'(?:출발|탑승)(?:할|하는)?\s*(?:때|시점|직전|부터)|(?:출발|탑승)\s*시(?:에|부터|\s)|탑승\s*전'


def comment_validation_error(text, situation):
    """Return a rule name, or None; conservative guard, not semantic inference."""
    if not isinstance(text, str) or not 10 <= len(text.strip()) <= 240:
        return 'response_length_or_type'
    if re.search(r'[\d{}\[\]`#\n]', text):
        return 'response_format'
    if re.search(r'현재|지금|바람|풍속|습도|건조|체감온도|기압|미세먼지|자외선|태풍|폭우|한파|탑승 예정 시간대|기온 차이가 거의', text):
        return 'unsupported_fact'
    if re.search(r'(?:도착|하차)(?:할|하는|\s*예정)?\s*(?:때|무렵|시간|시각|시점|직전|직후|후)|내릴\s*때|이동하면서', text):
        return 'arrival_time'
    sentences = [part.strip() for part in re.split(r'[.!?]+', text) if part.strip()]
    if not 1 <= len(sentences) <= 2:
        return 'sentence_count'
    mentions = list(re.finditer(PRECIPITATION_WORD, text))
    if situation['precipitation_status'] == 'none':
        if mentions or '우산' in text:
            return 'unexpected_precipitation'
    else:
        if not mentions:
            return 'precipitation_missing'
        has_rain = bool(re.search(RAIN_WORD, text)) or '진눈깨비' in text
        has_snow = bool(re.search(SNOW_WORD, text)) or '진눈깨비' in text
        kind = situation['precipitation_type']
        if ((kind == 'rain' and (not has_rain or has_snow)) or
            (kind == 'snow' and (not has_snow or has_rain)) or
            (kind == 'mixed' and not (has_rain and has_snow)) or
            (kind == 'unknown' and (has_rain or has_snow or '강수' not in text))):
            return 'precipitation_type'

        # Inspect weather claims separately from clothing/action clauses. For
        # example, "비가 예상되니 겉옷을 벗을 수 있습니다" is still a forecast.
        claims = []
        for sentence in sentences:
            if not re.search(PRECIPITATION_WORD, sentence):
                continue
            clauses = re.split(r'[,;]|(?<=되니)\s+|(?<=있으니)\s+|(?<=되지만)\s+|(?<=있지만)\s+|(?<=되며)\s+|(?<=있으며)\s+|(?<=되고)\s+|(?<=있고)\s+', sentence)
            for clause in clauses:
                if re.search(PRECIPITATION_WORD, clause):
                    claims.append(re.split(r'우산|외투|겉옷|옷차림|체온', clause, maxsplit=1)[0])
        for claim in claims:
            # "예보를 보면" describes the source, not precipitation certainty.
            # Keep the full claim for location/time validation below.
            mention = re.search(PRECIPITATION_WORD, claim)
            certainty_text = claim[mention.start():] if mention else claim
            possible = bool(re.search(r'가능성|수(?:도)?\s*있', certainty_text))
            forecast = bool(re.search(r'예상|예보', certainty_text))
            actual = bool(re.search(r'내립니다|옵니다|오고\s*있|내리고\s*있', certainty_text))
            if situation['precipitation_certainty'] == 'possible':
                if not possible or forecast or actual:
                    return 'precipitation_certainty'
            elif not forecast or possible or actual:
                return 'precipitation_certainty'

        evidence = ' '.join(claims)
        if situation['destination_precipitation']:
            if not re.search(DESTINATION, evidence) or re.search(BOARDING_TIME, evidence):
                return 'destination_context'
        if situation['precipitation_status'] == 'during_trip':
            context = r'탑승\s*후|이동\s*중|이동하는\s*동안|가는\s*길|출근길|퇴근길|귀갓길'
            destination_only = rf'(?:{DESTINATION})(?:에서|에)?만'
            if (not re.search(context, evidence) or re.search(BOARDING_TIME, evidence)
                    or re.search(destination_only, evidence)):
                return 'during_trip_context'
    sky = situation['sky_summary']
    if ((sky != 'clear' and '맑' in text) or
            (sky not in ('cloudy', 'clouds') and re.search(r'흐린|흐리|구름', text))):
        return 'sky_state'
    return None


def valid_comment(text, situation):
    return comment_validation_error(text, situation) is None


def generate_once(situation, trip_type, date_key=None):
    """One SDK call at most. Preview uses this directly, never the production cache."""
    fallback = render_advice(situation, trip_type, date_key=date_key)
    reason = 'unavailable' if situation['unavailable'] else ''
    try:
        if not reason:
            client = get_client()
            if client is None:
                reason = 'not_configured'
            else:
                response = client.models.generate_content(
                    model=MODEL, contents=build_prompt(situation, trip_type),
                    config={'response_mime_type': 'application/json',
                            'http_options': {'timeout': 15000, 'retry_options': {'attempts': 1}}})
                raw = response.text
                if not isinstance(raw, str) or len(raw) > 2000:
                    raise ValueError('invalid_response')
                parsed = json.loads(raw)
                text = parsed.get('comment') if isinstance(parsed, dict) else None
                validation_error = comment_validation_error(text, situation)
                if validation_error is None:
                    return {'text': text.strip(), 'source': 'gemini', 'reason': ''}

                reason = f'invalid_comment:{validation_error}'
    except Exception as exc:
        # No raw model text or SDK exception details (which can contain secrets).
        reason = type(exc).__name__

    logger.warning('[WEATHER_COMMENT] fallback reason=%s status=%s type=%s certainty=%s destination=%s',
                   reason, situation['precipitation_status'], situation['precipitation_type'],
                   situation['precipitation_certainty'], situation['destination_precipitation'])
    return {'text': fallback, 'source': 'python', 'reason': reason}


def clear_cache():
    with _lock:
        _cache.clear()


def generate_weather_advice(boarding, destination, trip_type='출근길'):
    situation = assess_weather(boarding, destination)
    key = comment_cache_key(situation, trip_type)
    # Local single-process app: serialize cache misses to avoid duplicate calls
    # from the scheduler and UI for an identical request.
    with _lock:
        now = time.monotonic()
        for old_key in list(_cache):
            if _cache[old_key][0] <= now:
                del _cache[old_key]
        if key in _cache:
            _cache.move_to_end(key)
            cached = dict(_cache[key][1])
            if cached['source'] == 'python':
                # Failure cooldown is shared, but deterministic fallback stays
                # tied to this caller's date/assessment, never another user's.
                cached['text'] = render_advice(situation, trip_type)
            return cached
        result = generate_once(situation, trip_type)
        ttl = SUCCESS_TTL if result['source'] == 'gemini' else FAILURE_TTL
        _cache[key] = (time.monotonic() + ttl, dict(result))
        while len(_cache) > MAX_CACHE:
            _cache.popitem(last=False)
        return result
