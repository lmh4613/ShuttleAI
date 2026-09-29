"""Interpret forecast facts before rendering concise preparation advice."""
import re
import hashlib
import json
from datetime import datetime, timezone, timedelta

SIMILAR_TEMPERATURE = 4
MEANINGFUL_TEMPERATURE = 5
COLD = 10
COOL = 20
HOT = 26


def temperature(value):
    match = re.fullmatch(r'\s*([-+]?\d+(?:\.\d+)?)\s*(?:°C)?\s*', str(value))
    return float(match.group(1)) if match else None


def assess_weather(boarding, destination):
    result = dict(precipitation_status='none', precipitation_type='unknown',
                  precipitation_certainty='possible', temperature_status='unknown',
                  meaningful_temperature_change=False, temperature_direction='',
                  unavailable=False, sky_summary='', thermal_feel='',
                  destination_precipitation=False,
                  forecast_date=str(boarding.get('target_time') or destination.get('target_time') or '')[:10])
    if boarding.get('available', True) and destination.get('available', True):
        sky = boarding.get('sky_status')
        if sky == destination.get('sky_status'):
            result['sky_summary'] = {'맑음': 'clear', '흐림': 'cloudy', '구름많음': 'clouds'}.get(sky, '')
    events = []
    for location, weather in [('boarding', boarding), ('destination', destination)]:
        if not weather.get('available', True):
            result['unavailable'] = True
            continue
        status = weather.get('precipitation_status')
        if status is None and weather.get('precipitation_note'):
            status = 'during_trip' if '탑승 후' in weather['precipitation_note'] else 'possible'
        if status and status != 'none':
            events.append((location, status, weather))
    if events:
        # Keep boarding/later/destination priority, without inventing arrival time.
        selected = next((event for event in events if event[:2] == ('boarding', 'boarding')), None)
        selected = selected or next((event for event in events if event[1] == 'during_trip'), None)
        selected = selected or next((event for event in events if event[2].get('precipitation_certainty') == 'forecast'), events[0])
        location, status, data = selected
        result.update(precipitation_status=status if status == 'during_trip' or location == 'boarding' else 'possible',
                      destination_precipitation=all(event[0] == 'destination' for event in events),
                      precipitation_type=data.get('precipitation_type', 'unknown'),
                      precipitation_certainty=data.get('precipitation_certainty', 'possible'))
    t1 = temperature(boarding.get('temperature')) if boarding.get('available', True) else None
    t2 = temperature(destination.get('temperature')) if destination.get('available', True) else None
    if t1 is None or t2 is None:
        result['unavailable'] = True
        return result
    diff = abs(t1 - t2)
    direction = 'colder_at_arrival' if t2 < t1 else 'warmer_at_arrival'
    result['temperature_status'] = 'similar' if diff <= SIMILAR_TEMPERATURE else direction
    low, high = min(t1, t2), max(t1, t2)
    # A 3+ degree crossing into an extra layer matters; tiny boundary crossings
    # and hot/mild crossings alone do not warrant a regional warning.
    layer_change = diff >= 3 and (low < COLD <= high or low < COOL <= high)
    result['meaningful_temperature_change'] = diff >= MEANINGFUL_TEMPERATURE or layer_change
    result['temperature_direction'] = direction if result['meaningful_temperature_change'] else ''
    result['thermal_feel'] = ('cold' if low < COLD else 'cool' if low < COOL else
                              'hot' if high >= HOT else 'mild')
    return result


SUMMARY = {
    'general': ('{commute}은 {sky}{thermal} 날씨가 예상됩니다.',
                '{commute}에는 {sky}{thermal} 날씨가 예상됩니다.',
                '{commute} 날씨는 {sky}{thermal} 편으로 예상됩니다.'),
    'colder': ('{commute}에는 하차 지역의 공기가 더 쌀쌀하게 느껴질 수 있습니다.',
               '{commute}에는 하차 지역이 한층 쌀쌀할 것으로 예상됩니다.',
               '{commute}에는 하차 지역의 공기가 한결 서늘할 것으로 예상됩니다.'),
    'warmer': ('{commute}에는 출발하는 곳이 더 쌀쌀하고 하차 지역은 상대적으로 포근할 것으로 예상됩니다.',
               '{commute}에는 출발 지역의 공기가 하차 지역보다 더 서늘할 것으로 예상됩니다.',
               '{commute}에는 출발 지역이 더 쌀쌀할 것으로 예상됩니다.'),
    'difference': ('{commute}에는 지역에 따라 체감하는 더위가 달라질 수 있습니다.',
                   '{commute}에는 두 지역의 더위가 다르게 느껴질 수 있습니다.',
                   '{commute}에는 지역별 기온 변화에 대비하면 좋겠습니다.'),
    'rain': ('{commute}에는 {when}{precipitation}.',
             '{commute} 예보상 {when}{precipitation}.',
             '{commute} 예보를 보면 {when}{precipitation}.'),
}
ACTION = {
    'layer': ('{layer} 챙겨 체온을 편안하게 유지하세요.',
              '체온 조절을 위해 {layer} 준비하세요.',
              '{layer} 준비해 편안하게 이동하세요.'),
    'hot': ('가볍고 시원한 옷차림으로 이동하세요.',
            '시원한 옷차림을 준비해 편안하게 이동하세요.',
            '가볍고 시원한 옷차림을 챙기세요.'),
    'mild': ('편안한 옷차림으로 여유 있게 이동하세요.',
             '편안한 옷차림으로 이동을 준비하세요.',
             '편안한 옷차림으로 기분 좋게 출발하세요.'),
    'home': ('편안한 옷차림으로 귀가하시고, 퇴근 후에는 여유로운 시간 보내세요.',
             '편안한 옷차림으로 귀가하며 하루를 여유롭게 마무리하세요.',
             '하루를 마무리하는 길, 편안한 옷차림으로 귀가하세요.'),
    'rain': ('우산을 챙겨 안전하게 이동하세요.', '우산을 준비해 이동에 대비하세요.',
             '우산을 챙겨 편안하게 {travel}하세요.'),
    'snow': ('미끄러운 길에 유의하며 이동하세요.', '미끄러운 길을 조심하며 천천히 이동하세요.',
             '발밑이 미끄럽지 않은지 살피며 안전하게 이동하세요.'),
    'rain_layer': ('우산과 {layer} 함께 챙겨 이동하세요.',
                   '우산과 {layer} 준비해 편안하게 이동하세요.',
                   '이동 준비로 우산과 {layer} 챙기세요.'),
    'snow_layer': ('{layer} 챙기고 미끄러운 길에 유의하세요.',
                   '미끄러운 길을 조심하고 {layer} 준비하세요.',
                   '{layer} 준비해 보온하고, 미끄러운 길은 천천히 이동하세요.'),
    'rain_hot': ('우산을 챙기고 시원한 옷차림으로 이동하세요.',
                 '시원한 옷차림과 우산을 준비하세요.',
                 '우산을 준비하고 가볍고 시원한 옷차림으로 이동하세요.'),
}


def render_advice(situation, trip_type='출근길', date_key=None):
    commute = '퇴근길' if trip_type == '퇴근길' else '출근길'
    day = date_key or situation.get('forecast_date') or datetime.now(timezone(timedelta(hours=9))).date().isoformat()
    seed = json.dumps([str(day), commute, situation], sort_keys=True, ensure_ascii=False)
    def choose(options, role):
        index = int.from_bytes(hashlib.sha256((seed + role).encode()).digest()[:4], 'big') % len(options)
        return options[index]
    rain = situation['precipitation_status'] != 'none'
    feel = situation['thermal_feel']
    direction = situation['temperature_direction']
    snow = situation['precipitation_type'] in ('snow', 'mixed')
    layer = '따뜻한 외투를' if feel == 'cold' else '얇은 겉옷을'
    if direction == 'warmer_at_arrival':
        layer = '벗기 쉬운 ' + layer
    values = dict(commute=commute, layer=layer, travel='귀가' if commute == '퇴근길' else '이동')
    if situation['unavailable']:
        summary = '일부 날씨 정보를 불러오지 못했으니 출발 전 다시 확인해 주세요.'
        return summary + (' ' + choose(ACTION['snow' if snow else 'rain'], 'action').format(**values) if rain else '')
    if rain:
        kind = {'rain': '비가', 'snow': '눈이', 'mixed': '비와 눈이 섞여', 'unknown': '강수가'}[situation['precipitation_type']]
        if situation['precipitation_type'] == 'mixed':
            precipitation = kind + (' 내릴 것으로 예상됩니다' if situation['precipitation_certainty'] == 'forecast' else ' 내릴 가능성이 있습니다')
        else:
            precipitation = kind + (' 예상됩니다' if situation['precipitation_certainty'] == 'forecast' else ' 내릴 가능성이 있습니다')
        when = '출발할 때 ' if situation['precipitation_status'] == 'boarding' else ''
        if situation['destination_precipitation']:
            when += '하차 지역에 '
        if situation['precipitation_status'] == 'during_trip':
            when += '탑승 후 '
        values.update(when=when, precipitation=precipitation)
        summary_key = 'rain'
        action_key = ('snow_layer' if snow else 'rain_layer') if feel in ('cold', 'cool') else (
            'snow' if snow else 'rain_hot' if feel == 'hot' else 'rain')
    else:
        if direction and feel in ('cold', 'cool'):
            summary_key = 'colder' if direction == 'colder_at_arrival' else 'warmer'
        elif direction:
            summary_key = 'difference'
        else:
            summary_key = 'general'
        values.update(sky={'clear': '대체로 맑고 ', 'cloudy': '대체로 흐리고 ', 'clouds': '대체로 구름이 많고 '}.get(situation['sky_summary'], ''),
                      thermal={'cold': '추운', 'cool': '선선한', 'hot': '더운', 'mild': '온화한'}[feel])
        action_key = 'layer' if feel in ('cold', 'cool') else 'hot' if feel == 'hot' else 'home' if commute == '퇴근길' else 'mild'
    return choose(SUMMARY[summary_key], 'summary').format(**values) + ' ' + choose(ACTION[action_key], 'action').format(**values)
