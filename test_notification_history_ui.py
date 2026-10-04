from datetime import datetime, time, timezone
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import notification_history_ui as history_ui
from notification_repository import NotificationHistoryEntry, NotificationRepositoryError


ENTRY = NotificationHistoryEntry(
    id=1,
    started_at=datetime(2026, 10, 1, 7, 50, tzinfo=timezone.utc),
    user_id=3,
    nickname="테스트",
    favorite_id=5,
    region_name="경기",
    route_name="경기 출근",
    trip_type="morning",
    boarding_name="탑승 정류장",
    destination_name="판교 제2테크노밸리",
    scheduled_time=time(8, 0),
    effective_channel="PUSH",
    status="PARTIAL",
    error_code="PUSH_SEND_FAILED",
    success_count=1,
    failed_count=1,
    expired_count=0,
)


def _app(admin=True, preview=False):
    return AppTest.from_string(
        "from notification_history_ui import render_admin_notification_history\n"
        f"render_admin_notification_history({admin!r}, {preview!r})"
    )


def test_admin_guard_hides_history_from_user_and_preview():
    with patch.object(history_ui, "_render_notification_history") as render:
        history_ui.render_admin_notification_history(False, False)
        history_ui.render_admin_notification_history(True, True)
        render.assert_not_called()
        history_ui.render_admin_notification_history(True, False)
        render.assert_called_once()
    assert not _app(False, False).run().expander
    assert not _app(True, True).run().expander


def test_history_is_lazy_refreshed_and_cached_without_repeat_query():
    with patch.object(history_ui, "load_notification_history", return_value=[ENTRY]) as load:
        app = _app().run()
        load.assert_not_called()
        assert app.expander[0].label == "🔔 알림 발송 이력"
        app.button(key="FormSubmitter:notification_history_form-🔄 새로고침").click().run()
        load.assert_called_once_with(3, limit=100)
        assert any("성공 1 / 실패 1 / 만료 0" in caption.value for caption in app.caption)
        app.run()
        load.assert_called_once()


def test_history_period_empty_result_and_database_error():
    with patch.object(history_ui, "load_notification_history", return_value=[]) as load:
        app = _app().run()
        app.selectbox(key="notification_history_period").select("최근 7일")
        app.button(key="FormSubmitter:notification_history_form-🔄 새로고침").click().run()
        load.assert_called_once_with(7, limit=100)
        assert any("조회 기간 내" in info.value for info in app.info)

    with patch.object(
        history_ui, "load_notification_history",
        side_effect=NotificationRepositoryError("internal connection detail"),
    ):
        app = _app().run()
        app.button(key="FormSubmitter:notification_history_form-🔄 새로고침").click().run()
        assert [error.value for error in app.error] == [
            "알림 발송 이력을 불러오지 못했습니다."
        ]


def test_delivery_summary_for_push_and_kakao():
    assert history_ui.delivery_summary(ENTRY) == "성공 1 / 실패 1 / 만료 0"
    kakao = NotificationHistoryEntry(
        **{**ENTRY.__dict__, "effective_channel": "KAKAO", "status": "SUCCESS",
           "error_code": None, "success_count": 1, "failed_count": 0}
    )
    assert history_ui.delivery_summary(kakao) == "카카오 발송 성공"
