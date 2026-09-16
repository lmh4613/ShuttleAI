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
                weather = weather_api.get_weather_forecast_by_coords(37.4, 127.1)
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

    def tearDown(self):
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
