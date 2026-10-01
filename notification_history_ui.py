"""Admin-only, explicitly refreshed notification history view."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import streamlit as st

from notification_repository import (
    NotificationHistoryEntry,
    NotificationRepositoryError,
    load_notification_history,
)


PREFIX = "notification_history_"
PERIOD_OPTIONS = {"최근 1일": 1, "최근 3일": 3, "최근 7일": 7}
KST = ZoneInfo("Asia/Seoul")


def reset_notification_history() -> None:
    for key in list(st.session_state):
        if key.startswith(PREFIX):
            del st.session_state[key]


def render_admin_notification_history(is_admin: bool, preview_user: bool = False) -> None:
    if not is_admin or preview_user:
        return
    _render_notification_history()


def _format_started_at(value: datetime) -> str:
    if value.tzinfo is not None:
        value = value.astimezone(KST)
    return value.strftime("%m/%d %H:%M:%S")


def _trip_label(trip_type: str | None) -> str:
    return {"morning": "출근", "evening": "퇴근"}.get(trip_type, "구분 없음")


def delivery_summary(entry: NotificationHistoryEntry) -> str:
    if entry.effective_channel == "PUSH":
        return (
            f"성공 {entry.success_count} / 실패 {entry.failed_count} / "
            f"만료 {entry.expired_count}"
        )
    if entry.success_count:
        return "카카오 발송 성공"
    if entry.failed_count:
        return "카카오 발송 실패"
    return "카카오 발송 결과 없음"


def _render_entry(entry: NotificationHistoryEntry) -> None:
    nickname = f" ({entry.nickname})" if entry.nickname else ""
    region = f"{entry.region_name} " if entry.region_name else ""
    route = entry.route_name or "삭제된 즐겨찾기"
    st.markdown(
        f"**{_format_started_at(entry.started_at)} | 사용자 {entry.user_id}{nickname} | "
        f"{region}{_trip_label(entry.trip_type)}**"
    )
    st.write(
        f"{entry.scheduled_time.strftime('%H:%M')} 탑승 | {entry.effective_channel} | "
        f"{entry.status}"
        + (f" | {entry.error_code}" if entry.error_code else "")
    )
    route_detail = route
    if entry.boarding_name and entry.destination_name:
        route_detail += f" · {entry.boarding_name} → {entry.destination_name}"
    st.caption(f"{route_detail} · {delivery_summary(entry)}")


@st.fragment
def _render_notification_history() -> None:
    with st.expander("🔔 알림 발송 이력", expanded=False):
        st.caption("기간을 선택한 뒤 새로고침할 때만 Aiven에서 최근 이력을 조회합니다.")
        with st.form(PREFIX + "form"):
            period_label = st.selectbox(
                "조회 기간",
                list(PERIOD_OPTIONS),
                index=1,
                key=PREFIX + "period",
            )
            refresh = st.form_submit_button("🔄 새로고침", width="stretch")

        if refresh:
            try:
                st.session_state[PREFIX + "entries"] = load_notification_history(
                    PERIOD_OPTIONS[period_label], limit=100
                )
                st.session_state[PREFIX + "loaded_days"] = PERIOD_OPTIONS[period_label]
                st.session_state.pop(PREFIX + "error", None)
            except (NotificationRepositoryError, ValueError):
                st.session_state[PREFIX + "entries"] = []
                st.session_state[PREFIX + "error"] = True

        if st.session_state.get(PREFIX + "error"):
            st.error("알림 발송 이력을 불러오지 못했습니다.")
            return
        if PREFIX + "entries" not in st.session_state:
            st.info("새로고침을 눌러 알림 발송 이력을 조회하세요.")
            return

        entries = st.session_state[PREFIX + "entries"]
        loaded_days = st.session_state.get(PREFIX + "loaded_days", 3)
        st.caption(f"최근 {loaded_days}일 · 최신순 · 최대 100건")
        if not entries:
            st.info("조회 기간 내 알림 발송 이력이 없습니다.")
            return
        with st.container(height=420, border=True):
            for index, entry in enumerate(entries):
                _render_entry(entry)
                if index < len(entries) - 1:
                    st.divider()
