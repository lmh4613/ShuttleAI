from unittest.mock import Mock

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


def test_native_route_select_updates_session_without_extra_rerun(monkeypatch):
    session = {"user_rt": "(출근) 망포"}
    monkeypatch.setattr(route_select_component.st, "session_state", session)
    monkeypatch.setattr(route_select_component, "_native_route_select",
                        Mock(return_value="(출근) 영통"))
    rerun = Mock()
    monkeypatch.setattr(route_select_component.st, "rerun", rerun)

    selected = route_select_component.native_route_select(
        "노선 선택", ["(출근) 망포", "(출근) 영통"], key="user_rt"
    )

    assert selected == "(출근) 영통"
    assert session["user_rt"] == "(출근) 영통"
    rerun.assert_not_called()
