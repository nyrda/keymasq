from collections.abc import Callable

import pytest

from tests.gui.support import collect_listbox_row_labels

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")


def test_combo_inspector_window_renders_snapshot_and_search(monkeypatch) -> None:
    from gi.repository import Gtk

    from keymasq.gui.widgets import combo_inspector_window as inspector_module
    from keymasq.gui.widgets.combo_inspector_window import ComboInspectorWindow

    snapshot = {
        "status": "ok",
        "active_profiles": ["Base", "Overlay"],
        "combos": [
            {
                "id": "combo-1",
                "name": "Quick Save",
                "profile_name": "Overlay",
                "order": 0,
                "steps": [
                    {
                        "timeout_ms": 500,
                        "events": [
                            {
                                "evdev": "key_s",
                                "hardware_id": "1234:5678",
                                "source": "kbd",
                                "device_name": "Gaming Keyboard",
                            }
                        ],
                    }
                ],
                "action": {"action": "keyboard", "target": "key_f5"},
                "recall_trigger_keys": True,
                "restore_trigger_keys": ["key_leftctrl"],
                "match_across_devices": True,
            }
        ],
    }
    app_snapshot = {
        "status": "ok",
        "active_profiles": ["Base", "Overlay", "Firefox"],
        "combos": [
            {
                "id": "combo-2",
                "name": "App Action",
                "profile_name": "Firefox",
                "order": 0,
                "steps": [
                    {
                        "events": [
                            {
                                "evdev": "key_f",
                                "hardware_id": "1234:5678",
                                "source": "kbd",
                                "device_name": "Gaming Keyboard",
                            }
                        ],
                    }
                ],
                "action": {"action": "keyboard", "target": "key_f6"},
            }
        ],
    }
    current_snapshot = {"value": snapshot}
    requests: list[dict[str, object]] = []
    callbacks: dict[str, list[Callable[[dict[str, object]], object]]] = {}

    def fake_register(event, callback):
        callbacks.setdefault(event, []).append(callback)

    def fake_unregister(event, callback):
        callbacks[event].remove(callback)

    def fake_request_async(payload, callback, timeout=5.0):
        requests.append(payload)
        callback(current_snapshot["value"])

    monkeypatch.setattr(inspector_module, "register_session_event_callback", fake_register)
    monkeypatch.setattr(inspector_module, "unregister_session_event_callback", fake_unregister)
    monkeypatch.setattr(inspector_module, "session_request_async", fake_request_async)

    window = ComboInspectorWindow(Gtk.Window())

    assert requests == [{"command": "get_combo_inspector_snapshot"}]
    assert window._status_label.get_text() == "Active Combos - 1 combo"
    assert window.active_profiles_label.get_text() == "Base, Overlay"
    assert window.section_label.get_visible() is False
    assert window.search_entry.get_visible() is False
    assert len(window._snapshots) == 1
    assert window.snapshot_dropdown.get_sensitive() is False

    assert collect_listbox_row_labels(window.combo_listbox) == ["Quick Save"]

    window.search_button.emit("clicked")
    assert window.search_entry.get_visible() is True

    window.search_entry.set_text("gaming kbd")
    assert window._combo_list.visible_count() == 1

    window.search_entry.set_text("across devices")
    assert window._combo_list.visible_count() == 1

    window.search_entry.set_text("missing")
    assert window._combo_list.visible_count() == 0
    assert window.section_label.get_text() == "No matching active combos."
    assert window.combo_listbox.get_visible() is False

    window.search_entry.set_text("")
    current_snapshot["value"] = app_snapshot
    callbacks["profiles_changed"][0]({"event": "profiles_changed"})
    assert requests[-1] == {"command": "get_combo_inspector_snapshot"}
    assert len(window._snapshots) == 2
    assert window.snapshot_dropdown.get_sensitive() is True
    assert window._status_label.get_text() == "Active Combos - 1 combo"
    assert window.active_profiles_label.get_text() == "Base, Overlay, Firefox"

    window.snapshot_dropdown.set_selected(1)
    assert window._status_label.get_text() == "Active Combos Snapshot - 1 combo"
    assert window.active_profiles_label.get_text() == "Base, Overlay"
    assert window._combo_list.visible_count() == 1

    current_snapshot["value"] = snapshot
    callbacks["profiles_changed"][0]({"event": "profiles_changed"})
    assert len(window._snapshots) == 2
    assert window._status_label.get_text() == "Active Combos - 1 combo"
    assert window.active_profiles_label.get_text() == "Base, Overlay"

    callbacks["keymasqd_status"][0]({"event": "keymasqd_status", "connected": False})
    assert window._status_label.get_text() == "Active Combos - Daemon disconnected"
    assert window.section_label.get_visible() is False

    window._finalize()
    assert callbacks["profiles_changed"] == []
    assert callbacks["runtime_reset"] == []
    assert callbacks["keymasqd_status"] == []


def test_combo_inspector_window_ignores_stale_snapshot_responses(monkeypatch) -> None:
    from gi.repository import Gtk

    from keymasq.gui.widgets import combo_inspector_window as inspector_module
    from keymasq.gui.widgets.combo_inspector_window import ComboInspectorWindow

    old_snapshot = {
        "status": "ok",
        "active_profiles": ["Old"],
        "combos": [
            {
                "id": "combo-old",
                "name": "Old Combo",
                "profile_name": "Old",
                "steps": [{"events": [{"evdev": "key_o"}]}],
                "action": {"action": "keyboard", "target": "key_o"},
            }
        ],
    }
    new_snapshot = {
        "status": "ok",
        "active_profiles": ["New"],
        "combos": [
            {
                "id": "combo-new",
                "name": "New Combo",
                "profile_name": "New",
                "steps": [{"events": [{"evdev": "key_n"}]}],
                "action": {"action": "keyboard", "target": "key_n"},
            }
        ],
    }
    event_callbacks: dict[str, list[Callable[[dict[str, object]], object]]] = {}
    response_callbacks: list[Callable[[dict[str, object]], object]] = []

    def fake_register(event, callback):
        event_callbacks.setdefault(event, []).append(callback)

    def fake_unregister(event, callback):
        event_callbacks[event].remove(callback)

    def fake_request_async(_payload, callback, timeout=5.0):
        response_callbacks.append(callback)

    monkeypatch.setattr(inspector_module, "register_session_event_callback", fake_register)
    monkeypatch.setattr(inspector_module, "unregister_session_event_callback", fake_unregister)
    monkeypatch.setattr(inspector_module, "session_request_async", fake_request_async)

    window = ComboInspectorWindow(Gtk.Window())
    event_callbacks["profiles_changed"][0]({"event": "profiles_changed"})

    assert len(response_callbacks) == 2

    response_callbacks[1](new_snapshot)
    assert window.active_profiles_label.get_text() == "New"
    assert collect_listbox_row_labels(window.combo_listbox) == ["New Combo"]

    response_callbacks[0](old_snapshot)
    assert window.active_profiles_label.get_text() == "New"
    assert collect_listbox_row_labels(window.combo_listbox) == ["New Combo"]
    assert len(window._snapshots) == 1

    window._finalize()


def test_combo_inspector_window_ignores_snapshot_response_after_disconnect(monkeypatch) -> None:
    from gi.repository import Gtk

    from keymasq.gui.widgets import combo_inspector_window as inspector_module
    from keymasq.gui.widgets.combo_inspector_window import ComboInspectorWindow

    snapshot = {
        "status": "ok",
        "active_profiles": ["Base"],
        "combos": [
            {
                "id": "combo-1",
                "name": "Late Combo",
                "profile_name": "Base",
                "steps": [{"events": [{"evdev": "key_l"}]}],
                "action": {"action": "keyboard", "target": "key_l"},
            }
        ],
    }
    event_callbacks: dict[str, list[Callable[[dict[str, object]], object]]] = {}
    response_callbacks: list[Callable[[dict[str, object]], object]] = []

    def fake_register(event, callback):
        event_callbacks.setdefault(event, []).append(callback)

    def fake_unregister(event, callback):
        event_callbacks[event].remove(callback)

    def fake_request_async(_payload, callback, timeout=5.0):
        response_callbacks.append(callback)

    monkeypatch.setattr(inspector_module, "register_session_event_callback", fake_register)
    monkeypatch.setattr(inspector_module, "unregister_session_event_callback", fake_unregister)
    monkeypatch.setattr(inspector_module, "session_request_async", fake_request_async)

    window = ComboInspectorWindow(Gtk.Window())

    assert len(response_callbacks) == 1

    event_callbacks["keymasqd_status"][0]({"event": "keymasqd_status", "connected": False})
    assert window._status_label.get_text() == "Active Combos - Daemon disconnected"

    response_callbacks[0](snapshot)
    assert window._status_label.get_text() == "Active Combos - Daemon disconnected"
    assert window.active_profiles_label.get_text() == "None"
    assert collect_listbox_row_labels(window.combo_listbox) == []
    assert window._snapshots == []

    window._finalize()


def test_combo_inspector_snapshot_signature_includes_match_across_devices() -> None:
    from keymasq.gui.widgets.combo_inspector_window import _snapshot_signature

    base_combo = {
        "id": "combo-1",
        "name": "Across Devices",
        "profile_name": "Base",
        "steps": [{"events": [{"evdev": "key_a"}]}],
        "action": {"action": "keyboard", "target": "key_b"},
    }

    scoped = {"status": "ok", "active_profiles": ["Base"], "combos": [base_combo]}
    across = {
        "status": "ok",
        "active_profiles": ["Base"],
        "combos": [{**base_combo, "match_across_devices": True}],
    }

    assert _snapshot_signature(scoped) != _snapshot_signature(across)


def test_combo_inspector_mapping_action_payload_preserves_macro_loop_stop_behavior() -> None:
    from keymasq.gui.widgets.combo_inspector_window import _mapping_action_from_payload

    action = _mapping_action_from_payload(
        {
            "action": "macro",
            "target": "Looped Macro",
            "loop_stop_behavior": "cancel_run",
            "keys": "not-a-list",
        }
    )

    assert action is not None
    assert action.macro_loop_stop_behavior == "cancel_run"
    assert action.keys is None


def test_combo_inspector_mapping_action_payload_keeps_unknown_actions_visible() -> None:
    from keymasq.common.models import ActionType
    from keymasq.gui.widgets.combo_inspector_window import _mapping_action_from_payload

    action = _mapping_action_from_payload(
        {
            "action": "future_action",
            "target": "future-target",
        }
    )

    assert action is not None
    assert action.action_type == ActionType.PASSTHROUGH
    assert action.target == "future-target"


def test_combo_inspector_mapping_action_payload_preserves_profile_deactivation() -> None:
    from keymasq.gui.widgets.combo_inspector_window import _mapping_action_from_payload

    action = _mapping_action_from_payload(
        {
            "action": "profile_toggle",
            "profile_name": "Layer",
            "deactivation": {"on_trigger_end": True},
        }
    )

    assert action is not None
    assert action.profile_deactivation is not None
    assert action.profile_deactivation.on_trigger_end is True
