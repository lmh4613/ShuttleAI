"""Server-side helpers for the isolated Web Push proof of concept."""

from __future__ import annotations

import json
from datetime import datetime, timezone
import logging
import time
from email.utils import parsedate_to_datetime

from requests.exceptions import ConnectionError, Timeout
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

PUSH_TITLE = "ShuttleAI 테스트 알림"
PUSH_BODY = "Web Push 알림이 정상적으로 연결되었습니다."


@dataclass(frozen=True)
class WebPushConfig:
    public_key: str = ""
    private_key: str = ""
    subject: str = ""
    click_url: str = ""

    @property
    def missing(self) -> tuple[str, ...]:
        values = {
            "WEB_PUSH_VAPID_PUBLIC_KEY": self.public_key,
            "WEB_PUSH_VAPID_PRIVATE_KEY": self.private_key,
            "WEB_PUSH_VAPID_SUBJECT": self.subject,
        }
        return tuple(name for name, value in values.items() if not value)

    @property
    def ready(self) -> bool:
        return not self.missing and valid_vapid_subject(self.subject)


def load_web_push_config(getter: Callable[[str, str], str]) -> WebPushConfig:
    """Load VAPID settings through the app's env/secrets-compatible getter."""
    return WebPushConfig(
        public_key=getter("WEB_PUSH_VAPID_PUBLIC_KEY", "").strip(),
        private_key=getter("WEB_PUSH_VAPID_PRIVATE_KEY", "").strip(),
        subject=getter("WEB_PUSH_VAPID_SUBJECT", "").strip(),
        click_url=getter("WEB_PUSH_CLICK_URL", "").strip(),
    )


def valid_vapid_subject(subject: str) -> bool:
    parsed = urlparse(subject)
    return parsed.scheme in {"mailto", "https"} and bool(parsed.path or parsed.netloc)


def validate_subscription(value: object) -> dict:
    """Validate and copy the browser PushSubscription JSON needed by pywebpush."""
    if not isinstance(value, Mapping):
        raise ValueError("subscription must be an object")

    endpoint = value.get("endpoint")
    keys = value.get("keys")
    if not isinstance(endpoint, str) or urlparse(endpoint).scheme != "https":
        raise ValueError("subscription endpoint must be HTTPS")
    if not isinstance(keys, Mapping):
        raise ValueError("subscription keys are missing")

    p256dh = keys.get("p256dh")
    auth = keys.get("auth")
    if not isinstance(p256dh, str) or not p256dh:
        raise ValueError("subscription p256dh key is missing")
    if not isinstance(auth, str) or not auth:
        raise ValueError("subscription auth key is missing")

    normalized = {
        "endpoint": endpoint,
        "keys": {"p256dh": p256dh, "auth": auth},
    }
    if "expirationTime" in value:
        normalized["expirationTime"] = value.get("expirationTime")
    return normalized


def valid_click_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


WEB_PUSH_TTL_SECONDS = 60
WEB_PUSH_URGENCIES = {"normal", "high"}
WEB_PUSH_RETRY_DELAY_SECONDS = 1
WEB_PUSH_MAX_RETRY_DELAY_SECONDS = 2
WEB_PUSH_SEND_BUDGET_SECONDS = 15


def _retry_delay(exc: Exception) -> float | None:
    # Only a definite HTTP rejection is eligible; transport outcomes are uncertain.
    if isinstance(exc, (Timeout, ConnectionError, OSError)):
        return None
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status not in {429, 500, 502, 503, 504}:
        return None
    value = getattr(response, "headers", {}).get("Retry-After")
    if value is None:
        return WEB_PUSH_RETRY_DELAY_SECONDS if status != 429 else None
    try:
        value = str(value).strip()
        if value.isascii() and value.isdigit():
            delay = int(value)
        else:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                return None
            delay = (retry_at - datetime.now(timezone.utc)).total_seconds()
    except (ValueError, TypeError, OverflowError):
        return None
    if 0 <= delay <= WEB_PUSH_MAX_RETRY_DELAY_SECONDS:
        return max(WEB_PUSH_RETRY_DELAY_SECONDS, delay)
    return None


def build_push_payload(
    title: str,
    body: str,
    click_url: str = "",
    *,
    include_received_time: bool = False,
) -> str:
    payload = {"title": title, "body": body}
    if click_url and valid_click_url(click_url):
        payload["url"] = click_url
    if include_received_time:
        payload["include_received_time"] = True
    return json.dumps(payload, ensure_ascii=False)


def build_test_payload(click_url: str = "") -> str:
    return build_push_payload(PUSH_TITLE, PUSH_BODY, click_url)


def sync_subscription(
    session: MutableMapping,
    subscription: object,
    browser_status: str,
    app_url: str = "",
) -> bool:
    """Synchronize component state into this Streamlit session only."""
    if browser_status == "unsubscribed":
        existed = "web_push_subscription" in session
        session.pop("web_push_subscription", None)
        session.pop("web_push_click_url", None)
        return existed

    if subscription is None:
        return False

    try:
        normalized = validate_subscription(subscription)
    except ValueError:
        return False

    changed = session.get("web_push_subscription") != normalized
    session["web_push_subscription"] = normalized
    if app_url and valid_click_url(app_url):
        session["web_push_click_url"] = app_url
    return changed


def send_test_push(
    subscription: object,
    config: WebPushConfig,
    click_url: str = "",
    sender: Callable | None = None,
    expired_handler: Callable[[], None] | None = None,
) -> tuple[bool, str]:
    """Send the fixed PoC notification without invoking app weather or Kakao APIs."""
    return send_web_push(
        subscription, config, PUSH_TITLE, PUSH_BODY, click_url,
        sender=sender, expired_handler=expired_handler,
    )


def send_web_push(
    subscription: object,
    config: WebPushConfig,
    title: str,
    body: str,
    click_url: str = "",
    *,
    sender: Callable | None = None,
    expired_handler: Callable[[], None] | None = None,
    urgency: str | None = None,
    delivery_source: str = "manual",
    subscription_id: int | None = None,
    include_received_time: bool = False,
) -> tuple[bool, str]:
    """Send one validated payload without logging subscription secrets."""
    if not config.ready:
        return False, "VAPID 설정이 완료되지 않았습니다."
    if urgency is not None and urgency not in WEB_PUSH_URGENCIES:
        return False, "Web Push urgency 설정이 올바르지 않습니다."

    try:
        normalized = validate_subscription(subscription)
    except ValueError:
        return False, "브라우저 알림 구독 정보가 올바르지 않습니다."

    if sender is None:
        from pywebpush import webpush

        sender = webpush

    target_url = config.click_url or click_url
    headers = {"Urgency": urgency} if urgency is not None else None
    source = delivery_source if delivery_source in {"automatic", "scheduled_test", "manual"} else "manual"
    started = time.monotonic()
    for attempt in range(2):
        try:
            kwargs = dict(
                subscription_info=normalized,
                data=build_push_payload(
                    title, body, target_url,
                    include_received_time=include_received_time,
                ),
                vapid_private_key=config.private_key,
                vapid_claims={"sub": config.subject},
                ttl=WEB_PUSH_TTL_SECONDS,
                timeout=15 if attempt == 0 else remaining,
            )
            if headers is not None:
                kwargs["headers"] = headers
            response = sender(**kwargs)
            logger.info("Web Push final http_status=%s source=%s retries=%s success=True",
                        getattr(response, "status_code", None), source, attempt)
            return True, "Push 알림을 발송했습니다. 운영체제 알림 영역을 확인해 주세요."
        except Exception as exc:
            status_code = getattr(exc, "status_code", None)
            if status_code is None:
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
            delay = _retry_delay(exc) if attempt == 0 else None
            if delay is not None and time.monotonic() - started + delay < WEB_PUSH_SEND_BUDGET_SECONDS:
                logger.info("Web Push retry http_status=%s source=%s retries=1", status_code, source)
                time.sleep(delay)
                remaining = WEB_PUSH_SEND_BUDGET_SECONDS - (time.monotonic() - started)
                if remaining > 0:
                    continue
            logger.warning("Web Push final http_status=%s source=%s retries=%s success=False",
                           status_code, source, attempt)
            if status_code in {404, 410}:
                deactivated = False
                internal_id = subscription_id
                if expired_handler is not None:
                    try:
                        result = expired_handler()
                        deactivated = bool(result)
                        if internal_id is None and type(result) is int:
                            internal_id = result
                    except Exception as handler_exc:
                        logger.warning("Expired Push cleanup failed type=%s", type(handler_exc).__name__)
                logger.warning(
                    "Push expiry http_status=%s subscription_id=%s occurred_at=%s source=%s deactivated=%s",
                    status_code, internal_id, datetime.now(timezone.utc).isoformat(),
                    delivery_source if delivery_source in {"automatic", "scheduled_test", "manual"} else "manual",
                    deactivated,
                )
                return False, "브라우저 구독이 만료되었습니다. 기기 알림 영역의 알림 다시 등록 버튼을 눌러 주세요."
            return False, "Push 알림 발송에 실패했습니다. VAPID 설정과 네트워크 연결을 확인해 주세요."
