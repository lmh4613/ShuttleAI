from unittest.mock import Mock

import route_select_component


def test_frontend_preserves_selection_until_options_change():
    source = route_select_component._COMPONENT_PATH.joinpath("index.html").read_text(
        encoding="utf-8"
    )

    assert "optionSignature" in source
    assert "if (nextSignature !== optionSignature)" in source
    assert "setComponentValue(currentValue)" in source
    assert "renderedValue" not in source


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


def test_native_route_select_key_is_stable_for_route_value_changes(monkeypatch):
    options = ["(출근) 망포", "(출근) 영통"]
    session = {"user_rt": "(출근) 망포"}
    component = Mock(side_effect=["(출근) 영통", "(출근) 영통"])
    monkeypatch.setattr(route_select_component.st, "session_state", session)
    monkeypatch.setattr(route_select_component, "_native_route_select", component)

    route_select_component.native_route_select("노선 선택", options, key="user_rt")
    first_key = component.call_args.kwargs["key"]
    route_select_component.native_route_select("노선 선택", options, key="user_rt")
    second_key = component.call_args.kwargs["key"]

    assert session["user_rt"] == "(출근) 영통"
    assert first_key == second_key


def test_native_route_select_key_changes_only_when_options_change(monkeypatch):
    session = {"user_rt": "(출근) 망포"}
    component = Mock(return_value="(출근) 망포")
    monkeypatch.setattr(route_select_component.st, "session_state", session)
    monkeypatch.setattr(route_select_component, "_native_route_select", component)

    route_select_component.native_route_select(
        "노선 선택", ["(출근) 망포", "(출근) 영통"], key="user_rt"
    )
    first_key = component.call_args.kwargs["key"]
    route_select_component.native_route_select(
        "노선 선택", ["(퇴근) 강남", "(퇴근) 사당"], key="user_rt"
    )
    second_key = component.call_args.kwargs["key"]

    assert session["user_rt"] == "(퇴근) 강남"
    assert first_key != second_key


def test_native_route_select_accepts_repeated_changes_without_value_in_key(monkeypatch):
    options = ["A", "B"]
    session = {"user_rt": "A"}
    component = Mock(side_effect=["B", "A", "B"])
    monkeypatch.setattr(route_select_component.st, "session_state", session)
    monkeypatch.setattr(route_select_component, "_native_route_select", component)

    route_select_component.native_route_select("노선 선택", options, key="user_rt")
    first_key = component.call_args.kwargs["key"]
    route_select_component.native_route_select("노선 선택", options, key="user_rt")
    second_key = component.call_args.kwargs["key"]
    route_select_component.native_route_select("노선 선택", options, key="user_rt")
    third_key = component.call_args.kwargs["key"]

    assert session["user_rt"] == "B"
    assert first_key == second_key == third_key
