import ast
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
import demo_support as support
import weather_api


def app_functions(*names):
    tree = ast.parse(Path("app.py").read_text(encoding="utf-8"))
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    ns = {"re": re}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "app.py", "exec"), ns)
    return ns


class DemoTests(unittest.TestCase):
    def test_route_options_preserve_order_and_defaults(self):
        options = app_functions('route_stop_options')['route_stop_options']
        stops = ['A', 'B', 'D (하차만)', 'E (하차만)', '판교']
        board, arrival = options(stops, False)
        self.assertEqual(board, ['A', 'B', '판교'])
        self.assertEqual(arrival, ['판교 제2테크노밸리', 'D (하차만)', 'E (하차만)'])
        self.assertEqual(options(stops, True), (['A'], stops))
        self.assertEqual(options(['A', 'B'], False)[1], ['판교 제2테크노밸리'])
        self.assertEqual(stops, ['A', 'B', 'D (하차만)', 'E (하차만)', '판교'])

    def test_signed_temperatures(self):
        parse = app_functions("parse_temp")["parse_temp"]
        for value, expected in [("-5°C", -5), ("+5°C", 5), ("23°C", 23),
                                ("-5.5°C", -5.5), ("+.5°C", .5), ("0°C", 0)]:
            with self.subTest(value=value):
                self.assertEqual(parse(value), expected)

    def test_oauth_state_one_use_and_expiry(self):
        token = support.prepare_login(None, {"user_reg": "seoul", "access_token": "secret"})
        self.assertEqual(support.consume_login(token), {"user_reg": "seoul"})
        self.assertIsNone(support.consume_login(token))
        with patch.object(support.time, "monotonic", return_value=1):
            token = support.prepare_login(None, {})
        with patch.object(support.time, "monotonic", return_value=602):
            self.assertIsNone(support.consume_login(token))

    def test_refresh_on_expiry_and_preserve_refresh_token(self):
        send = Mock(side_effect=[(401, {"code": -401}), (200, {"result_code": 0})])
        persist = Mock()
        result = support.deliver_message("old", "refresh", send,
                    Mock(return_value=("new", None)), persist)
        self.assertTrue(support.message_succeeded(*result))
        self.assertEqual([c.args[0] for c in send.call_args_list], ["old", "new"])
        persist.assert_called_once_with("new", "refresh")

    def test_retry_is_bounded_and_manual_unchanged(self):
        for scheduled, calls in [(True, 2), (False, 1)]:
            send = Mock(return_value=(503, {}))
            support.deliver_message("token", "r", send, Mock(), Mock(), scheduled, Mock())
            self.assertEqual(send.call_count, calls)
        send = Mock(return_value=(0, {}))
        support.deliver_message("token", "r", send, Mock(), Mock(), True, Mock())
        self.assertEqual(send.call_count, 1)

    def test_failed_refresh_stops(self):
        send = Mock(return_value=(401, {"code": -401}))
        persist = Mock()
        result = support.deliver_message("old", "r", send, Mock(return_value=(None, None)), persist)
        self.assertEqual(result[0], 401)
        persist.assert_not_called()

    def test_only_success_marks_sent(self):
        sent, attempts = {}, {}
        deliver = Mock(side_effect=[(503, {}), (200, {"result_code": 0})])
        self.assertFalse(support.notify_once(sent, attempts, "key", deliver, now=0))
        self.assertNotIn("key", sent)
        self.assertFalse(support.notify_once(sent, attempts, "key", deliver, now=10))
        self.assertTrue(support.notify_once(sent, attempts, "key", deliver, now=31))
        self.assertFalse(support.notify_once(sent, attempts, "key", deliver, now=62))
        self.assertEqual(deliver.call_count, 2)

    def test_api_exception_safe(self):
        with patch.object(support.requests, "request", side_effect=requests.ConnectionError("secret")):
            status, data = support.api_request("GET", "https://kapi.kakao.com/v2/user/me")
        self.assertEqual(status, 0)
        self.assertNotIn("secret", str(data))

    def test_weather_failures_never_fabricate_or_call_ai(self):
        for result in [requests.Timeout(), Mock(status_code=503),
                       Mock(status_code=200, json=Mock(return_value={}))]:
            kwargs = {"side_effect": result} if isinstance(result, Exception) else {"return_value": result}
            with patch.object(weather_api.requests, "get", **kwargs), patch.object(weather_api, "client") as ai:
                weather = weather_api.get_weather_forecast_by_coords(37.4, 127.1, target_datetime=weather_api.korea_now())
                self.assertFalse(weather["available"])
                self.assertEqual(weather["temperature"], "정보 없음")
                ai.models.generate_content.assert_not_called()
        ns = app_functions("parse_temp", "get_integrated_ai_message")
        text = ns["get_integrated_ai_message"](weather, weather, "A", "B")
        self.assertIn("불러오지 못", text)
        self.assertNotIn("기온 차이", text)


class AppFlowTests(unittest.TestCase):
    def setUp(self):
        from streamlit.testing.v1 import AppTest
        self.AppTest = AppTest
        self.temp = tempfile.TemporaryDirectory()
        self.data_file = str(Path(self.temp.name) / "users.json")
        self.source = Path("app.py").read_text(encoding="utf-8").replace(
            'USER_SETTINGS_FILE = "user_settings.json"', f'USER_SETTINGS_FILE = {self.data_file!r}')
        # Test-only source in memory: do not start real scheduler or send external requests.
        self.source = self.source.replace('\nstart_background_scheduler()\n', '\n# scheduler disabled in test\n')
        self.api = patch("demo_support.api_request")
        self.mock_api = self.api.start()
        # Production comment calls are mocked; legacy UI tests never contact Gemini.
        self.comment_client = patch('weather_comment_ai.get_client', return_value=None)
        self.comment_client.start()
        from weather_comment_ai import clear_cache
        clear_cache()

    def tearDown(self):
        self.comment_client.stop()
        self.api.stop()
        self.temp.cleanup()

    def login(self, uid):
        state = support.prepare_login(None, {"user_reg": "seoul", "user_rt": "(퇴근) 노원"})
        self.mock_api.side_effect = [(200, {"access_token": "test", "refresh_token": "test-refresh"}),
                                     (200, {"id": uid, "properties": {"nickname": "테스트"}})]
        app = self.AppTest.from_string(self.source)
        app.query_params.update({"code": "mock-code", "state": state})
        app.run(timeout=15)
        self.assertEqual(len(app.exception), 0)
        return app

    def test_new_user_role_selection_and_favorite(self):
        app = self.login(12345)
        self.assertTrue(app.selectbox(key="user_board_st").disabled)
        self.assertEqual(len(app.selectbox(key="user_board_st").options), 1)
        self.assertFalse(app.session_state["is_admin"])
        self.assertEqual(app.selectbox(key="user_reg").value, "seoul")
        self.assertEqual(app.selectbox(key="user_rt").value, "(퇴근) 노원")
        self.assertEqual(len(app.tabs), 1)
        next(b for b in app.button if b.label == "⭐ 통합 즐겨찾기 추가").click().run()
        self.assertEqual(len(app.exception), 0)
        data = json.loads(Path(self.data_file).read_text(encoding="utf-8"))["12345"]
        self.assertEqual(len(data["settings"]), 1)
        self.assertFalse(data["settings"][0]["notify_enabled"])
        next(b for b in app.button if b.label == "⭐ 통합 즐겨찾기 추가").click().run()
        self.assertTrue(any("이미 등록" in w.value for w in app.warning))

    def test_admin_preview_and_return(self):
        app = self.login(5070327065)
        before = {key: app.selectbox(key=key).value for key in support.SELECTION_KEYS}
        next(b for b in app.button if b.label == "일반 사용자 모드로 보기").click().run()
        self.assertTrue(app.session_state["is_admin"])
        self.assertTrue(app.session_state["preview_user"])
        self.assertEqual(len(app.tabs), 1)
        next(b for b in app.button if b.label == "⭐ 통합 즐겨찾기 추가").click().run()
        self.assertEqual(len(app.exception), 0)

        next(b for b in app.button if b.label == "관리자 모드로 돌아가기").click().run()
        self.assertTrue(app.session_state["is_admin"])
        self.assertFalse(app.session_state["preview_user"])
        self.assertEqual({key: app.selectbox(key=key).value for key in support.SELECTION_KEYS}, before)
        self.assertEqual(len(app.exception), 0)

    def test_weather_preview_admin_only_and_user_mode_cleanup(self):
        admin = self.login(5070327065)
        self.assertTrue(any(e.label == '🧪 날씨 코멘트 테스트' for e in admin.expander))
        real_selection = {key: admin.selectbox(key=key).value for key in support.SELECTION_KEYS}
        with patch('weather_advice_preview.generate_preview', wraps=__import__('weather_advice_preview').generate_preview) as generate:
            admin.selectbox(key='weather_preview_scenario').select('명확한 눈 예보 + 추운 날씨').run()
            self.assertGreater(generate.call_count, 0)
            self.assertEqual(len(admin.exception), 0)
            self.assertTrue(any('눈이 예상됩니다' in i.value for i in admin.info))
            self.assertEqual({key: admin.selectbox(key=key).value for key in support.SELECTION_KEYS}, real_selection)
            with patch('weather_comment_ai.generate_once', return_value={
                    'text': '출근길에는 눈이 예상됩니다. 따뜻한 외투를 챙기세요.',
                    'source': 'gemini', 'reason': ''}) as ai:
                admin.button(key='weather_preview_gemini_generate').click().run()
                ai.assert_called_once()
                self.assertIn('weather_preview_gemini_result', admin.session_state)
            generate.reset_mock()
            next(b for b in admin.button if b.label == '일반 사용자 모드로 보기').click().run()
            generate.assert_not_called()
            self.assertFalse(any(e.label == '🧪 날씨 코멘트 테스트' for e in admin.expander))
            self.assertFalse(any(key.startswith('weather_preview_') for key in admin.session_state.filtered_state))
            user = self.login(12345)
            generate.assert_not_called()
            self.assertFalse(any(e.label == '🧪 날씨 코멘트 테스트' for e in user.expander))
            self.assertFalse(any('Gemini 문구 생성' in b.label or 'Gemini 코멘트 생성' in b.label for b in user.button))

    def test_morning_dropoff_selection_coordinates_and_favorite(self):
        fixtures = [dict(region='seoul', route_name=route, stop_name=name,
                         arrival_time='07:10', lat=lat, lon=127.1)
                    for route, name, lat in [('(퇴근) 노원', '퇴근 출발', 37.1),
                                            ('출근 테스트', '탑승A', 37.2),
                                            ('출근 테스트', 'D (하차만)', 37.3),
                                            ('출근 테스트', 'E (하차만)', 37.4)]]
        with patch.object(weather_api, 'load_routes_from_db', return_value=fixtures):
            app = self.login(12345)
            app.selectbox(key='user_rt').select('출근 테스트').run()
            self.assertEqual(len(app.exception), 0)
            self.assertEqual(app.selectbox(key='user_arrive_st_fixed').value, '판교 제2테크노밸리')
            self.assertEqual(app.selectbox(key='user_board_st').options, ['탑승A'])
            self.assertFalse(app.selectbox(key='user_board_st').disabled)
            self.assertEqual(app.selectbox(key='user_arrive_st_fixed').options,
                             ['판교 제2테크노밸리', 'D (하차만)', 'E (하차만)'])
            app.selectbox(key='user_arrive_st_fixed').select('D (하차만)').run()
            missing = weather_api._unavailable('날씨 정보를 불러오지 못했습니다.')
            with patch.object(weather_api, 'get_weather_forecast_by_coords', return_value=missing) as fetch:
                next(b for b in app.button if b.label == '🔍 탑승·하차 통합 날씨 조회').click().run()
            self.assertEqual(fetch.call_args_list[1].args, (37.3, 127.1))
            self.assertEqual(fetch.call_args_list[0].kwargs['target_datetime'],
                             fetch.call_args_list[1].kwargs['target_datetime'])
            next(b for b in app.button if b.label == '⭐ 통합 즐겨찾기 추가').click().run()
            self.assertEqual(len(app.exception), 0)
            favorite = json.loads(Path(self.data_file).read_text(encoding='utf-8'))['12345']['settings'][0]
            self.assertEqual(favorite['arrive_stop'], 'D (하차만)')
            self.assertEqual(favorite['arrive_lat'], 37.3)

    def test_route_changes_reset_stops_even_when_names_overlap(self):
        routes = {
            '출근 A': ['A 기본', '공통 탑승', '공통 (하차만)'],
            '출근 B': ['B 기본', '공통 탑승', '공통 (하차만)'],
            '(퇴근) A': ['퇴근 A 출발', '공통 하차', 'A 종점'],
            '(퇴근) B': ['퇴근 B 출발', '공통 하차', 'B 종점'],
        }
        fixtures = [dict(region='seoul', route_name=route, stop_name=stop,
                         arrival_time='07:10', lat=37.3, lon=127.1)
                    for route, stops in routes.items() for stop in stops]
        with patch.object(weather_api, 'load_routes_from_db', return_value=fixtures):
            app = self.AppTest.from_string(self.source).run(timeout=15)
            for source, destination in [('출근 A', '출근 B'),
                                        ('출근 B', '(퇴근) A'),
                                        ('(퇴근) A', '(퇴근) B'),
                                        ('(퇴근) B', '출근 A')]:
                with self.subTest(source=source, destination=destination):
                    app.selectbox(key='user_rt').select(source).run()
                    if '퇴근' in source:
                        app.selectbox(key='user_arrive_st').select('공통 하차').run()
                    else:
                        app.selectbox(key='user_board_st').select('공통 탑승').run()
                        app.selectbox(key='user_arrive_st_fixed').select('공통 (하차만)').run()
                        # An unrelated rerun must retain choices within the same route.
                        app.run()
                        self.assertEqual(app.selectbox(key='user_board_st').value, '공통 탑승')
                        self.assertEqual(app.selectbox(key='user_arrive_st_fixed').value, '공통 (하차만)')
                    app.selectbox(key='user_rt').select(destination).run()
                    self.assertEqual(len(app.exception), 0)
                    self.assertEqual(app.selectbox(key='user_board_st').value, routes[destination][0])
                    self.assertEqual(app.selectbox(key='user_board_st').disabled, '퇴근' in destination)
                    if '퇴근' in destination:
                        self.assertEqual(app.selectbox(key='user_arrive_st').value, routes[destination][-1])
                    else:
                        self.assertEqual(app.selectbox(key='user_arrive_st_fixed').value, '판교 제2테크노밸리')
                        self.assertIn('공통 (하차만)', app.selectbox(key='user_arrive_st_fixed').options)
                        self.assertNotIn('공통 (하차만)', app.selectbox(key='user_board_st').options)

    def test_login_failure_shows_message(self):
        state = support.prepare_login(None, {"user_reg": "seoul"})
        self.mock_api.return_value = (0, {})
        app = self.AppTest.from_string(self.source)
        app.query_params.update({"code": "mock", "state": state})
        app.run(timeout=15)
        self.assertEqual(len(app.exception), 0)
        self.assertTrue(any("연결하지 못" in e.value for e in app.error))
        self.assertEqual(app.selectbox(key="user_reg").value, "seoul")

    def test_cancelled_and_invalid_login(self):
        for state in [support.prepare_login(None, {}), "invalid-state"]:
            app = self.AppTest.from_string(self.source)
            app.query_params.update({"error": "access_denied", "state": state})
            app.run(timeout=15)
            self.assertEqual(len(app.exception), 0)
            self.assertGreater(len(app.error), 0)
        self.mock_api.assert_not_called()

    def test_boarding_forecast_pair_and_existing_comment_ui(self):
        app = self.login(12345)
        target = weather_api.resolve_boarding_datetime("17:25")
        weather = {"available": True, "temperature": "21°C", "sky_status": "맑음",
                   "rain_probability": "정보 없음", "message": "탑승 시간대에는 가벼운 겉옷을 준비하세요.",
                   "precipitation_note": "탑승 후 1~2시간 이내 강수 가능성이 있으니 우산을 챙기세요."}
        with patch.object(weather_api, "get_weather_forecast_by_coords", return_value=weather) as fetch:
            next(b for b in app.button if b.label == "🔍 탑승·하차 통합 날씨 조회").click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(fetch.call_count, 2)
        first, second = [call.kwargs for call in fetch.call_args_list]
        self.assertEqual(first["target_datetime"], target)
        self.assertEqual(first["target_datetime"], second["target_datetime"])
        self.assertEqual([first["location"], second["location"]], ["boarding", "destination"])
        comments = [i.value for i in app.info if "통합 AI 코멘트" in i.value]
        self.assertEqual(len(comments), 1)
        self.assertIn('퇴근길', comments[0])
        self.assertNotIn("기온 차이", comments[0])
        self.assertIn("우산", comments[0])
        visible = " ".join(e.value for collection in (app.info, app.caption, app.markdown) for e in collection)
        for internal in ("UltraSrtFcst", "VilageFcst", "초단기예보", "단기예보"):
            self.assertNotIn(internal, visible)
        self.assertIn(target.strftime("%Y-%m-%d %H:%M"), visible)

    def test_weather_failure_ui_and_message_failure(self):
        app = self.login(12345)
        missing = {"available": False, "temperature": "정보 없음", "sky_status": "정보 없음",
                   "rain_probability": "정보 없음", "message": "날씨 정보를 불러오지 못했습니다."}
        with patch.object(weather_api, "get_weather_forecast_by_coords", return_value=missing):
            next(b for b in app.button if b.label == "🔍 탑승·하차 통합 날씨 조회").click().run()
            self.assertEqual(len(app.exception), 0)
            self.assertTrue(any("불러오지 못" in w.value for w in app.warning))
            self.assertTrue(all(m.value == "정보 없음" for m in app.metric))
            self.mock_api.side_effect = None
            self.mock_api.return_value = (503, {})
            next(b for b in app.button if b.label == "💬 카카오톡 통합 날씨 전송").click().run()
            self.assertEqual(len(app.exception), 0)
            self.assertTrue(any("전송에 실패" in e.value for e in app.error))


if __name__ == "__main__":
    unittest.main()
