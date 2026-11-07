import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from web_push import (
    PUSH_BODY,
    PUSH_TITLE,
    WEB_PUSH_TTL_SECONDS,
    WebPushConfig,
    build_test_payload,
    load_web_push_config,
    send_web_push,
    send_test_push,
    sync_subscription,
    validate_subscription,
)


VALID_SUBSCRIPTION = {
    "endpoint": "https://push.example.test/subscription/secret-value",
    "expirationTime": None,
    "keys": {"p256dh": "public-browser-key", "auth": "auth-secret"},
}


def ready_config(**changes):
    values = {
        "public_key": "public-vapid-key",
        "private_key": "private-vapid-key",
        "subject": "mailto:admin@example.com",
        "click_url": "",
    }
    values.update(changes)
    return WebPushConfig(**values)


def test_vapid_config_validation_and_loading():
    source = {
        "WEB_PUSH_VAPID_PUBLIC_KEY": " public ",
        "WEB_PUSH_VAPID_PRIVATE_KEY": " private ",
        "WEB_PUSH_VAPID_SUBJECT": "mailto:admin@example.com",
        "WEB_PUSH_CLICK_URL": "https://shuttle.example/app",
    }
    config = load_web_push_config(lambda name, default="": source.get(name, default))

    assert config.ready
    assert config.public_key == "public"
    assert config.click_url == "https://shuttle.example/app"
    assert WebPushConfig().missing == (
        "WEB_PUSH_VAPID_PUBLIC_KEY",
        "WEB_PUSH_VAPID_PRIVATE_KEY",
        "WEB_PUSH_VAPID_SUBJECT",
    )
    assert not ready_config(subject="admin@example.com").ready


def test_subscription_validation_preserves_required_push_structure():
    assert validate_subscription(VALID_SUBSCRIPTION) == VALID_SUBSCRIPTION

    for invalid in (
        None,
        {},
        {"endpoint": "http://push.example.test", "keys": VALID_SUBSCRIPTION["keys"]},
        {"endpoint": VALID_SUBSCRIPTION["endpoint"], "keys": {"auth": "x"}},
    ):
        with pytest.raises(ValueError):
            validate_subscription(invalid)


def test_subscription_is_session_only_and_unsubscribe_removes_it():
    session = {}
    assert sync_subscription(
        session,
        VALID_SUBSCRIPTION,
        "registered",
        "http://localhost:8501/",
    )
    assert session["web_push_subscription"] == VALID_SUBSCRIPTION
    assert session["web_push_click_url"] == "http://localhost:8501/"

    assert sync_subscription(session, None, "unsubscribed")
    assert "web_push_subscription" not in session
    assert "web_push_click_url" not in session


def test_push_payload_is_fixed_json_and_uses_valid_click_url_only():
    payload = json.loads(build_test_payload("https://shuttle.example/app"))
    assert payload == {
        "title": PUSH_TITLE,
        "body": PUSH_BODY,
        "url": "https://shuttle.example/app",
    }
    assert "url" not in json.loads(build_test_payload("javascript:alert(1)"))


def test_test_push_uses_only_webpush_sender():
    calls = []

    def sender(**kwargs):
        calls.append(kwargs)

    ok, message = send_test_push(
        VALID_SUBSCRIPTION,
        ready_config(),
        "http://localhost:8501/",
        sender=sender,
    )

    assert ok
    assert "발송" in message
    assert len(calls) == 1
    assert calls[0]["subscription_info"] == VALID_SUBSCRIPTION
    assert json.loads(calls[0]["data"])["title"] == PUSH_TITLE
    assert calls[0]["vapid_claims"] == {"sub": "mailto:admin@example.com"}
    assert calls[0]["ttl"] == WEB_PUSH_TTL_SECONDS
    assert calls[0]["timeout"] == 15
    assert "headers" not in calls[0]


def test_scheduled_push_can_set_urgency_header_without_changing_default():
    calls = []

    def sender(**kwargs):
        calls.append(kwargs)

    ok, _message = send_web_push(
        VALID_SUBSCRIPTION,
        ready_config(),
        "[ShuttleAI 예약 Push 테스트]",
        "예약: 18:30:00",
        sender=sender,
        urgency="high",
        include_received_time=True,
    )

    assert ok
    assert calls[0]["headers"] == {"Urgency": "high"}
    assert calls[0]["ttl"] == WEB_PUSH_TTL_SECONDS
    payload = json.loads(calls[0]["data"])
    assert payload["include_received_time"] is True


def test_test_push_handles_missing_config_and_expired_subscription():
    called = False

    def sender(**_kwargs):
        nonlocal called
        called = True

    ok, message = send_test_push(VALID_SUBSCRIPTION, WebPushConfig(), sender=sender)
    assert not ok
    assert "VAPID" in message
    assert not called

    class ExpiredPush(Exception):
        status_code = 410

    def expired_sender(**_kwargs):
        raise ExpiredPush()

    ok, message = send_test_push(
        VALID_SUBSCRIPTION, ready_config(), sender=expired_sender
    )
    assert not ok
    assert "만료" in message


def test_service_worker_and_component_keep_web_push_isolated():
    service_worker = Path("web_push_component_assets/push-sw.js").read_text("utf-8")
    component = Path("web_push_ui.py").read_text("utf-8")

    assert 'addEventListener("push"' in service_worker
    assert 'addEventListener("notificationclick"' in service_worker
    assert "showNotification" in service_worker
    assert "include_received_time" in service_worker
    assert "수신:" in service_worker
    assert "Notification.requestPermission()" in component
    assert "navigator.serviceWorker.register" in component
    assert "@media (max-width: 640px)" in component
    assert "width: 100%" in component
    assert "eval(" not in component
    assert "weather_api" not in component
    assert "server_state" in component
    assert "unsubscribed_subscription" in component
    assert "service_worker_scope" in component


def test_web_push_ui_renders_without_vapid_secrets():
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_string(
        "from web_push import WebPushConfig\n"
        "from web_push_ui import render_web_push_poc\n"
        "render_web_push_poc(WebPushConfig())\n"
    ).run()

    assert not app.exception
    assert any("로그인 후" in info.value for info in app.info)


def test_web_push_display_state_has_one_authoritative_result():
    from push_subscription_store import OWNERSHIP_CURRENT, OWNERSHIP_NONE, OWNERSHIP_OTHER
    from web_push_ui import DB_UNAVAILABLE, web_push_display_state

    assert web_push_display_state(False, OWNERSHIP_CURRENT) == "NO_BROWSER_SUBSCRIPTION"
    assert web_push_display_state(True, OWNERSHIP_CURRENT) == "REGISTERED_TO_CURRENT_USER"
    assert web_push_display_state(True, OWNERSHIP_OTHER) == "REGISTERED_TO_OTHER_USER"
    assert web_push_display_state(True, OWNERSHIP_NONE) == "REGISTERED_TO_OTHER_USER"
    assert web_push_display_state(True, DB_UNAVAILABLE) == "DB_UNAVAILABLE"


def test_component_uses_db_ownership_before_rendering_registered_state():
    import web_push_ui

    component = web_push_ui._COMPONENT_JS
    assert "data.server_state === 'current' ? 'registered' : 'browser_only'" in component
    assert "data.server_state === 'unavailable'" in component
    assert "data.server_state === 'unknown'" in component
    assert "['registered', 'db_unavailable', 'loading'].includes(state)" in component
    assert "data.server_registered" not in component
    assert component.count("setStateValue(") == 1
    assert "setStateValue('event'" in component


def test_coherent_push_event_is_processed_once_and_invalidates_on_mutation():
    from web_push_ui import handle_web_push_event

    session = {"web_push_db_ownership": "unknown"}
    register = Mock()
    invalidate = Mock()
    event = {
        "action": "register", "action_id": "one", "browser_status": "loading",
        "subscription": VALID_SUBSCRIPTION, "app_url": "http://localhost:8501/",
        "unsubscribed_subscription": None,
    }
    assert handle_web_push_event(
        session, 101, event, registerer=register, on_data_changed=invalidate
    )
    register.assert_called_once_with(101, VALID_SUBSCRIPTION)
    invalidate.assert_called_once()
    assert session["web_push_db_ownership"] == "current"
    assert not handle_web_push_event(
        session, 101, event, registerer=register, on_data_changed=invalidate
    )
    register.assert_called_once()


def test_push_inspect_reruns_only_when_ownership_changes_and_unsubscribe_invalidates():
    from web_push_ui import handle_web_push_event

    session = {
        "web_push_subscription": VALID_SUBSCRIPTION,
        "web_push_db_ownership": "unknown",
    }
    ownership = Mock(return_value="current")
    inspect = {
        "action": "inspect", "action_id": "inspect-1", "browser_status": "loading",
        "subscription": VALID_SUBSCRIPTION, "app_url": "",
    }
    assert handle_web_push_event(session, 101, inspect, ownership_loader=ownership)
    same_state = {**inspect, "action_id": "inspect-2"}
    assert not handle_web_push_event(session, 101, same_state, ownership_loader=ownership)
    assert ownership.call_count == 2
    ownership.assert_called_with(101, VALID_SUBSCRIPTION)

    deactivate = Mock()
    invalidate = Mock()
    unsubscribe = {
        "action": "unsubscribe", "action_id": "unsubscribe-1",
        "browser_status": "unsubscribed", "subscription": None,
        "unsubscribed_subscription": VALID_SUBSCRIPTION, "app_url": "",
    }
    assert handle_web_push_event(
        session, 101, unsubscribe, deactivator=deactivate,
        on_data_changed=invalidate,
    )
    deactivate.assert_called_once_with(101, VALID_SUBSCRIPTION)
    invalidate.assert_called_once()
    assert session["web_push_db_ownership"] == "none"


# Retry policy tests use explicit HTTP responses, never live Push services.
def http_failure(status, retry_after=None):
    from requests import Response
    from pywebpush import WebPushException
    response = Response()
    response.status_code = status
    if retry_after is not None:
        response.headers["Retry-After"] = retry_after
    return WebPushException("private response body", response=response)


@pytest.fixture
def retry_clock(monkeypatch):
    import web_push
    clock = [0.0]
    sleep = Mock(side_effect=lambda delay: clock.__setitem__(0, clock[0] + delay))
    monkeypatch.setattr(web_push.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(web_push.time, "sleep", sleep)
    return clock, sleep


@pytest.mark.parametrize("status", [500, 502, 503, 504])
@pytest.mark.parametrize("source", ["automatic", "scheduled_test", "manual"])
def test_transient_http_retry_success(status, source, retry_clock, caplog):
    caplog.set_level("INFO", logger="web_push")
    sender = Mock(side_effect=[http_failure(status), None])
    expired = Mock()
    ok, _ = send_web_push(VALID_SUBSCRIPTION, ready_config(), "title", "body",
                         sender=sender, expired_handler=expired, delivery_source=source)
    assert ok and sender.call_count == 2
    retry_clock[1].assert_called_once_with(1)
    expired.assert_not_called()
    assert sender.call_args_list[0].kwargs["data"] == sender.call_args_list[1].kwargs["data"]
    assert sender.call_args_list[1].kwargs["timeout"] == 14
    assert f"http_status={status} source={source} retries=1" in caplog.text
    assert f"source={source} retries=1 success=True" in caplog.text
    for secret in ("private response body", VALID_SUBSCRIPTION["endpoint"], "auth-secret", "private-vapid-key"):
        assert secret not in caplog.text


@pytest.mark.parametrize("second", [503, 410, 429])
def test_retry_stops_after_second_failure(second, retry_clock, caplog):
    sender = Mock(side_effect=[http_failure(500), http_failure(second)])
    expired = Mock(return_value=True)
    ok, _ = send_test_push(VALID_SUBSCRIPTION, ready_config(), sender=sender, expired_handler=expired)
    assert not ok and sender.call_count == 2
    assert expired.call_count == (1 if second == 410 else 0)
    assert f"http_status={second} source=manual retries=1 success=False" in caplog.text


@pytest.mark.parametrize("header,expected", [(None, 1), ("0", 2), ("1", 2), ("2", 2),
                                           ("3", 1), ("3600", 1), ("bad", 1), ("-1", 1), ("NaN", 1)])
def test_429_requires_short_valid_retry_after(header, expected, retry_clock):
    sender = Mock(side_effect=[http_failure(429, header), None])
    ok, _ = send_test_push(VALID_SUBSCRIPTION, ready_config(), sender=sender)
    assert sender.call_count == expected
    assert ok == (expected == 2)
    assert retry_clock[1].call_count == expected - 1


@pytest.mark.parametrize("offset,expected", [(1, 2), (30, 1), (-30, 1)])
def test_retry_after_http_date(offset, expected, retry_clock, monkeypatch):
    from datetime import datetime, timezone, timedelta
    from email.utils import format_datetime
    fixed = datetime(2026, 11, 7, tzinfo=timezone.utc)
    monkeypatch.setattr("web_push.datetime", Mock(now=Mock(return_value=fixed)))
    sender = Mock(side_effect=[http_failure(429, format_datetime(fixed + timedelta(seconds=offset))), None])
    send_test_push(VALID_SUBSCRIPTION, ready_config(), sender=sender)
    assert sender.call_count == expected


@pytest.mark.parametrize("status", [401, 403, 404, 410, 400, 501])
def test_permanent_http_errors_do_not_retry(status, retry_clock):
    sender = Mock(side_effect=http_failure(status, "1"))
    expired = Mock(return_value=True)
    ok, _ = send_test_push(VALID_SUBSCRIPTION, ready_config(), sender=sender, expired_handler=expired)
    assert not ok
    sender.assert_called_once()
    retry_clock[1].assert_not_called()
    assert expired.call_count == (1 if status in {404, 410} else 0)


@pytest.mark.parametrize("kind", ["timeout", "connection", "unknown", "status_only"])
def test_uncertain_transport_never_retries(kind, retry_clock):
    from requests.exceptions import Timeout, ConnectionError
    errors = {"timeout": Timeout("secret"), "connection": ConnectionError("secret"),
              "unknown": RuntimeError("secret"), "status_only": RuntimeError("secret")}
    error = errors[kind]
    if kind == "status_only":
        error.status_code = 503
    sender = Mock(side_effect=error)
    ok, _ = send_test_push(VALID_SUBSCRIPTION, ready_config(), sender=sender)
    assert not ok
    sender.assert_called_once()
    retry_clock[1].assert_not_called()


def test_slow_first_request_and_long_server_retry_after_do_not_wait(retry_clock):
    clock, sleep = retry_clock
    def slow(**kwargs):
        clock[0] += 14
        raise http_failure(503)
    sender = Mock(side_effect=slow)
    assert not send_test_push(VALID_SUBSCRIPTION, ready_config(), sender=sender)[0]
    sender.assert_called_once()
    sleep.assert_not_called()
    sender = Mock(side_effect=http_failure(503, "120"))
    assert not send_test_push(VALID_SUBSCRIPTION, ready_config(), sender=sender)[0]
    sender.assert_called_once()
    sleep.assert_not_called()


@pytest.mark.parametrize("source", ["automatic", "scheduled_test"])
def test_retry_records_only_final_device_result_and_preserves_claims(source, retry_clock):
    from test_notification_worker import (History, ScheduledHistory, cycle, run_scheduled,
                                          target, device, scheduled_test)
    transports = {1: Mock(side_effect=[http_failure(503), None]),
                  2: Mock(side_effect=[http_failure(500), http_failure(503)])}
    def sender(sub, config, title, body, url, **kwargs):
        return send_web_push(sub, config, title, body, url,
                             sender=transports[kwargs["subscription_id"]], **kwargs)
    if source == "automatic":
        history = History()
        for _ in range(2):
            cycle([target(devices=[device(1), device(2)])], push_sender=sender, history=history)
    else:
        history = ScheduledHistory(tests=[scheduled_test()], devices=[device(1), device(2)])
        for _ in range(2):
            run_scheduled(history, sender=sender)
    assert [d["status"] for d in history.deliveries] == ["SUCCESS", "FAILED"]
    assert len(history.deliveries) == 2
    assert all(t.call_count == 2 for t in transports.values())


def test_first_success_does_not_sleep_or_retry(retry_clock, caplog):
    from requests import Response
    response = Response()
    response.status_code = 201
    sender = Mock(return_value=response)
    caplog.set_level("INFO", logger="web_push")
    assert send_test_push(VALID_SUBSCRIPTION, ready_config(), sender=sender)[0]
    sender.assert_called_once()
    retry_clock[1].assert_not_called()
    assert "http_status=201 source=manual retries=0 success=True" in caplog.text


def test_budget_exhausted_during_sleep_prevents_resend(retry_clock):
    clock, sleep = retry_clock
    sleep.side_effect = lambda delay: clock.__setitem__(0, 16)
    sender = Mock(side_effect=http_failure(503))
    assert not send_test_push(VALID_SUBSCRIPTION, ready_config(), sender=sender)[0]
    sender.assert_called_once()
