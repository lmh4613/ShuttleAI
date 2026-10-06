"""Native select component for route selection without mobile text focus."""

import hashlib
import json
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components


_COMPONENT_PATH = Path(__file__).parent / "route_select_component"
_native_route_select = components.declare_component(
    "native_route_select",
    path=str(_COMPONENT_PATH),
)


def native_route_select(label: str, options, *, key: str) -> str:
    choices = list(options) or ["노선 없음"]
    current = st.session_state.get(key)
    if current not in choices:
        current = choices[0]
        st.session_state[key] = current

    component_identity = hashlib.sha1(
        json.dumps({"options": choices}, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:12]
    selected = _native_route_select(
        label=label,
        options=choices,
        value=current,
        key=f"{key}_native_{component_identity}",
        default=current,
    )
    if selected in choices and selected != st.session_state.get(key):
        st.session_state[key] = selected
    return st.session_state[key]
