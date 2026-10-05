from mobile_ui import mobile_css


def test_mobile_css_stacks_controls_and_keeps_touch_targets_readable():
    css = mobile_css()

    assert "@media (max-width: 640px)" in css
    assert 'data-testid="stColumn"' in css
    assert "flex: 1 1 100%" in css
    assert "min-height: 2.75rem" in css
    assert "overflow-wrap: anywhere" in css
    assert "env(safe-area-inset-top" in css
    assert "white-space: normal" in css
    assert "word-break: keep-all" in css


def test_mobile_weekdays_use_compact_touch_friendly_grid():
    css = mobile_css()

    assert ".st-key-mobile_weekdays" in css
    assert "grid-template-columns: repeat(4, minmax(0, 1fr))" in css


def test_mobile_styles_do_not_change_desktop_layout():
    css = mobile_css()

    style_body = css.split("@media (max-width: 640px)", 1)[0]
    assert "stHorizontalBlock" not in style_body
    assert "stColumn" not in style_body


def test_mobile_auth_is_hidden_on_desktop_and_visible_on_mobile():
    css = mobile_css()

    desktop, mobile = css.split("@media (max-width: 640px)", 1)
    assert ".st-key-mobile_auth" in desktop
    assert "display: none" in desktop
    assert ".st-key-mobile_auth" in mobile
    assert "display: block" in mobile
