"""Streamlit UI and browser bridge for the Web Push proof of concept."""

from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components_v1

from web_push import WebPushConfig, send_test_push, sync_subscription


_ASSET_DIR = Path(__file__).parent / "web_push_component_assets"


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

  function showState(state, subscription = null) {
    subscribeButton.hidden = state === 'registered';
    unsubscribeButton.hidden = state !== 'registered';
    subscribeButton.disabled = state === 'denied' || state === 'unsupported' || !data.config_ready;

    const labels = {
      loading: '현재 상태: 브라우저 알림 상태를 확인하고 있습니다.',
      unregistered: '현재 상태: 알림이 등록되지 않았습니다.',
      unsubscribed: '현재 상태: 알림이 등록되지 않았습니다.',
      registered: '현재 상태: 이 기기의 알림이 등록되어 있습니다.',
      denied: '현재 상태: 브라우저에서 알림 권한이 차단되어 있습니다.',
      unsupported: '현재 상태: 이 브라우저에서는 Web Push를 지원하지 않습니다.',
      error: '현재 상태: 기기 알림 등록을 완료하지 못했습니다.'
    };
    statusEl.textContent = labels[state] || labels.error;
    helpEl.textContent = state === 'denied'
      ? '브라우저 사이트 설정에서 알림 권한을 허용한 뒤 다시 시도해 주세요.'
      : (!data.config_ready ? 'VAPID 환경설정을 완료해야 기기 알림을 등록할 수 있습니다.' : '');

    if (subscription) {
      const json = subscription.toJSON();
      if (!data.server_endpoint || data.server_endpoint !== json.endpoint) {
        setStateValue('subscription', json);
        setStateValue('app_url', window.location.origin + window.location.pathname);
      }
    }
    setStateValue('browser_status', state);
  }

  async function inspectState() {
    if (!supported) return showState('unsupported');
    if (Notification.permission === 'denied') return showState('denied');
    const registration = await findRegistration();
    const subscription = registration ? await registration.pushManager.getSubscription() : null;
    showState(subscription ? 'registered' : 'unregistered', subscription);
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
      showState('registered', subscription);
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
      setStateValue('subscription', null);
      setStateValue('app_url', '');
      showState('unsubscribed');
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


def render_web_push_poc(config: WebPushConfig) -> None:
    """Render the isolated PoC and retain its subscription in this session."""
    component, service_worker_url, service_worker_scope = _register_components()
    with st.container(border=True, key="web_push_poc"):
        st.markdown("#### 🔔 기기 알림")
        st.caption("카카오 알림과 별개인 Web Push 기술 검증 기능입니다.")

        subscription = st.session_state.get("web_push_subscription")
        result = component(
            key="web_push_browser_bridge",
            data={
                "public_key": config.public_key,
                "config_ready": config.ready,
                "service_worker_url": service_worker_url,
                "service_worker_scope": service_worker_scope,
                "server_endpoint": subscription.get("endpoint", "") if subscription else "",
            },
            default={"subscription": None, "browser_status": "loading", "app_url": ""},
            on_subscription_change=lambda: None,
            on_browser_status_change=lambda: None,
            on_app_url_change=lambda: None,
        )

        sync_subscription(
            st.session_state,
            result.subscription,
            result.browser_status,
            result.app_url,
        )
        subscription = st.session_state.get("web_push_subscription")

        if config.missing:
            st.info("Web Push 테스트를 사용하려면 VAPID 환경설정을 먼저 추가해 주세요.")
        elif not config.ready:
            st.warning("WEB_PUSH_VAPID_SUBJECT는 mailto: 또는 https: 주소여야 합니다.")

        if subscription:
            if st.button("🧪 테스트 Push 보내기", key="web_push_test_send", width="stretch"):
                ok, message = send_test_push(
                    subscription,
                    config,
                    st.session_state.get("web_push_click_url", ""),
                )
                if ok:
                    st.success(message)
                else:
                    st.error(message)
