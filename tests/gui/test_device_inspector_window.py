import pytest

from tests.gui.support import collect_child_widgets

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")


def test_inspector_keeps_many_keyboard_backed_mouse_buttons_in_mouse_layout() -> None:
    from gi.repository import Gtk

    from keymasq.common.models import ButtonDefinition, DeviceType, EvdevDevice, HardwareConfig
    from keymasq.gui.widgets.device_inspector_window import DeviceInspectorWindow

    buttons = [
        ButtonDefinition(
            id="btn_left",
            label="Left Click",
            evdev="btn_left",
            evdev_code=272,
            source="mouse",
        ),
        ButtonDefinition(
            id="btn_right",
            label="Right Click",
            evdev="btn_right",
            evdev_code=273,
            source="mouse",
        ),
        ButtonDefinition(
            id="btn_middle",
            label="Middle Click",
            evdev="btn_middle",
            evdev_code=274,
            source="mouse",
        ),
        ButtonDefinition(
            id="scroll_up",
            label="Scroll Up",
            evdev="rel_wheel",
            evdev_code=8,
            evdev_value=1,
            source="mouse",
        ),
        ButtonDefinition(
            id="btn_side",
            label="Back",
            evdev="btn_side",
            evdev_code=275,
            source="mouse",
        ),
    ]
    buttons.extend(
        ButtonDefinition(
            id=f"key_extra_{index}",
            label=f"Extra {index}",
            evdev=f"key_{index}",
            evdev_code=index,
            source="keyboard",
        )
        for index in range(1, 15)
    )
    device = HardwareConfig(
        vendor_id="1532",
        product_id="00b4",
        name="Razer Naga",
        evdev_devices=[
            EvdevDevice(
                path="/dev/input/event10",
                device_type=DeviceType.MOUSE,
                id="mouse",
            ),
            EvdevDevice(
                path="/dev/input/event11",
                device_type=DeviceType.KEYBOARD,
                id="keyboard",
            ),
        ],
        buttons=buttons,
    )
    snapshot = {
        "active_profiles": ["Default"],
        "buttons": [
            {
                "id": button.id,
                "label": button.label,
                "kind": "button",
                "evdev": button.evdev,
                "evdev_code": button.evdev_code,
                "evdev_value": button.evdev_value,
                "source": button.source,
                "action": None,
            }
            for button in buttons
        ],
    }

    window = DeviceInspectorWindow.__new__(DeviceInspectorWindow)
    window.device = device
    window._device_kind = "mouse"
    window._mapping_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    window._control_widgets = {}

    window._render_mapping(snapshot)

    section_titles = [
        label.get_text()
        for label in collect_child_widgets(window._mapping_box, Gtk.Label)
        if label.has_css_class("button-section-title") and label.get_text() != "Resolved Mapping"
    ]

    assert section_titles == ["Extra Buttons", "Main Buttons", "Scroll", "Side Buttons"]
    assert "key_extra_14" in window._control_widgets


def _device():
    from keymasq.common.models import (
        AnalogAxisDefinition,
        AnalogInputDefinition,
        ButtonDefinition,
        DeviceType,
        EvdevDevice,
        HardwareConfig,
    )

    return HardwareConfig(
        vendor_id="1234",
        product_id="5678",
        name="Inspector Pad",
        evdev_devices=[
            EvdevDevice(
                path="/dev/input/event10",
                device_type=DeviceType.GAMEPAD,
                id="pad",
            )
        ],
        buttons=[
            ButtonDefinition(
                id="btn_south",
                label="A",
                evdev="btn_south",
                evdev_code=304,
                source="pad",
            )
        ],
        analog_inputs=[
            AnalogInputDefinition(
                id="left_stick",
                label="Left Stick",
                type="stick",
                source="pad",
                axes=[
                    AnalogAxisDefinition(
                        role="x",
                        evdev="abs_x",
                        evdev_code=0,
                        minimum=-32768,
                        maximum=32767,
                        center=0,
                    ),
                    AnalogAxisDefinition(
                        role="y",
                        evdev="abs_y",
                        evdev_code=1,
                        minimum=-32768,
                        maximum=32767,
                        center=0,
                    ),
                ],
            ),
            AnalogInputDefinition(
                id="left_trigger",
                label="Left Trigger",
                type="axis",
                source="pad",
                axes=[
                    AnalogAxisDefinition(
                        role="x",
                        evdev="abs_z",
                        evdev_code=2,
                        minimum=0,
                        maximum=255,
                    ),
                ],
            ),
        ],
    )


def _snapshot():
    return {
        "status": "ok",
        "hardware_id": "1234:5678",
        "device_name": "Inspector Pad",
        "model_id": "1234:5678",
        "active": True,
        "suppressed": False,
        "active_profiles": ["Desktop"],
        "interfaces": [{"id": "pad", "path": "/dev/input/event10", "type": "gamepad"}],
        "buttons": [
            {
                "id": "btn_south",
                "label": "A",
                "kind": "button",
                "evdev": "btn_south",
                "evdev_code": 304,
                "source": "pad",
                "profile_name": "Desktop",
                "action": {"action": "keyboard", "target": "key_space"},
            }
        ],
        "analog_inputs": [
            {
                "id": "left_stick",
                "label": "Left Stick",
                "kind": "analog",
                "type": "stick",
                "source": "pad",
                "profile_name": "",
                "action": None,
                "axes": [
                    {
                        "role": "x",
                        "evdev": "abs_x",
                        "evdev_code": 0,
                        "minimum": -32768,
                        "maximum": 32767,
                        "center": 0,
                    },
                    {
                        "role": "y",
                        "evdev": "abs_y",
                        "evdev_code": 1,
                        "minimum": -32768,
                        "maximum": 32767,
                        "center": 0,
                    },
                ],
            },
            {
                "id": "left_trigger",
                "label": "Left Trigger",
                "kind": "analog",
                "type": "axis",
                "source": "pad",
                "profile_name": "",
                "action": None,
                "axes": [
                    {
                        "role": "x",
                        "evdev": "abs_z",
                        "evdev_code": 2,
                        "minimum": 0,
                        "maximum": 255,
                    },
                ],
            },
        ],
    }


def test_device_inspector_window_starts_renders_events_and_toggles_suppression(
    monkeypatch,
):
    from gi.repository import Gdk, Gtk

    from keymasq.gui.widgets import device_inspector_window as inspector_module
    from keymasq.gui.widgets.device_inspector_window import DeviceInspectorWindow

    callbacks = {}
    requests = []

    def fake_register(event, callback):
        callbacks.setdefault(event, []).append(callback)

    def fake_unregister(event, callback):
        registered = callbacks.get(event, [])
        if callback in registered:
            registered.remove(callback)

    def fake_request_async(payload, callback, timeout=5.0):
        requests.append(dict(payload))
        command = payload.get("command")
        if command == "start_device_inspector":
            callback(_snapshot())
        elif command == "enable_device_inspector_suppression":
            callback(
                {
                    "status": "ok",
                    "hardware_id": "1234:5678",
                    "active": True,
                    "suppressed": True,
                }
            )
        elif command == "disable_device_inspector_suppression":
            callback(
                {
                    "status": "ok",
                    "hardware_id": "1234:5678",
                    "active": True,
                    "suppressed": False,
                }
            )
        else:
            callback({"status": "ok"})

    monkeypatch.setattr(inspector_module, "register_session_event_callback", fake_register)
    monkeypatch.setattr(inspector_module, "unregister_session_event_callback", fake_unregister)
    monkeypatch.setattr(inspector_module, "session_request_async", fake_request_async)

    window = DeviceInspectorWindow(Gtk.Window(), _device())

    assert requests[0]["command"] == "start_device_inspector"
    assert "btn_south" in window._control_widgets
    assert "left_stick" in window._analog_viewers
    assert "left_trigger" in window._analog_viewers
    assert window._header_device_icon.get_pixel_size() == 24
    assert window._paned.get_position() == 510
    assert window._paned.get_resize_start_child() is True
    assert window._paned.get_resize_end_child() is False
    trigger_viewer = window._analog_viewers["left_trigger"]
    trigger_level_bar = trigger_viewer.level_bar
    assert trigger_level_bar is not None
    assert trigger_level_bar.get_value() == pytest.approx(0.0)
    assert trigger_viewer.value_labels["x"].get_text() == "x: raw      0 | norm +0.000"
    assert window._status_label.get_text() == "Inspector Pad - Monitoring"
    assert window._status_label.get_halign() == Gtk.Align.START
    assert window._status_label.get_hexpand() is True
    assert window._header_title_spacer.get_parent() is not None
    assert window._status_label.has_css_class("inspector-header-title") is True
    assert window._status_label.has_css_class("inspector-header-monitoring") is True
    assert window._status_label.has_css_class("dim-label") is False
    assert window._axes_title.get_visible() is True
    assert window._axes_box.get_visible() is True
    window._render_axes({"analog_inputs": []})
    assert window._axes_title.get_visible() is False
    assert window._axes_box.get_visible() is False
    assert window._axes_box.get_first_child() is None
    window._render_axes(_snapshot())
    assert window._axes_title.get_visible() is True
    assert window._axes_box.get_visible() is True
    trigger_viewer = window._analog_viewers["left_trigger"]
    trigger_level_bar = trigger_viewer.level_bar
    assert trigger_level_bar is not None
    assert (
        window._action_label(
            {"action": "compositor_dispatch", "dispatcher": "workspace", "args": "1"},
            {},
        )
        == "🪟 workspace 1"
    )
    assert window._action_label({"action": "exec", "cmd": "grimblast copy area"}, {}) == (
        "▶ grimblast copy area"
    )

    def emit(event):
        callbacks["device_inspector_event"][0](
            {
                "event": "device_inspector_event",
                "hardware_id": "1234:5678",
                **event,
            }
        )

    emit(
        {
            "sequence": 1,
            "type_name": "ev_key",
            "code_name": "btn_south",
            "control_id": "btn_south",
            "value": 1,
            "source": "pad",
        }
    )

    assert len(window._event_rows) == 1
    assert len(window._event_history_by_category["button"]) == 1
    assert window._copy_events_button.get_tooltip_text() == "Copy visible events"
    assert window._visible_event_export_text() == ("#1 btn_south ev_key value=1 source=pad")

    for index in range(120):
        emit(
            {
                "sequence": index + 2,
                "type_name": "ev_rel",
                "code_name": "rel_x",
                "value": 4,
                "source": "pad",
            }
        )

    assert len(window._event_history_by_category["mousemove"]) == 100
    assert len(window._event_history_by_category["button"]) == 1
    assert len(window._event_rows) == 1

    emit(
        {
            "sequence": 122,
            "type_name": "ev_abs",
            "code_name": "abs_x",
            "analog_id": "left_stick",
            "analog_role": "x",
            "value": 14000,
            "source": "pad",
        }
    )

    assert len(window._event_history_by_category["axis"]) == 1
    assert len(window._event_rows) == 1
    assert window._analog_viewers["left_stick"].value_labels["x"].get_text() == (
        "x: raw  14000 | norm +0.427"
    )

    emit(
        {
            "sequence": 123,
            "type_name": "ev_msc",
            "code_name": "msc_scan",
            "value": 458792,
            "source": "pad",
        }
    )

    assert len(window._event_history_by_category["syn"]) == 1
    assert len(window._event_rows) == 1

    window._update_analog_value("left_trigger", "x", 128)
    assert trigger_viewer.value_labels["x"].get_text() == "x: raw    128 | norm +0.502"
    assert trigger_level_bar.get_value() == pytest.approx(128 / 255)

    window._event_filter_buttons["axis"].set_active(True)
    assert len(window._event_rows) == 2

    window._event_filter_buttons["mousemove"].set_active(True)
    assert len(window._event_rows) == 100

    window._event_filter_buttons["syn"].set_active(True)
    assert len(window._event_rows) == 100
    assert "#123 msc_scan ev_msc value=458792 source=pad" in (window._visible_event_export_text())

    emit(
        {
            "sequence": 124,
            "type_name": "ev_rel",
            "code_name": "rel_x",
            "value": 6,
            "source": "pad",
        }
    )

    assert window._event_render_source_id

    for index in range(105):
        emit(
            {
                "sequence": index + 125,
                "type_name": "ev_key",
                "code_name": "btn_south",
                "control_id": "btn_south",
                "value": 1,
                "source": "pad",
            }
        )

    assert len(window._event_history_by_category["button"]) == 100
    assert len(window._event_history_by_category["mousemove"]) == 100
    assert len(window._event_rows) == 100

    window._suppression_switch.set_active(True)
    assert requests[-1]["command"] == "enable_device_inspector_suppression"
    assert window._suppression_switch.get_active() is True
    assert window._status_label.get_text() == "Inspector Pad - Output suppressed"
    assert window._status_label.has_css_class("inspector-header-suppressed") is True
    assert window._suppression_hint_label.get_visible() is True
    assert window._on_key_pressed(Gtk.EventControllerKey(), Gdk.KEY_Escape, 0, 0) is True
    assert requests[-1] == {
        "command": "disable_device_inspector_suppression",
        "hardware_id": "1234:5678",
        "reason": "key_esc",
    }
    assert window._suppression_switch.get_active() is False

    window._suppression_switch.set_active(True)

    callbacks["device_inspector_status"][0](
        {
            "event": "device_inspector_status",
            "hardware_id": "1234:5678",
            "active": True,
            "suppressed": False,
            "reason": "key_esc",
        }
    )

    assert window._suppression_switch.get_active() is False
    assert window._status_label.get_text() == "Inspector Pad - Monitoring"
    assert window._status_label.has_css_class("inspector-header-monitoring") is True
    assert window._suppression_hint_label.get_visible() is False
    assert window._on_close_request() is False
    assert requests[-1]["command"] == "stop_device_inspector"
    assert callbacks["device_inspector_event"] == []
    window._on_destroy()
