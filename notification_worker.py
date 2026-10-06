"""Local DB-backed notification worker with explicit delivery-channel policy."""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import weather_api
from notification_repository import (
    NotificationTarget,
    claim_notification_run,
    complete_notification_delivery,
    complete_notification_run,
    deactivate_push_device,
    load_notification_targets,
    start_notification_delivery,
)
from scheduled_push_repository import (
    claim_due_scheduled_push_tests,
    complete_scheduled_push_delivery,
    complete_scheduled_push_test,
    load_scheduled_push_devices_for_test,
    start_scheduled_push_delivery,
)
from weather_comment_ai import generate_weather_advice
from web_push import WebPushConfig, send_web_push
from user_settings_repository import get_global_notification_policy


logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")
_worker_lock = threading.Lock()
_worker_thread = None


def worker_enabled(getter=os.getenv) -> bool:
    """Preserve local behavior, while defaulting Render instances to disabled."""
    configured = getter("SHUTTLE_WORKER_ENABLED")
    if configured is None or not str(configured).strip():
        return not bool(getter("RENDER"))
    return str(configured).strip().lower() in {"1", "true", "yes", "on"}


def resolve_delivery_channel(global_policy: str, user_channel: str) -> str:
    """Resolve the runtime override without mutating the user's preference."""
    if global_policy in {"PUSH", "KAKAO"}:
        return global_policy
    if global_policy == "AUTO" and user_channel in {"PUSH", "KAKAO"}:
        return user_channel
    raise ValueError("Invalid notification channel policy")


def as_kst(value: datetime | None = None) -> datetime:
    value = value or datetime.now(KST)
    if value.tzinfo is None:
        return value.replace(tzinfo=KST)
    return value.astimezone(KST)


def due_service_datetime(
    target: NotificationTarget,
    now: datetime,
    *,
    window_seconds: int = 120,
) -> tuple[datetime, datetime] | None:
    """Return service/notification datetimes without inferring an arrival time."""
    now = as_kst(now)
    for day_offset in (0, 1):
        service_date = now.date() + timedelta(days=day_offset)
        if service_date.weekday() != target.active_weekday:
            continue
        boarding = datetime.combine(service_date, target.scheduled_time, tzinfo=KST)
        notify_at = boarding - timedelta(minutes=target.lead_minutes)
        if 0 <= (now - notify_at).total_seconds() < window_seconds:
            return boarding, notify_at
    return None


def notification_key(target: NotificationTarget, boarding: datetime) -> tuple:
    return (
        target.user_id,
        target.favorite_id,
        boarding.date().isoformat(),
        boarding.strftime("%H:%M"),
    )


def _messages(target, boarding, boarding_weather, destination_weather, comment):
    trip = "퇴근" if target.trip_type == "evening" else "출근"
    boarding_time = boarding.strftime("%H:%M")
    push_title = f"ShuttleAI {trip} 알림"
    push_body = f"{boarding_time} 탑승 예정 · {comment}"
    kakao_title = f"[{target.route_name}] 탑승·하차 날씨 알림"
    kakao_body = (
        f"🚍 [셔틀 출발 {target.lead_minutes}분 전 알림]\n\n"
        f"노선: {target.route_name} ({trip}길)\n\n"
        f"🟢 [탑승] {target.boarding_name} ({boarding_time})\n"
        f"• 기온: {boarding_weather['temperature']} | 상태: {boarding_weather['sky_status']}\n\n"
        f"🔴 [하차] {target.destination_name}\n"
        f"• 기온: {destination_weather['temperature']} | 상태: {destination_weather['sky_status']}\n\n"
        f"🤖 **[AI 코멘트]**\n{comment}"
    )
    return push_title, push_body, kakao_title, kakao_body


def _scheduled_push_test_message(test, now: datetime) -> tuple[str, str]:
    scheduled_at = as_kst(test.scheduled_at)
    send_at = as_kst(now)
    return (
        "[ShuttleAI 예약 Push 테스트]",
        "\n".join([
            f"예약: {scheduled_at:%H:%M:%S}",
            f"서버 발송: {send_at:%H:%M:%S}",
            f"TTL: {test.ttl_seconds}s",
            f"Urgency: {test.urgency}",
            f"메시지: {test.message}",
        ]),
    )


def run_scheduled_push_test_cycle(
    *,
    config: WebPushConfig,
    now: datetime | None = None,
    push_sender=send_web_push,
    due_loader=claim_due_scheduled_push_tests,
    device_loader=load_scheduled_push_devices_for_test,
    delivery_starter=start_scheduled_push_delivery,
    delivery_completer=complete_scheduled_push_delivery,
    test_completer=complete_scheduled_push_test,
) -> dict:
    """Send due admin scheduled Push tests through the real Web Push path."""
    now = as_kst(now)
    stats = {
        "scheduled_tests": 0,
        "push_success": 0,
        "db_skipped": False,
    }
    try:
        tests = due_loader(now)
    except Exception as exc:
        logger.warning("Scheduled Push test cycle skipped type=%s", type(exc).__name__)
        stats["db_skipped"] = True
        return stats

    stats["scheduled_tests"] = len(tests)
    for test in tests:
        try:
            devices = device_loader(test)
        except Exception as exc:
            logger.warning("Scheduled Push test device query failed type=%s", type(exc).__name__)
            try:
                test_completer(test.id, "FAILED", "NO_PUSH_SUBSCRIPTION")
            except Exception as history_exc:
                logger.warning("Scheduled Push test completion failed type=%s",
                               type(history_exc).__name__)
            continue

        if not devices:
            try:
                test_completer(test.id, "FAILED", "NO_PUSH_SUBSCRIPTION")
            except Exception as exc:
                logger.warning("Scheduled Push test completion failed type=%s",
                               type(exc).__name__)
            continue

        title, body = _scheduled_push_test_message(test, now)
        success_count = 0
        expired_count = 0
        for device in devices:
            try:
                delivery_id = delivery_starter(test.id, device.id)
            except Exception as exc:
                logger.warning("Scheduled Push delivery start failed type=%s",
                               type(exc).__name__)
                stats["db_skipped"] = True
                continue

            expired = False

            def expire_device(device=device):
                nonlocal expired
                expired = True
                deactivate_push_device(device.id, test.user_id)

            try:
                ok, _message = push_sender(
                    device.subscription(),
                    config,
                    title,
                    body,
                    config.click_url,
                    expired_handler=expire_device,
                    urgency=test.urgency,
                    include_received_time=True,
                )
                if ok:
                    success_count += 1
                    stats["push_success"] += 1
                    delivery_status, delivery_error = "SUCCESS", None
                elif expired:
                    expired_count += 1
                    delivery_status, delivery_error = "EXPIRED", "PUSH_EXPIRED"
                else:
                    delivery_status, delivery_error = "FAILED", "PUSH_SEND_FAILED"
            except Exception as exc:
                logger.warning("Scheduled Push send failed type=%s", type(exc).__name__)
                delivery_status, delivery_error = "FAILED", "PUSH_SEND_FAILED"

            try:
                delivery_completer(delivery_id, delivery_status, delivery_error)
            except Exception as exc:
                logger.warning("Scheduled Push delivery completion failed type=%s",
                               type(exc).__name__)

        if success_count == len(devices):
            test_status, test_error = "SUCCESS", None
        elif success_count:
            test_status, test_error = "PARTIAL", "PUSH_SEND_FAILED"
        else:
            test_status = "FAILED"
            test_error = (
                "PUSH_EXPIRED" if expired_count == len(devices)
                else "PUSH_SEND_FAILED"
            )
        try:
            test_completer(test.id, test_status, test_error)
        except Exception as exc:
            logger.warning("Scheduled Push test completion failed type=%s",
                           type(exc).__name__)
    return stats


def run_notification_cycle(
    *,
    sent_cache: dict,
    attempt_cache: dict,
    config: WebPushConfig,
    now: datetime | None = None,
    holiday_checker=lambda _service_date: False,
    target_loader=load_notification_targets,
    policy_loader=get_global_notification_policy,
    weather_fetch=weather_api.get_weather_forecast_by_coords,
    advice_generator=generate_weather_advice,
    push_sender=send_web_push,
    kakao_sender=None,
    run_claimer=claim_notification_run,
    delivery_starter=start_notification_delivery,
    delivery_completer=complete_notification_delivery,
    run_completer=complete_notification_run,
    monotonic=time.monotonic,
) -> dict:
    """Process one cycle. DB failures skip the cycle and never fall back to JSON."""
    now = as_kst(now)
    weekdays = {now.weekday(), (now + timedelta(days=1)).weekday()}
    stats = {
        "targets": 0, "push_success": 0, "kakao_success": 0,
        "deduplicated": 0, "db_skipped": False,
    }
    try:
        global_policy = policy_loader()
        targets = target_loader(weekdays)
    except Exception as exc:
        logger.warning("Notification cycle skipped: settings database unavailable type=%s",
                       type(exc).__name__)
        stats["db_skipped"] = True
        return stats

    stats["targets"] = len(targets)
    for target in targets:
        due = due_service_datetime(target, now)
        if due is None:
            continue
        boarding, _notify_at = due
        try:
            holiday = target.exclude_holidays and holiday_checker(boarding.date())
        except Exception as exc:
            logger.warning("Holiday lookup failed type=%s", type(exc).__name__)
            holiday = False
        if holiday:
            continue

        key = notification_key(target, boarding)
        attempts, last_attempt = attempt_cache.get(key, (0, float("-inf")))
        current_tick = monotonic()
        if key in sent_cache or attempts >= 2 or current_tick - last_attempt < 30:
            continue

        try:
            effective_channel = resolve_delivery_channel(
                global_policy, target.delivery_channel
            )
        except ValueError:
            logger.warning("Notification skipped: invalid channel policy")
            continue

        try:
            run_id = run_claimer(
                target.user_id, target.favorite_id, boarding.date(),
                target.scheduled_time, effective_channel,
            )
        except Exception as exc:
            logger.warning("Notification claim failed; send skipped type=%s", type(exc).__name__)
            stats["db_skipped"] = True
            continue
        if run_id is None:
            stats["deduplicated"] += 1
            continue
        attempt_cache[key] = (attempts + 1, current_tick)

        trip_type = "퇴근길" if target.trip_type == "evening" else "출근길"
        try:
            boarding_weather = weather_fetch(
                target.boarding_latitude, target.boarding_longitude,
                stop_name=target.boarding_name, trip_type=trip_type,
                target_datetime=boarding, location="boarding",
            )
            destination_weather = weather_fetch(
                target.destination_latitude, target.destination_longitude,
                stop_name=target.destination_name, trip_type=trip_type,
                target_datetime=boarding, location="destination",
            )
            comment = advice_generator(boarding_weather, destination_weather, trip_type)["text"]
            push_title, push_body, kakao_title, kakao_body = _messages(
                target, boarding, boarding_weather, destination_weather, comment
            )
        except Exception as exc:
            logger.warning("Notification weather generation failed type=%s", type(exc).__name__)
            try:
                run_completer(run_id, "FAILED", "WEATHER_UNAVAILABLE")
            except Exception as history_exc:
                logger.warning("Notification run completion failed type=%s",
                               type(history_exc).__name__)
            continue

        delivered = False
        if effective_channel == "PUSH":
            if not target.devices:
                logger.info("Push notification skipped: no active device")
                try:
                    run_completer(run_id, "SKIPPED", "NO_PUSH_SUBSCRIPTION")
                except Exception as exc:
                    logger.warning("Notification run completion failed type=%s", type(exc).__name__)
                continue
            success_count = 0
            expired_count = 0
            for device in target.devices:
                try:
                    delivery_id = delivery_starter(run_id, "PUSH", device.id)
                except Exception as exc:
                    logger.warning("Push delivery history start failed; send skipped type=%s",
                                   type(exc).__name__)
                    stats["db_skipped"] = True
                    continue
                expired = False

                def expire_device(device=device):
                    nonlocal expired
                    expired = True
                    deactivate_push_device(device.id, target.user_id)

                try:
                    ok, _message = push_sender(
                        device.subscription(), config, push_title, push_body,
                        config.click_url,
                        expired_handler=expire_device,
                        urgency="high",
                    )
                    if ok:
                        delivered = True
                        success_count += 1
                        stats["push_success"] += 1
                        delivery_status, delivery_error = "SUCCESS", None
                    elif expired:
                        expired_count += 1
                        delivery_status, delivery_error = "EXPIRED", "PUSH_EXPIRED"
                    else:
                        delivery_status, delivery_error = "FAILED", "PUSH_SEND_FAILED"
                except Exception as exc:
                    logger.warning("Worker Web Push failed type=%s", type(exc).__name__)
                    delivery_status, delivery_error = "FAILED", "PUSH_SEND_FAILED"
                try:
                    delivery_completer(delivery_id, delivery_status, delivery_error)
                except Exception as exc:
                    logger.warning("Push delivery history completion failed type=%s",
                                   type(exc).__name__)
            if success_count == len(target.devices):
                run_status, run_error = "SUCCESS", None
            elif success_count:
                run_status, run_error = "PARTIAL", "PUSH_SEND_FAILED"
            else:
                run_status = "FAILED"
                run_error = (
                    "PUSH_EXPIRED" if expired_count == len(target.devices)
                    else "PUSH_SEND_FAILED"
                )
        else:
            try:
                delivery_id = delivery_starter(run_id, "KAKAO", None)
            except Exception as exc:
                logger.warning("Kakao delivery history start failed; send skipped type=%s",
                               type(exc).__name__)
                stats["db_skipped"] = True
                try:
                    run_completer(run_id, "FAILED", "KAKAO_SEND_FAILED")
                except Exception as history_exc:
                    logger.warning("Notification run completion failed type=%s",
                                   type(history_exc).__name__)
                continue
            try:
                delivered = bool(
                    kakao_sender(target, kakao_title, kakao_body)
                    if kakao_sender is not None else False
                )
            except Exception as exc:
                logger.warning("Worker Kakao delivery failed type=%s", type(exc).__name__)
                delivered = False
            if delivered:
                stats["kakao_success"] += 1
                delivery_status, delivery_error = "SUCCESS", None
                run_status, run_error = "SUCCESS", None
            else:
                delivery_status, delivery_error = "FAILED", "KAKAO_SEND_FAILED"
                run_status, run_error = "FAILED", "KAKAO_SEND_FAILED"
            try:
                delivery_completer(delivery_id, delivery_status, delivery_error)
            except Exception as exc:
                logger.warning("Kakao delivery history completion failed type=%s",
                               type(exc).__name__)

        try:
            run_completer(run_id, run_status, run_error)
        except Exception as exc:
            logger.warning("Notification run completion failed type=%s", type(exc).__name__)

        if delivered:
            sent_cache[key] = True
    return stats


def notification_background_worker(
    *,
    config: WebPushConfig,
    holiday_checker,
    kakao_sender,
    sleep=time.sleep,
) -> None:
    """Run forever; caches intentionally reset when this local process restarts."""
    sent_cache = {}
    attempt_cache = {}
    cache_date: date | None = None
    while True:
        now = datetime.now(KST)
        if cache_date != now.date():
            sent_cache.clear()
            attempt_cache.clear()
            cache_date = now.date()
        try:
            run_notification_cycle(
                sent_cache=sent_cache, attempt_cache=attempt_cache, config=config,
                now=now, holiday_checker=holiday_checker, kakao_sender=kakao_sender,
            )
            run_scheduled_push_test_cycle(config=config, now=now)
        except Exception as exc:
            logger.warning("Background worker cycle failed type=%s", type(exc).__name__)
        sleep(30)


def start_notification_worker(
    *,
    config: WebPushConfig,
    holiday_checker,
    kakao_sender,
    thread_factory=threading.Thread,
) -> bool:
    """Start one local worker only; return False when disabled/already running."""
    global _worker_thread
    if not worker_enabled():
        return False
    with _worker_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            return False
        _worker_thread = thread_factory(
            target=notification_background_worker,
            kwargs={
                "config": config,
                "holiday_checker": holiday_checker,
                "kakao_sender": kakao_sender,
            },
            daemon=True,
            name="shuttle-notification-worker",
        )
        _worker_thread.start()
        return True
