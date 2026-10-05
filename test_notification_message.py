import json

from notification_message import format_weather_notification
from push_subscription_store import OWNERSHIP_CURRENT, OWNERSHIP_OTHER
from web_push import WebPushConfig, send_web_push
from web_push_ui import can_send_weather_push


def weather(temp, sky):
    return {"temperature": temp, "sky_status": sky}


def test_manual_kakao_and_push_share_complete_weather_message():
    message = format_weather_notification(
        route_name="(출근) 서울시청 1호",
        trip_type="출근길",
        boarding_stop="탑승 정류장",
        boarding_time="06:28",
        boarding_weather=weather("19°C", "맑음"),
        destination_stop="판교 제2테크노밸리",
        destination_weather=weather("18°C", "구름많음"),
        comment="얇은 겉옷을 챙기세요.",
    )

    assert "(출근) 서울시청 1호" in message.title
    for expected in (
        "탑승 정류장", "06:28", "19°C", "맑음",
        "판교 제2테크노밸리", "18°C", "구름많음", "얇은 겉옷",
    ):
        assert expected in message.body

    sent = {}
    config = WebPushConfig("public", "private", "mailto:test@example.com", "")
    subscription = {
        "endpoint": "https://push.example.test/current",
        "keys": {"p256dh": "public", "auth": "auth"},
    }
    ok, _ = send_web_push(
        subscription, config, message.title, message.body,
        sender=lambda **kwargs: sent.update(kwargs),
    )
    assert ok
    payload = json.loads(sent["data"])
    assert payload == {"title": message.title, "body": message.body}


def test_push_send_guard_requires_current_browser_owner_and_current_result():
    session = {
        "web_push_subscription": {"endpoint": "https://push.example.test/current"},
        "web_push_db_ownership": OWNERSHIP_CURRENT,
    }
    assert can_send_weather_push(
        session, logged_in=True, preview_user=False, result_matches=True
    )
    session["web_push_db_ownership"] = OWNERSHIP_OTHER
    assert not can_send_weather_push(
        session, logged_in=True, preview_user=False, result_matches=True
    )
    session["web_push_db_ownership"] = OWNERSHIP_CURRENT
    assert not can_send_weather_push(
        session, logged_in=True, preview_user=True, result_matches=True
    )
    assert not can_send_weather_push(
        session, logged_in=True, preview_user=False, result_matches=False
    )
