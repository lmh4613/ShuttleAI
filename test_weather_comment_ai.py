from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
import json
from unittest.mock import Mock, patch

import pytest
from streamlit.testing.v1 import AppTest

import weather_advice as advice
import weather_advice_preview as preview
import weather_comment_ai as ai

DAY = date(2026, 9, 24)
GOOD = '출근길은 맑고 선선한 날씨가 예상됩니다. 얇은 겉옷을 챙겨 편안하게 이동하세요.'


@pytest.fixture
def client():
    mock = Mock()
    mock.models.generate_content.return_value = Mock(text=json.dumps({'comment': GOOD}))
    ai.clear_cache()
    with patch.object(ai, 'get_client', return_value=mock):
        yield mock
    ai.clear_cache()


def inputs(name='일반적인 맑고 선선한 날씨'):
    return preview.build_scenario(name, DAY)


def test_production_success_and_structured_input(client):
    boarding, destination = inputs()
    result = ai.generate_weather_advice(boarding, destination)
    assert result['text'] == GOOD
    assert result['source'] == 'gemini'
    call = client.models.generate_content.call_args.kwargs
    payload = json.loads(call['contents'].split('판정 입력:\n')[1])
    situation = advice.assess_weather(boarding, destination)
    assert payload['assessment'] == {k: v for k, v in situation.items() if k not in ('forecast_date', 'temperature_status')}
    assert payload['trip_type'] == '출근길'
    assert payload['recommended_clothing'] == '얇은 겉옷'
    assert 'temperature' not in payload  # Raw KMA rows/coordinates/names are not sent.
    assert call['model'] == ai.MODEL
    assert call['config']['http_options'] == {'timeout': 15000, 'retry_options': {'attempts': 1}}


@pytest.mark.parametrize('name,phrase', [
    ('명확한 비 예보', '출근길에는 출발할 때 비가 예상됩니다. 우산을 챙기세요.'),
    ('POP 70%만 있는 비 가능성', '출근길에는 비가 내릴 가능성이 있습니다. 우산을 챙기세요.'),
    ('명확한 눈 예보 + 추운 날씨', '출근길에는 출발할 때 눈이 예상됩니다. 따뜻한 외투를 입고 미끄러운 길에 유의하세요.'),
    ('비/눈 혼합 강수', '출근길에는 비와 눈이 섞여 내릴 것으로 예상됩니다. 따뜻한 외투를 준비하세요.'),
    ('탑승 후 비', '출근길에는 탑승 후 비가 예상됩니다. 우산을 챙기세요.'),
    ('하차 지역만 비', '출근길에는 하차 지역에 비가 예상됩니다. 우산을 챙기세요.'),
])
def test_precipitation_facts_are_preserved(client, name, phrase):
    client.models.generate_content.return_value = Mock(text=json.dumps({'comment': phrase}))
    boarding, destination = inputs(name)
    situation = advice.assess_weather(boarding, destination)
    assert ai.generate_weather_advice(boarding, destination)['text'] == phrase
    payload = json.loads(client.models.generate_content.call_args.kwargs['contents'].split('판정 입력:\n')[1])
    assert payload['assessment'] == {k: v for k, v in situation.items() if k not in ('forecast_date', 'temperature_status')}


@pytest.mark.parametrize('response', [None, Mock(text=None), Mock(text=''), Mock(text='not json'),
    Mock(text='[]'), Mock(text='{}'), Mock(text='{"comment":42}'),
    Mock(text=json.dumps({'comment': '너무 긴 문장' * 100})),
    Mock(text=json.dumps({'comment': '첫 문장입니다. 둘째 문장입니다. 셋째 문장입니다.'}))])
def test_invalid_responses_fall_back(client, response):
    client.models.generate_content.return_value = response
    boarding, destination = inputs()
    result = ai.generate_weather_advice(boarding, destination)
    assert result['source'] == 'python'
    assert result['text'] == advice.render_advice(advice.assess_weather(boarding, destination))
    client.models.generate_content.assert_called_once()


@pytest.mark.parametrize('error', [TimeoutError('secret'), RuntimeError('429 quota api_key=secret'), ValueError('api failure')])
def test_exceptions_and_failure_cache(client, error, caplog):
    client.models.generate_content.side_effect = error
    boarding, destination = inputs()
    result = ai.generate_weather_advice(boarding, destination)
    assert result == ai.generate_weather_advice(boarding, destination)
    assert result['source'] == 'python'
    assert result['text'] == advice.render_advice(advice.assess_weather(boarding, destination))
    assert 'secret' not in caplog.text
    assert 'fallback' in caplog.text
    client.models.generate_content.assert_called_once()


@pytest.mark.parametrize('name,text', [
    ('POP 70%만 있는 비 가능성', '출근길에는 비가 예상됩니다. 우산을 챙기세요.'),
    ('명확한 비 예보', '출근길에는 비가 내릴 가능성이 있습니다. 우산을 챙기세요.'),
    ('명확한 비 예보', '출근길에는 눈이 예상됩니다. 조심하세요.'),
    ('일반적인 맑고 선선한 날씨', '출근길에는 찬 바람이 붑니다. 겉옷을 챙기세요.'),
    ('일반적인 맑고 선선한 날씨', '습도가 높습니다. 편안하게 이동하세요.'),
    ('명확한 비 예보', '현재 비가 오고 있습니다. 우산을 챙기세요.'),
    ('하차 지역만 비', '도착할 무렵 비가 예상됩니다. 우산을 챙기세요.'),
    ('일반적인 맑고 선선한 날씨', '출근길에는 비가 예상됩니다. 우산을 챙기세요.'),
])
def test_obvious_fact_violations_fall_back(client, name, text):
    client.models.generate_content.return_value = Mock(text=json.dumps({'comment': text}))
    assert ai.generate_weather_advice(*inputs(name))['source'] == 'python'


def test_cache_key_time_trip_and_assessment(client):
    boarding, destination = inputs()
    ai.generate_weather_advice(boarding, destination)
    ai.generate_weather_advice(dict(boarding), dict(destination))
    assert client.models.generate_content.call_count == 1
    ai.generate_weather_advice(dict(boarding, target_time='2026-09-24T09:00:00+09:00'), destination)
    assert client.models.generate_content.call_count == 1
    ai.generate_weather_advice(boarding, destination, '퇴근길')
    ai.generate_weather_advice(dict(boarding, temperature='5°C'), destination)
    ai.generate_weather_advice(*inputs('명확한 비 예보'))
    assert client.models.generate_content.call_count == 4


def test_cache_ttl_capacity_and_concurrent_same_request(client):
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: ai.generate_weather_advice(*inputs()), range(4)))
    assert all(result == results[0] for result in results)
    client.models.generate_content.assert_called_once()
    with patch.object(ai.time, 'monotonic', return_value=10**12):
        ai.generate_weather_advice(*inputs())
    assert client.models.generate_content.call_count == 2
    with patch.object(ai, 'MAX_CACHE', 2):
        for temp in ('5°C', '22°C', '28°C'):
            boarding, destination = inputs()
            boarding['temperature'] = destination['temperature'] = temp
            ai.generate_weather_advice(boarding, destination)
        assert len(ai._cache) <= 2


def test_unavailable_and_missing_configuration_skip_api(client):
    assert ai.generate_weather_advice(*inputs('날씨 정보 일부 실패'))['source'] == 'python'
    client.models.generate_content.assert_not_called()
    with patch.object(ai, 'get_client', return_value=None):
        assert ai.generate_weather_advice(*inputs())['reason'] == 'not_configured'


def test_failure_cache_expires_and_recovers(client):
    client.models.generate_content.side_effect = TimeoutError()
    with patch.object(ai.time, 'monotonic', return_value=0):
        assert ai.generate_weather_advice(*inputs())['source'] == 'python'
    client.models.generate_content.side_effect = None
    with patch.object(ai.time, 'monotonic', return_value=ai.FAILURE_TTL - 1):
        assert ai.generate_weather_advice(*inputs())['source'] == 'python'
    with patch.object(ai.time, 'monotonic', return_value=ai.FAILURE_TTL + 1):
        assert ai.generate_weather_advice(*inputs())['source'] == 'gemini'
    assert client.models.generate_content.call_count == 2


def test_preview_bypasses_and_never_populates_production_cache(client):
    production = ai.generate_weather_advice(*inputs())
    for _ in range(2):
        assert preview.generate_gemini_preview('일반적인 맑고 선선한 날씨', '출근길', DAY)['source'] == 'gemini'
    assert client.models.generate_content.call_count == 3
    assert ai.generate_weather_advice(*inputs()) == production
    assert client.models.generate_content.call_count == 3


def test_preview_buttons_state_and_condition_changes(client):
    app = AppTest.from_string('from weather_advice_preview import render_admin_preview\nrender_admin_preview(True)').run()
    client.models.generate_content.assert_not_called()
    app.selectbox(key='weather_preview_trip').select('퇴근길').run()
    app.date_input(key='weather_preview_date').set_value(DAY).run()
    app.run()
    client.models.generate_content.assert_not_called()
    app.button(key='weather_preview_gemini_generate').click().run()
    assert not app.exception
    assert client.models.generate_content.call_count == 1
    assert any(GOOD == item.value for item in app.info)
    assert app.button(key='weather_preview_gemini_generate').label == '🔄 다른 Gemini 문구 생성'
    app.run()
    assert client.models.generate_content.call_count == 1
    app.button(key='weather_preview_gemini_generate').click().run()
    assert client.models.generate_content.call_count == 2
    for key, value in [('weather_preview_scenario', '추운 날씨'), ('weather_preview_trip', '출근길')]:
        app.selectbox(key=key).select(value).run()
        assert 'weather_preview_gemini_result' not in app.session_state
        client.models.generate_content.side_effect = TimeoutError('mock')
        before = client.models.generate_content.call_count
        app.button(key='weather_preview_gemini_generate').click().run()
        assert client.models.generate_content.call_count == before + 1
        assert any('Python fallback 사용' in item.value for item in app.warning)
    before = client.models.generate_content.call_count
    app.date_input(key='weather_preview_date').set_value(DAY + timedelta(days=1)).run()
    assert 'weather_preview_gemini_result' not in app.session_state
    assert client.models.generate_content.call_count == before
    assert not app.exception


def test_preview_has_no_other_external_or_file_side_effects(client):
    with patch('requests.sessions.Session.request', side_effect=AssertionError('network')) as requests, \
         patch('weather_api._request_forecast', side_effect=AssertionError('weather')) as weather, \
         patch('demo_support.deliver_message', side_effect=AssertionError('Kakao')) as kakao, \
         patch('builtins.open', side_effect=AssertionError('file writes')):
        preview.generate_gemini_preview('일반적인 맑고 선선한 날씨', '출근길', DAY)
    requests.assert_not_called()
    weather.assert_not_called()
    kakao.assert_not_called()
    client.models.generate_content.assert_called_once()


def guard_situation(status='during_trip', certainty='forecast', kind='rain', destination=False):
    situation = advice.assess_weather(*inputs('탑승 후 비'))
    return dict(situation, precipitation_status=status, precipitation_certainty=certainty,
                precipitation_type=kind, destination_precipitation=destination)


@pytest.mark.parametrize('text', [
    '출근길 이동 중 비가 내릴 것으로 예상되니 우산을 꼭 챙기시고, 온화한 날씨에 맞춰 편안한 옷차림으로 출발하세요.',
    '출근길 이동 중 비가 내릴 것으로 예상됩니다.',
    '탑승 후 비가 예상됩니다.',
    '이동하는 동안 비가 예보되어 있습니다.',
    '가는 길에 비가 예상되니 우산을 준비하세요.',
    '출근길에는 비가 예상됩니다.',
    '퇴근길에는 비가 내릴 것으로 예상됩니다.',
    '귀갓길에는 비가 예보되어 있습니다.',
    '출근길에는 비가 예상되니 겉옷은 벗을 수 있습니다.',
])
def test_natural_during_trip_forecast_is_accepted(client, text):
    situation = guard_situation()
    assert ai.valid_comment(text, situation)
    client.models.generate_content.return_value = Mock(text=json.dumps({'comment': text}))
    assert ai.generate_once(situation, '출근길')['text'] == text
    client.models.generate_content.assert_called_once()


@pytest.mark.parametrize('text', [
    '퇴근길 하차 지역에 비가 내릴 가능성이 있으니 우산을 챙기시고, 편안한 옷차림으로 안심하고 귀가하시기 바랍니다.',
    '퇴근길 하차 지역에 비가 내릴 가능성이 있습니다.',
    '목적지에는 비가 내릴 가능성이 있습니다.',
    '도착지에는 비가 내릴 수 있습니다.',
    '하차지에는 비가 내릴 수 있으니 우산을 준비하세요.',
    '목적지의 비 소식은 가능성이 있으니 우산을 챙기세요.',
])
def test_natural_destination_possibility_is_accepted(client, text):
    situation = guard_situation(status='possible', certainty='possible', destination=True)
    assert ai.comment_validation_error(text, situation) is None
    client.models.generate_content.return_value = Mock(text=json.dumps({'comment': text}))
    result = ai.generate_once(situation, '퇴근길')
    assert result['source'] == 'gemini'
    assert result['text'] == text


@pytest.mark.parametrize('situation,text,rule', [
    (guard_situation(), '출근길에는 출발할 때 비가 예상됩니다.', 'during_trip_context'),
    (guard_situation(), '퇴근길에는 탑승 전 비가 예상됩니다.', 'during_trip_context'),
    (guard_situation(), '출근길에는 하차 지역에만 비가 예상됩니다.', 'during_trip_context'),
    (guard_situation(destination=True), '출발할 때 하차 지역에 비가 예상됩니다.', 'destination_context'),
    (guard_situation(status='possible', certainty='possible', destination=True), '출발할 때 비가 내릴 가능성이 있습니다.', 'destination_context'),
    (guard_situation(certainty='possible'), '이동 중 비가 예상됩니다.', 'precipitation_certainty'),
    (guard_situation(), '이동 중 비가 내릴 가능성이 있습니다.', 'precipitation_certainty'),
    (guard_situation(), '이동 중 비가 내릴 수 있으니 우산을 챙기세요.', 'precipitation_certainty'),
    (guard_situation(), '이동 중 눈이 예상됩니다.', 'precipitation_type'),
    (guard_situation(kind='snow'), '이동 중 비가 예상됩니다.', 'precipitation_type'),
    (guard_situation(kind='unknown'), '이동 중 비가 예상되는 강수 예보입니다.', 'precipitation_type'),
    (guard_situation(kind='mixed'), '이동 중 비가 예상됩니다.', 'precipitation_type'),
    (guard_situation(status='none'), '출근길에 비 소식이 있으니 우산을 준비하세요.', 'unexpected_precipitation'),
    (guard_situation(status='none'), '편안한 출근길을 위해 우산을 준비하세요.', 'unexpected_precipitation'),
    (guard_situation(), '이동 중 비가 예상되고 찬 바람이 붑니다.', 'unsupported_fact'),
    (guard_situation(), '이동 중 비가 예상되고 습도가 높습니다.', 'unsupported_fact'),
    (guard_situation(), '현재 비가 내릴 것으로 예상됩니다.', 'unsupported_fact'),
    (guard_situation(destination=True), '도착할 때 하차 지역에 비가 예상됩니다.', 'arrival_time'),
    (guard_situation(destination=True), '도착할 무렵 목적지에 비가 예상됩니다.', 'arrival_time'),
    (guard_situation(destination=True), '하차 시각에 비가 예상됩니다.', 'arrival_time'),
])
def test_named_rules_reject_fact_changes(client, caplog, situation, text, rule):
    assert ai.comment_validation_error(text, situation) == rule
    assert not ai.valid_comment(text, situation)
    client.models.generate_content.return_value = Mock(text=json.dumps({'comment': text}))
    result = ai.generate_once(situation, '출근길')
    assert result['source'] == 'python'
    assert result['reason'] == f'invalid_comment:{rule}'
    assert result['text'] == advice.render_advice(situation, '출근길')
    assert f'invalid_comment:{rule}' in caplog.text
    assert text not in caplog.text
    assert 'rejected Gemini comment=' not in caplog.text


@pytest.mark.parametrize('fact', ['풍속이 높습니다', '습도가 높습니다', '건조합니다', '체감온도가 낮습니다',
                                 '미세먼지가 많습니다', '자외선이 강합니다', '태풍이 옵니다',
                                 '폭우가 내립니다', '한파가 옵니다', '지금 비가 옵니다'])
def test_unsupported_weather_facts_stay_blocked(fact):
    assert ai.comment_validation_error(f'이동 중 비가 예상됩니다. {fact}.', guard_situation()) == 'unsupported_fact'


def test_sky_and_mixed_unknown_kinds_stay_guarded():
    assert ai.valid_comment('이동 중 비와 눈이 섞여 내릴 것으로 예상됩니다.', guard_situation(kind='mixed'))
    assert ai.valid_comment('이동 중 진눈깨비가 예상됩니다.', guard_situation(kind='mixed'))
    assert ai.valid_comment('이동 중 강수가 예상됩니다.', guard_situation(kind='unknown'))
    assert not ai.valid_comment('이동 중 눈이 예상되는 강수 예보입니다.', guard_situation(kind='unknown'))
    situation = dict(guard_situation(status='none'), sky_summary='')
    assert ai.comment_validation_error('출근길은 맑은 날씨가 예상됩니다.', situation) == 'sky_state'


def test_reported_destination_forecast_regression(client):
    text = '출근길 예보를 보면 하차 지역에 비가 예상됩니다. 우산을 챙겨 안전하게 이동하세요.'
    boarding, destination = inputs('하차 지역만 비')
    situation = advice.assess_weather(boarding, destination)
    assert situation['precipitation_certainty'] == 'forecast'
    assert situation['destination_precipitation']
    client.models.generate_content.return_value = Mock(text=json.dumps({'comment': text}))
    assert ai.comment_validation_error(text, situation) is None
    result = ai.generate_weather_advice(boarding, destination)
    assert result['source'] == 'gemini'
    assert result['text'] == text


@pytest.mark.parametrize('phrase', ['비가 예상됩니다', '비가 예상되니 우산을 챙기세요',
    '비가 예상되므로 우산을 챙기세요', '비가 내릴 것으로 예상됩니다',
    '비가 내릴 것으로 예상되니 우산을 챙기세요', '비가 예보되어 있습니다',
    '눈이 예상됩니다', '눈이 내릴 것으로 예상됩니다'])
def test_forecast_certainty_endings(phrase):
    situation = guard_situation(kind='snow' if '눈' in phrase else 'rain', destination=True)
    assert ai.valid_comment(f'출근길 예보를 보면 탑승 후 하차 지역에 {phrase}.', situation)


@pytest.mark.parametrize('phrase', ['비가 내릴 가능성이 있습니다', '비가 내릴 가능성이 있으니 우산을 챙기세요',
                                  '비가 올 가능성이 있습니다', '비가 내릴 수 있습니다', '비가 올 수도 있습니다'])
def test_possibility_not_confused_by_forecast_preamble(phrase):
    text = f'출근길 예보를 보면 이동 중 {phrase}.'
    assert ai.valid_comment(text, guard_situation(certainty='possible'))
    assert ai.comment_validation_error(text, guard_situation()) == 'precipitation_certainty'


def test_semantic_cache_shares_other_locations_dates_temperatures(client):
    boarding, destination = inputs()
    other_boarding = dict(boarding, stop_name='다른 정류장', lat=38, lon=128, route_name='다른 노선',
                          user_id='other', target_time='2026-09-25T10:45:00+09:00', temperature='16°C', POP='20')
    other_destination = dict(destination, target_time=other_boarding['target_time'], temperature='17°C')
    s1, s2 = advice.assess_weather(boarding, destination), advice.assess_weather(other_boarding, other_destination)
    assert ai.comment_cache_key(s1, '출근길') == ai.comment_cache_key(s2, '출근길')
    assert ai.build_prompt(s1, '출근길') == ai.build_prompt(s2, '출근길')
    assert ai.generate_weather_advice(boarding, destination) == ai.generate_weather_advice(other_boarding, other_destination)
    client.models.generate_content.assert_called_once()
    prompt = client.models.generate_content.call_args.kwargs['contents']
    for forbidden in ('다른 정류장', 'target_time', 'forecast_date', 'temperature_status', 'user_id', '2026-09-25'):
        assert forbidden not in prompt


@pytest.mark.parametrize('changes', [
    {'precipitation_status': 'boarding'},
    {'destination_precipitation': True},
    {'precipitation_certainty': 'possible'},
    {'precipitation_type': 'snow'}, {'precipitation_type': 'mixed'}, {'precipitation_type': 'unknown'},
    {'thermal_feel': 'cold'}, {'thermal_feel': 'cool'}, {'thermal_feel': 'hot'},
    {'meaningful_temperature_change': True, 'temperature_direction': 'colder_at_arrival'},
    {'meaningful_temperature_change': True, 'temperature_direction': 'warmer_at_arrival'},
    {'sky_summary': 'cloudy'}, {'unavailable': True},
])
def test_meaning_changes_get_distinct_keys_and_prompts(changes):
    baseline = dict(guard_situation(), thermal_feel='mild')
    changed = dict(baseline, **changes)
    assert ai.comment_cache_key(baseline, '출근길') != ai.comment_cache_key(changed, '출근길')
    assert ai.build_prompt(baseline, '출근길') != ai.build_prompt(changed, '출근길')


def test_model_prompt_version_and_trip_separate_cache(client):
    boarding, destination = inputs()
    ai.generate_weather_advice(boarding, destination)
    ai.generate_weather_advice(boarding, destination, '퇴근길')
    with patch.object(ai, 'MODEL', 'different-model'):
        ai.generate_weather_advice(boarding, destination)
    with patch.object(ai, 'PROMPT_VERSION', ai.PROMPT_VERSION + 1):
        ai.generate_weather_advice(boarding, destination)
    assert client.models.generate_content.call_count == 4
    assert ai.PROMPT_VERSION == 2


def test_success_five_hour_ttl(client):
    assert ai.SUCCESS_TTL == 18000
    assert ai.FAILURE_TTL == 60
    assert ai.MAX_CACHE == 256
    with patch.object(ai.time, 'monotonic', return_value=0):
        assert ai.generate_weather_advice(*inputs())['source'] == 'gemini'
    for now in (3601, 17999):
        with patch.object(ai.time, 'monotonic', return_value=now):
            ai.generate_weather_advice(*inputs())
    client.models.generate_content.assert_called_once()
    with patch.object(ai.time, 'monotonic', return_value=18000):
        ai.generate_weather_advice(*inputs())
    assert client.models.generate_content.call_count == 2


def test_shared_failure_cooldown_preserves_each_dates_python_fallback(client):
    client.models.generate_content.side_effect = TimeoutError()
    boarding, destination = inputs()
    ai.generate_weather_advice(boarding, destination)
    for day in range(1, 10):
        b = dict(boarding, target_time=f'2026-10-{day:02}T08:00:00+09:00')
        d = dict(destination, target_time=b['target_time'])
        result = ai.generate_weather_advice(b, d)
        assert result['text'] == advice.render_advice(advice.assess_weather(b, d))
        assert result['source'] == 'python'
    client.models.generate_content.assert_called_once()


def test_semantic_concurrent_requests_and_lru(client):
    names = ['일반적인 맑고 선선한 날씨', '일반적인 온화한 날씨', '추운 날씨', '더운 날씨',
             '3°C 기온차 / 의미 있는 복장 변화', '명확한 비 예보', 'POP 70%만 있는 비 가능성',
             '명확한 눈 예보 + 추운 날씨', '탑승 후 비', '하차 지역만 비']
    requests = [inputs(names[i % 10]) for i in range(50)]
    for i, (b, d) in enumerate(requests):
        b['target_time'] = d['target_time'] = f'2026-09-24T08:{i:02}:00+09:00'
    keys = {ai.comment_cache_key(advice.assess_weather(b, d), '출근길') for b, d in requests}
    assert len(keys) == 10
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda pair: ai.generate_weather_advice(*pair), requests))
    assert client.models.generate_content.call_count == 10
    ai.clear_cache()
    client.models.generate_content.reset_mock()
    with patch.object(ai, 'MAX_CACHE', 2):
        for name in (names[0], names[1], names[0], names[2], names[1]):
            ai.generate_weather_advice(*inputs(name))
        assert len(ai._cache) == 2
        assert client.models.generate_content.call_count == 4


def test_style_is_prompt_only():
    situation = guard_situation()
    prompt = ai.build_prompt(situation, '출근길')
    assert '같은 의미의 표현을 반복하지' in prompt
    assert '~하시기 바랍니다' in prompt
    assert ai.valid_comment('이동 중 비가 예상되니 만약의 경우를 대비해 우산을 꼭 챙기시기 바랍니다.', situation)
