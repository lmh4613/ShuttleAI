import unittest
from unittest.mock import patch

import weather_api as w
from weather_advice import assess_weather, render_advice
from test_weather import dt, rows


def weather(temp=22, sky='맑음', rain='none'):
    return dict(available=True, temperature=f'{temp}°C', sky_status=sky,
                precipitation_status=rain, message='반복되면 안 되는 기존 설명')


class PreparationTests(unittest.TestCase):
    def test_temperature_thresholds_and_actions(self):
        for boarding, arrival, status, expected in [
            (22, 23, 'similar', '편안한'),
            (19, 18, 'similar', '얇은 겉옷'),
            (-5, -4, 'similar', '따뜻한 외투'),
            (10, 10, 'similar', '얇은 겉옷'),
            (20, 20, 'similar', '편안한'),
            (25, 25, 'similar', '편안한'),
            (26, 26, 'similar', '시원한'),
            (22, 16, 'colder_at_arrival', '하차 지역'),
            (13, 19, 'warmer_at_arrival', '벗기 쉬운'),
            (18, 20, 'similar', '얇은 겉옷'),
        ]:
            with self.subTest(boarding=boarding, arrival=arrival):
                situation = assess_weather(weather(boarding), weather(arrival))
                self.assertEqual(situation['temperature_status'], status)
                self.assertIn(expected, render_advice(situation))

    def test_actual_precipitation_fields_and_probability_threshold(self):
        for values, status in [({'PTY': '1'}, 'boarding'), ({'PTY': '3'}, 'boarding'),
                               ({'RN1': '1mm미만'}, 'boarding'), ({'PCP': '2'}, 'boarding'),
                               ({'POP': '59'}, 'none'), ({'POP': '60'}, 'possible'),
                               ({'POP': '100'}, 'possible'), ({'POP': '999'}, 'none'),
                               ({'PTY': '0'}, 'none')]:
            with self.subTest(values=values):
                forecast = rows(18, T1H='22', SKY='1', **values)
                with patch.object(w, '_request_forecast', return_value=forecast):
                    result = w.get_weather_forecast_by_coords(37.3, 127.1,
                                        target_datetime=dt(18, 10), now=dt(16))
                self.assertEqual(result['precipitation_status'], status)
                message = render_advice(assess_weather(result, weather()))
                self.assertEqual(('우산' in message or '미끄' in message), status != 'none')
                self.assertNotIn('비가 옵니다', message)

    def test_later_rain_and_two_hour_limit(self):
        for hour, expected in [(19, 'during_trip'), (20, 'during_trip'), (21, 'none')]:
            with patch.object(w, '_request_forecast', return_value=rows(18) | rows(hour, PTY='1')):
                result = w.get_weather_forecast_by_coords(37.3, 127.1,
                                    target_datetime=dt(18, 10), now=dt(16))
            self.assertEqual(result['precipitation_status'], expected)

    def test_arrival_time_cannot_be_inferred_from_destination_rain(self):
        situation = assess_weather(weather(), weather(rain='boarding'))
        self.assertEqual(situation['precipitation_status'], 'possible')
        self.assertIn('하차 지역', render_advice(situation))
        self.assertNotIn('도착할 무렵', render_advice(situation))

    def test_rain_priority_and_two_sentences_without_repeated_facts(self):
        for temp in (-5, 18, 22, 28):
            for rain in ('none', 'boarding', 'during_trip', 'possible'):
                with self.subTest(temp=temp, rain=rain):
                    situation = assess_weather(weather(temp, '흐림', rain), weather(temp, '구름많음'))
                    message = render_advice(situation)
                    self.assertLessEqual(message.count('.'), 2)
                    self.assertGreaterEqual(message.count('.'), 1)
                    for forbidden in ('°C', '흐림', '구름많음', '기온 차이', '기상 환경',
                                      '탑승 예정 시간대', '탑승 시간대', '기존 설명'):
                        self.assertNotIn(forbidden, message)
                    if rain != 'none':
                        self.assertIn('강수', message.split('.')[0])
                        self.assertIn('우산', message.split('.')[1])
                    if rain != 'none' and temp != 22:
                        self.assertEqual(message.count('.'), 2)

    def test_partial_failure_keeps_known_rain_without_fake_temperature(self):
        situation = assess_weather({'available': False}, weather(18, rain='boarding'))
        message = render_advice(situation)
        self.assertIn('불러오지 못', message)
        self.assertIn('우산', message)
        self.assertNotIn('겉옷', message)
        self.assertEqual(situation['temperature_status'], 'unknown')

    def test_commute_caster_style(self):
        cases = [
            (weather(19), weather(18), '출근길', '선선한', '얇은 겉옷'),
            (weather(25), weather(25), '퇴근길', '온화한', '귀가'),
            (weather(22), weather(16), '출근길', '하차 지역', '얇은 겉옷'),
            (weather(13), weather(19), '출근길', '출발', '벗기 쉬운'),
            (weather(22, rain='boarding'), weather(22), '출근길', '출발할 때', '우산'),
            (weather(22, rain='during_trip'), weather(22), '퇴근길', '탑승 후', '우산'),
            (weather(19, rain='possible'), weather(18), '퇴근길', '내릴 가능성', '우산과 얇은 겉옷'),
            (weather(22, '흐림'), weather(22, '흐림'), '퇴근길', '대체로 흐리고', '편안한'),
        ]
        for boarding, destination, commute, summary, action in cases:
            with self.subTest(commute=commute, summary=summary):
                boarding['stop_name'] = '반복금지 정류장A'
                destination['stop_name'] = '반복금지 정류장B'
                result = render_advice(assess_weather(boarding, destination), commute)
                first, second, _ = result.split('.')
                self.assertIn(commute, first)
                self.assertIn(summary, first)
                self.assertIn(action, second)
                self.assertNotRegex(result, r'\d|°|반복금지|기온 차이|기상 환경|탑승 예정 시간대')

    def test_minor_sky_differences_do_not_exaggerate_or_invent_sunshine(self):
        for skies in [('흐림', '구름많음'), ('맑음', '구름많음')]:
            result = render_advice(assess_weather(weather(22, skies[0]), weather(22, skies[1])))
            self.assertIn('온화한', result)
            self.assertNotIn('대체로 맑', result)
            self.assertNotIn('차이', result)
            self.assertNotIn('비슷', result)

    def test_meaningful_change_separate_from_similarity(self):
        for first, last, similar, meaningful in [
            (19, 16, True, False), (21, 18, True, True),
            (28, 25, True, False), (24, 18, False, True),
            (20, 19, True, False), (10, 9, True, False),
            (12, 9, True, True), (18, 18, True, False),
            (24, 20, True, False), (25, 20, False, True)]:
            with self.subTest(first=first, last=last):
                situation = assess_weather(weather(first), weather(last))
                self.assertEqual(situation['temperature_status'] == 'similar', similar)
                self.assertEqual(situation['meaningful_temperature_change'], meaningful)
                for day in range(1, 12):
                    text = render_advice(situation, date_key=f'2026-09-{day:02}')
                    if not meaningful:
                        self.assertNotIn('지역', text)
                    elif last < 20:
                        self.assertIn('하차 지역', text)

    def test_precipitation_type_certainty_through_api_and_render(self):
        for values, kind, certainty, phrase in [
            ({'PTY': '1'}, 'rain', 'forecast', '비가 예상됩니다'),
            ({'PTY': '3'}, 'snow', 'forecast', '눈이 예상됩니다'),
            ({'PTY': '2'}, 'mixed', 'forecast', '비와 눈이 섞여'),
            ({'PTY': '6'}, 'mixed', 'forecast', '비와 눈이 섞여'),
            ({'RN1': '1mm미만'}, 'rain', 'forecast', '비가 예상됩니다'),
            ({'PCP': '1'}, 'unknown', 'forecast', '강수가 예상됩니다'),
            ({'POP': '60'}, 'rain', 'possible', '비가 내릴 가능성이 있습니다')]:
            for location in ('boarding', 'destination'):
                with self.subTest(values=values, location=location):
                    with patch.object(w, '_request_forecast', return_value=rows(18, T1H='22', SKY='1', **values)):
                        result = w.get_weather_forecast_by_coords(37.3, 127.1,
                            target_datetime=dt(18, 10), now=dt(16), location=location)
                    self.assertEqual(result['precipitation_type'], kind)
                    self.assertEqual(result['precipitation_certainty'], certainty)
                    situation = assess_weather(result, weather()) if location == 'boarding' else assess_weather(weather(), result)
                    text = render_advice(situation)
                    self.assertIn(phrase, text)
                    if location == 'destination':
                        self.assertIn('하차 지역', text)
                    self.assertNotIn('도착할', text)
                    self.assertNotIn('현재', text)

    def test_later_snow_keeps_type_and_forecast_certainty(self):
        with patch.object(w, '_request_forecast', return_value=rows(18) | rows(19, PTY='3')):
            result = w.get_weather_forecast_by_coords(37.3, 127.1, target_datetime=dt(18, 10), now=dt(16))
        text = render_advice(assess_weather(weather(), result), '퇴근길')
        self.assertIn('탑승 후 눈이 예상됩니다', text)
        self.assertIn('미끄', text)

    def test_templates_stable_and_varied_without_repeating_facts(self):
        for temp in (5, 18, 22, 28):
            for rain in ('none', 'boarding', 'during_trip', 'possible'):
                for commute in ('출근길', '퇴근길'):
                    situation = assess_weather(weather(temp, rain=rain), weather(temp))
                    texts = set()
                    for day in range(1, 29):
                        key = f'2026-09-{day:02}'
                        text = render_advice(situation, commute, key)
                        self.assertEqual(text, render_advice(dict(reversed(list(situation.items()))), commute, key))
                        self.assertEqual(text.count('.'), 2)
                        self.assertNotRegex(text, r'\d|°|기온 차이|기상 환경|현재|탑승 예정 시간대')
                        self.assertIn(commute, text)
                        texts.add(text)
                    self.assertGreaterEqual(len(texts), 3)

    def test_target_date_is_used_instead_of_rerun_time(self):
        situation = assess_weather(dict(weather(18), target_time='2026-09-24T07:00:00+09:00'), weather(18))
        self.assertEqual(situation['forecast_date'], '2026-09-24')
        self.assertEqual(render_advice(situation), render_advice(situation, date_key='2026-09-24'))
        self.assertNotIn('clothing_advice', situation)
        self.assertNotIn('umbrella_advice', situation)


if __name__ == '__main__':
    unittest.main()
