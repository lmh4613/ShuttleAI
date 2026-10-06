from unittest.mock import Mock

import pytest

import route_select_component


def test_native_route_select_keeps_restored_value(monkeypatch):
    session = {"user_rt": "(퇴근) 노원"}
    component = Mock(return_value="(퇴근) 노원")
    monkeypatch.setattr(route_select_component.st, "session_state", session)
    monkeypatch.setattr(route_select_component, "_native_route_select", component)

    selected = route_select_component.native_route_select(
        "노선 선택", ["(퇴근) 노원", "(출근) 판교"], key="user_rt"
    )

    assert selected == "(퇴근) 노원"
    assert session["user_rt"] == "(퇴근) 노원"
    assert component.call_args.kwargs["value"] == "(퇴근) 노원"
    assert component.call_args.kwargs["default"] == "(퇴근) 노원"


def test_native_route_select_resets_missing_route_to_first_option(monkeypatch):
    session = {"user_rt": "(퇴근) 노원"}
    monkeypatch.setattr(route_select_component.st, "session_state", session)
    monkeypatch.setattr(route_select_component, "_native_route_select",
                        Mock(return_value="(출근) 망포"))

    selected = route_select_component.native_route_select(
        "노선 선택", ["(출근) 망포", "(출근) 영통"], key="user_rt"
    )

    assert selected == "(출근) 망포"
    assert session["user_rt"] == "(출근) 망포"


def test_native_route_select_updates_session_and_reruns_on_component_change(monkeypatch):
    session = {"user_rt": "(출근) 망포"}
    monkeypatch.setattr(route_select_component.st, "session_state", session)
    monkeypatch.setattr(route_select_component, "_native_route_select",
                        Mock(return_value="(출근) 영통"))
    monkeypatch.setattr(route_select_component.st, "rerun",
                        Mock(side_effect=RuntimeError("rerun")))

    with pytest.raises(RuntimeError, match="rerun"):
        route_select_component.native_route_select(
            "노선 선택", ["(출근) 망포", "(출근) 영통"], key="user_rt"
        )

    assert session["user_rt"] == "(출근) 영통"
    route_select_component.st.rerun.assert_called_once_with()
