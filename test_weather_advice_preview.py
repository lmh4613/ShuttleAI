from datetime import date, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

import weather_advice
import weather_advice_preview as preview

DAY = date(2026, 9, 24)


@pytest.mark.parametrize('admin,user_mode,visible', [(True, False, True), (False, False, False), (True, True, False)])
def test_admin_guard(admin, user_mode, visible):
    with patch.object(preview, '_render_preview') as ui:
        preview.render_admin_preview(admin, user_mode)
    assert ui.call_count == int(visible)


@pytest.mark.parametrize('name,expected', list(preview.SCENARIOS.items()))
def test_all_scenario_inputs(name, expected):
    first, last, case = expected
    boarding, destination = preview.build_scenario(name, DAY)
    assert boarding['temperature'] == f'{first}°C'
    assert boarding['target_time'].startswith(DAY.isoformat())
    assert destination['temperature'] == ('정보 없음' if case == 'unavailable' else f'{last}°C')
    target = destination if case == 'destination' else boarding
    if case in ('none', 'unavailable'):
        assert boarding['precipitation_status'] == destination['precipitation_status'] == 'none'
    elif case == 'pop':
        assert target['POP'] == 70
        assert target['rain_probability'] == '70%'
        assert target['precipitation_certainty'] == 'possible'
        assert 'PTY' not in target
    else:
        assert target['precipitation_type'] == (case if case in ('snow', 'mixed') else 'rain')
        assert target['precipitation_certainty'] == 'forecast'
        assert target['precipitation_status'] == ('during_trip' if case == 'later' else 'boarding')
    if case == 'destination':
        assert boarding['precipitation_status'] == 'none'
    assert destination['available'] == (case != 'unavailable')
    boarding['temperature'] = 'mutated'
    assert preview.build_scenario(name, DAY)[0]['temperature'] == f'{first}°C'


def test_required_temperature_scenarios_are_explicit():
    assert preview.SCENARIOS['3°C 기온차 / 동일 복장구간'][:2] == (19, 16)
    assert preview.SCENARIOS['3°C 기온차 / 의미 있는 복장 변화'][:2] == (21, 18)
    assert preview.SCENARIOS['3°C 기온차 / 더위 구간 경계'][:2] == (28, 25)
    assert preview.SCENARIOS['5°C 이상 의미 있는 기온차'][:2] == (24, 18)
    assert len(preview.SCENARIOS) == 17


def test_preview_calls_production_functions():
    with patch.object(weather_advice, 'assess_weather', wraps=weather_advice.assess_weather) as assess, \
         patch.object(weather_advice, 'render_advice', wraps=weather_advice.render_advice) as render:
        boarding, destination, situation, comment = preview.generate_preview('명확한 비 예보', '퇴근길', DAY)
    assess.assert_called_once_with(boarding, destination)
    render.assert_called_once_with(situation, '퇴근길', date_key=DAY.isoformat())
    assert comment == weather_advice.render_advice(weather_advice.assess_weather(boarding, destination), '퇴근길', date_key=DAY.isoformat())


@pytest.mark.parametrize('name', list(preview.SCENARIOS))
def test_deterministic_candidates_use_real_dates(name):
    for trip in ('출근길', '퇴근길'):
        first = preview.generate_preview(name, trip, DAY)[3]
        assert first == preview.generate_preview(name, trip, DAY)[3]
        candidates = preview.preview_candidates(name, trip, DAY, limit=100)
        assert 1 <= len(candidates) <= 4
        assert len({comment for _, comment in candidates}) == len(candidates)
        for day, comment in candidates:
            assert DAY <= day <= DAY + timedelta(days=27)
            assert comment == preview.generate_preview(name, trip, day)[3]
            assert 1 <= comment.count('.') <= 2
        if name != '날씨 정보 일부 실패':
            assert len(candidates) >= 3
    assert preview.preview_candidates(name, '출근길', DAY, limit=0) == []
    assert len(preview.preview_candidates(name, '출근길', DAY, limit=1)) == 1


def test_preview_has_no_external_or_persistent_side_effects():
    paths = [Path('routes_db.json'), Path('user_settings.json')]
    before = {path: path.read_bytes() if path.exists() else None for path in paths}
    gemini = Mock()
    with patch('requests.sessions.Session.request', side_effect=AssertionError('Network forbidden')) as network, \
         patch('weather_api._request_forecast', side_effect=AssertionError('Forecast forbidden')) as forecast, \
         patch('weather_api.client', gemini), \
         patch('demo_support.deliver_message', side_effect=AssertionError('Kakao forbidden')) as kakao, \
         patch('builtins.open', side_effect=AssertionError('File operations forbidden')), \
         patch.object(preview.st, 'session_state', {'user_board_st': 'preserved', 'w_board': {'real': True}}) as state:
        for name in preview.SCENARIOS:
            preview.generate_preview(name, '출근길', DAY)
            preview.preview_candidates(name, '퇴근길', DAY)
        assert state == {'user_board_st': 'preserved', 'w_board': {'real': True}}
    network.assert_not_called()
    forecast.assert_not_called()
    kakao.assert_not_called()
    assert gemini.mock_calls == []
    assert before == {path: path.read_bytes() if path.exists() else None for path in paths}


def test_reset_only_removes_preview_state():
    state = {'weather_preview_date': DAY, 'weather_preview_trip': '퇴근길',
             'user_board_st': 'real stop', 'w_board': {'real': True}}
    with patch.object(preview.st, 'session_state', state):
        preview.reset_preview()
    assert state == {'user_board_st': 'real stop', 'w_board': {'real': True}}
