"""Small, UI-independent helpers for the local demo. No persistent auth changes."""
import logging
import secrets
import threading
import time

import requests

logger = logging.getLogger(__name__)
_login_states = {}
_login_lock = threading.Lock()
SELECTION_KEYS = ("user_reg", "user_rt", "user_board_st", "user_arrive_st")


def prepare_login(state, selection):
    """Opaque, short-lived OAuth state; selections never go to Kakao."""
    now = time.monotonic()
    with _login_lock:
        for key in list(_login_states):
            if _login_states[key][0] < now:
                del _login_states[key]
        if state not in _login_states:
            state = secrets.token_urlsafe(32)
        _login_states[state] = (now + 600, {
            key: selection[key] for key in SELECTION_KEYS if key in selection
        })
    return state


def consume_login(state):
    with _login_lock:
        pending = _login_states.pop(state, None)
    if pending is None or pending[0] < time.monotonic():
        return None
    return pending[1]


def api_request(method, url, **kwargs):
    """Return safe errors to UI; log only metadata, never tokens or request bodies."""
    try:
        response = requests.request(method, url, timeout=8, **kwargs)
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("Expected JSON object")
        if response.status_code >= 400:
            logger.warning("Kakao API %s: HTTP %s, code=%s", url,
                           response.status_code, data.get("code", data.get("error")))
        return response.status_code, data
    except (requests.RequestException, ValueError) as exc:
        logger.error("Kakao API %s failed: %s", url, type(exc).__name__, exc_info=True)
        return 0, {"error": "connection_or_response_error"}


def message_succeeded(status, data):
    return status == 200 and data.get("result_code") == 0


def deliver_message(access_token, refresh_token, send, refresh, persist,
                    retry_transient=False, sleep=time.sleep):
    """Refresh once on expiry; at most one transient retry for scheduled sends.

    A timeout has an ambiguous delivery outcome, so do not immediately resend it.
    """
    refreshed = False
    retried = False
    while True:
        if not access_token:
            status, data = 401, {"code": -401}
        else:
            status, data = send(access_token)
        if message_succeeded(status, data):
            return status, data
        if (status == 401 or data.get("code") == -401) and refresh_token and not refreshed:
            refreshed = True
            new_access, new_refresh = refresh(refresh_token)
            if not new_access:
                return 401, {"error": "login_required"}
            access_token = new_access
            refresh_token = new_refresh or refresh_token
            persist(access_token, refresh_token)
            continue
        if retry_transient and not retried and (status == 429 or 500 <= status <= 599):
            retried = True
            sleep(1)
            continue
        return status, data


def notify_once(sent, attempted, key, deliver, now=None):
    """Keep attempts separate from confirmed delivery; bound per-window retries."""
    now = time.monotonic() if now is None else now
    count, last = attempted.get(key, (0, float('-inf')))
    if key in sent or count >= 2 or now - last < 30:
        return False
    attempted[key] = (count + 1, now)
    status, data = deliver()
    if message_succeeded(status, data):
        sent[key] = True
        return True
    return False
