"""Isolated admin fixtures; Gemini is invoked only by explicit preview buttons."""
from datetime import datetime, timedelta, timezone
import json

import streamlit as st
import weather_advice

PREFIX = 'weather_preview_'
SCENARIOS = {
    '일반적인 맑고 선선한 날씨': (19, 18, 'none'),
    '일반적인 온화한 날씨': (25, 25, 'none'),
    '추운 날씨': (5, 4, 'none'),
    '더운 날씨': (28, 29, 'none'),
    '3°C 기온차 / 동일 복장구간': (19, 16, 'none'),
    '3°C 기온차 / 의미 있는 복장 변화': (21, 18, 'none'),
    '3°C 기온차 / 더위 구간 경계': (28, 25, 'none'),
    '5°C 이상 의미 있는 기온차': (24, 18, 'none'),
    '명확한 비 예보': (22, 22, 'rain'),
    'POP 70%만 있는 비 가능성': (22, 22, 'pop'),
    '명확한 눈 예보 + 추운 날씨': (5, 4, 'snow'),
    '비/눈 혼합 강수': (5, 4, 'mixed'),
    '탑승 후 비': (22, 22, 'later'),
    '하차 지역만 비': (22, 22, 'destination'),
    '강수 + 선선한 날씨': (19, 18, 'rain'),
    '강수 + 추운 날씨': (5, 4, 'rain'),
    '날씨 정보 일부 실패': (19, 18, 'unavailable'),
}


def build_scenario(name, reference_date):
    """Create fresh weather dictionaries at the production advice input boundary.

    Precipitation fields below are explicit fixtures, not a second classifier.
    """
    first, last, case = SCENARIOS[name]

    def weather(temp):
        return dict(available=True, temperature=f'{temp}°C', sky_status='맑음',
                    precipitation_status='none', precipitation_type='unknown',
                    precipitation_certainty='possible', rain_probability='정보 없음',
                    target_time=f'{reference_date.isoformat()}T08:00:00+09:00')

    boarding, destination = weather(first), weather(last)
    if case in ('rain', 'snow', 'mixed', 'later', 'destination', 'pop'):
        target = destination if case == 'destination' else boarding
        target.update(precipitation_status='during_trip' if case == 'later' else 'possible' if case == 'pop' else 'boarding',
                      precipitation_type=case if case in ('snow', 'mixed') else 'rain',
                      precipitation_certainty='possible' if case == 'pop' else 'forecast')
        if case == 'pop':
            target.update(rain_probability='70%', POP=70)
        else:
            target.update(PTY={'snow': 3, 'mixed': 2}.get(case, 1))
            if case != 'later':
                target['sky_status'] = {'snow': '눈', 'mixed': '비/눈'}.get(case, '비')
    elif case == 'unavailable':
        destination.update(available=False, temperature='정보 없음', sky_status='정보 없음')
    return boarding, destination


def generate_preview(name, trip_type, reference_date):
    boarding, destination = build_scenario(name, reference_date)
    situation = weather_advice.assess_weather(boarding, destination)
    comment = weather_advice.render_advice(situation, trip_type, date_key=reference_date.isoformat())
    return boarding, destination, situation, comment


def preview_candidates(name, trip_type, reference_date, limit=4):
    """Sample real date-based selection; never select template indices directly."""
    results, seen = [], set()
    limit = max(0, min(limit, 4))
    for offset in range(28):
        if len(results) >= limit:
            break
        try:
            day = reference_date + timedelta(days=offset)
        except OverflowError:
            break
        comment = generate_preview(name, trip_type, day)[3]
        if comment not in seen:
            seen.add(comment)
            results.append((day, comment))
    return results


def reset_preview():
    for key in list(st.session_state):
        if key.startswith(PREFIX):
            del st.session_state[key]


def generate_gemini_preview(name, trip_type, reference_date):
    from weather_comment_ai import generate_once
    _, _, situation, _ = generate_preview(name, trip_type, reference_date)
    return generate_once(situation, trip_type, date_key=reference_date.isoformat())


def _request_gemini_preview(name, trip_type, reference_date):
    st.session_state[PREFIX + 'gemini_result'] = generate_gemini_preview(name, trip_type, reference_date)


def render_admin_preview(is_admin, preview_user=False):
    if not is_admin or preview_user:
        return
    _render_preview()


@st.fragment
def _render_preview():
    # Widget reruns stay within this fragment, away from real user workflows.
    with st.expander('🧪 날씨 코멘트 테스트', expanded=False):
        st.caption('가상 입력 전용 · Gemini 버튼을 누를 때만 AI 호출 · 날씨 조회, 알림 발송, 데이터 저장 없음')
        st.button('테스트 입력 초기화', key=PREFIX + 'reset', on_click=reset_preview)
        trip = st.selectbox('테스트 출퇴근 구분', ['출근길', '퇴근길'], key=PREFIX + 'trip')
        name = st.selectbox('테스트 시나리오', list(SCENARIOS), key=PREFIX + 'scenario')
        day = st.date_input('테스트 기준 날짜',
                            value=datetime.now(timezone(timedelta(hours=9))).date(),
                            key=PREFIX + 'date')
        boarding, destination, situation, comment = generate_preview(name, trip, day)
        condition = json.dumps([name, trip, day.isoformat(), situation], sort_keys=True, ensure_ascii=False)
        result_key = PREFIX + 'gemini_result'
        if st.session_state.get(PREFIX + 'gemini_condition') != condition:
            st.session_state.pop(result_key, None)
        st.session_state[PREFIX + 'gemini_condition'] = condition
        fields = ('available', 'temperature', 'sky_status', 'precipitation_type',
                  'precipitation_certainty', 'precipitation_status', 'rain_probability')
        for column, title, data in zip(st.columns(2), ('탑승지 가상 입력', '하차지 가상 입력'),
                                       (boarding, destination)):
            with column:
                st.markdown(f'**{title}**')
                st.json({**{key: data[key] for key in fields},
                         'during_trip': data['precipitation_status'] == 'during_trip',
                         **{key: data[key] for key in ('PTY', 'POP') if key in data}})
        st.caption('강수 필드는 판정 완료된 가상 입력입니다. 테스트 기준 날짜는 문구 선택에만 사용합니다.')
        st.markdown('**Python 판정**')
        st.json(situation)
        st.markdown('**Python fallback**')
        st.info(comment)
        if st.checkbox('문구 후보 미리보기', key=PREFIX + 'candidates'):
            st.caption('기준일부터 최대 28일의 실제 선택 결과 중 중복 없이 최대 4개를 표시합니다.')
            for candidate_day, candidate in preview_candidates(name, trip, day):
                st.write(f'{candidate_day:%Y-%m-%d} · {candidate}')
        label = '🔄 다른 Gemini 문구 생성' if result_key in st.session_state else '✨ Gemini 코멘트 생성'
        st.button(label, key=PREFIX + 'gemini_generate', on_click=_request_gemini_preview,
                  args=(name, trip, day))
        if result_key in st.session_state:
            result = st.session_state[result_key]
            st.markdown('**Gemini API 코멘트**')
            st.info(result['text'])
            if result['source'] == 'gemini':
                st.success('Gemini 생성 성공')
            else:
                st.warning('Gemini 생성 실패 → Python fallback 사용')
