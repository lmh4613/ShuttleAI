"""Streamlit UI and browser bridge for the Web Push proof of concept."""

from pathlib import Path
from uuid import uuid4

import streamlit as st
import streamlit.components.v1 as components_v1

from push_subscription_store import (
    OWNERSHIP_CURRENT,
    OWNERSHIP_EXPIRED,
    OWNERSHIP_REVOKED,
    OWNERSHIP_NONE,
    OWNERSHIP_OTHER,
    PushSubscriptionError,
    PushSubscriptionExpiredError,
    count_active_push_subscriptions,
    canonicalize_subscription,
    deactivate_all_subscriptions,
    deactivate_subscription,
    register_subscription,
    subscription_ownership,
)
from web_push import WebPushConfig, sync_subscription


_ASSET_DIR = Path(__file__).parent / "web_push_component_assets"
DB_UNAVAILABLE = "unavailable"
DB_UNKNOWN = "unknown"


def web_push_display_state(has_browser_subscription, ownership=DB_UNKNOWN):
    """Resolve the one UI state from browser presence and persistent ownership."""
    if not has_browser_subscription:
        return "NO_BROWSER_SUBSCRIPTION"
    if ownership == DB_UNAVAILABLE:
        return "DB_UNAVAILABLE"
    if ownership == OWNERSHIP_EXPIRED:
        return "EXPIRED"
    if ownership == OWNERSHIP_REVOKED:
        return "REVOKED"
    if ownership == OWNERSHIP_CURRENT:
        return "REGISTERED_TO_CURRENT_USER"
    return "REGISTERED_TO_OTHER_USER"


def can_send_weather_push(session, *, logged_in, preview_user, result_matches):
    """Return whether the current browser may receive the displayed weather result."""
    return bool(
        logged_in
        and not preview_user
        and result_matches
        and session.get("web_push_subscription")
        and session.get("web_push_db_ownership") == OWNERSHIP_CURRENT
    )


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

  let disposed = false;
  let inspecting = false;
  let missingReported = false;

  function emitEvent(action, state, subscription = null, removed = null) {
    if (disposed) return;
    if (action === 'inspect_missing') {
      if (missingReported) return;
      missingReported = true;
    } else if (subscription) {
      missingReported = false;
    }
    setStateValue('event', {
      action,
      owner_context: data.owner_context,
      browser_status: state,
      subscription: subscription ? subscription.toJSON() : null,
      unsubscribed_subscription: removed ? removed.toJSON() : null,
      app_url: window.location.origin + window.location.pathname,
      action_id: `${Date.now()}-${Math.random()}`
    });
  }

  function showState(state, subscription = null, action = '', removed = null) {
    if (disposed) return;
    subscribeButton.hidden = ['registered', 'db_unavailable', 'loading'].includes(state);
    unsubscribeButton.hidden = !['registered', 'browser_only', 'revoked'].includes(state);
    subscribeButton.disabled = state === 'denied' || state === 'unsupported' || !data.config_ready;
    subscribeButton.textContent = state === 'expired' ? '알림 다시 등록' : ['browser_only', 'other'].includes(state)
      ? '현재 계정에 이 기기 알림 연결' : '이 기기에서 알림 받기';

    const labels = {
      loading: '현재 상태: 브라우저 알림 상태를 확인하고 있습니다.',
      unregistered: '현재 브라우저에 알림 구독이 없거나 제거되었습니다. 알림을 받으려면 이 기기를 등록해 주세요.',
      unsubscribed: '현재 상태: 알림이 등록되지 않았습니다.',
      registered: '현재 기기의 알림이 정상적으로 등록되어 있습니다.',
      expired: '이 기기의 알림 구독이 만료되어 알림을 받을 수 없습니다. 아래 버튼을 눌러 다시 등록해 주세요.',
      renewal_failed: '알림 재등록에 실패했습니다. 브라우저 알림 권한 및 연결 상태를 확인해 주세요.',
      renewal_unsupported: '이 브라우저에서는 알림 구독 재발급을 지원하지 않습니다. 브라우저를 업데이트하거나 다른 브라우저를 이용해 주세요.',
      browser_only: '브라우저 구독이 현재 계정의 DB 등록 상태와 일치하지 않습니다. 아래 버튼으로 현재 계정에 연결해 주세요.',
      other: '이 브라우저의 구독은 다른 계정에 연결되어 있습니다. 아래 버튼을 누르면 현재 계정으로 연결이 변경됩니다.',
      revoked: '이 기기의 알림 등록이 해제되어 있습니다. 다시 받으려면 직접 등록해 주세요.',
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

  async function inspectState(refresh = false) {
    if (disposed || busy || inspecting) return;
    inspecting = true;
    try {
      if (!supported) return showState('unsupported');
      if (Notification.permission === 'denied') {
        return showState('denied', null, data.server_endpoint ? 'inspect_missing' : '');
      }
      const registration = await findRegistration();
      if (registration && !refresh && typeof registration.update === 'function') {
        registration.update().catch(() => {}); // Update the hint handler, never the subscription.
      }
      const subscription = registration ? await registration.pushManager.getSubscription() : null;
      if (!subscription) return showState('unregistered', null, data.server_endpoint ? 'inspect_missing' : '');
      if (data.server_state === 'unavailable' && !refresh) return showState('db_unavailable', subscription);
      if (data.server_state === 'unknown' || subscription.endpoint !== data.server_endpoint) {
        return showState('loading', subscription, 'inspect');
      }
      const state = data.server_state === 'unavailable' ? 'db_unavailable'
        : data.server_state === 'expired' ? 'expired'
        : data.server_state === 'revoked' ? 'revoked'
        : ['other', 'other_expired'].includes(data.server_state) ? 'other'
        : data.server_state === 'current' ? 'registered' : 'browser_only';
      showState(state, subscription, refresh ? 'inspect' : '');
    } catch (_error) {
      showState('error', null, data.server_endpoint ? 'inspect_missing' : '');
    } finally {
      inspecting = false;
    }
  }

  async function applyServerUnsubscribe() {
    const actionId = data.unsubscribe_all_action_id || '';
    if (!actionId) return false;
    const storageKey = 'shuttle-web-push-unsubscribe-all';
    if (window.sessionStorage.getItem(storageKey) === actionId) return false;
    window.sessionStorage.setItem(storageKey, actionId);
    const registration = await findRegistration();
    const subscription = registration ? await registration.pushManager.getSubscription() : null;
    if (subscription) await subscription.unsubscribe();
    if (registration) await registration.unregister();
    showState('unsubscribed', null, 'unsubscribe_all_browser', subscription);
    return true;
  }

  let busy = false;
  subscribeButton.onclick = async () => {
    if (disposed || busy || inspecting) return;
    const renewing = ['expired', 'other_expired'].includes(data.server_state);
    if (!supported || !data.config_ready || Notification.permission === 'denied') {
      return inspectState();
    }
    busy = true;
    subscribeButton.disabled = true;
    try {
      const permission = Notification.permission === 'granted'
        ? 'granted' : await Notification.requestPermission();
      if (permission !== 'granted') return showState(permission === 'denied' ? 'denied' : 'unregistered');
      const registration = await waitForActive(await navigator.serviceWorker.register(
        data.service_worker_url, { scope: data.service_worker_scope }
      ));
      const existing = await registration.pushManager.getSubscription();
      if (renewing && (typeof registration.pushManager.subscribe !== 'function' ||
          (existing && typeof existing.unsubscribe !== 'function'))) {
        return showState('renewal_unsupported');
      }
      if (renewing && existing) {
        await existing.unsubscribe();
        if (await registration.pushManager.getSubscription()) throw new Error('unsubscribe_failed');
      }
      const subscription = (!renewing && existing) || await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: applicationServerKey(data.public_key)
      });
      if (renewing && (subscription.endpoint === data.server_endpoint ||
          (existing && subscription.endpoint === existing.endpoint))) {
        throw new Error('same_expired_endpoint');
      }
      showState('loading', subscription, renewing ? 'reregister' : 'register', existing);
    } catch (error) {
      showState(renewing ? 'renewal_failed' : 'error');
    } finally {
      busy = false;
      subscribeButton.disabled = !supported || !data.config_ready || Notification.permission === 'denied';
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

  const refresh = () => inspectState(true);
  const visible = () => { if (window.document?.visibilityState === 'visible') refresh(); };
  const subscriptionChanged = event => {
    if (event.data?.type === 'shuttle-push-subscription-changed' &&
        event.source?.scriptURL === new URL(data.service_worker_url, window.location.origin).href) {
      refresh();
    }
  };
  window.addEventListener?.('focus', refresh);
  window.addEventListener?.('pageshow', refresh);
  window.document?.addEventListener('visibilitychange', visible);
  navigator.serviceWorker?.addEventListener?.('message', subscriptionChanged);

  applyServerUnsubscribe()
    .then(handled => { if (!handled) return inspectState(); })
    .catch(() => showState('error'));
  return () => {
    disposed = true;
    window.removeEventListener?.('focus', refresh);
    window.removeEventListener?.('pageshow', refresh);
    window.document?.removeEventListener('visibilitychange', visible);
    navigator.serviceWorker?.removeEventListener?.('message', subscriptionChanged);
  };
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
    if kakao_user_id is None or not isinstance(event, dict):
        return False
    if event.get("owner_context", kakao_user_id) != kakao_user_id:
        return False
    action_id = event.get("action_id", "")
    processed = session.get("web_push_processed_actions", [])
    if not action_id or action_id == session.get("web_push_last_action_id") or action_id in processed:
        return False
    session["web_push_last_action_id"] = action_id
    session["web_push_processed_actions"] = (processed + [action_id])[-64:]
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
        if action in {"register", "reregister"}:
            subscription = event.get("subscription")
            canonicalize_subscription(subscription)
            session.pop("web_push_flash_success", None)
            if action == "reregister":
                old = event.get("unsubscribed_subscription") or previous_subscription
                if old and old.get("endpoint") == subscription.get("endpoint"):
                    raise PushSubscriptionError("알림 재등록에 실패했습니다. 브라우저 알림 권한 및 연결 상태를 확인해 주세요.")
            registerer(kakao_user_id, subscription)
            session["web_push_db_ownership"] = OWNERSHIP_CURRENT
            session.pop("web_push_db_status_error", None)
            session["web_push_flash_success"] = (
                "기기 알림이 다시 등록되었습니다." if action == "reregister"
                else "이 기기의 알림 등록이 완료되었습니다."
            )
            if on_data_changed is not None:
                on_data_changed()
            return True
        if action == "inspect_missing":
            session.pop("web_push_flash_success", None)
            session.pop("web_push_subscription", None)
            session["web_push_db_ownership"] = OWNERSHIP_NONE
            return bool(previous_subscription)
        if action == "inspect" and subscription:
            ownership = ownership_loader(kakao_user_id, subscription)
            changed = (session.get("web_push_db_ownership") != ownership
                       or previous_subscription != subscription)
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
        if action == "unsubscribe_all_browser":
            session["web_push_db_ownership"] = OWNERSHIP_NONE
            return False
    except PushSubscriptionExpiredError as exc:
        session.pop("web_push_flash_success", None)
        session["web_push_db_ownership"] = OWNERSHIP_EXPIRED
        session["web_push_db_status_error"] = str(exc)
        return True
    except PushSubscriptionError as exc:
        session["web_push_db_ownership"] = DB_UNAVAILABLE
        session.pop("web_push_flash_success", None)
        session["web_push_db_status_error"] = (
            "알림 재등록에 실패했습니다. 브라우저 알림 권한 및 연결 상태를 확인해 주세요."
            if action == "reregister" else str(exc)
        )
        return True
    return False


def render_web_push_poc(
    config: WebPushConfig,
    kakao_user_id: int | None = None,
    on_data_changed=None,
    active_device_count: int | None = None,
) -> None:
    """Render Web Push for one authenticated Kakao user with DB ownership."""
    if kakao_user_id is None:
        st.info("로그인 후 이 기기에서 알림을 등록할 수 있습니다.")
        return

    if st.session_state.get("web_push_owner_context") != kakao_user_id:
        st.session_state["web_push_owner_context"] = kakao_user_id
        st.session_state["web_push_db_ownership"] = DB_UNKNOWN
        st.session_state.pop("web_push_last_action_id", None)
        st.session_state.pop("web_push_processed_actions", None)
        st.session_state.pop("web_push_flash_success", None)
        st.session_state.pop("web_push_db_status_error", None)
        st.session_state.pop("web_push_confirm_deactivate_all", None)
        st.session_state.pop("web_push_unsubscribe_all_action_id", None)

    cached_subscription = st.session_state.get("web_push_subscription")
    if cached_subscription:
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

        if active_device_count is None:
            try:
                active_device_count = count_active_push_subscriptions(kakao_user_id)
            except PushSubscriptionError:
                active_device_count = None
        if active_device_count is not None:
            st.markdown(f"**등록된 알림 기기: {active_device_count}대**")

        subscription = st.session_state.get("web_push_subscription")
        result = component(
            key="web_push_browser_bridge",
            data={
                "owner_context": kakao_user_id,
                "public_key": config.public_key,
                "config_ready": config.ready,
                "service_worker_url": service_worker_url,
                "service_worker_scope": service_worker_scope,
                "server_endpoint": subscription.get("endpoint", "") if subscription else "",
                "server_state": st.session_state.get("web_push_db_ownership", DB_UNKNOWN),
                "unsubscribe_all_action_id": st.session_state.get(
                    "web_push_unsubscribe_all_action_id", ""
                ),
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
        if display_state != "REGISTERED_TO_CURRENT_USER":
            st.caption("날씨 조회 결과를 보내려면 이 기기 알림을 현재 계정에 등록해 주세요.")

        if active_device_count:
            if not st.session_state.get("web_push_confirm_deactivate_all"):
                if st.button(
                    "내 모든 기기 알림 해제",
                    key="web_push_deactivate_all_start",
                    width="stretch",
                ):
                    st.session_state["web_push_confirm_deactivate_all"] = True
                    st.rerun()
            else:
                st.warning("등록된 모든 기기의 Push 알림을 해제할까요?")
                confirm_col, cancel_col = st.columns(2)
                with confirm_col:
                    if st.button(
                        "모든 기기 해제 확인",
                        key="web_push_deactivate_all_confirm",
                        type="primary",
                        width="stretch",
                    ):
                        try:
                            current_browser_owned = (
                                st.session_state.get("web_push_db_ownership")
                                == OWNERSHIP_CURRENT
                            )
                            count = deactivate_all_subscriptions(kakao_user_id)
                        except PushSubscriptionError as exc:
                            st.error(str(exc))
                        else:
                            if current_browser_owned:
                                st.session_state["web_push_db_ownership"] = OWNERSHIP_NONE
                                st.session_state["web_push_unsubscribe_all_action_id"] = uuid4().hex
                            st.session_state["web_push_flash_success"] = (
                                f"등록된 알림 기기 {count}대를 해제했습니다."
                            )
                            st.session_state.pop("web_push_confirm_deactivate_all", None)
                            if on_data_changed is not None:
                                on_data_changed()
                            st.rerun()
                with cancel_col:
                    if st.button(
                        "취소", key="web_push_deactivate_all_cancel", width="stretch"
                    ):
                        st.session_state.pop("web_push_confirm_deactivate_all", None)
                        st.rerun()
