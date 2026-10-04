import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from web_push import (
    PUSH_BODY,
    PUSH_TITLE,
    WebPushConfig,
    build_test_payload,
    load_web_push_config,
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
    assert calls[0]["ttl"] == 60
    assert calls[0]["timeout"] == 15


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
    ownership.assert_called_once_with(101, VALID_SUBSCRIPTION)

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
