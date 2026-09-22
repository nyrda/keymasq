from keymasq.gui.widgets import type_macro_layout


def test_type_macro_layout_uses_session_value_and_ignores_stale_reply(monkeypatch) -> None:
    requests = []
    registered = []
    unregistered = []
    changed = []

    monkeypatch.setattr(
        type_macro_layout,
        "session_request_async",
        lambda request, callback, timeout=1.0: requests.append((request, callback, timeout)),
    )
    monkeypatch.setattr(
        type_macro_layout,
        "register_session_event_callback",
        lambda event, callback, *, connect: registered.append((event, callback, connect)),
    )
    monkeypatch.setattr(
        type_macro_layout,
        "unregister_session_event_callback",
        lambda event, callback: unregistered.append((event, callback)),
    )

    state = type_macro_layout.TypeMacroLayout(lambda: changed.append(state.layout_id))
    state.refresh()
    assert requests[0][0] == {"command": "get_settings"}
    assert registered[0][0] == "settings_changed"
    assert registered[0][2] is False

    # The session applied de but could not save it. An older get_settings
    # response must not restore the value that is still on disk.
    state._on_settings_changed({"keyboard_layout": "de"})
    requests[0][1]({"status": "ok", "keyboard_layout": "us"})
    assert state.layout_id == "de"
    assert changed == [None, "de"]

    state.refresh()
    requests[1][1]({"status": "ok", "keyboard_layout": "de", "persisted": False})
    assert state.layout_id == "de"
    state.close()
    assert unregistered == [("settings_changed", registered[0][1])]
