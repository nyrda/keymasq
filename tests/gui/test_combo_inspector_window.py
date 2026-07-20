import pytest

from tests.gui.support import SessionIpcHarness, collect_listbox_row_labels

gi = pytest.importorskip("gi")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")


def test_combo_inspector_window_renders_snapshot_and_search(monkeypatch) -> None:
    from gi.repository import Gdk, Gtk

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

    def request_handler(payload, callback, _timeout):
        callback(current_snapshot["value"])

    session = SessionIpcHarness(request_handler=request_handler).install(
        monkeypatch, inspector_module
    )

    window = ComboInspectorWindow(Gtk.Window())

    assert session.requests == [{"command": "get_combo_inspector_snapshot"}]
    assert window._status_label.get_text() == "Active Combos - 1 combo"
    assert window.active_profiles_label.get_text() == "Base, Overlay"
    assert window.section_label.get_visible() is False
    assert window.search_entry.get_visible() is False
    assert len(window._snapshots) == 1
    assert window.snapshot_dropdown.get_sensitive() is False

    assert collect_listbox_row_labels(window.combo_listbox) == ["Quick Save"]

    assert (
        window._on_key_pressed(
            Gtk.EventControllerKey(),
            Gdk.KEY_q,
            0,
            Gdk.ModifierType(0),
        )
        is True
    )
    assert window.search_entry.get_visible() is True
    assert window.search_entry.get_text() == "q"
    window._combo_list.hide_search()

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
    session.emit("profiles_changed")
    assert session.requests[-1] == {"command": "get_combo_inspector_snapshot"}
    assert len(window._snapshots) == 2
    assert window.snapshot_dropdown.get_sensitive() is True
    assert window._status_label.get_text() == "Active Combos - 1 combo"
    assert window.active_profiles_label.get_text() == "Base, Overlay, Firefox"

    window.snapshot_dropdown.set_selected(1)
    assert window._status_label.get_text() == "Active Combos Snapshot - 1 combo"
    assert window.active_profiles_label.get_text() == "Base, Overlay"
    assert window._combo_list.visible_count() == 1

    current_snapshot["value"] = snapshot
    session.emit("profiles_changed")
    assert len(window._snapshots) == 2
    assert window._status_label.get_text() == "Active Combos - 1 combo"
    assert window.active_profiles_label.get_text() == "Base, Overlay"

    session.emit("keymasqd_status", {"connected": False})
    assert window._status_label.get_text() == "Active Combos - Daemon disconnected"
    assert window.section_label.get_visible() is False

    window._finalize()
    assert session.callbacks["profiles_changed"] == []
    assert session.callbacks["runtime_reset"] == []
    assert session.callbacks["keymasqd_status"] == []


def test_combo_inspector_search_scopes_visible_and_runtime_fields(monkeypatch) -> None:
    from gi.repository import Gtk

    from keymasq.gui.widgets import combo_inspector_window as inspector_module
    from keymasq.gui.widgets.combo_inspector_window import ComboInspectorWindow

    def workspace_combo(workspace: int) -> dict[str, object]:
        suffix = f" {workspace}" if workspace > 1 else ""
        return {
            "id": f"workspace-{workspace}",
            "name": f"movetoworkspace{suffix}",
            "profile_name": "Desktop",
            "order": workspace - 1,
            "steps": [
                {
                    "events": [
                        {
                            "evdev": f"key_f{workspace + 4}",
                            "hardware_id": "t3-controller-4",
                            "source": "kbd",
                            "device_name": "Test Keyboard",
                        }
                    ]
                }
            ],
            "action": {
                "action": "compositor_dispatch",
                "dispatcher": "movetoworkspace",
                "args": str(workspace),
            },
        }

    snapshot = {
        "status": "ok",
        "active_profiles": ["Desktop"],
        "combos": [workspace_combo(workspace) for workspace in range(1, 7)],
    }

    def request_handler(_payload, callback, _timeout):
        callback(snapshot)

    SessionIpcHarness(request_handler=request_handler).install(monkeypatch, inspector_module)
    window = ComboInspectorWindow(Gtk.Window())

    def visible_combo_ids() -> list[str]:
        return [
            row._combo_id
            for row in window._combo_list.iter_rows()
            if row.get_child_visible()
        ]

    window.search_entry.set_text("movetoworkspace 4")
    assert visible_combo_ids() == ["workspace-4"]

    window.search_entry.set_text("test t3")
    assert visible_combo_ids() == []
    assert window.section_label.get_text() == "No matching active combos."

    window.search_entry.set_text("")
    assert visible_combo_ids() == [f"workspace-{workspace}" for workspace in range(1, 7)]

    window._finalize()


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
    session = SessionIpcHarness().install(monkeypatch, inspector_module)

    window = ComboInspectorWindow(Gtk.Window())
    session.emit("profiles_changed")

    assert len(session.response_callbacks) == 2

    session.respond(1, new_snapshot)
    assert window.active_profiles_label.get_text() == "New"
    assert collect_listbox_row_labels(window.combo_listbox) == ["New Combo"]

    session.respond(0, old_snapshot)
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
    session = SessionIpcHarness().install(monkeypatch, inspector_module)

    window = ComboInspectorWindow(Gtk.Window())

    assert len(session.response_callbacks) == 1

    session.emit("keymasqd_status", {"connected": False})
    assert window._status_label.get_text() == "Active Combos - Daemon disconnected"

    session.respond(0, snapshot)
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
