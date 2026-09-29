import contextlib
import io
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import weather_api as w
from test_demo import app_functions


def dt(hour, minute=0, day=16):
    return datetime(2026, 9, day, hour, minute, tzinfo=w.KST)


def rows(hour, **values):
    return {dt(hour): values or {'T1H': '21', 'TMP': '21', 'SKY': '1', 'PTY': '0'}}


class ForecastTests(unittest.TestCase):
    def setUp(self):
        self.ai = patch.object(w, 'client', None)
        self.ai.start()
        self.addCleanup(self.ai.stop)

    def fetch(self, now, target, data):
        with patch.object(w, '_request_forecast', side_effect=data) as request:
            result = w.get_weather_forecast_by_coords(37.4, 127.1, target_datetime=target, now=now)
        return result, request.call_args_list

    def test_A_B_target_not_current_and_over_four_hours(self):
        data = rows(18, TMP='25', SKY='1') | rows(14, TMP='10', SKY='4')
        result, calls = self.fetch(dt(14), dt(18, 10), [data])
        self.assertEqual(result['temperature'], '25°C')
        self.assertEqual(calls[0].args[0], 'VilageFcst')
        self.assertEqual(result['target_time'], dt(18, 10).isoformat())

    def test_C_four_hour_boundary(self):
        for target, source in [(dt(18), 'UltraSrtFcst'), (dt(18, 1), 'VilageFcst'), (dt(14, 10), 'UltraSrtFcst')]:
            with self.subTest(target=target):
                result, calls = self.fetch(dt(14), target, [rows(target.hour)])
                self.assertTrue(result['available'])
                self.assertEqual(calls[0].args[0], source)

    def test_D_missing_target_or_incomplete_ultra_falls_back(self):
        for data in [rows(17), rows(18, T1H='21'), {}]:
            with self.subTest(data=data):
                result, calls = self.fetch(dt(15), dt(18, 10), [data, rows(18, TMP='17', SKY='3')])
                self.assertEqual([c.args[0] for c in calls], ['UltraSrtFcst', 'VilageFcst'])
                self.assertEqual(result['temperature'], '17°C')

    def test_no_nearby_hour_substitution(self):
        result, calls = self.fetch(dt(14), dt(18, 10), [rows(17), rows(19)])
        self.assertFalse(result['available'])
        self.assertEqual(len(calls), 2)

    def test_E_future_precipitation_in_integrated_comment_even_similar_weather(self):
        forecast = rows(18) | rows(19, RN1='1mm미만', PTY='0', POP='0') | rows(20, PTY='0')
        result, _ = self.fetch(dt(15), dt(18, 10), [forecast])
        dry = dict(result, precipitation_note='')
        combine = app_functions('parse_temp', 'get_integrated_ai_message')['get_integrated_ai_message']
        message = combine(dry, result, '탑승A', '하차B')
        for expected in ['탑승 후', '우산']:
            self.assertIn(expected, message)
        self.assertNotIn('탑승A', message)
        self.assertNotIn('하차B', message)
        self.assertNotIn('현재', message)

    def test_F_boarding_precipitation_and_available_fields(self):
        for values in [{'PTY': '1', 'POP': '0'}, {'RN1': '0.5'}, {'POP': '60'}, {'PTY': '3'}]:
            with self.subTest(values=values):
                note, window = w.precipitation_note(rows(18, **values), dt(18), 'UltraSrtFcst')
                self.assertIn('탑승 시간대', note)
                self.assertEqual(set(window[0]) - {'time'}, set(values))

    def test_rain_window_only_real_slots_up_to_two_hours(self):
        forecast = rows(18, PTY='0') | rows(20, PTY='1') | rows(21, PTY='1')
        note, window = w.precipitation_note(forecast, dt(18), 'UltraSrtFcst')
        self.assertEqual(len(window), 2)
        self.assertIn('우산', note)
        for source in ['UltraSrtFcst', 'VilageFcst']:
            note, _ = w.precipitation_note(rows(18, PTY='0') | rows(21, PTY='1'), dt(18), source)
            self.assertEqual(note, '')
        note, _ = w.precipitation_note(rows(18, SKY='1'), dt(18), 'UltraSrtFcst')
        self.assertEqual(note, '')
        self.assertFalse(w._has_precipitation({'RN1': '-999', 'POP': '999', 'PTY': '-999'}))

    def test_G_negative_decimal_temperature(self):
        result, _ = self.fetch(dt(15), dt(18, 10), [rows(18, T1H='-5.5', SKY='1')])
        parse = app_functions('parse_temp')['parse_temp']
        self.assertEqual(parse(result['temperature']), -5.5)
        self.assertEqual(result['rain_probability'], '정보 없음')

    def test_H_missing_time_and_api_failure_no_fake_weather(self):
        with patch.object(w, '_request_forecast') as request:
            result = w.get_weather_forecast_by_coords(37.4, 127.1)
        request.assert_not_called()
        self.assertFalse(result['available'])
        result, _ = self.fetch(dt(15), dt(18), [{}, {}, {}])
        self.assertFalse(result['available'])
        self.assertEqual(result['temperature'], '정보 없음')
        self.assertEqual(result['precipitation_note'], '')

    def test_I_rollover_and_invalid_times(self):
        self.assertEqual(w.resolve_boarding_datetime('00:10', dt(23, 55)), dt(0, 10, 17))
        self.assertEqual(w.resolve_boarding_datetime('18:10', dt(18, 11)), dt(18, 10, 17))
        end = datetime(2026, 12, 31, 23, 59, tzinfo=w.KST)
        self.assertEqual(w.resolve_boarding_datetime('00:10', end), datetime(2027, 1, 1, 0, 10, tzinfo=w.KST))
        for value in [None, '', '-', '24:00', '08:60', '도착 미정']:
            self.assertIsNone(w.resolve_boarding_datetime(value, dt(14)))
        result, _ = self.fetch(dt(23), dt(0, 10, 17), [{dt(0, 0, 17): {'T1H': '12', 'SKY': '1'}}])
        self.assertTrue(result['available'])

    def test_official_release_availability_and_midnight(self):
        for now, source, expected in [(dt(14, 44), 'UltraSrtFcst', dt(13, 30)),
                (dt(14, 45), 'UltraSrtFcst', dt(14, 30)),
                (dt(14, 9), 'VilageFcst', dt(11)), (dt(14, 10), 'VilageFcst', dt(14)),
                (dt(0, 20, 17), 'UltraSrtFcst', dt(23, 30))]:
            self.assertEqual(w.forecast_base(now, source), expected)

    def test_J_logs_only_source_and_no_secret(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result, _ = self.fetch(dt(15), dt(18, 10), [{}, rows(18)])
        self.assertIn('fallback to VilageFcst', output.getvalue())
        self.assertIn('target=2026-09-16 18:10', output.getvalue())
        self.assertIn('source=VilageFcst', output.getvalue())
        self.assertNotIn('Fcst', str(result))
        with patch.object(w.requests, 'get', side_effect=RuntimeError('serviceKey=SECRET')), contextlib.redirect_stdout(output):
            self.assertEqual(w._request_forecast('UltraSrtFcst', dt(14, 30), 60, 120), {})
        self.assertNotIn('SECRET', output.getvalue())

    def test_K_comment_does_not_call_gemini_or_repeat_weather(self):
        ai = Mock()
        with patch.object(w, 'client', ai), patch.object(w, 'GEMINI_API_KEY', 'test'):
            result, _ = self.fetch(dt(15), dt(18, 10), [rows(18)])
        ai.models.generate_content.assert_not_called()
        self.assertEqual(result['target_time'], dt(18, 10).isoformat())
        self.assertEqual(result['message'], '')

    def test_destination_never_invents_arrival_weather(self):
        from weather_advice import assess_weather, render_advice
        with patch.object(w, '_request_forecast', return_value=rows(18, T1H='21', SKY='1', PTY='1')):
            result = w.get_weather_forecast_by_coords(37.4, 127.1, stop_name='하차B',
                         target_datetime=dt(18, 10), now=dt(15), location='destination')
        dry = dict(result, precipitation_status='none', precipitation_note='')
        situation = assess_weather(dry, result)
        self.assertEqual(situation['precipitation_status'], 'possible')
        message = render_advice(situation)
        self.assertIn('하차 지역', message)
        self.assertNotIn('도착할 무렵', message)
        self.assertNotIn('하차B', message)

    def test_ai_timeout_cannot_affect_real_weather_or_comment(self):
        from weather_advice import assess_weather, render_advice
        ai = Mock()
        ai.models.generate_content.side_effect = TimeoutError('sensitive-url')
        with patch.object(w, 'client', ai), patch.object(w, 'GEMINI_API_KEY', 'test'):
            result, _ = self.fetch(dt(15), dt(18, 10), [rows(18)])
        self.assertTrue(result['available'])
        self.assertTrue(render_advice(assess_weather(result, result)))
        ai.models.generate_content.assert_not_called()

    def test_actual_response_categories_and_dates_are_parsed(self):
        payload = {'response': {'header': {'resultCode': '00'}, 'body': {'items': {'item': [
            {'fcstDate': '20260917', 'fcstTime': '0000', 'category': 'T1H', 'fcstValue': '-2'},
            {'fcstDate': '20260917', 'fcstTime': '0000', 'category': 'RN1', 'fcstValue': '1mm미만'}]}}}}
        with patch.object(w.requests, 'get', return_value=Mock(status_code=200, json=Mock(return_value=payload))) as request:
            forecast = w._request_forecast('UltraSrtFcst', dt(23, 30), 60, 120)
        self.assertEqual(forecast[dt(0, 0, 17)], {'T1H': '-2', 'RN1': '1mm미만'})
        self.assertIn('getUltraSrtFcst', request.call_args.args[0])
        self.assertEqual(request.call_args.kwargs['params']['base_time'], '2330')

    def test_rounding_for_both_sources_and_date_boundary(self):
        for now, source in [(dt(10), 'VilageFcst'), (dt(16), 'UltraSrtFcst')]:
            for minute, hour in [(5, 17), (29, 17), (30, 18), (45, 18), (59, 18)]:
                with self.subTest(source=source, minute=minute):
                    result, calls = self.fetch(now, dt(17, minute), [rows(hour)])
                    self.assertTrue(result['available'])
                    self.assertEqual(calls[0].args[0], source)
                    self.assertEqual(calls[0].args[1], w.forecast_base(now, source))
                    self.assertEqual(result['target_time'], dt(17, minute).isoformat())
        for now in (dt(10), dt(22)):
            result, _ = self.fetch(now, dt(23, 45), [
                {dt(0, day=17): {'TMP': '12', 'T1H': '12', 'SKY': '1'}}])
            self.assertEqual(result['temperature'], '12°C')

    def test_rounded_target_missing_falls_back_not_previous_hour(self):
        result, calls = self.fetch(dt(16), dt(17, 45), [rows(17), rows(18, TMP='14', SKY='1')])
        self.assertEqual(result['temperature'], '14°C')
        self.assertEqual([c.args[0] for c in calls], ['UltraSrtFcst', 'VilageFcst'])

    def test_rounded_rain_window_never_exceeds_boarding_plus_two_hours(self):
        result, _ = self.fetch(dt(16), dt(17, 45), [rows(18) | rows(20, PTY='1')])
        self.assertEqual(result['precipitation_note'], '')
        result, _ = self.fetch(dt(16), dt(17, 45), [rows(18) | rows(19, PTY='1')])
        self.assertIn('우산', result['precipitation_note'])

    def test_concise_comment_and_single_rain_advice(self):
        combine = app_functions('parse_temp', 'get_integrated_ai_message')['get_integrated_ai_message']
        board = dict(available=True, temperature='26°C', sky_status='흐림',
                     message='탑승 시간대에는 가벼운 옷차림을 준비하세요.', precipitation_note='')
        arrival = dict(board, sky_status='구름많음')
        message = combine(board, arrival, '긴 탑승지 이름', '긴 하차지 이름')
        self.assertNotIn('차이가 거의 없', message)
        self.assertNotIn('기상 환경에 차이', message)
        self.assertNotIn('26', message)
        self.assertNotIn('긴 ', message)
        self.assertEqual(message.count('옷차림'), 1)
        note = '탑승 시간대 강수 가능성이 있으니 우산을 챙기세요.'
        message = combine(dict(board, precipitation_note=note), dict(arrival, precipitation_note=note), 'A', 'B')
        self.assertEqual(message.count('우산'), 1)
        self.assertLessEqual(message.count('.'), 2)
        message = combine(board, dict(arrival, temperature='20°C'), 'A', 'B')
        self.assertNotIn('6°C', message)

    def test_ai_repeated_weather_uses_concise_fallback(self):
        ai = Mock()
        ai.models.generate_content.return_value = Mock(text='정류장A는 26도로 흐립니다. 가벼운 옷을 입으세요.')
        with patch.object(w, 'client', ai), patch.object(w, 'GEMINI_API_KEY', 'test'):
            result, _ = self.fetch(dt(16), dt(17, 45), [rows(18)])
        self.assertNotIn('26', result['message'])
        self.assertNotIn('흐', result['message'])


if __name__ == '__main__':
    unittest.main()
