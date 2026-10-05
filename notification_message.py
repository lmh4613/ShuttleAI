"""Shared user-facing message formatting for manual weather delivery."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WeatherNotificationMessage:
    title: str
    body: str


def format_weather_notification(
    *,
    route_name: str,
    trip_type: str,
    boarding_stop: str,
    boarding_time: str | None,
    boarding_weather: dict,
    destination_stop: str,
    destination_weather: dict,
    comment: str,
    destination_time: str | None = None,
) -> WeatherNotificationMessage:
    """Build one canonical message used by manual Kakao and Web Push sends."""
    boarding_time_text = boarding_time or "등록된 탑승시간 없음"
    destination_time_text = f" ({destination_time})" if destination_time else ""
    body = (
        f"🚍 노선: {route_name} ({trip_type})\n\n"
        f"🟢 [탑승] {boarding_stop} ({boarding_time_text})\n"
        f"• 기온: {boarding_weather['temperature']} | 상태: {boarding_weather['sky_status']}\n\n"
        f"🔴 [하차] {destination_stop}{destination_time_text}\n"
        f"• 기온: {destination_weather['temperature']} | 상태: {destination_weather['sky_status']}\n\n"
        f"🤖 [AI 코멘트]\n{comment}"
    )
    return WeatherNotificationMessage(
        title=f"[{route_name}] 탑승·하차 날씨 안내",
        body=body,
    )
