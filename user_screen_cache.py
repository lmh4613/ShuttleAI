"""Session-scoped reuse for the Aiven-backed ordinary-user screen."""

from __future__ import annotations

from collections.abc import Callable, MutableMapping
import time


CACHE_KEY = "_user_screen_dashboard_cache"
ROUTE_CACHE_KEY = "_user_screen_route_cache"
CACHE_TTL_SECONDS = 300


def get_user_screen_data(
    session: MutableMapping,
    kakao_user_id: int,
    *,
    loader: Callable | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict:
    cached = session.get(CACHE_KEY)
    now = clock()
    if (isinstance(cached, dict)
            and cached.get("kakao_user_id") == kakao_user_id
            and now - cached.get("_cache_loaded_at", float("-inf")) < CACHE_TTL_SECONDS):
        return cached
    if loader is None:
        from user_settings_repository import get_user_dashboard_data
        loader = get_user_dashboard_data
    loaded = dict(loader(kakao_user_id))
    if (loaded.get("kakao_user_id") != kakao_user_id
            or loaded.get("internal_user_id") is None):
        raise ValueError("User screen cache identity mismatch")
    loaded["_cache_loaded_at"] = now
    session[CACHE_KEY] = loaded
    return loaded


def invalidate_user_screen_data(
    session: MutableMapping,
    kakao_user_id: int | None = None,
) -> bool:
    cached = session.get(CACHE_KEY)
    if not isinstance(cached, dict):
        return False
    if kakao_user_id is not None and cached.get("kakao_user_id") != kakao_user_id:
        return False
    del session[CACHE_KEY]
    return True


def get_user_route_data(
    session: MutableMapping,
    *,
    loader: Callable | None = None,
) -> list[dict]:
    """Return route rows once per Streamlit session for ordinary-user filtering."""
    cached = session.get(ROUTE_CACHE_KEY)
    if isinstance(cached, list):
        return cached
    if loader is None:
        from route_repository import load_routes_for_ui
        loader = load_routes_for_ui
    loaded = list(loader())
    session[ROUTE_CACHE_KEY] = loaded
    return loaded


def invalidate_user_route_data(session: MutableMapping) -> bool:
    if ROUTE_CACHE_KEY not in session:
        return False
    del session[ROUTE_CACHE_KEY]
    return True
