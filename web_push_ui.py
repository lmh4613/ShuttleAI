"""Streamlit UI and browser bridge for the Web Push proof of concept."""

from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components_v1

from push_subscription_store import (
    OWNERSHIP_CURRENT,
    OWNERSHIP_NONE,
    OWNERSHIP_OTHER,
    PushSubscriptionError,
    deactivate_subscription,
    mark_subscription_expired,
    register_subscription,
    subscription_ownership,
)
from web_push import WebPushConfig, send_test_push, sync_subscription


_ASSET_DIR = Path(__file__).parent / "web_push_component_assets"
DB_UNAVAILABLE = "unavailable"
DB_UNKNOWN = "unknown"


def web_push_display_state(has_browser_subscription, ownership=DB_UNKNOWN):
    """Resolve the one UI state from browser presence and persistent ownership."""
    if not has_browser_subscription:
        return "NO_BROWSER_SUBSCRIPTION"
    if ownership == DB_UNAVAILABLE:
        return "DB_UNAVAILABLE"
    if ownership == OWNERSHIP_CURRENT:
        return "REGISTERED_TO_CURRENT_USER"
    return "REGISTERED_TO_OTHER_USER"


_COMPONENT_HTML = """
<div class="push-panel">
  <div class="push-status" role="status">브라우저 알림 상태를 확인하고 있습니다.</div>
  <div class="push-actions">
    <button class="subscribe" type="button">이 기기에서 알림 받기</button>
    <button class="unsubscribe" type="button">이 기기 알림 해제</button>
  </div>
  <p class="push-help"></p>
</div>
"""


_COMPONENT_CSS = """
.push-panel { font-family: var(--st-font, sans-serif); }
.push-status { font-weight: 600; margin-bottom: .65rem; }
.push-actions { display: flex; flex-wrap: wrap; gap: .55rem; }
.push-actions button {
  min-height: 44px; padding: .55rem .9rem; border-radius: .5rem;
  border: 1px solid rgba(128,128,128,.35); cursor: pointer;
  background: var(--st-secondary-background-color, #f2f2f2);
  color: var(--st-text-color, #222); font-weight: 600;
}
.push-actions button:disabled { cursor: not-allowed; opacity: .55; }
.subscribe { background: var(--st-primary-color, #ff4b4b) !important; color: white !important; }
.push-help { margin: .55rem 0 0; font-size: .88rem; opacity: .8; }
@media (max-width: 640px) {
  .push-actions { display: grid; grid-template-columns: 1fr; }
  .push-actions button { width: 100%; }
}
"""


_COMPONENT_JS = r"""
export default function(component) {
  const { data, parentElement, setStateValue } = component;
  const statusEl = parentElement.querySelector('.push-status');
  const helpEl = parentElement.querySelector('.push-help');
  const subscribeButton = parentElement.querySelector('.subscribe');
  const unsubscribeButton = parentElement.querySelector('.unsubscribe');

  const supported = Boolean(
    window.isSecureContext && 'serviceWorker' in navigator &&
    'PushManager' in window && 'Notification' in window
  );

  function applicationServerKey(value) {
    const padding = '='.repeat((4 - value.length % 4) % 4);
    const base64 = (value + padding).replace(/-/g, '+').replace(/_/g, '/');
    const raw = atob(base64);
    return Uint8Array.from([...raw].map(char => char.charCodeAt(0)));
  }

  async function findRegistration() {
    const registrations = await navigator.serviceWorker.getRegistrations();
    const scopeUrl = new URL(data.service_worker_scope, window.location.origin).href;
    return registrations.find(registration => registration.scope === scopeUrl) || null;
  }

  async function waitForActive(registration) {
    if (registration.active) return registration;
    const worker = registration.installing || registration.waiting;
    if (!worker) return registration;
    await new Promise((resolve, reject) => {
      const timeout = window.setTimeout(() => reject(new Error('service_worker_timeout')), 10000);
      worker.addEventListener('statechange', () => {
        if (worker.state === 'activated') {
          window.clearTimeout(timeout);
          resolve();
        } else if (worker.state === 'redundant') {
          window.clearTimeout(timeout);
          reject(new Error('service_worker_redundant'));
        }
      });
    });
    return registration;
  }

  function emitEvent(action, state, subscription = null, removed = null) {
    setStateValue('event', {
      action,
      browser_status: state,
      subscription: subscription ? subscription.toJSON() : null,
      unsubscribed_subscription: removed ? removed.toJSON() : null,
      app_url: window.location.origin + window.location.pathname,
      action_id: `${Date.now()}-${Math.random()}`
    });
  }

  function showState(state, subscription = null, action = '', removed = null) {
    subscribeButton.hidden = ['registered', 'db_unavailable', 'loading'].includes(state);
    unsubscribeButton.hidden = !['registered', 'browser_only'].includes(state);
    subscribeButton.disabled = state === 'denied' || state === 'unsupported' || !data.config_ready;
    subscribeButton.textContent = state === 'browser_only'
      ? '현재 계정에 이 기기 알림 연결' : '이 기기에서 알림 받기';

    const labels = {
      loading: '현재 상태: 브라우저 알림 상태를 확인하고 있습니다.',
      unregistered: '현재 상태: 알림이 등록되지 않았습니다.',
      unsubscribed: '현재 상태: 알림이 등록되지 않았습니다.',
      registered: '현재 상태: 이 기기 알림 등록됨',
      browser_only: '현재 상태: 브라우저 알림을 현재 계정에 연결해야 합니다.',
      db_unavailable: '현재 상태: 기기 알림 등록 정보를 확인할 수 없습니다.',
      denied: '현재 상태: 브라우저에서 알림 권한이 차단되어 있습니다.',
      unsupported: '현재 상태: 이 브라우저에서는 Web Push를 지원하지 않습니다.',
      error: '현재 상태: 기기 알림 등록을 완료하지 못했습니다.'
    };
    statusEl.textContent = labels[state] || labels.error;
    helpEl.textContent = state === 'denied'
      ? '브라우저 사이트 설정에서 알림 권한을 허용한 뒤 다시 시도해 주세요.'
      : (state === 'db_unavailable'
        ? '데이터베이스 연결을 확인한 뒤 다시 시도해 주세요.'
        : (!data.config_ready ? 'VAPID 환경설정을 완료해야 기기 알림을 등록할 수 있습니다.' : ''));

    if (action) emitEvent(action, state, subscription, removed);
  }

  async function inspectState() {
    if (!supported) return showState('unsupported');
    if (Notification.permission === 'denied') return showState('denied');
    const registration = await findRegistration();
    const subscription = registration ? await registration.pushManager.getSubscription() : null;
    if (!subscription) return showState('unregistered');
    if (data.server_state === 'unavailable') return showState('db_unavailable', subscription);
    if (data.server_state === 'unknown') return showState('loading', subscription, 'inspect');
    showState(
      data.server_state === 'current' ? 'registered' : 'browser_only',
      subscription
    );
  }

  subscribeButton.onclick = async () => {
    if (!supported || !data.config_ready || Notification.permission === 'denied') {
      return inspectState();
    }
    try {
      const permission = Notification.permission === 'granted'
        ? 'granted' : await Notification.requestPermission();
      if (permission !== 'granted') return showState(permission === 'denied' ? 'denied' : 'unregistered');
      const registration = await waitForActive(await navigator.serviceWorker.register(
        data.service_worker_url, { scope: data.service_worker_scope }
      ));
      const existing = await registration.pushManager.getSubscription();
      const subscription = existing || await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: applicationServerKey(data.public_key)
      });
      showState('loading', subscription, 'register');
    } catch (error) {
      showState('error');
    }
  };

  unsubscribeButton.onclick = async () => {
    try {
      const registration = await findRegistration();
      const subscription = registration ? await registration.pushManager.getSubscription() : null;
      if (subscription) await subscription.unsubscribe();
      if (registration) await registration.unregister();
      showState('unsubscribed', null, 'unsubscribe', subscription);
    } catch (error) {
      showState('error');
    }
  };

  inspectState().catch(() => showState('error'));
}
"""


def _register_components():
    """Register assets and the v2 bridge in the active Streamlit runtime."""
    asset_component = components_v1.declare_component(
        "web_push_assets", path=str(_ASSET_DIR)
    )
    service_worker_url = f"/component/{asset_component.name}/push-sw.js"
    service_worker_scope = service_worker_url.rsplit("/", 1)[0] + "/"
    renderer = st.components.v2.component(
        "shuttle_web_push",
        html=_COMPONENT_HTML,
        css=_COMPONENT_CSS,
        js=_COMPONENT_JS,
    )
    return renderer, service_worker_url, service_worker_scope


def handle_web_push_event(
    session,
    kakao_user_id,
    event,
    *,
    registerer=register_subscription,
    ownership_loader=subscription_ownership,
    deactivator=deactivate_subscription,
    on_data_changed=None,
) -> bool:
    """Process one coherent browser event; return whether server state needs redraw."""
    if not isinstance(event, dict):
        return False
    action_id = event.get("action_id", "")
    if not action_id or action_id == session.get("web_push_last_action_id"):
        return False
    session["web_push_last_action_id"] = action_id
    previous_subscription = session.get("web_push_subscription")
    sync_subscription(
        session,
        event.get("subscription"),
        event.get("browser_status", ""),
        event.get("app_url", ""),
    )
    subscription = session.get("web_push_subscription")
    action = event.get("action", "")
    try:
        if action == "register" and subscription:
            registerer(kakao_user_id, subscription)
            session["web_push_db_ownership"] = OWNERSHIP_CURRENT
            session["web_push_flash_success"] = "이 기기의 알림 등록이 완료되었습니다."
            if on_data_changed is not None:
                on_data_changed()
            return True
        if action == "inspect" and subscription:
            if session.get("web_push_db_ownership") != DB_UNKNOWN:
                return False
            ownership = ownership_loader(kakao_user_id, subscription)
            changed = session.get("web_push_db_ownership") != ownership
            session["web_push_db_ownership"] = ownership
            return changed
        if action == "unsubscribe":
            removed = event.get("unsubscribed_subscription") or previous_subscription
            if removed:
                deactivator(kakao_user_id, removed)
            session["web_push_db_ownership"] = OWNERSHIP_NONE
            session["web_push_flash_success"] = "이 기기의 브라우저 알림을 해제했습니다."
            if on_data_changed is not None:
                on_data_changed()
            return True
    except PushSubscriptionError as exc:
        session["web_push_db_ownership"] = DB_UNAVAILABLE
        session["web_push_db_status_error"] = str(exc)
        return True
    return False


def render_web_push_poc(
    config: WebPushConfig,
    kakao_user_id: int | None = None,
    on_data_changed=None,
) -> None:
    """Render Web Push for one authenticated Kakao user with DB ownership."""
    if kakao_user_id is None:
        st.info("로그인 후 이 기기에서 알림을 등록할 수 있습니다.")
        return

    if st.session_state.get("web_push_owner_context") != kakao_user_id:
        st.session_state["web_push_owner_context"] = kakao_user_id
        st.session_state["web_push_db_ownership"] = DB_UNKNOWN
        st.session_state.pop("web_push_last_action_id", None)

    cached_subscription = st.session_state.get("web_push_subscription")
    if cached_subscription and st.session_state.get("web_push_db_ownership") == DB_UNKNOWN:
        try:
            st.session_state["web_push_db_ownership"] = subscription_ownership(
                kakao_user_id, cached_subscription
            )
        except PushSubscriptionError:
            st.session_state["web_push_db_ownership"] = DB_UNAVAILABLE
            st.session_state["web_push_db_status_error"] = (
                "기기 알림 등록 상태를 확인하지 못했습니다. 잠시 후 다시 시도해 주세요."
            )

    component, service_worker_url, service_worker_scope = _register_components()
    with st.container(border=True, key="web_push_poc"):
        st.markdown("#### 🔔 기기 알림")
        st.caption("현재 브라우저의 Web Push 알림을 로그인한 계정에 연결합니다.")

        subscription = st.session_state.get("web_push_subscription")
        result = component(
            key="web_push_browser_bridge",
            data={
                "public_key": config.public_key,
                "config_ready": config.ready,
                "service_worker_url": service_worker_url,
                "service_worker_scope": service_worker_scope,
                "server_endpoint": subscription.get("endpoint", "") if subscription else "",
                "server_state": st.session_state.get("web_push_db_ownership", DB_UNKNOWN),
            },
            default={"event": None},
            on_event_change=lambda: None,
        )

        event = getattr(result, "event", None) or {}
        subscription = st.session_state.get("web_push_subscription")
        if handle_web_push_event(
            st.session_state, kakao_user_id, event,
            on_data_changed=on_data_changed,
        ):
            st.rerun()
        subscription = st.session_state.get("web_push_subscription")

        if config.missing:
            st.info("Web Push 테스트를 사용하려면 VAPID 환경설정을 먼저 추가해 주세요.")
        elif not config.ready:
            st.warning("WEB_PUSH_VAPID_SUBJECT는 mailto: 또는 https: 주소여야 합니다.")
        if st.session_state.get("web_push_db_status_error"):
            st.error(st.session_state.pop("web_push_db_status_error"))
        if st.session_state.get("web_push_flash_success"):
            st.success(st.session_state.pop("web_push_flash_success"))

        display_state = web_push_display_state(
            bool(subscription), st.session_state.get("web_push_db_ownership", DB_UNKNOWN)
        )
        if display_state == "REGISTERED_TO_CURRENT_USER":
            if st.button("🧪 테스트 Push 보내기", key="web_push_test_send", width="stretch"):
                def expire_current_subscription():
                    mark_subscription_expired(kakao_user_id, subscription)
                    st.session_state["web_push_db_ownership"] = OWNERSHIP_NONE
                    if on_data_changed is not None:
                        on_data_changed()

                ok, message = send_test_push(
                    subscription,
                    config,
                    st.session_state.get("web_push_click_url", ""),
                    expired_handler=expire_current_subscription,
                )
                if ok:
                    st.success(message)
                else:
                    st.error(message)
