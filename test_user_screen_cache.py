from unittest.mock import Mock

from user_screen_cache import (
    CACHE_KEY,
    get_user_screen_data,
    invalidate_user_screen_data,
)


def dashboard(kakao_user_id, internal_user_id):
    return {
        "kakao_user_id": kakao_user_id,
        "internal_user_id": internal_user_id,
        "notification_settings": {"user_id": internal_user_id},
        "favorites": [],
    }


def test_cache_miss_then_stable_rerun_has_no_repository_read():
    session = {}
    loader = Mock(return_value=dashboard(101, 1))
    first = get_user_screen_data(session, 101, loader=loader, clock=lambda: 10)
    second = get_user_screen_data(session, 101, loader=loader, clock=lambda: 20)
    assert first is second
    loader.assert_called_once_with(101)


def test_cache_is_never_shared_between_users():
    session = {}
    loader = Mock(side_effect=[dashboard(101, 1), dashboard(202, 2)])
    assert get_user_screen_data(session, 101, loader=loader, clock=lambda: 10)["internal_user_id"] == 1
    assert get_user_screen_data(session, 202, loader=loader, clock=lambda: 20)["internal_user_id"] == 2
    assert loader.call_args_list[0].args == (101,)
    assert loader.call_args_list[1].args == (202,)


def test_invalidation_is_owner_scoped_and_forces_reload():
    session = {CACHE_KEY: {**dashboard(101, 1), "_cache_loaded_at": 10}}
    assert not invalidate_user_screen_data(session, 202)
    assert CACHE_KEY in session
    assert invalidate_user_screen_data(session, 101)
    loader = Mock(return_value=dashboard(101, 1))
    get_user_screen_data(session, 101, loader=loader, clock=lambda: 20)
    loader.assert_called_once_with(101)


def test_cache_ttl_prevents_indefinite_staleness():
    session = {}
    loader = Mock(side_effect=[dashboard(101, 1), dashboard(101, 1)])
    get_user_screen_data(session, 101, loader=loader, clock=lambda: 10)
    get_user_screen_data(session, 101, loader=loader, clock=lambda: 309)
    assert loader.call_count == 1
    get_user_screen_data(session, 101, loader=loader, clock=lambda: 310)
    assert loader.call_count == 2
