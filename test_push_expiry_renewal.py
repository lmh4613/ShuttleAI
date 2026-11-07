"""Local expiry/renewal regressions; no production DB or push service access."""
import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest
from pywebpush import WebPushException
from requests import Response

import notification_worker as worker
import notification_repository as repository
import push_subscription_store as store
from test_account_reregistration import db, login
from test_push_subscription_store import subscription
from test_notification_worker import cycle, run_scheduled, ScheduledHistory, scheduled_test, target, CONFIG
from notification_repository import PushDevice
from web_push import send_web_push
from web_push_ui import handle_web_push_event, _COMPONENT_JS


def registered(db, suffix):
    value = subscription(suffix)
    store.register_subscription(123, value, transaction_factory=db.transaction)
    row = db.raw.execute('SELECT id FROM push_subscriptions WHERE endpoint_hash=?',
                         (store.endpoint_hash(value['endpoint']),)).fetchone()
    return value, PushDevice(row[0], value['endpoint'], value['keys']['p256dh'], value['keys']['auth'])


@pytest.mark.parametrize('source', ['automatic', 'scheduled_test', 'manual'])
@pytest.mark.parametrize('status', [404, 410, 429, 500, 503, None])
def test_delivery_expiry_is_scoped_and_logged(db, monkeypatch, caplog, source, status):
    monkeypatch.setattr("web_push.time.sleep", Mock())
    login(db)
    old, device = registered(db, 'old')
    other, other_device = registered(db, 'other')
    response = Response()
    response.status_code = status or 500
    error = WebPushException('sensitive response body', response=response) if status else OSError('secret network error')
    transport = Mock(side_effect=error)
    def sender(sub, config, title, body, url, **kwargs):
        return send_web_push(sub, config, title, body, url, sender=transport, **kwargs)
    monkeypatch.setattr(worker, 'deactivate_push_device',
        lambda sid, uid: repository.deactivate_push_device(sid, uid, transaction_factory=db.transaction))
    if source == 'automatic':
        cycle([target(devices=[device])], push_sender=sender)
    elif source == 'scheduled_test':
        run_scheduled(ScheduledHistory(tests=[scheduled_test()], devices=[device]), sender=sender)
    else:
        assert store.current_subscription_id(123, old, connection_factory=db.transaction) == device.id
        sender(old, CONFIG, 'title', 'body', '', subscription_id=device.id,
               expired_handler=lambda: store.mark_subscription_expired(123, old, transaction_factory=db.transaction))
    assert transport.call_count == (2 if status in (500, 503) else 1)
    expired = status in (404, 410)
    assert db.raw.execute('SELECT enabled FROM push_subscriptions WHERE id=?', (device.id,)).fetchone() == (int(not expired),)
    assert store.subscription_ownership(123, other, connection_factory=db.transaction) == 'current'
    assert store.subscription_ownership(123, old, connection_factory=db.transaction) == ('expired' if expired else 'current')
    if expired:
        assert f'http_status={status} subscription_id={device.id}' in caplog.text
        assert f'source={source} deactivated=True' in caplog.text
        assert 'occurred_at=' in caplog.text
        assert not repository.deactivate_push_device(device.id, 1, transaction_factory=db.transaction)
        with pytest.raises(store.PushSubscriptionExpiredError):
            store.register_subscription(123, old, transaction_factory=db.transaction)
    else:
        assert 'Push expiry' not in caplog.text
    for secret in (old['endpoint'], old['keys']['p256dh'], old['keys']['auth'], 'sensitive response body', 'secret network error'):
        assert secret not in caplog.text


@pytest.mark.parametrize('history_table', ['notification_deliveries', 'scheduled_push_test_deliveries'])
def test_legacy_expiry_history_prevents_reuse_even_across_users(db, history_table):
    login(db)
    old, device = registered(db, 'legacy')
    db.raw.execute('UPDATE push_subscriptions SET enabled=FALSE, revoked_at=now() WHERE id=?', (device.id,))
    db.raw.execute(f'INSERT INTO {history_table} VALUES (?, ?)', (device.id, 'PUSH_EXPIRED'))
    db.raw.commit()
    assert store.subscription_ownership(123, old, connection_factory=db.transaction) == 'expired'
    store_user = __import__('user_settings_repository').provision_or_sync_login_user
    store_user(456, nickname='other', access_token='test', refresh_token='test', transaction_factory=db.transaction)
    assert store.subscription_ownership(456, old, connection_factory=db.transaction) == 'other_expired'
    for uid in (123, 456):
        with pytest.raises(store.PushSubscriptionExpiredError):
            store.register_subscription(uid, old, transaction_factory=db.transaction)
    assert db.raw.execute(f'SELECT count(*) FROM {history_table}').fetchone() == (1,)


def test_explicit_renewal_new_endpoint_and_replay_and_old_row_preserved(db):
    login(db)
    old, device = registered(db, 'old')
    registered(db, 'unaffected')
    assert store.mark_subscription_expired(123, old, transaction_factory=db.transaction) == device.id
    session = {'web_push_subscription': old, 'web_push_db_ownership': 'expired'}
    new = subscription('renewed')
    register = Mock(side_effect=lambda uid, sub: store.register_subscription(uid, sub, transaction_factory=db.transaction))
    event = dict(action='reregister', action_id='renewal', subscription=new,
                 unsubscribed_subscription=old, browser_status='loading', owner_context=123)
    assert handle_web_push_event(session, 123, event, registerer=register)
    assert not handle_web_push_event(session, 123, event, registerer=register)
    register.assert_called_once()
    assert session['web_push_flash_success'] == '기기 알림이 다시 등록되었습니다.'
    assert store.count_active_push_subscriptions(123, connection_factory=db.transaction) == 2
    assert db.raw.execute('SELECT enabled, expiration_time IS NOT NULL FROM push_subscriptions WHERE id=?', (device.id,)).fetchone() == (0, 1)
    assert db.raw.execute('SELECT count(*) FROM push_subscriptions').fetchone() == (3,)


def test_revocation_and_owner_checks_are_not_expiry(db):
    login(db)
    old, device = registered(db, 'revoked')
    assert not repository.deactivate_push_device(device.id, 999, transaction_factory=db.transaction)
    assert not store.mark_subscription_expired(999, old, transaction_factory=db.transaction)
    assert store.deactivate_subscription(123, old, transaction_factory=db.transaction)
    assert store.subscription_ownership(123, old, connection_factory=db.transaction) == 'revoked'
    store.register_subscription(123, old, transaction_factory=db.transaction)
    assert store.current_subscription_id(123, old, connection_factory=db.transaction) == device.id
    with pytest.raises(store.PushSubscriptionError):
        store.current_subscription_id(999, old, connection_factory=db.transaction)


@pytest.mark.parametrize('failure', ['same_endpoint', 'database', 'old_event'])
def test_renewal_failure_never_claims_success(failure):
    old = subscription('old')
    session = {'web_push_subscription': old, 'web_push_db_ownership': 'expired'}
    register = Mock(side_effect=store.PushSubscriptionDatabaseError('safe error'))
    event = dict(action='reregister', action_id='attempt', subscription=old if failure == 'same_endpoint' else subscription('new'),
                 unsubscribed_subscription=old, owner_context=999 if failure == 'old_event' else 123)
    handle_web_push_event(session, 123, event, registerer=register)
    assert session['web_push_db_ownership'] != 'current'
    assert 'web_push_flash_success' not in session
    if failure != 'database':
        register.assert_not_called()


def test_failed_expiry_cleanup_logs_failure_without_leaking(caplog):
    response = Response(); response.status_code = 410
    ok, _ = send_web_push(subscription(), CONFIG, 'title', 'body',
        sender=Mock(side_effect=WebPushException('secret', response=response)),
        expired_handler=Mock(side_effect=RuntimeError('secret')), subscription_id=61,
        delivery_source='automatic')
    assert not ok
    assert 'subscription_id=61' in caplog.text
    assert 'deactivated=False' in caplog.text
    assert 'secret' not in caplog.text


@pytest.mark.parametrize('mode', ['renew', 'same', 'subscribe_fail', 'unsubscribe_fail', 'unsupported', 'denied', 'normal', 'other', 'other_expired', 'revoked'])
def test_browser_renewal_javascript(tmp_path, mode):
    """Execute the actual component with fake browser APIs, including failure paths."""
    node = shutil.which('node')
    assert node, 'Node is required for the browser component regression tests'
    component = tmp_path / 'component.mjs'
    component.write_text(_COMPONENT_JS, encoding='utf-8')
    harness = tmp_path / 'test.mjs'
    harness.write_text(r'''
import component from './component.mjs';
import assert from 'node:assert/strict';
const mode = process.argv[2];
const elements = Object.fromEntries(['.push-status','.push-help','.subscribe','.unsubscribe'].map(k => [k, {}]));
let unsubscribed = 0, subscribed = 0;
const events = [];
const make = endpoint => ({endpoint, toJSON() {return {endpoint, keys: {p256dh: 'p', auth: 'a'}}},
  async unsubscribe() {unsubscribed++; if (mode !== 'unsubscribe_fail') current = null; return mode !== 'unsubscribe_fail';}});
const old = make('https://example.test/old');
let current = old;
if (mode === 'unsupported') old.unsubscribe = undefined;
const registration = {scope: 'https://example.test/component/', active: true,
  pushManager: {async getSubscription() {return current}, async subscribe() {
    subscribed++;
    if (mode === 'subscribe_fail') throw Error('failed');
    current = make(mode === 'same' ? old.endpoint : 'https://example.test/new'); return current;
  }}};
Object.defineProperty(globalThis, 'navigator', {value: {serviceWorker: {
  async getRegistrations() {return [registration]}, async register() {return registration}
}}});
globalThis.window = {isSecureContext: true, PushManager: {}, Notification: {},
  location: {origin: 'https://example.test', pathname: '/'}, sessionStorage: {getItem() {return null},setItem() {}}};
globalThis.Notification = {permission: mode === 'denied' ? 'denied' : 'granted'};
const server = mode === 'normal' ? 'current' : mode === 'other_expired' ? 'other_expired' : mode === 'other' ? 'other' : mode === 'revoked' ? 'revoked' : 'expired';
component({data: {server_state: server, server_endpoint: old.endpoint, owner_context: 123,
  config_ready: true, service_worker_scope: '/component/', service_worker_url: '/component/sw.js', public_key: 'AQID'},
  parentElement: {querySelector: key => elements[key]}, setStateValue: (_key, value) => events.push(value)});
await new Promise(resolve => setTimeout(resolve, 0));
assert.equal(unsubscribed, 0); assert.equal(subscribed, 0); // Inspection never mutates.
if (mode === 'normal') {
  assert.equal(elements['.subscribe'].hidden, true);
  assert.match(elements['.push-status'].textContent, /정상적으로/);
} else {
  await Promise.all([elements['.subscribe'].onclick(), elements['.subscribe'].onclick()]);
  if (['renew', 'other_expired'].includes(mode)) {
    assert.equal(unsubscribed, 1); assert.equal(subscribed, 1);
    assert.equal(events.length, 1); assert.equal(events[0].action, 'reregister');
    assert.notEqual(events[0].subscription.endpoint, old.endpoint);
  } else if (['other','revoked'].includes(mode)) {
    assert.equal(unsubscribed, 0); assert.equal(subscribed, 0);
    assert.equal(events.length, 1); assert.equal(events[0].action, 'register');
  } else {
    assert.equal(events.length, 0);
    assert.match(elements['.push-status'].textContent, /실패|지원하지|차단/);
  }
}
''', encoding='utf-8')
    result = subprocess.run([node, str(harness), mode], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_missing_new_subscription_never_reuses_cached_browser_subscription():
    old = subscription('cached')
    session = {'web_push_subscription': old, 'web_push_db_ownership': 'expired'}
    register = Mock()
    handle_web_push_event(session, 123, dict(action='reregister', action_id='missing', subscription=None),
                          registerer=register)
    register.assert_not_called()
    assert 'web_push_flash_success' not in session
    assert session['web_push_db_ownership'] != 'current'


def test_expired_endpoint_of_withdrawn_owner_requires_new_subscription(db):
    from user_settings_repository import deactivate_user_account, provision_or_sync_login_user
    login(db)
    old, device = registered(db, 'withdrawn')
    store.mark_subscription_expired(123, old, transaction_factory=db.transaction)
    deactivate_user_account(123, transaction_factory=db.transaction)
    provision_or_sync_login_user(456, nickname='other', access_token='test', refresh_token='test',
                                transaction_factory=db.transaction)
    assert store.subscription_ownership(456, old, connection_factory=db.transaction) == 'other_expired'
    with pytest.raises(store.PushSubscriptionExpiredError):
        store.register_subscription(456, old, transaction_factory=db.transaction)
    store.register_subscription(456, subscription('new-owner-endpoint'), transaction_factory=db.transaction)
    assert store.count_active_push_subscriptions(456, connection_factory=db.transaction) == 1


def test_rerun_refreshes_current_device_status_without_mutating_registration():
    from types import SimpleNamespace
    from streamlit.testing.v1 import AppTest
    from unittest.mock import patch
    from web_push_ui import web_push_display_state
    old = subscription('stale-state')
    renderer = Mock(return_value=SimpleNamespace(event=None))
    source = """
import streamlit as st
from web_push_ui import render_web_push_poc
from web_push import WebPushConfig
render_web_push_poc(WebPushConfig(), 123, active_device_count=2)
"""
    with patch('web_push_ui._register_components', return_value=(renderer, '/sw.js', '/')), \
         patch('web_push_ui.subscription_ownership', side_effect=['current', 'expired']):
        app = AppTest.from_string(source)
        app.session_state['web_push_subscription'] = old
        app.run()
        assert not app.exception
        assert renderer.call_args.kwargs['data']['server_state'] == 'current'
        app.run()
        assert not app.exception
        assert renderer.call_args.kwargs['data']['server_state'] == 'expired'
        assert any('등록된 알림 기기: 2대' in item.value for item in app.markdown)
    assert web_push_display_state(True, 'expired') == 'EXPIRED'
    assert web_push_display_state(True, 'revoked') == 'REVOKED'


def test_missing_browser_subscription_clears_stale_session_without_db_mutation():
    session = {'web_push_subscription': subscription(), 'web_push_db_ownership': 'current'}
    register, deactivate = Mock(), Mock()
    event = dict(action='inspect_missing', action_id='missing-browser', owner_context=123)
    assert handle_web_push_event(session, 123, event, registerer=register, deactivator=deactivate)
    assert 'web_push_subscription' not in session
    assert session['web_push_db_ownership'] == 'none'
    assert not handle_web_push_event(session, 123, event, registerer=register, deactivator=deactivate)
    register.assert_not_called()
    deactivate.assert_not_called()
