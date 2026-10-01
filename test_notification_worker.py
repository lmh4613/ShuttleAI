from datetime import datetime, time
from unittest.mock import Mock, patch

import pytest

import notification_worker as worker
from notification_repository import (
    NotificationRepositoryError,
    NotificationTarget,
    PushDevice,
)
from web_push import WebPushConfig


KST = worker.KST
CONFIG = WebPushConfig("public", "private", "mailto:test@example.com", "http://localhost:8501")
WEATHER = {
    "available": True, "temperature": "19°C", "sky_status": "맑음",
    "precipitation_status": "none", "target_time": "2026-09-30 08:00",
}


def target(*, devices=None, trip_type="morning", scheduled=time(8, 0), weekday=2,
           channel="PUSH"):
    return NotificationTarget(
        user_id=1, favorite_id=2, active_weekday=weekday,
        exclude_holidays=True, timezone="Asia/Seoul", delivery_channel=channel,
        route_name="노선 A",
        trip_type=trip_type, scheduled_time=scheduled, lead_minutes=10,
        boarding_name="탑승지", boarding_latitude=37.1, boarding_longitude=127.1,
        destination_name="하차지", destination_latitude=37.2,
        destination_longitude=127.2, devices=list(devices or []),
    )


def device(number):
    return PushDevice(number, f"https://push.example.test/{number}", f"p{number}", f"a{number}")


class History:
    def __init__(self):
        self.claims = {}
        self.runs = {}
        self.deliveries = []

    def claim(self, user_id, favorite_id, service_date, scheduled_time, channel):
        key = (user_id, favorite_id, service_date, scheduled_time)
        if key in self.claims:
            return None
        run_id = len(self.claims) + 1
        self.claims[key] = run_id
        self.runs[run_id] = {"channel": channel, "status": "PROCESSING", "error": None}
        return run_id

    def start_delivery(self, run_id, channel, subscription_id=None):
        self.deliveries.append({
            "run_id": run_id, "channel": channel, "subscription_id": subscription_id,
            "status": "PROCESSING", "error": None,
        })
        return len(self.deliveries)

    def complete_delivery(self, delivery_id, status, error_code=None):
        self.deliveries[delivery_id - 1].update(status=status, error=error_code)

    def complete_run(self, run_id, status, error_code=None):
        self.runs[run_id].update(status=status, error=error_code)


def cycle(targets, *, push_sender=None, kakao_sender=None, sent=None, attempts=None,
          now=datetime(2026, 9, 30, 7, 50, tzinfo=KST), history=None):
    history = history or History()
    result = worker.run_notification_cycle(
        sent_cache=sent if sent is not None else {},
        attempt_cache=attempts if attempts is not None else {},
        config=CONFIG, now=now, target_loader=lambda _days: targets,
        policy_loader=lambda: "AUTO",
        holiday_checker=lambda _day: False,
        weather_fetch=Mock(return_value=dict(WEATHER)),
        advice_generator=Mock(return_value={"text": "얇은 겉옷을 챙기세요."}),
        push_sender=push_sender or Mock(return_value=(True, "ok")),
        kakao_sender=kakao_sender, run_claimer=history.claim,
        delivery_starter=history.start_delivery,
        delivery_completer=history.complete_delivery,
        run_completer=history.complete_run,
        monotonic=Mock(return_value=100),
    )
    return result


def test_lead_minutes_kst_and_midnight_rollover_without_arrival_inference():
    morning = target()
    due = worker.due_service_datetime(
        morning, datetime(2026, 9, 30, 7, 50, tzinfo=KST)
    )
    assert due[0].strftime("%Y-%m-%d %H:%M %Z") == "2026-09-30 08:00 KST"
    assert due[1].strftime("%H:%M") == "07:50"

    rollover = target(scheduled=time(0, 5), weekday=3)
    due = worker.due_service_datetime(
        rollover, datetime(2026, 9, 30, 23, 55, tzinfo=KST)
    )
    assert due[0].strftime("%Y-%m-%d %H:%M") == "2026-10-01 00:05"


def test_one_user_multiple_devices_and_same_logical_notification_dedup():
    sender = Mock(return_value=(True, "ok"))
    sent, attempts = {}, {}
    history = History()
    current = target(devices=[device(1), device(2)])
    result = cycle([current], push_sender=sender, sent=sent, attempts=attempts,
                   history=history)
    assert result["push_success"] == 2
    assert sender.call_count == 2
    first_body = sender.call_args_list[0].args[3]
    assert first_body.startswith("08:00 탑승 예정 ·")
    assert "탑승지" not in first_body and "하차지" not in first_body
    cycle([current], push_sender=sender, sent=sent, attempts=attempts, history=history)
    assert sender.call_count == 2
    assert history.runs[1]["status"] == "SUCCESS"
    assert [item["status"] for item in history.deliveries] == ["SUCCESS", "SUCCESS"]


def test_database_dedup_survives_worker_restart_with_empty_process_caches():
    history = History()
    sender = Mock(return_value=(True, "ok"))
    current = target(devices=[device(1)])
    cycle([current], push_sender=sender, sent={}, attempts={}, history=history)
    result = cycle([current], push_sender=sender, sent={}, attempts={}, history=history)
    assert sender.call_count == 1
    assert result["deduplicated"] == 1


def test_push_partial_failure_records_device_results_and_partial_run():
    history = History()
    sender = Mock(side_effect=[(False, "failed"), (True, "ok")])
    cycle([target(devices=[device(1), device(2)])], push_sender=sender, history=history)
    assert [item["status"] for item in history.deliveries] == ["FAILED", "SUCCESS"]
    assert history.runs[1] == {
        "channel": "PUSH", "status": "PARTIAL", "error": "PUSH_SEND_FAILED"
    }


def test_push_one_device_success_and_all_failed_run_statuses():
    success_history = History()
    cycle(
        [target(devices=[device(1)])], push_sender=Mock(return_value=(True, "ok")),
        history=success_history,
    )
    assert success_history.deliveries[0]["status"] == "SUCCESS"
    assert success_history.runs[1]["status"] == "SUCCESS"

    failed_history = History()
    cycle(
        [target(devices=[device(1), device(2)])],
        push_sender=Mock(return_value=(False, "failed")), history=failed_history,
    )
    assert [item["status"] for item in failed_history.deliveries] == ["FAILED", "FAILED"]
    assert failed_history.runs[1]["status"] == "FAILED"


def test_device_failure_continues_to_other_device_without_secret_logging(caplog):
    secret = "https://push.example.test/private-endpoint"
    current = target(devices=[PushDevice(1, secret, "private-key", "private-auth"), device(2)])
    sender = Mock(side_effect=[RuntimeError(secret), (True, "ok")])
    result = cycle([current], push_sender=sender)
    assert sender.call_count == 2
    assert result["push_success"] == 1
    assert secret not in caplog.text
    assert "private-key" not in caplog.text
    assert "private-auth" not in caplog.text


@pytest.mark.parametrize("status_code", [404, 410])
def test_expired_device_is_deactivated_and_other_device_continues(status_code):
    calls = []
    history = History()

    def sender(_subscription, _config, _title, _body, _url, expired_handler):
        if not calls:
            calls.append(status_code)
            expired_handler()
            return False, "expired"
        calls.append("success")
        return True, "ok"

    with patch.object(worker, "deactivate_push_device") as deactivate:
        result = cycle([target(devices=[device(1), device(2)])], push_sender=sender,
                       history=history)
    deactivate.assert_called_once_with(1, 1)
    assert calls == [status_code, "success"]
    assert result["push_success"] == 1
    assert [item["status"] for item in history.deliveries] == ["EXPIRED", "SUCCESS"]
    assert history.runs[1]["status"] == "PARTIAL"


def test_push_without_subscription_has_no_kakao_fallback():
    push = Mock()
    kakao = Mock()
    history = History()
    result = cycle([target()], push_sender=push, kakao_sender=kakao, history=history)
    push.assert_not_called()
    kakao.assert_not_called()
    assert result["kakao_success"] == 0
    assert history.deliveries == []
    assert history.runs[1]["status"] == "SKIPPED"
    assert history.runs[1]["error"] == "NO_PUSH_SUBSCRIPTION"


def test_kakao_channel_ignores_existing_push_devices():
    push = Mock()
    kakao = Mock(return_value=True)
    history = History()
    result = cycle(
        [target(channel="KAKAO", devices=[device(1)])],
        push_sender=push, kakao_sender=kakao, history=history,
    )
    push.assert_not_called()
    kakao.assert_called_once()
    assert result["kakao_success"] == 1
    assert history.deliveries[0]["status"] == "SUCCESS"
    assert history.deliveries[0]["subscription_id"] is None
    assert history.runs[1]["status"] == "SUCCESS"


def test_kakao_without_credentials_does_not_fallback_to_push():
    push = Mock()
    kakao = Mock(return_value=False)
    history = History()
    result = cycle(
        [target(channel="KAKAO", devices=[device(1)])],
        push_sender=push, kakao_sender=kakao, history=history,
    )
    push.assert_not_called()
    kakao.assert_called_once()
    assert result["push_success"] == 0
    assert history.deliveries[0]["status"] == "FAILED"
    assert history.runs[1]["status"] == "FAILED"


def test_db_failure_skips_cycle_without_json_fallback():
    def unavailable(_weekdays):
        raise NotificationRepositoryError("unavailable")

    push = Mock()
    kakao = Mock()
    result = worker.run_notification_cycle(
        sent_cache={}, attempt_cache={}, config=CONFIG,
        now=datetime(2026, 9, 30, 7, 50, tzinfo=KST),
        target_loader=unavailable, push_sender=push, kakao_sender=kakao,
        policy_loader=lambda: "AUTO",
    )
    assert result["db_skipped"]
    push.assert_not_called()
    kakao.assert_not_called()


def test_claim_database_failure_prevents_external_delivery():
    push = Mock()
    kakao = Mock()

    def failed_claim(*_args):
        raise NotificationRepositoryError("unavailable")

    result = worker.run_notification_cycle(
        sent_cache={}, attempt_cache={}, config=CONFIG,
        now=datetime(2026, 9, 30, 7, 50, tzinfo=KST),
        target_loader=lambda _days: [target(devices=[device(1)])],
        policy_loader=lambda: "AUTO", holiday_checker=lambda _day: False,
        run_claimer=failed_claim, push_sender=push, kakao_sender=kakao,
    )
    assert result["db_skipped"]
    push.assert_not_called()
    kakao.assert_not_called()


def test_active_day_holiday_and_evening_context():
    evening = target(trip_type="evening", weekday=2, devices=[device(1)])
    sender = Mock(return_value=(True, "ok"))
    result = worker.run_notification_cycle(
        sent_cache={}, attempt_cache={}, config=CONFIG,
        now=datetime(2026, 9, 30, 7, 50, tzinfo=KST),
        target_loader=lambda days: [evening] if 2 in days else [],
        policy_loader=lambda: "AUTO",
        holiday_checker=lambda _day: True, push_sender=sender,
    )
    assert result["push_success"] == 0
    sender.assert_not_called()


def test_worker_environment_policy():
    def getter(values):
        return lambda key: values.get(key)

    assert worker.worker_enabled(getter({}))
    assert not worker.worker_enabled(getter({"RENDER": "true"}))
    assert not worker.worker_enabled(getter({"SHUTTLE_WORKER_ENABLED": "false"}))
    assert worker.worker_enabled(getter({"SHUTTLE_WORKER_ENABLED": "true", "RENDER": "true"}))


@pytest.mark.parametrize(
    ("global_policy", "user_channel", "expected"),
    [
        ("AUTO", "PUSH", "PUSH"), ("AUTO", "KAKAO", "KAKAO"),
        ("PUSH", "PUSH", "PUSH"), ("PUSH", "KAKAO", "PUSH"),
        ("KAKAO", "PUSH", "KAKAO"), ("KAKAO", "KAKAO", "KAKAO"),
    ],
)
def test_resolve_delivery_channel(global_policy, user_channel, expected):
    assert worker.resolve_delivery_channel(global_policy, user_channel) == expected


def test_global_override_changes_runtime_channel_not_user_preference():
    current = target(channel="KAKAO", devices=[device(1)])
    push = Mock(return_value=(True, "ok"))
    history = History()
    result = worker.run_notification_cycle(
        sent_cache={}, attempt_cache={}, config=CONFIG,
        now=datetime(2026, 9, 30, 7, 50, tzinfo=KST),
        target_loader=lambda _days: [current], policy_loader=lambda: "PUSH",
        holiday_checker=lambda _day: False,
        weather_fetch=Mock(return_value=dict(WEATHER)),
        advice_generator=Mock(return_value={"text": "안내"}),
        push_sender=push, kakao_sender=Mock(), run_claimer=history.claim,
        delivery_starter=history.start_delivery,
        delivery_completer=history.complete_delivery,
        run_completer=history.complete_run, monotonic=Mock(return_value=100),
    )
    assert result["push_success"] == 1
    assert current.delivery_channel == "KAKAO"


def test_duplicate_worker_start_is_blocked(monkeypatch):
    class FakeThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.started = False
        def start(self): self.started = True
        def is_alive(self): return self.started

    created = []
    def factory(**kwargs):
        thread = FakeThread(**kwargs)
        created.append(thread)
        return thread

    monkeypatch.setattr(worker, "_worker_thread", None)
    monkeypatch.setattr(worker, "worker_enabled", lambda: True)
    args = dict(config=CONFIG, holiday_checker=Mock(), kakao_sender=Mock(),
                thread_factory=factory)
    assert worker.start_notification_worker(**args)
    assert not worker.start_notification_worker(**args)
    assert len(created) == 1
    assert created[0].kwargs["daemon"] is True
