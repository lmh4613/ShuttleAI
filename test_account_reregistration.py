"""Local lifecycle regression using real SQLite constraints and repository SQL.

Only PostgreSQL placeholder/constraint-name/locking syntax is adapted. The
optional Aiven suite remains the authority for PostgreSQL-specific behavior.
"""
import sqlite3
from contextlib import contextmanager

import pytest
from cryptography.fernet import Fernet

from test_user_settings_repository import selection
from test_push_subscription_store import subscription
from token_crypto import TOKEN_KEY_ENV, decrypt_token
from user_settings_repository import (
    create_favorite, deactivate_user_account, provision_or_sync_login_user,
    list_favorites, get_notification_settings, update_notification_settings,
    DuplicateFavoriteError,
)
from push_subscription_store import register_subscription, count_active_push_subscriptions


class Cursor:
    def __init__(self, connection):
        self.raw = connection.cursor()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.raw.close()

    @staticmethod
    def sql(sql):
        return (sql.replace('%s', '?').replace(' FOR UPDATE', '')
                .replace('ON CONFLICT ON CONSTRAINT uq_favorites_selection',
                         'ON CONFLICT (user_id, route_id, boarding_route_stop_id, alighting_route_stop_id)')
                .replace('UPDATE favorite_notifications fn ',
                         'UPDATE favorite_notifications AS fn '))

    def execute(self, sql, params=()):
        self.raw.execute(self.sql(sql), params)

    def executemany(self, sql, params):
        self.raw.executemany(self.sql(sql), params)

    def fetchone(self):
        return self.raw.fetchone()

    def fetchall(self):
        return self.raw.fetchall()


class Database:
    def __init__(self):
        self.raw = sqlite3.connect(':memory:', detect_types=sqlite3.PARSE_DECLTYPES)
        self.raw.create_function('now', 0, lambda: '2026-10-15 00:00:00')
        self.raw.executescript('''
            PRAGMA foreign_keys=ON;
            CREATE TABLE users (id INTEGER PRIMARY KEY, kakao_user_id INTEGER UNIQUE,
                nickname TEXT, role TEXT, enabled BOOLEAN, last_login_at TEXT);
            CREATE TABLE notification_settings (user_id INTEGER PRIMARY KEY REFERENCES users(id),
                exclude_holidays BOOLEAN, timezone TEXT, delivery_channel TEXT);
            CREATE TABLE notification_active_days (user_id INTEGER REFERENCES users(id),
                weekday INTEGER, PRIMARY KEY(user_id, weekday));
            CREATE TABLE kakao_credentials (user_id INTEGER PRIMARY KEY REFERENCES users(id),
                access_token_ciphertext BLOB, refresh_token_ciphertext BLOB, token_updated_at TEXT);
            CREATE TABLE regions (id INTEGER PRIMARY KEY, code TEXT);
            CREATE TABLE routes (id INTEGER PRIMARY KEY, region_id INTEGER REFERENCES regions(id),
                name TEXT, trip_type TEXT, active BOOLEAN);
            CREATE TABLE stops (id INTEGER PRIMARY KEY, name TEXT, latitude REAL, longitude REAL,
                active BOOLEAN);
            CREATE TABLE route_stops (id INTEGER PRIMARY KEY, route_id INTEGER REFERENCES routes(id),
                stop_id INTEGER REFERENCES stops(id), stop_order INTEGER, scheduled_time TEXT,
                boarding_allowed BOOLEAN, alighting_allowed BOOLEAN, is_default_dropoff BOOLEAN,
                source_kind TEXT, active BOOLEAN, UNIQUE(id, route_id));
            CREATE TABLE favorites (id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id),
                route_id INTEGER REFERENCES routes(id), boarding_route_stop_id INTEGER,
                alighting_route_stop_id INTEGER, active BOOLEAN DEFAULT TRUE,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, route_id, boarding_route_stop_id, alighting_route_stop_id),
                FOREIGN KEY(boarding_route_stop_id, route_id) REFERENCES route_stops(id, route_id),
                FOREIGN KEY(alighting_route_stop_id, route_id) REFERENCES route_stops(id, route_id));
            CREATE TABLE favorite_notifications (favorite_id INTEGER PRIMARY KEY REFERENCES favorites(id),
                enabled BOOLEAN, lead_minutes INTEGER);
            CREATE TABLE push_subscriptions (id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id),
                endpoint TEXT, endpoint_hash TEXT UNIQUE, p256dh TEXT, auth TEXT,
                expiration_time TEXT, enabled BOOLEAN, last_seen_at TEXT,
                owner_changed_at TEXT, revoked_at TEXT);
            INSERT INTO regions VALUES(1, 'seoul');
            INSERT INTO routes VALUES(22, 1, '출근 테스트', 'morning', TRUE);
            INSERT INTO stops VALUES(1, '탑승 A', 37.1, 127.1, TRUE);
            INSERT INTO stops VALUES(2, '판교 제2테크노밸리', 37.412605, 127.095703, TRUE);
            INSERT INTO route_stops VALUES(31,22,1,1,NULL,TRUE,FALSE,FALSE,'document',TRUE);
            INSERT INTO route_stops VALUES(32,22,2,99,NULL,FALSE,TRUE,TRUE,'system_default',TRUE);
        ''')

    def cursor(self):
        return Cursor(self.raw)

    @contextmanager
    def transaction(self):
        with self.raw:
            yield self


@pytest.fixture
def db(monkeypatch):
    # Decimal parameters are used by the production coordinate resolver.
    from decimal import Decimal
    sqlite3.register_adapter(Decimal, float)
    monkeypatch.setenv(TOKEN_KEY_ENV, Fernet.generate_key().decode('ascii'))
    database = Database()
    yield database
    database.raw.close()


def login(db, access='new-access'):
    return provision_or_sync_login_user(123, nickname='test', access_token=access,
        refresh_token='new-refresh', transaction_factory=db.transaction)


def test_withdraw_rejoin_and_explicit_favorite_and_push_registration(db):
    assert login(db)['created']
    favorite_id = create_favorite(123, selection(), transaction_factory=db.transaction)
    assert [f['favorite_id'] for f in list_favorites(123, connection_factory=db.transaction)] == [favorite_id]
    # New favorites keep the existing opt-in default; explicit restoration enables the old row.
    assert db.raw.execute('SELECT enabled FROM favorite_notifications').fetchone() == (0,)
    update_notification_settings(123, active_days=[1, 3], exclude_holidays=False,
        delivery_channel='PUSH', transaction_factory=db.transaction)
    db.raw.execute('UPDATE favorite_notifications SET enabled=TRUE, lead_minutes=25')
    device = subscription()
    register_subscription(123, device, transaction_factory=db.transaction)
    device_id = db.raw.execute('SELECT id FROM push_subscriptions').fetchone()[0]
    assert count_active_push_subscriptions(123, connection_factory=db.transaction) == 1

    deactivate_user_account(123, transaction_factory=db.transaction)
    assert db.raw.execute('SELECT enabled FROM users').fetchone() == (0,)
    assert db.raw.execute('SELECT active FROM favorites').fetchone() == (0,)
    assert db.raw.execute('SELECT enabled FROM favorite_notifications').fetchone() == (0,)
    assert db.raw.execute('SELECT enabled, revoked_at IS NOT NULL FROM push_subscriptions').fetchone() == (0, 1)
    assert db.raw.execute('SELECT access_token_ciphertext, refresh_token_ciphertext FROM kakao_credentials').fetchone() == (None, None)

    assert login(db)['reactivated']
    assert list_favorites(123, connection_factory=db.transaction) == []
    assert count_active_push_subscriptions(123, connection_factory=db.transaction) == 0
    settings = get_notification_settings(123, connection_factory=db.transaction)
    assert settings['active_days'] == [1, 3]
    assert settings['exclude_holidays'] == 0
    assert settings['delivery_channel'] == 'PUSH'
    tokens = db.raw.execute('SELECT access_token_ciphertext, refresh_token_ciphertext FROM kakao_credentials').fetchone()
    assert tuple(map(decrypt_token, tokens)) == ('new-access', 'new-refresh')

    assert create_favorite(123, selection(), transaction_factory=db.transaction) == favorite_id
    favorites = list_favorites(123, connection_factory=db.transaction)
    assert len(favorites) == 1
    assert favorites[0]['notify_enabled'] == 1
    assert favorites[0]['notify_min'] == 25
    db.raw.execute('UPDATE favorite_notifications SET enabled=FALSE')
    db.raw.commit()
    with pytest.raises(DuplicateFavoriteError):
        create_favorite(123, selection(), transaction_factory=db.transaction)
    assert db.raw.execute('SELECT enabled FROM favorite_notifications').fetchone() == (0,)
    assert db.raw.execute('SELECT count(*) FROM favorites').fetchone() == (1,)
    assert db.raw.execute('SELECT count(*) FROM favorite_notifications').fetchone() == (1,)

    device['keys'] = {'p256dh': 'updated-public', 'auth': 'updated-auth'}
    for _ in range(2):
        register_subscription(123, device, transaction_factory=db.transaction)
    assert db.raw.execute('SELECT id, enabled, revoked_at, p256dh, auth FROM push_subscriptions').fetchall() == [
        (device_id, 1, None, 'updated-public', 'updated-auth')]
    assert count_active_push_subscriptions(123, connection_factory=db.transaction) == 1
    for table in ('users', 'notification_settings', 'kakao_credentials'):
        assert db.raw.execute(f'SELECT count(*) FROM {table}').fetchone() == (1,)
    assert db.raw.execute('SELECT count(*) FROM notification_active_days').fetchone() == (2,)


def test_favorite_restore_rolls_back_if_notification_write_fails(db):
    login(db)
    create_favorite(123, selection(), transaction_factory=db.transaction)
    deactivate_user_account(123, transaction_factory=db.transaction)
    login(db)
    db.raw.execute('''CREATE TRIGGER reject_notification BEFORE UPDATE ON favorite_notifications
                      BEGIN SELECT RAISE(ABORT, 'test failure'); END''')
    from user_settings_repository import UserSettingsError
    with pytest.raises(UserSettingsError):
        create_favorite(123, selection(), transaction_factory=db.transaction)
    assert db.raw.execute('SELECT active FROM favorites').fetchone() == (0,)
    assert list_favorites(123, connection_factory=db.transaction) == []


def test_login_flags_and_withdrawal_are_committed(db):
    first = login(db)
    assert first == {'user_id': 1, 'created': True, 'reactivated': False}
    assert not db.raw.in_transaction
    deactivate_user_account(123, transaction_factory=db.transaction)
    assert not db.raw.in_transaction
    assert db.raw.execute('SELECT enabled FROM users').fetchone() == (0,)
    second = login(db)
    assert second == {'user_id': 1, 'created': False, 'reactivated': True}
    assert not db.raw.in_transaction
    assert db.raw.execute('SELECT enabled FROM users').fetchone() == (1,)
    assert login(db) == {'user_id': 1, 'created': False, 'reactivated': False}
