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
        for expected in ['탑승A', '하차B', '기온 차이', '탑승 후 1~2시간', '우산']:
            self.assertIn(expected, message)
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

    def test_K_prompt_time_and_reject_current_weather_wording(self):
        ai = Mock()
        for generated in ['현재 21도입니다.', '탑승 시간대에는 가벼운 겉옷을 준비해 보세요.']:
            ai.models.generate_content.return_value = Mock(text=generated)
            with patch.object(w, 'client', ai), patch.object(w, 'GEMINI_API_KEY', 'test'):
                result, _ = self.fetch(dt(15), dt(18, 10), [rows(18)])
            prompt = ai.models.generate_content.call_args.kwargs['contents']
            self.assertIn('2026-09-16 18:10', prompt)
            self.assertIn('예상 기온: 21°C', prompt)
            self.assertNotIn('현재', result['message'])
            self.assertNotIn('Fcst', result['message'])

    def test_destination_never_invents_arrival_weather(self):
        ai = Mock()
        ai.models.generate_content.return_value = Mock(text="도착 시간대에는 21도입니다.")
        with patch.object(w, "client", ai), patch.object(w, "GEMINI_API_KEY", "test"), patch.object(w, "_request_forecast", return_value=rows(18)):
            result = w.get_weather_forecast_by_coords(37.4, 127.1, stop_name="하차B",
                         target_datetime=dt(18, 10), now=dt(15), location="destination")
        self.assertIn("탑승 시간대", result["message"])
        self.assertNotIn("도착 시간대", result["message"])
        prompt = ai.models.generate_content.call_args.kwargs["contents"]
        self.assertIn("위치 역할: 하차지", prompt)
        self.assertIn("2026-09-16 18:10", prompt)

    def test_ai_timeout_keeps_real_weather_and_safe_comment(self):
        ai = Mock()
        ai.models.generate_content.side_effect = TimeoutError("sensitive-url")
        with patch.object(w, "client", ai), patch.object(w, "GEMINI_API_KEY", "test"):
            result, _ = self.fetch(dt(15), dt(18, 10), [rows(18)])
        self.assertTrue(result["available"])
        self.assertIn("탑승 시간대", result["message"])
        self.assertNotIn("sensitive", result["message"])
        self.assertEqual(ai.models.generate_content.call_args.kwargs["config"]["http_options"],
                         {"timeout": 20000, "retry_options": {"attempts": 1}})

    def test_actual_response_categories_and_dates_are_parsed(self):
        payload = {'response': {'header': {'resultCode': '00'}, 'body': {'items': {'item': [
            {'fcstDate': '20260917', 'fcstTime': '0000', 'category': 'T1H', 'fcstValue': '-2'},
            {'fcstDate': '20260917', 'fcstTime': '0000', 'category': 'RN1', 'fcstValue': '1mm미만'}]}}}}
        with patch.object(w.requests, 'get', return_value=Mock(status_code=200, json=Mock(return_value=payload))) as request:
            forecast = w._request_forecast('UltraSrtFcst', dt(23, 30), 60, 120)
        self.assertEqual(forecast[dt(0, 0, 17)], {'T1H': '-2', 'RN1': '1mm미만'})
        self.assertIn('getUltraSrtFcst', request.call_args.args[0])
        self.assertEqual(request.call_args.kwargs['params']['base_time'], '2330')


if __name__ == '__main__':
    unittest.main()
