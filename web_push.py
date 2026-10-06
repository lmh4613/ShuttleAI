"""Server-side helpers for the isolated Web Push proof of concept."""

from __future__ import annotations

import json
import logging
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
            timeout=15,
        )
        if headers is not None:
            kwargs["headers"] = headers
        sender(**kwargs)
        return True, "Push 알림을 발송했습니다. 운영체제 알림 영역을 확인해 주세요."
    except Exception as exc:
        status_code = getattr(exc, "status_code", None)
        logger.warning("Web Push send failed status=%s type=%s", status_code, type(exc).__name__)
        if status_code in {404, 410}:
            if expired_handler is not None:
                try:
                    expired_handler()
                except Exception as handler_exc:
                    logger.warning(
                        "Expired Push cleanup failed type=%s", type(handler_exc).__name__
                    )
            return False, "브라우저 구독이 만료되었습니다. 이 기기 알림을 해제한 뒤 다시 등록해 주세요."
        return False, "Push 알림 발송에 실패했습니다. VAPID 설정과 네트워크 연결을 확인해 주세요."
