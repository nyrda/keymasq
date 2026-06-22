# ruff: noqa: I001
from collections.abc import Callable
from types import SimpleNamespace
from typing import cast

import pytest

gi = pytest.importorskip("gi")


def _make_learn_analog_flow(device, on_complete=None, parent=None):
    from keymasq.gui.widgets.device_tab.learn_analog_flow import LearnAnalogFlow

    if parent is None:
        parent = SimpleNamespace(
            _recording_unlock_required=False,
            _recording_unlocked=False,
            _recording_refresh_owner=False,
        )
    return LearnAnalogFlow(
        parent,
        lambda _payload, callback: callback({"status": "ok"}),
        device,
        on_complete or (lambda _result: None),
    )


def test_notify_session_reload_returns_false_without_shell_fallback(monkeypatch):
    from keymasq.gui import session_reload

    monkeypatch.setattr(session_reload, "session_request", lambda payload, timeout=5.0: None)

    assert session_reload.notify_session_reload(timeout=0.1) is False


class _SavingHardwareManager:
    def __init__(self) -> None:
        self.saved = []

    def save_hardware(self, device) -> None:
        self.saved.append(device)


def _build_analog_learning_flow(monkeypatch, interface_id="joystick"):
    from keymasq.common.models import DeviceType, EvdevDevice, HardwareConfig
    import keymasq.gui.widgets.device_tab as device_tab_module
    from keymasq.gui.widgets.device_tab import DeviceTab

    monkeypatch.setattr(
        device_tab_module,
        "session_request_async",
        lambda _payload, callback: callback({"status": "ok"}),
    )

    device = HardwareConfig(
        vendor_id="1234",
        product_id="5678",
        name="Pad",
        evdev_devices=[
            EvdevDevice(
                path="/dev/input/event0",
                device_type=DeviceType.GAMEPAD,
                id=interface_id,
            )
        ],
        buttons=[],
    )
    hardware_manager = _SavingHardwareManager()
    tab = DeviceTab(
        device=device,
        profile_manager=None,
        hardware_manager=hardware_manager,
        demo_mode=True,
    )
    monkeypatch.setattr(tab, "_reload_ui", lambda: None)
    flow = _make_learn_analog_flow(device, tab._on_learn_analog_complete)
    flow._context = {"candidates": {}}
    return flow, hardware_manager


def _record_analog_candidates(flow, candidates):
    for evdev_name, code, value in candidates:
        flow.record_candidate(
            {
                "evdev": evdev_name,
                "code": code,
                "value": value,
                "source": "joystick",
                "stable_path": "/dev/input/event0",
            }
        )


def _populate_analog_review(flow, selected_type=0):
    from gi.repository import Gtk

    type_dropdown = Gtk.DropDown.new_from_strings(["Generic Axis", "Stick"])
    type_dropdown.set_selected(selected_type)
    review_list = Gtk.ListBox()
    status = Gtk.Label()
    save_btn = Gtk.Button()
    flow.populate_review(type_dropdown, review_list, status, save_btn)
    return SimpleNamespace(
        type_dropdown=type_dropdown,
        review_list=review_list,
        status=status,
        save_btn=save_btn,
    )


def _save_learned_analog(flow, review, analog_id, label):
    from gi.repository import Adw, Gtk

    id_entry = Gtk.Entry()
    id_entry.set_text(analog_id)
    label_entry = Gtk.Entry()
    label_entry.set_text(label)
    flow._on_save_clicked(
        Gtk.Button(),
        Adw.Dialog(),
        review.type_dropdown,
        id_entry,
        label_entry,
        review.review_list,
        review.status,
    )


def test_device_tab_learn_analog_axis_saves_highest_movement(monkeypatch, temp_config_dir):
    flow, hardware_manager = _build_analog_learning_flow(monkeypatch)
    _record_analog_candidates(
        flow,
        (
            ("abs_x", 0, 100),
            ("abs_x", 0, -32000),
            ("abs_y", 1, 4000),
        ),
    )

    review = _populate_analog_review(flow)

    assert review.save_btn.get_sensitive() is True
    assert review.review_list.get_row_at_index(0)._analog_evdev == "abs_x"

    _save_learned_analog(flow, review, "left_trigger", "Left Trigger")

    saved = hardware_manager.saved[-1]
    analog = saved.analog_inputs[0]
    assert analog.id == "left_trigger"
    assert analog.type == "axis"
    assert analog.axes[0].evdev == "abs_x"
    assert analog.axes[0].minimum == -32000
    assert analog.axes[0].maximum == 100
    assert analog.axes[0].rest == 100
    assert analog.axes[0].invert is False


def test_device_tab_learn_analog_assigns_source_to_existing_interface(
    monkeypatch,
    temp_config_dir,
):
    flow, hardware_manager = _build_analog_learning_flow(
        monkeypatch,
        interface_id=None,
    )
    _record_analog_candidates(
        flow,
        (
            ("abs_z", 2, 100),
            ("abs_z", 2, 255),
        ),
    )

    review = _populate_analog_review(flow)
    _save_learned_analog(flow, review, "left_trigger", "Left Trigger")

    assert hardware_manager.saved[-1].evdev_devices[0].id == "joystick"


def test_device_tab_learn_analog_stick_allows_role_swap(monkeypatch, temp_config_dir):
    flow, hardware_manager = _build_analog_learning_flow(monkeypatch)
    _record_analog_candidates(
        flow,
        (
            ("abs_rx", 3, 0),
            ("abs_rx", 3, 20000),
            ("abs_ry", 4, 0),
            ("abs_ry", 4, 30000),
        ),
    )

    review = _populate_analog_review(flow, selected_type=1)

    first_row = review.review_list.get_row_at_index(0)
    second_row = review.review_list.get_row_at_index(1)
    assert first_row is not None
    assert second_row is not None
    assert first_row.get_selectable() is False
    assert first_row.get_activatable() is False
    assert first_row._analog_evdev == "abs_ry"
    assert first_row._analog_role_dropdown.get_selected() == 1
    assert first_row._analog_rest_spin.get_value() == 0
    assert second_row._analog_evdev == "abs_rx"
    assert second_row._analog_role_dropdown.get_selected() == 0
    assert second_row._analog_rest_spin.get_value() == 0
    first_row._analog_role_dropdown.set_selected(0)
    second_row._analog_role_dropdown.set_selected(1)

    _save_learned_analog(flow, review, "right_stick", "Right Stick")

    saved = hardware_manager.saved[-1]
    analog = saved.analog_inputs[0]
    assert [(axis.evdev, axis.role, axis.center) for axis in analog.axes] == [
        ("abs_ry", "x", 0),
        ("abs_rx", "y", 0),
    ]


def test_device_tab_learn_analog_stick_defaults_center_to_zero(
    temp_config_dir,
):
    import gi

    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.common.models import DeviceType, EvdevDevice, HardwareConfig

    device = HardwareConfig(
        vendor_id="1234",
        product_id="5678",
        name="Pad",
        evdev_devices=[
            EvdevDevice(
                path="/dev/input/event0",
                device_type=DeviceType.GAMEPAD,
                id="joystick",
            )
        ],
        buttons=[],
    )
    flow = _make_learn_analog_flow(device)
    flow._context = {"candidates": {}}
    for evdev_name, code, value in (
        ("abs_x", 0, 0),
        ("abs_x", 0, 255),
        ("abs_y", 1, 0),
        ("abs_y", 1, 255),
    ):
        flow.record_candidate(
            {
                "evdev": evdev_name,
                "code": code,
                "value": value,
                "source": "joystick",
                "stable_path": "/dev/input/event0",
                "absinfo": {"minimum": 0, "maximum": 255},
            }
        )

    type_dropdown = Gtk.DropDown.new_from_strings(["Generic Axis", "Stick"])
    type_dropdown.set_selected(1)
    review_list = Gtk.ListBox()
    status = Gtk.Label()
    save_btn = Gtk.Button()
    flow.populate_review(type_dropdown, review_list, status, save_btn)

    assert review_list.get_row_at_index(0)._analog_rest_spin.get_value() == 0
    assert review_list.get_row_at_index(1)._analog_rest_spin.get_value() == 0


def test_capture_status_row_shows_recording_dot(temp_config_dir):
    import gi

    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.gui.widgets.device_tab import (
        _make_capture_status_row,
        _set_capture_status,
    )

    status = Gtk.Label()
    row = _make_capture_status_row(status)
    dot = row.get_first_child()

    assert dot is not None
    assert dot.get_visible() is False

    _set_capture_status(status, "Recording button presses...", recording=True)

    assert status.get_text() == "Recording button presses..."
    assert dot.get_visible() is True


def test_device_tab_learn_analog_stick_guesses_hat_axis_roles(temp_config_dir):
    from gi.repository import Gtk

    from keymasq.common.models import DeviceType, EvdevDevice, HardwareConfig

    device = HardwareConfig(
        vendor_id="1234",
        product_id="5678",
        name="Pad",
        evdev_devices=[
            EvdevDevice(
                path="/dev/input/event0",
                device_type=DeviceType.GAMEPAD,
                id="joystick",
            )
        ],
        buttons=[],
    )
    flow = _make_learn_analog_flow(device)
    flow._context = {
        "candidates": {
            "joystick:17": {
                "evdev": "abs_hat0y",
                "code": 17,
                "source": "joystick",
                "stable_path": "/dev/input/event0",
                "rest": 0,
                "minimum": -1,
                "maximum": 1,
                "observed_minimum": -1,
                "observed_maximum": 1,
                "count": 2,
            },
            "joystick:16": {
                "evdev": "abs_hat0x",
                "code": 16,
                "source": "joystick",
                "stable_path": "/dev/input/event0",
                "rest": 0,
                "minimum": -1,
                "maximum": 1,
                "observed_minimum": -1,
                "observed_maximum": 1,
                "count": 2,
            },
        }
    }

    type_dropdown = Gtk.DropDown.new_from_strings(["Generic Axis", "Stick"])
    type_dropdown.set_selected(1)
    review_list = Gtk.ListBox()
    status = Gtk.Label()
    save_btn = Gtk.Button()
    flow.populate_review(type_dropdown, review_list, status, save_btn)

    first_row = review_list.get_row_at_index(0)
    second_row = review_list.get_row_at_index(1)
    assert first_row is not None
    assert second_row is not None
    assert first_row._analog_evdev == "abs_hat0y"
    assert first_row._analog_role_dropdown.get_selected() == 1
    assert second_row._analog_evdev == "abs_hat0x"
    assert second_row._analog_role_dropdown.get_selected() == 0


def test_resolve_keymasq_record_helper_path(tmp_path, monkeypatch):
    from keymasq.common import paths

    helper = tmp_path / "keymasq-record"
    helper.write_text("#!/bin/sh\n")
    helper.chmod(0o755)
    monkeypatch.setattr(paths, "KEYMASQ_RECORD_HELPER_PATH", helper)

    assert paths.resolve_keymasq_record_helper_path() == str(helper)


def test_run_gui_task_calls_callback_and_on_done_when_worker_raises(monkeypatch):
    import threading

    from gi.repository import GLib

    from keymasq.gui import session_client

    callback_results: list[object] = []
    done = threading.Event()

    monkeypatch.setattr(GLib, "idle_add", lambda callback, *args: callback(*args))

    session_client.run_gui_task(
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
        lambda result: callback_results.append(result) or False,
        on_done=done.set,
    )

    assert done.wait(1.0) is True
    assert len(callback_results) == 1
    assert isinstance(callback_results[0], session_client.GuiTaskResult)
    assert callback_results[0].ok is False
    assert isinstance(callback_results[0].error, RuntimeError)


def test_persistent_session_connection_clears_partial_buffer_on_disconnect_and_reconnect():
    import queue

    from keymasq.gui.session_client import _PersistentSessionConnection

    class _FakeSocket:
        def __init__(self, chunks: list[bytes]) -> None:
            self._chunks = list(chunks)

        def recv(self, _size: int) -> bytes:
            if self._chunks:
                return self._chunks.pop(0)
            return b""

        def close(self) -> None:
            return

    connection = _PersistentSessionConnection()
    first_queue: queue.Queue[dict | None] = queue.Queue(maxsize=1)
    connection._sock = _FakeSocket([b'{"status":"ok"'])
    connection._response_queue = first_queue
    connection._reader_loop()

    assert connection._buffer == b""
    assert first_queue.get_nowait() is None

    second_queue: queue.Queue[dict | None] = queue.Queue(maxsize=1)
    connection._sock = _FakeSocket([b'{"status":"ok","value":1}\n'])
    connection._response_queue = second_queue
    connection._reader_loop()

    assert second_queue.get_nowait() == {"status": "ok", "value": 1}


def test_device_tab_builds_captured_window_rules():
    from keymasq.common.models import ButtonDefinition, HardwareConfig
    from keymasq.gui.widgets.device_tab import DeviceTab

    device = HardwareConfig(
        vendor_id="1234",
        product_id="5678",
        name="Test Mouse",
        evdev_devices=[],
        buttons=[ButtonDefinition(id="btn_back", label="Back", evdev="btn_side")],
    )

    tab = DeviceTab(
        device=device,
        profile_manager=None,
        demo_mode=True,
        compositor_capabilities=["window_tags"],
    )

    rules = tab._build_captured_window_rules(
        {
            "class": "steam.desktop",
            "title": "Counter-Strike 2 (DX11)",
            "tags": ["discord*", "fullscreen"],
        }
    )

    assert [(rule.field, rule.pattern) for rule in rules] == [
        ("class", "steam\\.desktop"),
        ("title", "Counter\\-Strike\\ 2\\ \\(DX11\\)"),
        ("tag", "discord"),
    ]


def test_device_tab_delete_button_visibility_depends_on_rule_count():
    from keymasq.common.models import ButtonDefinition, HardwareConfig, WindowRule
    from keymasq.gui.widgets.device_tab import DeviceTab

    device = HardwareConfig(
        vendor_id="1234",
        product_id="5678",
        name="Test Mouse",
        evdev_devices=[],
        buttons=[ButtonDefinition(id="btn_back", label="Back", evdev="btn_side")],
    )

    tab = DeviceTab(
        device=device,
        profile_manager=None,
        demo_mode=True,
    )

    row_one = tab._create_rule_row(WindowRule(field="class", pattern="one"), is_first=True)
    row_two = tab._create_rule_row(WindowRule(field="title", pattern="two"))
    tab._rule_rows = [row_one, row_two]

    tab._update_first_rule_delete_button()

    assert row_one._delete_btn.get_visible() is True
    assert row_two._delete_btn.get_visible() is True

    tab._rule_rows = [row_one]
    tab._update_first_rule_delete_button()

    assert row_one._delete_btn.get_visible() is False


def test_device_tab_refresh_profiles_does_not_save_on_programmatic_settings_update(temp_config_dir):
    from keymasq.common.models import (
        ButtonDefinition,
        DeviceProfileLayer,
        HardwareConfig,
        ProfileConfig,
    )
    from keymasq.gui.widgets.device_tab import DeviceTab
    from keymasq.session.profiles import ProfileManager

    profile_manager = ProfileManager()
    profile_manager.save_profile(
        ProfileConfig(
            name="Permanent",
            enabled=True,
            is_permanent=True,
            priority=5,
            device_layers={"1234:5678": DeviceProfileLayer(hardware_id="1234:5678")},
        )
    )
    profile_manager.save_profile(
        ProfileConfig(
            name="Conditional",
            enabled=True,
            is_permanent=False,
            priority=1,
            device_layers={"1234:5678": DeviceProfileLayer(hardware_id="1234:5678")},
        )
    )

    device = HardwareConfig(
        vendor_id="1234",
        product_id="5678",
        name="Test Mouse",
        evdev_devices=[],
        buttons=[ButtonDefinition(id="btn_back", label="Back", evdev="btn_side")],
    )

    tab = DeviceTab(
        device=device,
        profile_manager=profile_manager,
        demo_mode=True,
    )
    save_calls = []
    tab._save_profile = lambda: save_calls.append(True) or True

    tab.refresh_profiles(preferred_profile_name="Permanent", publish_selection=False)
    tab.refresh_profiles(preferred_profile_name="Conditional", publish_selection=False)

    assert save_calls == []


def test_device_tab_legacy_passthrough_is_shown_as_active_mask(temp_config_dir):
    from keymasq.common.models import (
        ActionType,
        ButtonDefinition,
        DeviceProfileLayer,
        HardwareConfig,
        MappingAction,
        ProfileConfig,
    )
    from keymasq.gui.widgets.device_tab import DeviceTab
    from keymasq.session.profiles import ProfileManager

    profile_manager = ProfileManager()
    profile_manager.save_profile(
        ProfileConfig(
            name="Base",
            enabled=True,
            is_permanent=True,
            priority=1,
            device_layers={
                "1234:5678": DeviceProfileLayer(
                    hardware_id="1234:5678",
                    mappings={
                        "btn_back": MappingAction(action_type=ActionType.KEYBOARD, target="key_1")
                    },
                )
            },
        )
    )
    profile_manager.save_profile(
        ProfileConfig(
            name="Mask",
            enabled=True,
            is_permanent=True,
            priority=2,
            device_layers={
                "1234:5678": DeviceProfileLayer(
                    hardware_id="1234:5678",
                    mappings={"btn_back": MappingAction(action_type=ActionType.PASSTHROUGH)},
                )
            },
        )
    )

    device = HardwareConfig(
        vendor_id="1234",
        product_id="5678",
        name="Test Mouse",
        evdev_devices=[],
        buttons=[ButtonDefinition(id="btn_back", label="Back", evdev="btn_side")],
    )

    tab = DeviceTab(
        device=device,
        profile_manager=profile_manager,
        demo_mode=True,
    )
    tab._active_profile_names = ["Base", "Mask"]

    tab.refresh_profiles(preferred_profile_name="Mask")
    tab._update_button_display("btn_back")

    widget = tab._button_widgets["btn_back"]
    assert widget._action_label.get_text() == "→ Back"
    assert widget.has_css_class("button-card-mapped-active") is True


def test_device_tab_does_not_auto_switch_to_active_profile(temp_config_dir):
    from keymasq.common.models import (
        ButtonDefinition,
        DeviceProfileLayer,
        HardwareConfig,
        ProfileConfig,
    )
    from keymasq.gui.widgets.device_tab import DeviceTab
    from keymasq.session.profiles import ProfileManager

    profile_manager = ProfileManager()
    profile_manager.save_profile(
        ProfileConfig(
            name="Desktop",
            enabled=True,
            is_permanent=True,
            device_layers={"1234:5678": DeviceProfileLayer(hardware_id="1234:5678")},
        )
    )
    profile_manager.save_profile(
        ProfileConfig(
            name="Gaming",
            enabled=True,
            is_permanent=True,
            device_layers={"1234:5678": DeviceProfileLayer(hardware_id="1234:5678")},
        )
    )

    device = HardwareConfig(
        vendor_id="1234",
        product_id="5678",
        name="Test Mouse",
        evdev_devices=[],
        buttons=[ButtonDefinition(id="btn_back", label="Back", evdev="btn_side")],
    )

    tab = DeviceTab(
        device=device,
        profile_manager=profile_manager,
        demo_mode=True,
    )
    tab.refresh_profiles(preferred_profile_name="Desktop", publish_selection=False)

    assert tab._selected_profile is not None
    assert tab._selected_profile.config.name == "Desktop"

    tab._on_active_profile_response({"devices": {"1234:5678": {"profiles": ["Gaming"]}}})

    assert tab._active_profile_names == ["Gaming"]
    assert tab._selected_profile is not None
    assert tab._selected_profile.config.name == "Desktop"


def test_window_rules_dialog_applies_to_profile_it_was_opened_for(temp_config_dir):
    from keymasq.common.models import (
        ButtonDefinition,
        DeviceProfileLayer,
        HardwareConfig,
        ProfileConfig,
    )
    from keymasq.gui.widgets.device_tab import DeviceTab
    from keymasq.session.profiles import ProfileManager

    profile_manager = ProfileManager()
    profile_manager.save_profile(
        ProfileConfig(
            name="Desktop",
            enabled=True,
            is_permanent=False,
            device_layers={"1234:5678": DeviceProfileLayer(hardware_id="1234:5678")},
        )
    )
    profile_manager.save_profile(
        ProfileConfig(
            name="Gaming",
            enabled=True,
            is_permanent=False,
            device_layers={"1234:5678": DeviceProfileLayer(hardware_id="1234:5678")},
        )
    )

    device = HardwareConfig(
        vendor_id="1234",
        product_id="5678",
        name="Test Mouse",
        evdev_devices=[],
        buttons=[ButtonDefinition(id="btn_back", label="Back", evdev="btn_side")],
    )

    tab = DeviceTab(
        device=device,
        profile_manager=profile_manager,
        demo_mode=True,
    )
    tab.refresh_profiles(preferred_profile_name="Desktop", publish_selection=False)
    tab._show_window_rules_dialog()
    tab._set_window_rule_rows([])
    tab._on_add_window_rule(None)

    rule_row = next(row for row in tab._rule_rows if hasattr(row, "_is_rule_row"))
    rule_row._field_dropdown.set_selected(0)
    rule_row._pattern_entry.set_text("steam")

    tab.refresh_profiles(preferred_profile_name="Gaming", publish_selection=False)
    tab._on_apply_window_rules(None)

    desktop = profile_manager.get_profile("Desktop")
    gaming = profile_manager.get_profile("Gaming")

    assert desktop is not None
    assert gaming is not None
    assert [(rule.field, rule.pattern) for rule in desktop.config.window_rules] == [
        ("class", "steam")
    ]
    assert desktop.config.is_permanent is False
    assert gaming.config.window_rules == []

    tab.refresh_profiles(preferred_profile_name="Desktop", publish_selection=False)
    tab._show_window_rules_dialog()
    tab._set_window_rule_rows([])
    tab._on_apply_window_rules(None)

    desktop = profile_manager.get_profile("Desktop")
    assert desktop is not None
    assert desktop.config.window_rules == []
    assert desktop.config.is_permanent is True


def test_window_rules_remove_button_tracks_captured_rules(temp_config_dir):
    from keymasq.common.models import ButtonDefinition, HardwareConfig, ProfileConfig
    from keymasq.gui.widgets.device_tab import DeviceTab
    from keymasq.session.profiles import ProfileManager

    profile_manager = ProfileManager()
    profile_manager.save_profile(ProfileConfig(name="Desktop", enabled=True, is_permanent=True))
    device = HardwareConfig(
        vendor_id="1234",
        product_id="5678",
        name="Test Mouse",
        evdev_devices=[],
        buttons=[ButtonDefinition(id="btn_back", label="Back", evdev="btn_side")],
    )
    tab = DeviceTab(device=device, profile_manager=profile_manager, demo_mode=True)
    tab.refresh_profiles(preferred_profile_name="Desktop", publish_selection=False)
    tab._show_window_rules_dialog()

    assert tab._remove_window_rules_btn.get_sensitive() is False

    tab._set_window_rule_rows(tab._build_captured_window_rules({"class": "Steam"}))

    assert tab._remove_window_rules_btn.get_sensitive() is True


def test_describe_mapping_action_compact_includes_runtime_markers():
    from keymasq.common.models import ActionType, MappingAction
    from keymasq.gui.widgets.action_labels import describe_mapping_action_compact

    action = MappingAction(
        action_type=ActionType.KEYBOARD,
        target="key_a",
        rapidfire_enabled=True,
        tap_enabled=True,
    )

    assert describe_mapping_action_compact(action, include_state=True) == "→ key_a ⚡ ↓"


def test_device_tab_uses_pango_ellipsizing_for_long_action_summary(temp_config_dir):
    from keymasq.common.models import (
        ActionType,
        ButtonDefinition,
        DeviceProfileLayer,
        HardwareConfig,
        MappingAction,
        ProfileConfig,
    )
    from keymasq.gui.widgets.device_tab import DeviceTab
    from keymasq.session.profiles import ProfileManager

    profile_manager = ProfileManager()
    profile_manager.save_profile(
        ProfileConfig(
            name="Desktop",
            enabled=True,
            is_permanent=True,
            device_layers={
                "1234:5678": DeviceProfileLayer(
                    hardware_id="1234:5678",
                    mappings={
                        "btn_extra": MappingAction(
                            action_type=ActionType.EXEC,
                            cmd="grimblast --freeze --notify copy area",
                        )
                    },
                )
            },
        )
    )
    device = HardwareConfig(
        vendor_id="1234",
        product_id="5678",
        name="Test Mouse",
        evdev_devices=[],
        buttons=[ButtonDefinition(id="btn_extra", label="Extra Button 10", evdev="btn_extra")],
    )

    tab = DeviceTab(device=device, profile_manager=profile_manager, demo_mode=True)
    tab._update_button_display("btn_extra")

    widget = tab._button_widgets["btn_extra"]
    assert widget.get_size_request()[0] == 187
    assert widget._action_label.get_ellipsize().value_name == "PANGO_ELLIPSIZE_MIDDLE"
    assert widget._action_label.get_hexpand() is True
    assert widget._action_label.get_text() == "▶ grimblast copy area"
    assert widget._action_label.get_tooltip_text() == (
        "▶ grimblast --freeze --notify copy area"
    )


def test_device_tab_renders_analog_controls_for_keyboard_layout(temp_config_dir):
    from keymasq.common.models import (
        AnalogAxisDefinition,
        AnalogInputDefinition,
        ButtonDefinition,
        DeviceType,
        EvdevDevice,
        HardwareConfig,
    )
    from keymasq.gui.widgets.device_tab import DeviceTab

    device = HardwareConfig(
        vendor_id="1234",
        product_id="5678",
        name="Hybrid Keyboard",
        evdev_devices=[
            EvdevDevice(
                path="/dev/input/event0",
                device_type=DeviceType.KEYBOARD,
                id="keys",
            ),
            EvdevDevice(
                path="/dev/input/event1",
                device_type=DeviceType.GAMEPAD,
                id="analog",
            ),
        ],
        buttons=[
            ButtonDefinition(id=f"key_custom_{index}", label=f"Key {index}", evdev="key_a")
            for index in range(40)
        ],
        analog_inputs=[
            AnalogInputDefinition(
                id="left_stick",
                label="Left Stick",
                type="stick",
                source="analog",
                axes=[
                    AnalogAxisDefinition(role="x", evdev="abs_x", evdev_code=0),
                    AnalogAxisDefinition(role="y", evdev="abs_y", evdev_code=1),
                ],
            )
        ],
    )

    tab = DeviceTab(device=device, profile_manager=None, demo_mode=True)

    assert tab.device_layout_kind() == "keyboard"
    assert "left_stick" in tab._button_widgets
    assert tab._button_widgets["left_stick"]._name_label.get_text() == "Left Stick"


def test_key_selector_dialog_passthrough_clears_current_profile_mapping():
    from gi.repository import Gtk

    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

    def collect_buttons(widget):
        buttons = []
        child = widget.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.Button):
                buttons.append(child)
            buttons.extend(collect_buttons(child))
            child = child.get_next_sibling()
        return buttons

    results = []
    dialog = KeySelectorDialog(Gtk.Box(), "Back")
    dialog.connect("key-selected", lambda _dialog, action: results.append(action))

    special_tab = dialog._build_special_tab()
    buttons_by_label = {button.get_label(): button for button in collect_buttons(special_tab)}

    assert "Passthrough" in buttons_by_label
    assert "No Override" not in buttons_by_label

    buttons_by_label["Passthrough"].emit("clicked")
    assert results == [None]


def test_key_selector_type_tab_creates_macro_and_maps_it(monkeypatch):
    from gi.repository import Gtk

    from keymasq.common.models import ActionType
    from keymasq.gui.widgets.key_selector import type_tab as type_tab_module
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

    requests: list[dict[str, object]] = []

    def fake_session_request_async(payload, callback, on_start=None, on_done=None):
        requests.append(payload)
        if on_start:
            on_start()
        macro = payload["macro"]
        callback({"status": "ok", "macro": {"name": macro["name"]}})
        if on_done:
            on_done()

    monkeypatch.setattr(
        type_tab_module,
        "session_request_async",
        fake_session_request_async,
    )

    results = []
    dialog = KeySelectorDialog(Gtk.Box(), "Back")
    dialog.connect("key-selected", lambda _dialog, action: results.append(action))
    dialog.stack.set_visible_child_name("type")
    dialog.type_text_view.get_buffer().set_text("Hi")
    dialog.type_down_spin.set_value(5)
    dialog.type_pause_spin.set_value(7)

    dialog._on_map_clicked(dialog.map_btn)

    assert len(requests) == 1
    assert requests[0]["command"] == "create_macro"
    macro = cast(dict[str, object], requests[0]["macro"])
    assert str(macro["name"]).startswith("type_text_")
    assert macro["type_binding"] is True
    assert macro["type_text"] == "Hi"
    assert macro["type_down_ms"] == 5
    assert macro["type_pause_ms"] == 7
    assert macro["type_use_unicode_input"] is False
    assert macro["events"]
    assert len(results) == 1
    assert results[0].action_type == ActionType.MACRO
    assert results[0].macro_name == macro["name"]
    assert results[0].macro_replay_mouse_movement is True
    assert results[0].macro_replay_mouse_clicks is True
    assert results[0].macro_speed == 1.0


def test_key_selector_type_tab_allows_whitespace_only_text(monkeypatch):
    from gi.repository import Gtk

    from keymasq.gui.widgets.key_selector import type_tab as type_tab_module
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

    requests: list[dict[str, object]] = []

    def fake_session_request_async(payload, callback, on_start=None, on_done=None):
        requests.append(payload)
        macro = payload["macro"]
        callback({"status": "ok", "macro": {"name": macro["name"]}})

    monkeypatch.setattr(
        type_tab_module,
        "session_request_async",
        fake_session_request_async,
    )

    dialog = KeySelectorDialog(Gtk.Box(), "Back")
    dialog.stack.set_visible_child_name("type")
    dialog.type_text_view.get_buffer().set_text(" ")

    assert dialog.map_btn.get_sensitive() is True
    dialog._on_map_clicked(dialog.map_btn)

    assert len(requests) == 1
    assert requests[0]["command"] == "create_macro"
    macro = cast(dict[str, object], requests[0]["macro"])
    assert macro["type_text"] == " "
    assert macro["type_down_ms"] == 5
    assert macro["type_pause_ms"] == 10


def test_key_selector_type_tab_loads_macro_details_only_when_opened(monkeypatch):
    from gi.repository import Gtk

    from keymasq.common.models import ActionType, MappingAction
    from keymasq.gui.widgets.key_selector import type_tab as type_tab_module
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

    requests: list[dict[str, object]] = []

    def fake_session_request_async(payload, callback, on_start=None, on_done=None):
        requests.append(payload)
        callback(
            {
                "status": "ok",
                "macro": {
                    "name": "typed",
                    "type_binding": True,
                    "type_text": "Hello",
                    "type_down_ms": 6,
                    "type_pause_ms": 8,
                },
            }
        )

    monkeypatch.setattr(
        type_tab_module,
        "session_request_async",
        fake_session_request_async,
    )

    dialog = KeySelectorDialog(
        Gtk.Box(),
        "Back",
        MappingAction(action_type=ActionType.MACRO, macro_name="typed"),
    )

    assert requests == []

    dialog.stack.set_visible_child_name("type")

    assert requests == [{"command": "get_macro", "name": "typed"}]
    assert dialog._type_buffer_text() == "Hello"
    assert int(dialog.type_down_spin.get_value()) == 6
    assert int(dialog.type_pause_spin.get_value()) == 8

    dialog.stack.set_visible_child_name("macro")
    dialog.stack.set_visible_child_name("type")

    assert requests == [{"command": "get_macro", "name": "typed"}]


def test_key_selector_type_tab_does_not_clobber_user_edits_after_async_load(monkeypatch):
    from gi.repository import Gtk

    from keymasq.common.models import ActionType, MappingAction
    from keymasq.gui.widgets.key_selector import type_tab as type_tab_module
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

    callbacks = []

    def fake_session_request_async(payload, callback, on_start=None, on_done=None):
        assert payload == {"command": "get_macro", "name": "typed"}
        callbacks.append(callback)

    monkeypatch.setattr(
        type_tab_module,
        "session_request_async",
        fake_session_request_async,
    )

    dialog = KeySelectorDialog(
        Gtk.Box(),
        "Back",
        MappingAction(action_type=ActionType.MACRO, macro_name="typed"),
    )
    dialog.stack.set_visible_child_name("type")
    dialog.type_text_view.get_buffer().set_text("User edit")
    dialog.type_down_spin.set_value(22)

    callbacks[0](
        {
            "status": "ok",
            "macro": {
                "name": "typed",
                "type_binding": True,
                "type_text": "Stored text",
                "type_down_ms": 6,
                "type_pause_ms": 8,
            },
        }
    )

    assert dialog._type_buffer_text() == "User edit"
    assert int(dialog.type_down_spin.get_value()) == 22


def test_key_selector_type_tab_preserves_macro_playback_options(monkeypatch):
    from gi.repository import Gtk

    from keymasq.common.models import ActionType, MappingAction
    from keymasq.gui.widgets.key_selector import type_tab as type_tab_module
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

    requests: list[dict[str, object]] = []
    results = []

    def fake_session_request_async(payload, callback, on_start=None, on_done=None):
        requests.append(payload)
        if payload["command"] == "get_macro":
            callback(
                {
                    "status": "ok",
                    "macro": {
                        "name": "typed",
                        "type_binding": True,
                        "type_text": "Old",
                    },
                }
            )
            return
        macro = payload["macro"]
        callback({"status": "ok", "macro": {"name": macro["name"]}})

    monkeypatch.setattr(
        type_tab_module,
        "session_request_async",
        fake_session_request_async,
    )

    dialog = KeySelectorDialog(
        Gtk.Box(),
        "Back",
        MappingAction(
            action_type=ActionType.MACRO,
            macro_name="typed",
            macro_replay_mouse_movement=False,
            macro_replay_mouse_clicks=False,
            macro_speed=1.75,
        ),
    )
    dialog.connect("key-selected", lambda _dialog, action: results.append(action))
    dialog.stack.set_visible_child_name("type")
    dialog.type_text_view.get_buffer().set_text("New")

    dialog._on_map_clicked(dialog.map_btn)

    assert [request["command"] for request in requests] == ["get_macro", "create_macro"]
    assert len(results) == 1
    assert results[0].action_type == ActionType.MACRO
    assert results[0].macro_replay_mouse_movement is False
    assert results[0].macro_replay_mouse_clicks is False
    assert results[0].macro_speed == 1.75


def test_key_selector_type_tab_resyncs_map_button_after_unicode_toggle(monkeypatch):
    from gi.repository import Gtk

    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

    dialog = KeySelectorDialog(Gtk.Box(), "Back")
    dialog.stack.set_visible_child_name("type")
    dialog.type_text_view.get_buffer().set_text("\u00ad")

    assert dialog.type_unicode_check.get_visible() is True
    assert dialog.type_unicode_check.get_active() is True
    assert dialog.map_btn.get_sensitive() is True

    dialog.type_unicode_check.set_active(False)

    assert dialog.map_btn.get_sensitive() is False


def test_analog_key_selector_default_tab_and_special_has_no_passthrough(temp_config_dir):
    from gi.repository import Gtk

    from keymasq.common.models import AnalogControlConfig
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog
    from keymasq.session.analog_controls import AnalogControlManager

    def collect_buttons(widget):
        buttons = []
        child = widget.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.Button):
                buttons.append(child)
            buttons.extend(collect_buttons(child))
            child = child.get_next_sibling()
        return buttons

    # New users have no saved controls, so the dialog opens on the Presets tab
    # with a link through to the full Analog Controls manager.
    dialog = KeySelectorDialog(Gtk.Box(), "Left Stick", source_type="analog")
    assert dialog.stack.get_visible_child_name() == "analog_presets"
    presets_tab = dialog.stack.get_child_by_name("analog_presets")
    assert presets_tab is not None
    presets_labels = {button.get_label() for button in collect_buttons(presets_tab)}
    assert "Open Analog Controls…" in presets_labels

    # Once a control exists, the dialog opens on the picker instead.
    AnalogControlManager().save_analog_control(AnalogControlConfig(name="My Stick"))
    populated = KeySelectorDialog(Gtk.Box(), "Left Stick", source_type="analog")
    assert populated.stack.get_visible_child_name() == "analog_control"

    special_tab = dialog._build_special_tab()
    button_labels = {button.get_label() for button in collect_buttons(special_tab)}

    assert "Clear Mapping" in button_labels
    assert "Suppress" in button_labels
    assert "Passthrough" not in button_labels


def test_analog_key_selector_preset_uses_suffixed_name_for_storage_collision(
    temp_config_dir,
    monkeypatch,
):
    from gi.repository import Gtk

    from keymasq.common.models import ActionType, AnalogControlConfig, MappingAction
    from keymasq.gui.widgets import key_selector_dialog as dialog_module
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog
    from keymasq.session.analog_controls import (
        AnalogControlManager,
        analog_control_presets,
    )

    monkeypatch.setattr(dialog_module, "notify_session_reload_async", lambda: None)

    manager = AnalogControlManager()
    manager.save_analog_control(AnalogControlConfig(name="Mouse_Move"))

    results: list[MappingAction] = []
    dialog = KeySelectorDialog(
        Gtk.Box(),
        "Left Stick",
        source_type="analog",
        analog_input_type="stick",
    )
    dialog.connect("key-selected", lambda _dialog, action: results.append(action))
    preset = next(
        preset
        for preset in analog_control_presets("stick")
        if preset.preset_id == "mouse_move"
    )

    dialog._on_analog_preset_clicked(Gtk.Button(), preset)

    assert results[0].action_type == ActionType.ANALOG_CONTROL
    assert results[0].analog_control_names == ["Mouse Move 2"]
    assert AnalogControlManager().get_analog_control("Mouse Move 2") is not None


def test_key_selector_dialog_repeat_uses_special_toggle_buttons_and_inline_rapidfire():
    from gi.repository import Gtk

    from keymasq.common.models import (
        REPEAT_CATEGORY_MOUSE,
        REPEAT_CATEGORY_SPECIAL,
        ActionType,
        MappingAction,
    )
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

    def collect_buttons(widget):
        buttons = []
        child = widget.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.Button):
                buttons.append(child)
            buttons.extend(collect_buttons(child))
            child = child.get_next_sibling()
        return buttons

    results: list[MappingAction] = []
    dialog = KeySelectorDialog(Gtk.Box(), "Back")
    dialog.connect("key-selected", lambda _dialog, action: results.append(action))

    dialog.stack.set_visible_child_name("special")
    # Repeat carries its own rapidfire controls, so the shared footer stays hidden.
    assert dialog.options_box.get_visible() is False
    assert "keyboard keys, mouse buttons, mouse wheel actions" in (
        dialog.repeat_rapidfire_check.get_tooltip_text() or ""
    )

    special_tab = dialog._build_special_tab()
    buttons_by_label = {button.get_label(): button for button in collect_buttons(special_tab)}
    assert "Repeat Last Action" in buttons_by_label
    assert "Map Repeat" in buttons_by_label
    assert dialog._repeat_options_box is not None
    assert dialog._repeat_options_box.get_visible() is False
    assert dialog._repeat_button is not None
    assert dialog._repeat_button.get_active() is False

    dialog._repeat_button.emit("clicked")
    assert dialog._repeat_options_box.get_visible() is True
    assert dialog._repeat_button.get_active() is True

    dialog._repeat_button.emit("clicked")
    assert dialog._repeat_options_box.get_visible() is False
    assert dialog._repeat_button.get_active() is False

    dialog._repeat_button.emit("clicked")
    assert dialog._repeat_options_box.get_visible() is True

    toggles = dialog._repeat_toggle_buttons
    toggle_labels = {toggle.get_label() for toggle in toggles.values()}
    assert toggle_labels == {"Keys", "Mouse", "Gamepad", "Macros", "Other"}
    assert all(toggle.get_hexpand() for toggle in toggles.values())

    mouse_toggle = toggles[REPEAT_CATEGORY_MOUSE]
    other_toggle = toggles[REPEAT_CATEGORY_SPECIAL]
    assert isinstance(mouse_toggle, Gtk.ToggleButton)
    assert isinstance(other_toggle, Gtk.ToggleButton)
    assert "Keymasq special actions" in (other_toggle.get_tooltip_text() or "")

    dialog.repeat_rapidfire_check.set_active(True)
    dialog.repeat_hold_spin.set_value(35)
    dialog.repeat_wait_spin.set_value(45)
    other_toggle.set_active(False)
    buttons_by_label["Map Repeat"].emit("clicked")

    assert len(results) == 1
    action = results[0]
    assert action.action_type == ActionType.REPEAT
    assert "mouse" in (action.repeat_categories or [])
    assert "mouse_move" not in (action.repeat_categories or [])
    assert "special" not in (action.repeat_categories or [])
    assert action.rapidfire_enabled is True
    assert action.rapidfire_hold_ms == 35
    assert action.rapidfire_wait_ms == 45


def test_key_selector_dialog_repeat_preserves_empty_categories():
    from gi.repository import Gtk

    from keymasq.common.models import ActionType, MappingAction
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

    dialog = KeySelectorDialog(
        Gtk.Box(),
        "Back",
        MappingAction(action_type=ActionType.REPEAT, repeat_categories=[]),
    )

    assert dialog.stack.get_visible_child_name() == "special"
    assert dialog._repeat_button is not None
    assert dialog._repeat_button.get_active() is True
    assert dialog._repeat_map_btn is not None
    assert dialog._repeat_map_btn.get_sensitive() is False
    assert all(not toggle.get_active() for toggle in dialog._repeat_toggle_buttons.values())


def test_key_selector_dialog_uses_dedicated_superkey_tab(temp_config_dir, monkeypatch):
    from gi.repository import Gtk

    from keymasq.common import paths
    from keymasq.common.models import (
        ActionType,
        MappingAction,
        SuperkeyAction,
        SuperkeyConfig,
        SuperkeyMode,
    )
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog
    from keymasq.session.superkeys import SuperkeyManager

    def collect_buttons(widget):
        buttons = []
        child = widget.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.Button):
                buttons.append(child)
            buttons.extend(collect_buttons(child))
            child = child.get_next_sibling()
        return buttons

    superkeys_dir = temp_config_dir / "superkeys"
    monkeypatch.setattr(paths, "SUPERKEYS_DIR", superkeys_dir)
    SuperkeyManager().save_superkey(
        SuperkeyConfig(
            name="volume_rocker",
            mode=SuperkeyMode.PATTERN,
            tap_actions=[SuperkeyAction(action_type=ActionType.KEYBOARD, target="key_volumeup")],
        )
    )

    dialog = KeySelectorDialog(
        Gtk.Box(),
        "Back",
        current_action=MappingAction(
            action_type=ActionType.SUPERKEY,
            superkey_name="volume_rocker",
        ),
    )
    results: list[MappingAction] = []
    dialog.connect("key-selected", lambda _dialog, action: results.append(action))

    assert dialog.stack.get_visible_child_name() == "superkey"
    assert dialog._superkey_names == ["volume_rocker"]
    superkey_tab = dialog.stack.get_child_by_name("superkey")
    assert superkey_tab is not None
    button_labels = {button.get_label() for button in collect_buttons(superkey_tab)}
    assert "Open Super Keys…" in button_labels
    assert "Refresh" not in button_labels
    assert dialog.map_btn.get_visible() is True
    assert dialog.map_btn.get_sensitive() is True

    dialog._on_map_clicked(dialog.map_btn)

    assert len(results) == 1
    assert results[0].action_type == ActionType.SUPERKEY
    assert results[0].superkey_name == "volume_rocker"


def test_key_selector_superkey_right_click_opens_manager_for_superkey(
    temp_config_dir,
    monkeypatch,
):
    from gi.repository import Gtk

    from keymasq.common.models import SuperkeyConfig
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog
    from keymasq.session.superkeys import SuperkeyManager

    SuperkeyManager().save_superkey(SuperkeyConfig(name="volume_rocker"))
    dialog = KeySelectorDialog(Gtk.Window(), "Back")
    opened: list[str | None] = []
    monkeypatch.setattr(dialog, "_open_superkey_manager", opened.append)

    dialog._on_superkey_row_right_pressed(
        Gtk.GestureClick(),
        1,
        0.0,
        0.0,
        "volume_rocker",
    )

    assert opened == ["volume_rocker"]


def test_key_selector_open_superkey_manager_uses_root_profile_manager(
    monkeypatch,
):
    from gi.repository import Gtk

    import keymasq.gui.widgets.superkey_dialog as superkey_dialog_module
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

    captured: dict[str, object] = {}
    parent = Gtk.Box()
    root = Gtk.Window()
    profile_manager = object()
    root.profile_manager = profile_manager
    parent.main_window = root

    class DummySuperkeyDialog:
        def __init__(self, root, profile_manager_arg):
            captured["root"] = root
            captured["profile_manager"] = profile_manager_arg
            captured["signals"] = []

        def connect(self, signal_name, callback):
            captured["signals"].append(signal_name)
            captured[signal_name] = callback

        def present(self, root):
            captured["present_root"] = root

        def select_superkey_by_name(self, name):
            captured["selected_name"] = name

    monkeypatch.setattr(superkey_dialog_module, "SuperkeyDialog", DummySuperkeyDialog)

    dialog = KeySelectorDialog(parent, "Back")
    monkeypatch.setattr(dialog, "get_root", lambda: root)

    dialog._open_superkey_manager("volume_rocker")

    assert captured["root"] is root
    assert captured["profile_manager"] is profile_manager
    assert captured["present_root"] is root
    assert captured["signals"] == ["superkey-saved", "superkey-deleted"]
    assert captured["selected_name"] == "volume_rocker"


def test_key_selector_macro_right_click_opens_macro_editor(monkeypatch):
    from gi.repository import Gtk

    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

    dialog = KeySelectorDialog(Gtk.Window(), "Back")
    opened: list[str] = []
    monkeypatch.setattr(dialog, "_open_macro_editor", opened.append)

    dialog._on_macro_row_right_pressed(
        Gtk.GestureClick(),
        1,
        0.0,
        0.0,
        "demo_macro",
    )

    assert opened == ["demo_macro"]


def test_key_selector_open_macro_editor_presents_and_reloads_on_close(monkeypatch):
    from gi.repository import Gtk

    import keymasq.gui.widgets.macro_editor_dialog as macro_editor_dialog_module
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

    captured: dict[str, object] = {}
    callbacks: list[Callable[[object], object]] = []
    parent = Gtk.Window()

    class DummyMacroEditorDialog:
        def __init__(self, root, macro_name):
            captured["root"] = root
            captured["macro_name"] = macro_name

        def connect(self, signal_name, callback):
            captured["signal_name"] = signal_name
            callbacks.append(callback)

        def present(self, root):
            captured["present_root"] = root

    monkeypatch.setattr(
        macro_editor_dialog_module,
        "MacroEditorDialog",
        DummyMacroEditorDialog,
    )

    dialog = KeySelectorDialog(parent, "Back")
    monkeypatch.setattr(dialog, "get_root", lambda: parent)
    reloaded: list[bool] = []
    monkeypatch.setattr(dialog, "_load_macro_list", lambda: reloaded.append(True) or False)

    dialog._open_macro_editor("demo_macro")
    callbacks[0](captured["root"])

    assert captured["root"] is parent
    assert captured["macro_name"] == "demo_macro"
    assert captured["present_root"] is parent
    assert captured["signal_name"] == "closed"
    assert reloaded == [True]


def test_key_selector_dialog_keyboard_mapping_uses_rapidfire_or_tap_state():
    from gi.repository import Gtk

    from keymasq.common.models import ActionType, MappingAction
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

    dialog = KeySelectorDialog(Gtk.Box(), "Back")
    results: list[MappingAction] = []
    dialog.connect("key-selected", lambda _dialog, action: results.append(action))

    dialog.rapidfire_check.set_active(True)
    dialog.hold_spin.set_value(40)
    dialog.wait_spin.set_value(25)
    dialog._on_keyboard_clicked(None, "key_f5")

    assert len(results) == 1
    assert results[0].action_type == ActionType.KEYBOARD
    assert results[0].target == "key_f5"
    assert results[0].rapidfire_enabled is True
    assert results[0].rapidfire_hold_ms == 40
    assert results[0].rapidfire_wait_ms == 25
    assert results[0].tap_enabled is False

    dialog = KeySelectorDialog(Gtk.Box(), "Back")
    tap_results: list[MappingAction] = []
    dialog.connect("key-selected", lambda _dialog, action: tap_results.append(action))

    dialog.tap_check.set_active(True)
    dialog.tap_spin.set_value(70)
    dialog._on_keyboard_clicked(None, "key_f6")

    assert len(tap_results) == 1
    assert tap_results[0].target == "key_f6"
    assert tap_results[0].rapidfire_enabled is False
    assert tap_results[0].tap_enabled is True
    assert tap_results[0].tap_hold_ms == 70


def test_key_selector_dialog_selection_emits_and_closes(monkeypatch):
    from gi.repository import Gtk

    from keymasq.common.models import ActionType, MappingAction
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

    dialog = KeySelectorDialog(Gtk.Box(), "Back")
    results: list[MappingAction | None] = []
    close_calls: list[bool] = []
    dialog.connect("key-selected", lambda _dialog, selected: results.append(selected))
    monkeypatch.setattr(dialog, "close", lambda: close_calls.append(True))

    dialog._on_keyboard_clicked(None, "key_f5")

    assert close_calls == [True]
    assert len(results) == 1
    assert results[0] is not None
    assert results[0].action_type == ActionType.KEYBOARD
    assert results[0].target == "key_f5"


def test_key_selector_dialog_media_tab_hides_options_and_raw_keys_ignore_them():
    from gi.repository import Gtk

    from keymasq.common.models import ActionType, MappingAction
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

    dialog = KeySelectorDialog(Gtk.Box(), "Back")
    results: list[MappingAction] = []
    dialog.connect("key-selected", lambda _dialog, action: results.append(action))

    dialog.rapidfire_check.set_active(True)
    dialog.hold_spin.set_value(40)
    dialog.wait_spin.set_value(25)
    dialog.stack.set_visible_child_name("media")

    assert dialog.options_box.get_visible() is False

    dialog._on_keyboard_clicked(None, "key_playpause")

    assert len(results) == 1
    assert results[0].action_type == ActionType.KEYBOARD
    assert results[0].target == "key_playpause"
    assert results[0].rapidfire_enabled is False
    assert results[0].tap_enabled is False

    tap_dialog = KeySelectorDialog(Gtk.Box(), "Back")
    tap_results: list[MappingAction] = []
    tap_dialog.connect("key-selected", lambda _dialog, action: tap_results.append(action))

    tap_dialog.tap_check.set_active(True)
    tap_dialog.tap_spin.set_value(70)
    tap_dialog.stack.set_visible_child_name("media")

    assert tap_dialog.options_box.get_visible() is False

    tap_dialog._on_keyboard_clicked(None, "key_pause")

    assert len(tap_results) == 1
    assert tap_results[0].target == "key_pause"
    assert tap_results[0].rapidfire_enabled is False
    assert tap_results[0].tap_enabled is False


def test_key_selector_dialog_system_keys_use_keyboard_options():
    from gi.repository import Gtk

    from keymasq.common.models import ActionType, MappingAction
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

    dialog = KeySelectorDialog(Gtk.Box(), "Back")
    results: list[MappingAction] = []
    dialog.connect("key-selected", lambda _dialog, action: results.append(action))

    dialog.rapidfire_check.set_active(True)
    dialog.hold_spin.set_value(40)
    dialog.wait_spin.set_value(25)
    dialog._on_keyboard_clicked(None, "key_volumeup")

    assert len(results) == 1
    assert results[0].action_type == ActionType.KEYBOARD
    assert results[0].target == "key_volumeup"
    assert results[0].rapidfire_enabled is True
    assert results[0].rapidfire_hold_ms == 40
    assert results[0].rapidfire_wait_ms == 25
    assert results[0].tap_enabled is False

    mute_dialog = KeySelectorDialog(Gtk.Box(), "Back")
    mute_results: list[MappingAction] = []
    mute_dialog.connect("key-selected", lambda _dialog, action: mute_results.append(action))

    mute_dialog.rapidfire_check.set_active(True)
    mute_dialog.hold_spin.set_value(45)
    mute_dialog.wait_spin.set_value(30)
    mute_dialog._on_keyboard_clicked(None, "key_mute")

    assert len(mute_results) == 1
    assert mute_results[0].target == "key_mute"
    assert mute_results[0].rapidfire_enabled is True
    assert mute_results[0].rapidfire_hold_ms == 45
    assert mute_results[0].rapidfire_wait_ms == 30
    assert mute_results[0].tap_enabled is False

    tap_dialog = KeySelectorDialog(Gtk.Box(), "Back")
    tap_results: list[MappingAction] = []
    tap_dialog.connect("key-selected", lambda _dialog, action: tap_results.append(action))

    tap_dialog.tap_check.set_active(True)
    tap_dialog.tap_spin.set_value(80)
    tap_dialog._on_keyboard_clicked(None, "key_micmute")

    assert len(tap_results) == 1
    assert tap_results[0].target == "key_micmute"
    assert tap_results[0].rapidfire_enabled is False
    assert tap_results[0].tap_enabled is True
    assert tap_results[0].tap_hold_ms == 80


def test_key_selector_dialog_map_code_media_keys_ignore_options():
    from gi.repository import Gtk

    from keymasq.common.models import ActionType, MappingAction
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

    dialog = KeySelectorDialog(Gtk.Box(), "Back")
    results: list[MappingAction] = []
    dialog.connect("key-selected", lambda _dialog, action: results.append(action))

    dialog.rapidfire_check.set_active(True)
    dialog.hold_spin.set_value(40)
    dialog.wait_spin.set_value(25)
    dialog.kb_code_entry.set_text("key_playpause")

    dialog._on_map_code_clicked(None)

    assert len(results) == 1
    assert results[0].action_type == ActionType.KEYBOARD
    assert results[0].target == "key_playpause"
    assert results[0].rapidfire_enabled is False
    assert results[0].rapidfire_hold_ms == 20
    assert results[0].rapidfire_wait_ms == 20
    assert results[0].tap_enabled is False
    assert results[0].tap_hold_ms == 150

    tap_dialog = KeySelectorDialog(Gtk.Box(), "Back")
    tap_results: list[MappingAction] = []
    tap_dialog.connect("key-selected", lambda _dialog, action: tap_results.append(action))

    tap_dialog.tap_check.set_active(True)
    tap_dialog.tap_spin.set_value(70)
    tap_dialog.kb_code_entry.set_text("key_pause")

    tap_dialog._on_map_code_clicked(None)

    assert len(tap_results) == 1
    assert tap_results[0].target == "key_pause"
    assert tap_results[0].rapidfire_enabled is False
    assert tap_results[0].rapidfire_hold_ms == 20
    assert tap_results[0].rapidfire_wait_ms == 20
    assert tap_results[0].tap_enabled is False
    assert tap_results[0].tap_hold_ms == 150

    system_dialog = KeySelectorDialog(Gtk.Box(), "Back")
    system_results: list[MappingAction] = []
    system_dialog.connect("key-selected", lambda _dialog, action: system_results.append(action))

    system_dialog.rapidfire_check.set_active(True)
    system_dialog.hold_spin.set_value(40)
    system_dialog.wait_spin.set_value(25)
    system_dialog.kb_code_entry.set_text("key_brightnessup")

    system_dialog._on_map_code_clicked(None)

    assert len(system_results) == 1
    assert system_results[0].target == "key_brightnessup"
    assert system_results[0].rapidfire_enabled is True
    assert system_results[0].rapidfire_hold_ms == 40
    assert system_results[0].rapidfire_wait_ms == 25


def test_superkey_action_dialog_media_tab_hides_rapidfire_and_raw_keys_ignore_it():
    from gi.repository import Gtk

    from keymasq.common.models import ActionType, SuperkeyAction
    from keymasq.gui.widgets.key_selector_dialog import SuperkeyActionDialog

    dialog = SuperkeyActionDialog(Gtk.Box(), "hold")
    assert dialog.rapidfire_check is not None
    results: list[SuperkeyAction] = []
    dialog.connect("action-selected", lambda _dialog, action: results.append(action))

    dialog.rapidfire_check.set_active(True)
    dialog.hold_spin.set_value(40)
    dialog.wait_spin.set_value(25)
    dialog.stack.set_visible_child_name("media")

    assert dialog.options_box.get_visible() is False

    dialog._on_keyboard_clicked(None, "key_playpause")

    assert len(results) == 1
    assert results[0].action_type == ActionType.KEYBOARD
    assert results[0].target == "key_playpause"
    assert results[0].rapidfire_enabled is False


def test_key_selector_dialog_gamepad_axis_mapping_uses_raw_and_percent_values():
    from gi.repository import Gtk

    from keymasq.common.models import ActionType, MappingAction
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

    dialog = KeySelectorDialog(Gtk.Box(), "Back")
    results: list[MappingAction] = []
    dialog.connect("key-selected", lambda _dialog, action: results.append(action))

    dialog.stack.set_visible_child_name("gamepad")
    dialog.gamepad_axis_dropdown.set_selected(dialog.gamepad_axis_targets.index("abs_x"))
    dialog.gamepad_axis_percent.set_value(-100)
    assert int(dialog.gamepad_axis_value.get_value()) == -32768
    dialog.gamepad_axis_value.set_value(16384)
    assert round(dialog.gamepad_axis_percent.get_value()) == 50
    dialog.gamepad_axis_percent.set_value(-100)
    dialog._on_gamepad_axis_apply_clicked(None)

    assert len(results) == 1
    assert results[0].action_type == ActionType.GAMEPAD_AXIS
    assert results[0].target == "abs_x"
    assert results[0].axis_value == -32768


def test_key_selector_dialog_gamepad_trigger_presets_use_axis_actions():
    from gi.repository import Gtk

    from keymasq.common.models import ActionType, MappingAction
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

    def collect_buttons(widget: Gtk.Widget) -> list[Gtk.Button]:
        buttons: list[Gtk.Button] = []
        if isinstance(widget, Gtk.Button):
            buttons.append(widget)
        child = widget.get_first_child()
        while child is not None:
            buttons.extend(collect_buttons(child))
            child = child.get_next_sibling()
        return buttons

    def click_gamepad_button(label: str) -> MappingAction:
        dialog = KeySelectorDialog(Gtk.Box(), "Back")
        results: list[MappingAction] = []
        dialog.connect("key-selected", lambda _dialog, action: results.append(action))
        gamepad_tab = dialog.stack.get_child_by_name("gamepad")
        buttons_by_label = {
            button.get_label(): button
            for button in collect_buttons(gamepad_tab)
            if button.get_label()
        }

        buttons_by_label[label].emit("clicked")

        assert len(results) == 1
        return results[0]

    left_trigger = click_gamepad_button("LT")
    right_trigger = click_gamepad_button("RT")

    assert left_trigger.action_type == ActionType.GAMEPAD_AXIS
    assert left_trigger.target == "abs_z"
    assert left_trigger.axis_value == 255
    assert right_trigger.action_type == ActionType.GAMEPAD_AXIS
    assert right_trigger.target == "abs_rz"
    assert right_trigger.axis_value == 255


def test_key_selector_dialog_gamepad_output_selector_lives_in_title(monkeypatch):
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.gui.widgets import key_selector_dialog as dialog_module
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

    monkeypatch.setattr(dialog_module, "virtual_gamepad_count", lambda: 2)
    monkeypatch.setattr(
        dialog_module,
        "HardwareManager",
        lambda: SimpleNamespace(list_hardware=lambda: []),
    )

    dialog = KeySelectorDialog(Gtk.Box(), "Extra Button 14")
    gamepad_tab = dialog.stack.get_child_by_name("gamepad")

    assert dialog._gamepad_output_header is not None
    assert dialog._gamepad_output_dropdown is not None
    assert dialog._gamepad_output_header.get_parent() is not gamepad_tab
    assert dialog._gamepad_output_dropdown.get_parent() is dialog._gamepad_output_header
    assert dialog._gamepad_output_header.get_visible() is False

    dialog.stack.set_visible_child_name("gamepad")

    assert dialog._gamepad_output_header.get_visible() is True


def test_key_selector_dialog_gamepad_output_labels_hardware_by_name(monkeypatch):
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.common.models import ButtonDefinition, HardwareConfig
    from keymasq.gui.widgets import key_selector_dialog as dialog_module
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

    hardware = HardwareConfig(
        vendor_id="045e",
        product_id="028e",
        name="Living Room Pad",
        evdev_devices=[],
        buttons=[ButtonDefinition(id="btn_a", label="A", evdev="btn_a")],
        id="045e:028e@2",
    )
    monkeypatch.setattr(dialog_module, "virtual_gamepad_count", lambda: 1)
    monkeypatch.setattr(
        dialog_module,
        "HardwareManager",
        lambda: SimpleNamespace(list_hardware=lambda: [hardware]),
    )

    dialog = KeySelectorDialog(Gtk.Box(), "Extra Button 14")

    assert ("045e:028e@2", "Living Room Pad (045e:028e@2)") in dialog._gamepad_output_choices()


def test_key_selector_dialog_gamepad_default_warns_when_virtual_count_zero(monkeypatch):
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.gui.widgets import key_selector_dialog as dialog_module
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

    monkeypatch.setattr(dialog_module, "virtual_gamepad_count", lambda: 0)
    monkeypatch.setattr(
        dialog_module,
        "HardwareManager",
        lambda: SimpleNamespace(list_hardware=lambda: []),
    )

    dialog = KeySelectorDialog(Gtk.Box(), "Extra Button 14")

    assert dialog._gamepad_output_choices()[0] == (None, "Default output unavailable")
    assert dialog._gamepad_output_warning_label is not None
    assert dialog._gamepad_output_warning_label.get_visible() is True
    assert dialog._gamepad_output_warning_label.get_label() == "No virtual gamepads are configured."


def test_key_selector_dialog_explicit_missing_virtual_output_warns(monkeypatch):
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.common.models import ActionType, MappingAction
    from keymasq.gui.widgets import key_selector_dialog as dialog_module
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

    monkeypatch.setattr(dialog_module, "virtual_gamepad_count", lambda: 1)
    monkeypatch.setattr(
        dialog_module,
        "HardwareManager",
        lambda: SimpleNamespace(list_hardware=lambda: []),
    )

    dialog = KeySelectorDialog(
        Gtk.Box(),
        "Extra Button 14",
        MappingAction(
            action_type=ActionType.GAMEPAD,
            target="btn_south",
            output_id="virtual-gamepad-3",
        ),
    )

    assert ("virtual-gamepad-3", "virtual-gamepad-3 (unavailable)") in (
        dialog._gamepad_output_choices()
    )
    assert dialog._gamepad_output_warning_label is not None
    assert dialog._gamepad_output_warning_label.get_visible() is True
    assert "virtual-gamepad-3 is not configured" in (
        dialog._gamepad_output_warning_label.get_label()
    )


def test_key_selector_dialog_explicit_first_virtual_output_uses_default_choice(monkeypatch):
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.common.models import ActionType, MappingAction
    from keymasq.gui.widgets import key_selector_dialog as dialog_module
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

    monkeypatch.setattr(dialog_module, "virtual_gamepad_count", lambda: 1)
    monkeypatch.setattr(
        dialog_module,
        "HardwareManager",
        lambda: SimpleNamespace(list_hardware=lambda: []),
    )

    dialog = KeySelectorDialog(
        Gtk.Box(),
        "Extra Button 14",
        MappingAction(
            action_type=ActionType.GAMEPAD,
            target="btn_south",
            output_id="virtual-gamepad-1",
        ),
    )

    assert dialog._gamepad_output_choices() == [(None, "Virtual Gamepad 1")]
    assert dialog._gamepad_output_dropdown is not None
    assert dialog._gamepad_output_dropdown.get_selected() == 0
    assert dialog._gamepad_output_warning_label is not None
    assert dialog._gamepad_output_warning_label.get_visible() is False


def test_superkey_action_dialog_explicit_first_virtual_output_uses_default_choice(
    monkeypatch,
):
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.common.models import ActionType, SuperkeyAction
    from keymasq.gui.widgets import key_selector_dialog as dialog_module
    from keymasq.gui.widgets.key_selector_dialog import SuperkeyActionDialog

    monkeypatch.setattr(dialog_module, "virtual_gamepad_count", lambda: 1)
    monkeypatch.setattr(
        dialog_module,
        "HardwareManager",
        lambda: SimpleNamespace(list_hardware=lambda: []),
    )
    monkeypatch.setattr(
        dialog_module,
        "session_request_async",
        lambda payload, callback, timeout=5.0: None,
    )

    dialog = SuperkeyActionDialog(
        Gtk.Box(),
        "hold",
        SuperkeyAction(
            action_type=ActionType.GAMEPAD,
            target="btn_south",
            output_id="virtual-gamepad-1",
        ),
    )

    assert dialog._gamepad_output_choices() == [(None, "Virtual Gamepad 1")]
    assert dialog._gamepad_output_ids == [None]
    assert dialog._gamepad_output_warning_label is not None
    assert dialog._gamepad_output_warning_label.get_visible() is False


def test_key_selector_dialog_mouse_back_forward_use_browser_button_codes():
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.common.models import ActionType, MappingAction
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

    def collect_buttons(widget):
        buttons = []
        child = widget.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.Button):
                buttons.append(child)
            buttons.extend(collect_buttons(child))
            child = child.get_next_sibling()
        return buttons

    dialog = KeySelectorDialog(Gtk.Box(), "Back")
    results: list[MappingAction] = []
    dialog.connect("key-selected", lambda _dialog, action: results.append(action))

    mouse_tab = dialog._build_mouse_tab()
    buttons_by_label = {button.get_label(): button for button in collect_buttons(mouse_tab)}

    buttons_by_label["Back"].emit("clicked")
    buttons_by_label["Forward"].emit("clicked")

    assert [action.action_type for action in results] == [
        ActionType.MOUSE,
        ActionType.MOUSE,
    ]
    assert [action.target for action in results] == ["btn_side", "btn_extra"]


def test_key_selector_dialog_warns_when_exec_ignores_rapidfire(caplog: pytest.LogCaptureFixture):
    from gi.repository import Gtk

    from keymasq.common.models import ActionType, MappingAction
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

    dialog = KeySelectorDialog(Gtk.Box(), "Back")
    results: list[MappingAction] = []
    dialog.connect("key-selected", lambda _dialog, action: results.append(action))

    dialog.rapidfire_check.set_active(True)
    dialog.exec_entry.set_text("echo hi")

    with caplog.at_level("WARNING", logger="keymasq.gui.widgets.key_selector_dialog"):
        dialog._on_exec_map_clicked(None)

    assert len(results) == 1
    assert results[0].action_type == ActionType.EXEC
    assert dialog._rapidfire_enabled is False
    assert "Ignoring rapidfire for unsupported exec action in key selector" in caplog.text


def test_key_selector_dialog_map_code_handles_valid_and_invalid_input():
    from gi.repository import Gtk

    from keymasq.common.models import ActionType, MappingAction
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

    dialog = KeySelectorDialog(Gtk.Box(), "Back")
    results: list[MappingAction] = []
    dialog.connect("key-selected", lambda _dialog, action: results.append(action))

    dialog.kb_code_entry.set_text("125")
    dialog._on_map_code_clicked(None)

    assert len(results) == 1
    assert results[0].action_type == ActionType.KEYBOARD
    assert results[0].target == "key_leftmeta"

    invalid_dialog = KeySelectorDialog(Gtk.Box(), "Back")
    invalid_dialog.kb_code_entry.set_text("not-a-key")
    invalid_dialog._on_map_code_clicked(None)

    assert invalid_dialog.kb_code_entry.get_text() == ""
    assert invalid_dialog.kb_code_entry.get_placeholder_text() == "Unknown key code"


def test_resolve_gamepad_button_target_accepts_names_and_codes():
    from keymasq.gui.widgets.key_selector_dialog import _resolve_gamepad_button_target

    assert _resolve_gamepad_button_target("btn_c") == "btn_c"
    assert _resolve_gamepad_button_target("  BTN_Z ") == "btn_z"
    assert _resolve_gamepad_button_target("btn_trigger_happy1") == "btn_trigger_happy1"
    assert _resolve_gamepad_button_target("305") == "btn_east"
    assert _resolve_gamepad_button_target("0x132") == "btn_c"
    # Non-button codes and garbage are rejected.
    assert _resolve_gamepad_button_target("125") is None
    assert _resolve_gamepad_button_target("key_a") is None
    assert _resolve_gamepad_button_target("abs_x") is None
    assert _resolve_gamepad_button_target("not-a-button") is None
    assert _resolve_gamepad_button_target("") is None


def test_resolve_gamepad_axis_target_accepts_names_and_codes():
    from keymasq.gui.widgets.key_selector_dialog import _resolve_gamepad_axis_target

    assert _resolve_gamepad_axis_target("abs_hat0x") == "abs_hat0x"
    assert _resolve_gamepad_axis_target("  ABS_THROTTLE ") == "abs_throttle"
    assert _resolve_gamepad_axis_target("16") == "abs_hat0x"
    assert _resolve_gamepad_axis_target("0x10") == "abs_hat0x"
    assert _resolve_gamepad_axis_target("btn_c") is None
    assert _resolve_gamepad_axis_target("key_a") is None
    assert _resolve_gamepad_axis_target("abs_nope") is None
    assert _resolve_gamepad_axis_target("") is None


def test_key_selector_dialog_custom_axis_maps_and_toggles_percent():
    from gi.repository import Gtk

    from keymasq.common.models import ActionType, MappingAction
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

    dialog = KeySelectorDialog(Gtk.Box(), "Back")
    results: list[MappingAction] = []
    dialog.connect("key-selected", lambda _dialog, action: results.append(action))

    dialog.stack.set_visible_child_name("gamepad")

    # Standard axis: percent shown, custom entry hidden.
    assert dialog.gamepad_axis_percent.get_visible() is True
    assert dialog.gamepad_axis_custom_entry.get_visible() is False

    custom_index = dialog.gamepad_axis_targets.index("custom")
    dialog.gamepad_axis_dropdown.set_selected(custom_index)

    # Custom: percent hidden, custom entry shown.
    assert dialog.gamepad_axis_percent.get_visible() is False
    assert dialog.gamepad_axis_percent_label.get_visible() is False
    assert dialog.gamepad_axis_custom_entry.get_visible() is True

    dialog.gamepad_axis_custom_entry.set_text("abs_hat0x")
    dialog.gamepad_axis_value.set_value(1)
    dialog._on_gamepad_axis_apply_clicked(None)

    assert len(results) == 1
    assert results[0].action_type == ActionType.GAMEPAD_AXIS
    assert results[0].target == "abs_hat0x"
    assert results[0].axis_value == 1

    invalid = KeySelectorDialog(Gtk.Box(), "Back")
    invalid.stack.set_visible_child_name("gamepad")
    invalid.gamepad_axis_dropdown.set_selected(
        invalid.gamepad_axis_targets.index("custom")
    )
    invalid.gamepad_axis_custom_entry.set_text("not-an-axis")
    invalid._on_gamepad_axis_apply_clicked(None)

    assert invalid.gamepad_axis_custom_entry.get_text() == ""
    assert invalid.gamepad_axis_custom_entry.get_placeholder_text() == "Unknown axis code"


def test_key_selector_dialog_prefills_custom_button_and_axis_fields():
    from gi.repository import Gtk

    from keymasq.common.models import ActionType, MappingAction
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

    # Custom button code pre-fills the free-form field; a template button does not.
    custom_btn = KeySelectorDialog(
        Gtk.Box(),
        "B",
        current_action=MappingAction(action_type=ActionType.GAMEPAD, target="btn_tr2"),
    )
    assert custom_btn.gamepad_code_entry.get_text() == "btn_tr2"

    template_btn = KeySelectorDialog(
        Gtk.Box(),
        "A",
        current_action=MappingAction(action_type=ActionType.GAMEPAD, target="btn_south"),
    )
    assert template_btn.gamepad_code_entry.get_text() == ""

    # Custom axis selects Custom, fills the abs code, and restores the raw value.
    custom_axis = KeySelectorDialog(
        Gtk.Box(),
        "A",
        current_action=MappingAction(
            action_type=ActionType.GAMEPAD_AXIS, target="abs_hat2x", axis_value=200
        ),
    )
    assert custom_axis._selected_gamepad_axis_slot() == "custom"
    assert custom_axis.gamepad_axis_custom_entry.get_text() == "abs_hat2x"
    assert int(custom_axis.gamepad_axis_value.get_value()) == 200
    assert custom_axis.gamepad_axis_percent.get_visible() is False

    # Standard axis selects its dropdown entry and restores the value.
    standard_axis = KeySelectorDialog(
        Gtk.Box(),
        "A",
        current_action=MappingAction(
            action_type=ActionType.GAMEPAD_AXIS, target="abs_ry", axis_value=-16384
        ),
    )
    assert standard_axis._selected_gamepad_axis_slot() == "abs_ry"
    assert int(standard_axis.gamepad_axis_value.get_value()) == -16384
    assert standard_axis.gamepad_axis_custom_entry.get_visible() is False


def test_key_selector_dialog_gamepad_code_maps_and_routes(monkeypatch):
    from gi.repository import Gtk

    from keymasq.common.models import ActionType, MappingAction
    from keymasq.gui.widgets import key_selector_dialog as dialog_module
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

    monkeypatch.setattr(dialog_module, "virtual_gamepad_count", lambda: 2)
    monkeypatch.setattr(
        dialog_module,
        "HardwareManager",
        lambda: SimpleNamespace(list_hardware=lambda: []),
    )

    dialog = KeySelectorDialog(Gtk.Box(), "Extra Button 13")
    results: list[MappingAction] = []
    dialog.connect("key-selected", lambda _dialog, action: results.append(action))

    # Off-standard button by numeric code, routed to the selected output.
    dialog._selected_gamepad_output_id = "virtual-gamepad-2"
    dialog.gamepad_code_entry.set_text("305")
    dialog._on_gamepad_code_clicked(None)

    assert len(results) == 1
    assert results[0].action_type == ActionType.GAMEPAD
    assert results[0].target == "btn_east"
    assert results[0].output_id == "virtual-gamepad-2"

    invalid = KeySelectorDialog(Gtk.Box(), "Extra Button 13")
    invalid.gamepad_code_entry.set_text("not-a-button")
    invalid._on_gamepad_code_clicked(None)

    assert invalid.gamepad_code_entry.get_text() == ""
    assert invalid.gamepad_code_entry.get_placeholder_text() == "Unknown button code"


def test_key_selector_dialog_profile_tab_populates_and_maps_selected_action(monkeypatch):
    from gi.repository import Gtk

    from keymasq.common.models import ActionType, MappingAction
    from keymasq.gui.widgets import key_selector_dialog as dialog_module
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

    requests: list[dict] = []

    def fake_session_request_async(payload, callback, timeout=5.0):
        _ = timeout
        requests.append(payload)
        if payload["command"] == "list_profiles":
            callback(
                {
                    "status": "ok",
                    "profiles": [
                        {"name": "Desktop", "enabled": True},
                        {"name": "Gaming", "enabled": False},
                    ],
                }
            )
        return None

    monkeypatch.setattr(dialog_module, "session_request_async", fake_session_request_async)
    dialog = KeySelectorDialog(Gtk.Box(), "Back")
    results: list[MappingAction] = []
    dialog.connect("key-selected", lambda _dialog, action: results.append(action))
    dialog._load_profile_overview()

    dialog._on_profile_overview_loaded(
        {
            "status": "ok",
            "profiles": [
                {"name": "Desktop", "enabled": True},
                {"name": "Gaming", "enabled": False},
            ],
        }
    )
    dialog.stack.set_visible_child_name("profile")
    dialog._on_tab_changed(dialog.stack, None)
    assert dialog._profile_action_dropdown.get_selected() == 0
    assert dialog._profile_hint_label.get_label() == "Enable profile 'Desktop'."
    assert dialog._profile_lifetime_dropdown.get_sensitive() is False
    assert dialog._profile_lifetime_dropdown.get_selected() == 0
    assert dialog._profile_lifetime_notice_label.get_visible() is True
    assert (
        dialog._profile_lifetime_notice_label.get_label()
        == "Disable this profile first to use it as a temporary layer."
    )

    dialog._profile_action_dropdown.set_selected(2)
    dialog._on_profile_action_changed(dialog._profile_action_dropdown, None)
    dialog._profile_name_dropdown.set_selected(1)
    dialog._on_profile_name_changed(dialog._profile_name_dropdown, None)
    dialog._on_profile_map_clicked(None)

    assert {"command": "list_profiles"} in requests
    assert dialog._profile_name_items == ["Desktop", "Gaming"]
    assert dialog._profile_hint_label.get_label() == "Disable profile 'Gaming'."
    assert dialog.map_btn.get_sensitive() is True
    assert len(results) == 1
    assert results[0].action_type == ActionType.PROFILE_DISABLE
    assert results[0].profile_name == "Gaming"


def test_key_selector_dialog_profile_tab_restores_edited_profile_mapping(monkeypatch):
    from gi.repository import Gtk

    from keymasq.common.models import (
        ActionType,
        MappingAction,
        ProfileDeactivationPolicy,
    )
    from keymasq.gui.widgets import key_selector_dialog as dialog_module
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

    def fake_session_request_async(_payload, _callback, timeout=5.0):
        _ = timeout
        return None

    monkeypatch.setattr(dialog_module, "session_request_async", fake_session_request_async)
    dialog = KeySelectorDialog(
        Gtk.Box(),
        "Back",
        current_action=MappingAction(
            action_type=ActionType.PROFILE_ENABLE,
            profile_name="Gaming",
            profile_deactivation=ProfileDeactivationPolicy(after_actions=1),
        ),
    )
    results: list[MappingAction] = []
    dialog.connect("key-selected", lambda _dialog, action: results.append(action))

    dialog._on_profile_name_changed(dialog._profile_name_dropdown, None)
    assert dialog._selected_profile_name == "Gaming"

    dialog._on_profile_overview_loaded(
        {
            "status": "ok",
            "profiles": [
                {"name": "Desktop", "enabled": True},
                {"name": "Gaming", "enabled": False},
            ],
        }
    )
    dialog.stack.set_visible_child_name("profile")
    dialog._on_tab_changed(dialog.stack, None)

    assert dialog._profile_action_dropdown.get_selected() == 0
    assert dialog._profile_name_dropdown.get_selected() == 1
    assert dialog._profile_lifetime_dropdown.get_selected() == 2
    assert dialog._profile_lifetime_dropdown.get_sensitive() is True
    assert dialog._profile_lifetime_notice_label.get_visible() is False
    assert dialog._profile_hint_label.get_label() == "Enable profile 'Gaming'."
    assert dialog.map_btn.get_sensitive() is True

    dialog._on_profile_map_clicked(None)

    assert len(results) == 1
    assert results[0].action_type == ActionType.PROFILE_ENABLE
    assert results[0].profile_name == "Gaming"
    assert results[0].profile_deactivation == ProfileDeactivationPolicy(after_actions=1)


def test_key_selector_dialog_profile_toggle_lifetime_controls(monkeypatch):
    from gi.repository import Gtk

    from keymasq.common.models import (
        ActionType,
        MappingAction,
        ProfileDeactivationPolicy,
    )
    from keymasq.gui.widgets import key_selector_dialog as dialog_module
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

    def fake_session_request_async(_payload, _callback, timeout=5.0):
        _ = timeout
        return None

    monkeypatch.setattr(dialog_module, "session_request_async", fake_session_request_async)
    dialog = KeySelectorDialog(
        Gtk.Box(),
        "Back",
        current_action=MappingAction(
            action_type=ActionType.PROFILE_TOGGLE,
            profile_name="Gaming",
            profile_deactivation=ProfileDeactivationPolicy(after_actions=1),
        ),
    )
    results: list[MappingAction] = []
    dialog.connect("key-selected", lambda _dialog, action: results.append(action))

    dialog._on_profile_overview_loaded(
        {
            "status": "ok",
            "profiles": [
                {"name": "Desktop", "enabled": True},
                {"name": "Gaming", "enabled": False},
            ],
        }
    )
    dialog.stack.set_visible_child_name("profile")
    dialog._on_tab_changed(dialog.stack, None)

    assert dialog._profile_action_dropdown.get_selected() == 1
    assert dialog._profile_name_dropdown.get_selected() == 1
    assert dialog._profile_lifetime_box.get_visible() is True
    assert dialog._profile_lifetime_title.get_label() == "When toggled on"
    labels = [
        dialog._profile_lifetime_model.get_string(idx)
        for idx in range(dialog._profile_lifetime_model.get_n_items())
    ]
    assert labels == ["Persistent", "One-shot", "Custom"]
    assert dialog._profile_lifetime_dropdown.get_selected() == 1
    assert dialog._profile_lifetime_custom_box.get_visible() is True
    assert dialog._profile_custom_count_row.get_visible() is False
    assert dialog._profile_custom_timeout_row.get_visible() is True
    assert dialog._profile_custom_timeout_toggle.get_active() is False
    assert dialog._profile_custom_timeout_spin.get_visible() is False
    assert dialog._profile_custom_timeout_unit.get_visible() is False
    assert dialog._profile_custom_trigger_toggle.get_visible() is False

    dialog._profile_custom_timeout_toggle.set_active(True)
    assert dialog._profile_custom_timeout_spin.get_visible() is True
    assert dialog._profile_custom_timeout_unit.get_visible() is True

    dialog._on_profile_map_clicked(None)

    assert len(results) == 1
    assert results[0].action_type == ActionType.PROFILE_TOGGLE
    assert results[0].profile_name == "Gaming"
    assert results[0].profile_deactivation == ProfileDeactivationPolicy(
        after_actions=1,
        timeout_ms=1500,
    )


def test_key_selector_dialog_profile_custom_lifetime_restores_count_row(monkeypatch):
    from gi.repository import Gtk

    from keymasq.common.models import (
        ActionType,
        MappingAction,
        ProfileDeactivationPolicy,
    )
    from keymasq.gui.widgets import key_selector_dialog as dialog_module
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

    def fake_session_request_async(_payload, _callback, timeout=5.0):
        _ = timeout
        return None

    monkeypatch.setattr(dialog_module, "session_request_async", fake_session_request_async)
    dialog = KeySelectorDialog(
        Gtk.Box(),
        "Back",
        current_action=MappingAction(
            action_type=ActionType.PROFILE_ENABLE,
            profile_name="Gaming",
            profile_deactivation=ProfileDeactivationPolicy(after_actions=3),
        ),
    )
    results: list[MappingAction] = []
    dialog.connect("key-selected", lambda _dialog, action: results.append(action))

    dialog._on_profile_overview_loaded(
        {
            "status": "ok",
            "profiles": [
                {"name": "Gaming", "enabled": False},
            ],
        }
    )
    dialog.stack.set_visible_child_name("profile")
    dialog._on_tab_changed(dialog.stack, None)

    assert dialog._profile_lifetime_dropdown.get_selected() == 3
    assert dialog._profile_lifetime_custom_box.get_visible() is True
    assert dialog._profile_custom_count_toggle.get_active() is True
    assert int(dialog._profile_lifetime_count_spin.get_value()) == 3
    assert dialog._profile_lifetime_count_spin.get_sensitive() is True
    assert dialog._profile_lifetime_timeout_row.get_visible() is False
    assert dialog._profile_custom_timeout_toggle.get_active() is False
    assert dialog._profile_custom_timeout_spin.get_visible() is False
    assert dialog._profile_custom_timeout_unit.get_visible() is False
    assert dialog._profile_custom_trigger_toggle.get_active() is False

    dialog._profile_custom_timeout_toggle.set_active(True)
    assert dialog._profile_lifetime_timeout_row.get_visible() is False
    assert dialog._profile_custom_timeout_spin.get_visible() is True
    assert dialog._profile_custom_timeout_spin.get_sensitive() is True
    assert dialog._profile_custom_timeout_unit.get_visible() is True

    dialog._on_profile_map_clicked(None)

    assert len(results) == 1
    assert results[0].profile_deactivation == ProfileDeactivationPolicy(
        after_actions=3,
        timeout_ms=1500,
    )


def test_key_selector_dialog_profile_timeout_only_lifetime_restores_as_custom(
    monkeypatch,
):
    from gi.repository import Gtk

    from keymasq.common.models import (
        ActionType,
        MappingAction,
        ProfileDeactivationPolicy,
    )
    from keymasq.gui.widgets import key_selector_dialog as dialog_module
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

    def fake_session_request_async(_payload, _callback, timeout=5.0):
        _ = timeout
        return None

    monkeypatch.setattr(dialog_module, "session_request_async", fake_session_request_async)
    dialog = KeySelectorDialog(
        Gtk.Box(),
        "Back",
        current_action=MappingAction(
            action_type=ActionType.PROFILE_ENABLE,
            profile_name="Gaming",
            profile_deactivation=ProfileDeactivationPolicy(timeout_ms=2500),
        ),
    )
    results: list[MappingAction] = []
    dialog.connect("key-selected", lambda _dialog, action: results.append(action))

    dialog._on_profile_overview_loaded(
        {
            "status": "ok",
            "profiles": [
                {"name": "Gaming", "enabled": False},
            ],
        }
    )
    dialog.stack.set_visible_child_name("profile")
    dialog._on_tab_changed(dialog.stack, None)

    assert dialog._profile_lifetime_dropdown.get_selected() == 3
    assert dialog._profile_lifetime_custom_box.get_visible() is True
    assert dialog._profile_custom_count_toggle.get_active() is False
    assert dialog._profile_custom_timeout_toggle.get_active() is True
    assert dialog._profile_custom_timeout_spin.get_visible() is True
    assert int(dialog._profile_custom_timeout_spin.get_value()) == 2500

    dialog._on_profile_map_clicked(None)

    assert len(results) == 1
    assert results[0].profile_deactivation == ProfileDeactivationPolicy(timeout_ms=2500)


def test_key_selector_dialog_profile_lifetime_requires_disabled_profile(monkeypatch):
    from gi.repository import Gtk

    from keymasq.common.models import (
        ActionType,
        MappingAction,
        ProfileDeactivationPolicy,
    )
    from keymasq.gui.widgets import key_selector_dialog as dialog_module
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

    def fake_session_request_async(_payload, _callback, timeout=5.0):
        _ = timeout
        return None

    monkeypatch.setattr(dialog_module, "session_request_async", fake_session_request_async)
    dialog = KeySelectorDialog(
        Gtk.Box(),
        "Back",
        current_action=MappingAction(
            action_type=ActionType.PROFILE_ENABLE,
            profile_name="Desktop",
            profile_deactivation=ProfileDeactivationPolicy(after_actions=1),
        ),
    )
    results: list[MappingAction] = []
    dialog.connect("key-selected", lambda _dialog, action: results.append(action))

    dialog._on_profile_overview_loaded(
        {
            "status": "ok",
            "profiles": [
                {"name": "Desktop", "enabled": True},
                {"name": "Gaming", "enabled": False},
            ],
        }
    )
    dialog.stack.set_visible_child_name("profile")
    dialog._on_tab_changed(dialog.stack, None)

    assert dialog._profile_name_dropdown.get_selected() == 0
    assert dialog._profile_lifetime_dropdown.get_selected() == 0
    assert dialog._profile_lifetime_dropdown.get_sensitive() is False
    assert dialog._profile_lifetime_notice_label.get_visible() is True

    dialog._profile_name_dropdown.set_selected(1)
    dialog._on_profile_name_changed(dialog._profile_name_dropdown, None)

    assert dialog._profile_lifetime_dropdown.get_sensitive() is True
    assert dialog._profile_lifetime_dropdown.get_selected() == 2
    assert dialog._profile_lifetime_notice_label.get_visible() is False

    dialog._profile_name_dropdown.set_selected(0)
    dialog._on_profile_name_changed(dialog._profile_name_dropdown, None)

    assert dialog._profile_lifetime_dropdown.get_selected() == 0
    assert dialog._profile_lifetime_dropdown.get_sensitive() is False
    assert dialog._profile_lifetime_notice_label.get_visible() is True

    dialog._on_profile_map_clicked(None)

    assert len(results) == 1
    assert results[0].action_type == ActionType.PROFILE_ENABLE
    assert results[0].profile_name == "Desktop"
    assert results[0].profile_deactivation is None


def test_key_selector_dialog_shows_hyprland_actions_for_active_listener_or_saved_action():
    from gi.repository import Gtk

    from keymasq.common.models import ActionType, MappingAction
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

    active_dialog = KeySelectorDialog(
        Gtk.Box(),
        "Back",
        compositor_action_status={
            "listener_name": "hyprland",
            "compositor_dispatch_available": True,
        },
    )
    assert active_dialog.stack.get_child_by_name("hyprland") is not None

    saved_dialog = KeySelectorDialog(
        Gtk.Box(),
        "Back",
        MappingAction(
            action_type=ActionType.COMPOSITOR_DISPATCH,
            compositor_id="hyprland",
            compositor_dispatcher="workspace",
            compositor_args="2",
        ),
        compositor_action_status={
            "listener_name": "x11",
            "compositor_dispatch_available": False,
        },
    )
    assert saved_dialog.stack.get_child_by_name("hyprland") is not None
    assert saved_dialog.stack.get_visible_child_name() == "hyprland"


def test_key_selector_dialog_shows_niri_dispatch_for_active_listener_or_saved_action():
    from gi.repository import Gtk

    from keymasq.common.models import ActionType, MappingAction
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

    active_dialog = KeySelectorDialog(
        Gtk.Box(),
        "Back",
        compositor_action_status={
            "listener_name": "niri",
            "compositor_dispatch_available": True,
        },
    )
    assert active_dialog.stack.get_child_by_name("niri") is not None
    assert active_dialog.stack.get_child_by_name("hyprland") is None
    page = active_dialog.stack.get_child_by_name("niri")
    assert page is not None
    assert page._preset_dropdown.get_selected() == 0
    assert page._dispatcher_entry.get_text() == ""
    assert page._args_entry.get_text() == ""
    assert page._dispatcher_entry.get_editable() is True
    assert page._args_entry.get_editable() is True

    saved_dialog = KeySelectorDialog(
        Gtk.Box(),
        "Back",
        MappingAction(
            action_type=ActionType.COMPOSITOR_DISPATCH,
            compositor_id="niri",
            compositor_dispatcher="focus-workspace",
            compositor_args="2",
        ),
        compositor_action_status={
            "listener_name": "x11",
            "compositor_dispatch_available": False,
        },
    )
    assert saved_dialog.stack.get_child_by_name("niri") is not None
    assert saved_dialog.stack.get_visible_child_name() == "niri"


def test_key_selector_dialog_shows_gnome_dispatch_for_active_gnome_listener():
    from gi.repository import Gtk

    from keymasq.common.models import ActionType, MappingAction
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

    dialog = KeySelectorDialog(
        Gtk.Box(),
        "Back",
        MappingAction(
            action_type=ActionType.COMPOSITOR_DISPATCH,
            compositor_id="gnome",
            compositor_dispatcher="workspace",
            compositor_args="2",
        ),
        compositor_action_status={
            "listener_name": "gnome",
            "compositor_dispatch_available": True,
        },
    )

    assert dialog.stack.get_child_by_name("gnome") is not None
    assert dialog.stack.get_visible_child_name() == "gnome"

    page = dialog.stack.get_child_by_name("gnome")
    assert page is not None
    assert page._preset_dropdown.get_selected() == 3
    assert page._dispatcher_entry.get_text() == "workspace"
    assert page._args_entry.get_text() == "2"
    assert page._dispatcher_entry.get_editable() is False
    assert page._args_entry.get_editable() is False

    saved_dialog = KeySelectorDialog(
        Gtk.Box(),
        "Back",
        MappingAction(
            action_type=ActionType.COMPOSITOR_DISPATCH,
            compositor_id="gnome",
            compositor_dispatcher="workspace",
            compositor_args="2",
        ),
        compositor_action_status={
            "listener_name": "x11",
            "compositor_dispatch_available": False,
        },
    )
    assert saved_dialog.stack.get_child_by_name("gnome") is not None
    assert saved_dialog.stack.get_visible_child_name() == "gnome"


def test_key_selector_dialog_compositor_set_cursor_reuses_position_capture(monkeypatch):
    from gi.repository import Gtk

    from keymasq.common.models import ActionType, MappingAction
    from keymasq.gui.widgets import key_selector_dialog as dialog_module
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

    class _Result:
        def __init__(self, x: int, y: int) -> None:
            self.x = x
            self.y = y

    class _SlurpCapture:
        available = True

        def set_compositor(self, compositor: str) -> None:
            assert compositor == "gnome"

        def capture_point(self, callback) -> None:
            callback(_Result(640, 480))

    monkeypatch.setattr(dialog_module, "get_slurp_capture", lambda: _SlurpCapture())
    monkeypatch.setattr(dialog_module, "detect_compositor_sync", lambda: "gnome")

    dialog = KeySelectorDialog(
        Gtk.Box(),
        "Back",
        MappingAction(
            action_type=ActionType.COMPOSITOR_DISPATCH,
            compositor_id="gnome",
            compositor_dispatcher="set_cursor_position",
            compositor_args="0 0",
        ),
        compositor_action_status={
            "listener_name": "gnome",
            "compositor_dispatch_available": True,
        },
    )
    results: list[MappingAction] = []
    dialog.connect("key-selected", lambda _dialog, action: results.append(action))

    page = dialog.stack.get_child_by_name("gnome")
    assert page is not None
    assert page._capture_row.get_visible() is True

    page._on_capture_clicked(page._capture_btn)

    assert page._args_entry.get_text() == "640 480"
    assert page._capture_status.get_text() == "Captured: 640, 480"

    page._on_map_clicked(page._map_btn)

    assert len(results) == 1
    assert results[0].action_type == ActionType.COMPOSITOR_DISPATCH
    assert results[0].compositor_id == "gnome"
    assert results[0].compositor_dispatcher == "set_cursor_position"
    assert results[0].compositor_args == "640 480"


def test_key_selector_dialog_reopens_set_cursor_with_captured_coordinates():
    from gi.repository import Gtk

    from keymasq.common.models import ActionType, MappingAction
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

    dialog = KeySelectorDialog(
        Gtk.Box(),
        "Back",
        MappingAction(
            action_type=ActionType.COMPOSITOR_DISPATCH,
            compositor_id="hyprland",
            compositor_dispatcher="set_cursor_position",
            compositor_args="640 480",
        ),
        compositor_action_status={
            "listener_name": "hyprland",
            "compositor_dispatch_available": True,
        },
    )

    page = dialog.stack.get_child_by_name("hyprland")
    assert page is not None
    assert page._preset_dropdown.get_selected() != 0
    assert page._dispatcher_entry.get_text() == "set_cursor_position"
    assert page._args_entry.get_text() == "640 480"
    assert page._capture_row.get_visible() is True


def test_key_selector_dialog_shows_kde_dispatch_for_active_listener_or_saved_action():
    from gi.repository import Gtk

    from keymasq.common.models import ActionType, MappingAction
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

    active_dialog = KeySelectorDialog(
        Gtk.Box(),
        "Back",
        compositor_action_status={
            "listener_name": "kde",
            "compositor_dispatch_available": True,
        },
    )
    assert active_dialog.stack.get_child_by_name("kde") is not None
    assert active_dialog.stack.get_child_by_name("hyprland") is None
    assert active_dialog.stack.get_child_by_name("gnome") is None
    page = active_dialog.stack.get_child_by_name("kde")
    assert page is not None
    assert page._preset_dropdown.get_selected() == 0
    assert page._dispatcher_entry.get_text() == "desktop_next"
    assert page._args_entry.get_text() == ""
    assert page._dispatcher_entry.get_editable() is False
    assert page._args_entry.get_editable() is False

    saved_dialog = KeySelectorDialog(
        Gtk.Box(),
        "Back",
        MappingAction(
            action_type=ActionType.COMPOSITOR_DISPATCH,
            compositor_id="kde",
            compositor_dispatcher="tile_left",
            compositor_args="",
        ),
        compositor_action_status={
            "listener_name": "x11",
            "compositor_dispatch_available": False,
        },
    )
    assert saved_dialog.stack.get_child_by_name("kde") is not None
    assert saved_dialog.stack.get_visible_child_name() == "kde"


def test_key_selector_dialog_keeps_hyprland_custom_dispatch_enabled():
    from gi.repository import Gtk

    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

    dialog = KeySelectorDialog(
        Gtk.Box(),
        "Back",
        compositor_action_status={
            "listener_name": "hyprland",
            "compositor_dispatch_available": True,
        },
    )
    page = dialog.stack.get_child_by_name("hyprland")
    assert page is not None
    assert page._preset_dropdown.get_selected() == 0
    assert page._dispatcher_entry.get_text() == ""
    assert page._args_entry.get_text() == ""
    assert page._dispatcher_entry.get_editable() is True
    assert page._args_entry.get_editable() is True


def test_compositor_action_helpers_resolve_kde_actions() -> None:
    from keymasq.common.models import ActionType, MappingAction
    from keymasq.gui.widgets.compositor_actions import (
        compositor_action_tab_name,
        describe_compositor_action,
    )

    action = MappingAction(
        action_type=ActionType.COMPOSITOR_DISPATCH,
        compositor_id="kde",
        compositor_dispatcher="tile_left",
        compositor_args="",
    )

    assert compositor_action_tab_name(
        action,
        {
            "listener_name": "kde",
            "compositor_dispatch_available": True,
        },
    ) == "kde"
    assert describe_compositor_action(action) == "KDE Plasma → tile_left"


def test_compositor_action_definitions_share_dispatch_behavior() -> None:
    from keymasq.common.models import ActionType, MappingAction
    from keymasq.gui.widgets.compositor_actions.compositors import (
        COMPOSITOR_ACTION_DEFINITIONS,
    )

    for definition in COMPOSITOR_ACTION_DEFINITIONS:
        action = MappingAction(
            action_type=ActionType.COMPOSITOR_DISPATCH,
            compositor_id=definition.compositor_id,
            compositor_dispatcher="test_dispatcher",
            compositor_args="test args",
        )

        assert (
            definition.is_available(
                None,
                {
                    "listener_name": definition.compositor_id,
                    "compositor_dispatch_available": True,
                },
            )
            is True
        )
        assert (
            definition.is_available(
                None,
                {
                    "listener_name": definition.compositor_id,
                    "compositor_dispatch_available": False,
                },
            )
            is False
        )
        assert definition.extract_fields(action) == ("test_dispatcher", "test args")
        assert definition.describe_action(action) == (
            f"{definition.title} → test_dispatcher test args"
        )

        built = definition.build_action("test_dispatcher", "test args")
        assert built.action_type == ActionType.COMPOSITOR_DISPATCH
        assert built.compositor_id == definition.compositor_id
        assert built.compositor_dispatcher == "test_dispatcher"
        assert built.compositor_args == "test args"

        legacy_action = MappingAction(
            action_type=ActionType.COMPOSITOR_DISPATCH,
            compositor_dispatcher="legacy_dispatcher",
            compositor_args="legacy args",
        )
        assert definition.extract_fields(legacy_action) == (
            "legacy_dispatcher",
            "legacy args",
        )

        foreign_action = MappingAction(
            action_type=ActionType.COMPOSITOR_DISPATCH,
            compositor_id="foreign",
            compositor_dispatcher="test_dispatcher",
            compositor_args="test args",
        )
        assert definition.extract_fields(foreign_action) == ("", "")


def test_compositor_action_helpers_resolve_niri_actions() -> None:
    from keymasq.common.models import ActionType, MappingAction
    from keymasq.gui.widgets.compositor_actions import (
        compositor_action_tab_name,
        describe_compositor_action,
    )

    action = MappingAction(
        action_type=ActionType.COMPOSITOR_DISPATCH,
        compositor_id="niri",
        compositor_dispatcher="focus-workspace",
        compositor_args="2",
    )

    assert compositor_action_tab_name(
        action,
        {
            "listener_name": "niri",
            "compositor_dispatch_available": True,
        },
    ) == "niri"
    assert describe_compositor_action(action) == "Niri → focus-workspace 2"


def test_key_selector_dialog_mouse_capture_and_move_mapping_paths(monkeypatch):
    from gi.repository import Gtk

    from keymasq.common.models import ActionType, MappingAction
    from keymasq.gui.widgets import key_selector_dialog as dialog_module
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

    class _Result:
        def __init__(self, x: int, y: int) -> None:
            self.x = x
            self.y = y

    class _SlurpCapture:
        available = True

        def __init__(self) -> None:
            self.captured = False
            self.compositor = None

        def set_compositor(self, compositor: str) -> None:
            self.compositor = compositor

        def capture_point(self, callback) -> None:
            self.captured = True
            callback(_Result(640, 480))

    monkeypatch.setattr(dialog_module, "get_slurp_capture", lambda: _SlurpCapture())
    monkeypatch.setattr(dialog_module, "detect_compositor_sync", lambda: "hyprland")

    dialog = KeySelectorDialog(Gtk.Box(), "Back")
    results: list[MappingAction] = []
    dialog.connect("key-selected", lambda _dialog, action: results.append(action))

    dialog.stack.set_visible_child_name("mouse")
    dialog.mouse_move_abs_check.set_active(True)
    dialog._on_mouse_move_mode_changed(dialog.mouse_move_abs_check)
    dialog._on_capture_position_clicked(Gtk.Button())

    assert dialog.mouse_move_x_spin.get_value_as_int() == 640
    assert dialog.mouse_move_y_spin.get_value_as_int() == 480
    assert dialog.mouse_move_capture_status.get_text() == "Captured: 640, 480"
    assert dialog.map_btn.get_visible() is True
    assert dialog.map_btn.get_label() == "Map Move"

    dialog._on_map_clicked(dialog.map_btn)

    assert len(results) == 1
    assert results[0].action_type == ActionType.MOUSE_MOVE_ABS
    assert results[0].move_x == 640
    assert results[0].move_y == 480

    error_dialog = KeySelectorDialog(Gtk.Box(), "Back")
    error_dialog._on_capture_position_clicked(Gtk.Button())
    error_dialog._on_capture_position_response(
        error_dialog._capture_request_id,
        {"status": "error", "message": "Unknown command: get_cursor_position"}
    )

    assert (
        error_dialog.mouse_move_capture_status.get_text()
        == "Please restart Keymasq Session, then try again"
    )


def test_key_selector_dialog_repeated_delayed_capture_ignores_stale_response(monkeypatch):
    from collections.abc import Callable
    from gi.repository import Gtk

    from keymasq.gui.widgets import key_selector_dialog as dialog_module
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

    callbacks: list[Callable[[], bool]] = []
    requests: list[Callable[[dict[str, object]], bool | None]] = []

    class _SlurpCapture:
        available = False

        def set_compositor(self, compositor: str) -> None:
            return None

    def fake_timeout_add(_delay, callback):
        callbacks.append(callback)
        return len(callbacks)

    def fake_source_remove(_source_id):
        return None

    def fake_session_request_async(payload, callback, timeout=5.0):
        assert payload == {"command": "get_cursor_position"}
        requests.append(callback)

    monkeypatch.setattr(dialog_module, "get_slurp_capture", lambda: _SlurpCapture())
    monkeypatch.setattr(dialog_module, "detect_compositor_sync", lambda: "hyprland")
    monkeypatch.setattr(dialog_module.GLib, "timeout_add", fake_timeout_add)
    monkeypatch.setattr(dialog_module.GLib, "source_remove", fake_source_remove)
    monkeypatch.setattr(dialog_module, "session_request_async", fake_session_request_async)

    dialog = KeySelectorDialog(Gtk.Box(), "Back")
    dialog.mouse_move_abs_check.set_active(True)
    dialog._on_mouse_move_mode_changed(dialog.mouse_move_abs_check)

    dialog._on_capture_position_clicked(Gtk.Button())
    timer1 = callbacks.pop(0)
    assert timer1() is False
    stale_response = requests.pop(0)

    dialog._on_capture_position_clicked(Gtk.Button())
    timer2 = callbacks.pop(0)
    stale_response({"status": "ok", "x": 100, "y": 200})

    assert timer2() is False
    fresh_response = requests.pop(0)
    fresh_response({"status": "ok", "x": 300, "y": 400})

    assert dialog.mouse_move_x_spin.get_value_as_int() == 300
    assert dialog.mouse_move_y_spin.get_value_as_int() == 400


def test_shared_navigation_picker_builds_dropdown():
    from gi.repository import Gtk

    from keymasq.gui.widgets.input_picker_shared import build_navigation_tab

    class _Owner:
        def _create_key_button(
            self,
            label: str,
            evdev: str,
            width: float = 1,
            large: bool = False,
            protected: bool = False,
        ) -> Gtk.Button:
            return Gtk.Button(label=label)

        def _on_keyboard_clicked(self, *_args) -> None:
            return None

        def _on_f_key_selected(self, *_args) -> None:
            return None

        def _on_f_dropdown_changed(self, *_args) -> None:
            return None

    owner = _Owner()
    widget = build_navigation_tab(owner, f_extra=["F13", "F14"])

    assert isinstance(widget, Gtk.Box)
    assert isinstance(owner.f_dropdown, Gtk.DropDown)


def test_shared_keyboard_picker_builds_system_key_row():
    from gi.repository import Gtk

    from keymasq.gui.widgets.input_picker_shared import build_keyboard_tab
    from keymasq.gui.widgets.key_selector_dialog import SYSTEM_KEY_GROUPS

    class _Owner:
        def __init__(self) -> None:
            self.clicked: list[str] = []

        def _create_key_button(
            self,
            label: str,
            evdev: str,
            width: float = 1,
            large: bool = False,
            protected: bool = False,
        ) -> Gtk.Button:
            button = Gtk.Button(label=label)
            button._evdev_name = evdev
            return button

        def _on_keyboard_clicked(self, _btn, evdev_id: str) -> None:
            self.clicked.append(evdev_id)

    def collect_buttons(widget: Gtk.Widget) -> list[Gtk.Button]:
        buttons: list[Gtk.Button] = []
        if isinstance(widget, Gtk.Button):
            buttons.append(widget)
        child = widget.get_first_child()
        while child is not None:
            buttons.extend(collect_buttons(child))
            child = child.get_next_sibling()
        return buttons

    owner = _Owner()
    widget = build_keyboard_tab(
        owner,
        keyboard_layout=[["Esc"]],
        key_to_evdev={"Esc": "key_esc"},
        key_widths={},
        system_key_groups=SYSTEM_KEY_GROUPS,
    )

    assert isinstance(widget, Gtk.ScrolledWindow)
    buttons = collect_buttons(widget)
    system_buttons = [
        button
        for button in buttons
        if getattr(button, "_evdev_name", "") in {"key_volumeup", "key_brightnessdown"}
    ]
    assert len(system_buttons) == 2
    assert system_buttons[0].get_size_request() == (36, 34)
    assert system_buttons[0].get_label() is None
    assert "key_volumeup" in (system_buttons[0].get_tooltip_text() or "")

    system_buttons[0].emit("clicked")
    system_buttons[1].emit("clicked")

    assert owner.clicked == ["key_volumeup", "key_brightnessdown"]


def test_shared_media_picker_builds_icon_buttons():
    from gi.repository import Gtk

    from keymasq.gui.widgets.input_picker_shared import build_media_tab
    from keymasq.gui.widgets.key_selector_dialog import MEDIA_KEY_GROUPS

    class _Owner:
        def __init__(self) -> None:
            self.clicked: list[str] = []

        def _on_keyboard_clicked(self, _btn, evdev_id: str) -> None:
            self.clicked.append(evdev_id)

    def collect_buttons(widget: Gtk.Widget) -> list[Gtk.Button]:
        buttons: list[Gtk.Button] = []
        if isinstance(widget, Gtk.Button):
            buttons.append(widget)
        child = widget.get_first_child()
        while child is not None:
            buttons.extend(collect_buttons(child))
            child = child.get_next_sibling()
        return buttons

    def collect_expanders(widget: Gtk.Widget) -> list[Gtk.Expander]:
        expanders: list[Gtk.Expander] = []
        if isinstance(widget, Gtk.Expander):
            expanders.append(widget)
        child = widget.get_first_child()
        while child is not None:
            expanders.extend(collect_expanders(child))
            child = child.get_next_sibling()
        return expanders

    owner = _Owner()
    widget = build_media_tab(owner, media_groups=MEDIA_KEY_GROUPS)

    assert isinstance(widget, Gtk.ScrolledWindow)
    expanders = collect_expanders(widget)
    assert len(expanders) == 1
    assert expanders[0].get_label() == "Raw Transport Keys"
    assert expanders[0].get_expanded() is False

    raw_child = expanders[0].get_child()
    assert raw_child is not None
    raw_buttons = collect_buttons(raw_child)
    assert len(raw_buttons) == 6
    assert isinstance(raw_buttons[0].get_child(), Gtk.Box)
    raw_buttons[2].emit("clicked")

    assert owner.clicked == ["key_nextsong"]


def test_shared_media_picker_can_put_mpris_controls_first():
    from gi.repository import Gtk

    from keymasq.gui.widgets.input_picker_shared import build_media_tab
    from keymasq.gui.widgets.key_selector_dialog import MEDIA_KEY_GROUPS, MPRIS_MEDIA_GROUPS

    class _Owner:
        def __init__(self) -> None:
            self.mpris_clicked: list[str] = []
            self.media_key_clicked: list[str] = []

        def _on_mpris_clicked(self, _btn, command: str) -> None:
            self.mpris_clicked.append(command)

        def _on_keyboard_clicked(self, _btn, evdev_id: str) -> None:
            self.media_key_clicked.append(evdev_id)

    def collect_buttons(widget: Gtk.Widget) -> list[Gtk.Button]:
        buttons: list[Gtk.Button] = []
        if isinstance(widget, Gtk.Button):
            buttons.append(widget)
        child = widget.get_first_child()
        while child is not None:
            buttons.extend(collect_buttons(child))
            child = child.get_next_sibling()
        return buttons

    def collect_expanders(widget: Gtk.Widget) -> list[Gtk.Expander]:
        expanders: list[Gtk.Expander] = []
        if isinstance(widget, Gtk.Expander):
            expanders.append(widget)
        child = widget.get_first_child()
        while child is not None:
            expanders.extend(collect_expanders(child))
            child = child.get_next_sibling()
        return expanders

    owner = _Owner()
    widget = build_media_tab(
        owner,
        media_groups=MEDIA_KEY_GROUPS,
        mpris_groups=MPRIS_MEDIA_GROUPS,
    )

    buttons = collect_buttons(widget)
    assert len(buttons) == 6

    expanders = collect_expanders(widget)
    assert len(expanders) == 1
    assert expanders[0].get_label() == "Raw Transport Keys"
    assert expanders[0].get_expanded() is False
    raw_child = expanders[0].get_child()
    assert raw_child is not None
    raw_buttons = collect_buttons(raw_child)
    assert len(raw_buttons) == 6

    buttons[1].emit("clicked")
    raw_buttons[2].emit("clicked")

    assert owner.mpris_clicked == ["play_pause"]
    assert owner.media_key_clicked == ["key_nextsong"]


def test_shared_gamepad_picker_buttons_use_shared_metadata():
    from gi.repository import Gtk

    from keymasq.gui.widgets.input_picker_shared import GAMEPAD_BUTTONS, build_gamepad_tab
    from keymasq.gui.widgets.key_selector_dialog import EVDEV_TO_GAMEPAD

    class _Owner:
        def _create_key_button(
            self,
            label: str,
            evdev: str,
            width: float = 1,
            large: bool = False,
            protected: bool = False,
        ) -> Gtk.Button:
            button = Gtk.Button(label=label)
            button._evdev_name = evdev
            return button

        def _on_gamepad_clicked(self, *_args) -> None:
            return None

        def _on_gamepad_axis_clicked(self, *_args) -> None:
            return None

    def collect_buttons(widget: Gtk.Widget) -> list[Gtk.Button]:
        buttons: list[Gtk.Button] = []
        if isinstance(widget, Gtk.Button):
            buttons.append(widget)
        child = widget.get_first_child()
        while child is not None:
            buttons.extend(collect_buttons(child))
            child = child.get_next_sibling()
        return buttons

    widget = build_gamepad_tab(_Owner())
    rendered_buttons: set[str] = set()
    for button in collect_buttons(widget):
        evdev_name = getattr(button, "_evdev_name", None)
        if isinstance(evdev_name, str) and evdev_name.startswith("btn_"):
            rendered_buttons.add(evdev_name)

    metadata_buttons = set(GAMEPAD_BUTTONS.values())
    assert rendered_buttons == metadata_buttons
    assert {EVDEV_TO_GAMEPAD[evdev_id] for evdev_id in rendered_buttons} == set(GAMEPAD_BUTTONS)


def test_shared_gamepad_picker_falls_back_without_axis_handler():
    from gi.repository import Gtk

    from keymasq.gui.widgets.input_picker_shared import build_gamepad_tab

    class _Owner:
        def __init__(self) -> None:
            self.clicked: list[str] = []

        def _create_key_button(
            self,
            label: str,
            evdev: str,
            width: float = 1,
            large: bool = False,
            protected: bool = False,
        ) -> Gtk.Button:
            return Gtk.Button(label=label)

        def _on_gamepad_clicked(self, _btn, evdev_id: str) -> None:
            self.clicked.append(evdev_id)

    def collect_buttons(widget: Gtk.Widget) -> list[Gtk.Button]:
        buttons: list[Gtk.Button] = []
        if isinstance(widget, Gtk.Button):
            buttons.append(widget)
        child = widget.get_first_child()
        while child is not None:
            buttons.extend(collect_buttons(child))
            child = child.get_next_sibling()
        return buttons

    owner = _Owner()
    widget = build_gamepad_tab(owner)

    assert isinstance(widget, Gtk.Box)
    buttons_by_label = {
        button.get_label(): button
        for button in collect_buttons(widget)
        if button.get_label()
    }

    for label in ("LT", "LX+", "RY-", "RT"):
        buttons_by_label[label].emit("clicked")

    assert owner.clicked == ["abs_z", "abs_x", "abs_ry", "abs_rz"]


def test_key_selector_dialog_opens_media_tab_for_media_key_action():
    from gi.repository import Gtk

    from keymasq.common.models import ActionType, MappingAction
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

    dialog = KeySelectorDialog(
        Gtk.Box(),
        "Back",
        current_action=MappingAction(action_type=ActionType.KEYBOARD, target="key_playpause"),
    )

    assert dialog.stack.get_visible_child_name() == "media"


def test_key_selector_dialog_docs_button_tracks_visible_tab(monkeypatch: pytest.MonkeyPatch):
    from gi.repository import Gtk

    from keymasq.gui.widgets import key_selector_dialog as dialog_module
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

    monkeypatch.setattr(dialog_module, "__version__", "1.2.3")

    dialog = KeySelectorDialog(Gtk.Box(), "Back")

    dialog.stack.set_visible_child_name("media")

    assert dialog.actions_docs_btn.get_visible() is True
    assert dialog.actions_docs_btn.get_tooltip_text() == "Open Media documentation"
    assert dialog._active_actions_docs_link() == ("media", "Media")
    assert dialog_module._actions_docs_url("media") == (
        "https://keymasq.tools/docs/v1.2.3/ACTIONS/#media"
    )

    dialog.stack.set_visible_child_name("mouse")

    assert dialog.actions_docs_btn.get_tooltip_text() == "Open Mouse documentation"

    dialog.stack.set_visible_child_name("type")

    assert dialog.actions_docs_btn.get_tooltip_text() == "Open Type documentation"
    assert dialog._active_actions_docs_link() == ("type-macro-inline-controls", "Type")
    assert dialog_module._actions_docs_url("type-macro-inline-controls") == (
        "https://keymasq.tools/docs/v1.2.3/MACROS/#type-macro-inline-controls"
    )

    monkeypatch.setattr(dialog_module, "__version__", "1.2.3.dev1")
    assert dialog_module._actions_docs_url("mouse") == (
        "https://keymasq.tools/docs/master/ACTIONS/#mouse"
    )


def test_analog_controls_layout_orders_triggers_then_sticks() -> None:
    from keymasq.common.models import AnalogInputDefinition
    from keymasq.gui.widgets.device_tab import _grouped_analog_inputs, _ordered_analog_inputs

    analogs = [
        AnalogInputDefinition(id="left_stick", label="Left Stick", type="stick"),
        AnalogInputDefinition(id="left_trigger", label="Left Trigger", type="axis"),
        AnalogInputDefinition(id="right_stick", label="Right Stick", type="stick"),
        AnalogInputDefinition(id="right_trigger", label="Right Trigger", type="axis"),
    ]

    assert [analog.id for analog in _ordered_analog_inputs(analogs)] == [
        "left_trigger",
        "right_trigger",
        "left_stick",
        "right_stick",
    ]
    assert [
        (title, [analog.id for analog in grouped_analogs])
        for title, grouped_analogs in _grouped_analog_inputs(analogs)
    ] == [
        ("1D Axes / Triggers", ["left_trigger", "right_trigger"]),
        ("Sticks", ["left_stick", "right_stick"]),
    ]
