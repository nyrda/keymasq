# ruff: noqa: I001
from types import SimpleNamespace

import pytest

gi = pytest.importorskip("gi")


def _make_add_inputs_flow(device, on_complete=None, parent=None):
    from keymasq.gui.widgets.device_tab.add_inputs_flow import AddInputsFlow

    if parent is None:
        parent = SimpleNamespace(
            _recording_unlock_required=False,
            _recording_unlocked=False,
            _recording_refresh_owner=False,
        )
    return AddInputsFlow(
        parent,
        lambda _payload, callback: callback({"status": "ok"}),
        device,
        on_complete or (lambda _result: None),
    )


class TestDeviceTabWidget:
    def test_device_tab_creation(self):
        from gi.repository import Gtk

        from keymasq.common.models import ButtonDefinition, HardwareConfig
        from keymasq.gui.widgets.device_tab import DeviceTab

        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Test Mouse",
            evdev_devices=[],
            buttons=[
                ButtonDefinition(id="btn_back", label="Back", evdev="btn_side"),
                ButtonDefinition(id="btn_forward", label="Forward", evdev="btn_extra"),
            ],
        )

        tab = DeviceTab(
            device=device,
            profile_manager=None,
            demo_mode=True,
        )

        assert tab.demo_mode is True
        assert tab.device.name == "Test Mouse"
        assert len(tab._button_widgets) == 2

        scrolled = tab.get_last_child()
        assert isinstance(scrolled, Gtk.ScrolledWindow)
        h_policy, v_policy = scrolled.get_policy()
        assert h_policy == Gtk.PolicyType.AUTOMATIC
        assert v_policy == Gtk.PolicyType.AUTOMATIC

    def test_analog_learning_is_available_for_unknown_raw_devices(self):
        from keymasq.common.models import ButtonDefinition, DeviceType, EvdevDevice, HardwareConfig
        from keymasq.gui.widgets.device_tab import DeviceTab

        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Raw Device",
            evdev_devices=[
                EvdevDevice(
                    path="/dev/input/event9",
                    device_type=DeviceType.OTHER,
                    id="raw",
                )
            ],
            buttons=[ButtonDefinition(id="btn_0", label="Button 0", evdev="btn_0")],
        )

        tab = DeviceTab(device=device, profile_manager=None, demo_mode=False)

        assert tab._supports_analog_learning() is True

    def test_analog_learning_stays_hidden_for_plain_keyboard_devices(self):
        from keymasq.common.models import ButtonDefinition, DeviceType, EvdevDevice, HardwareConfig
        from keymasq.gui.widgets.device_tab import DeviceTab

        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Keyboard",
            evdev_devices=[
                EvdevDevice(
                    path="/dev/input/event3",
                    device_type=DeviceType.KEYBOARD,
                    id="keys",
                )
            ],
            buttons=[ButtonDefinition(id="key_a", label="A", evdev="key_a")],
        )

        tab = DeviceTab(device=device, profile_manager=None, demo_mode=False)

        assert tab._supports_analog_learning() is False

    def test_device_tab_inspect_button_delegates_to_main_window(self):
        from keymasq.common.models import ButtonDefinition, DeviceType, EvdevDevice, HardwareConfig
        from keymasq.gui.widgets.device_tab import DeviceTab

        calls = []

        class MainWindow:
            def open_device_inspector(self, device):
                calls.append(device)

        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Mouse",
            evdev_devices=[
                EvdevDevice(
                    path="/dev/input/event4",
                    device_type=DeviceType.MOUSE,
                    id="mouse",
                )
            ],
            buttons=[ButtonDefinition(id="btn_back", label="Back", evdev="btn_side")],
        )
        tab = DeviceTab(
            device=device,
            profile_manager=None,
            main_window=MainWindow(),
            demo_mode=False,
        )

        tab._on_inspect_device_clicked(None)  # type: ignore[arg-type]

        assert calls == [device]

    def test_device_tab_header_includes_hardware_settings_button(self):
        from gi.repository import Gtk

        from keymasq.common.models import ButtonDefinition, DeviceType, EvdevDevice, HardwareConfig
        from keymasq.gui.widgets.device_tab import DeviceTab
        from tests.gui.support import collect_widgets

        class _HardwareManager:
            pass

        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Mouse",
            evdev_devices=[
                EvdevDevice(
                    path="/dev/input/event4",
                    device_type=DeviceType.MOUSE,
                    id="mouse",
                )
            ],
            buttons=[ButtonDefinition(id="btn_back", label="Back", evdev="btn_side")],
        )
        tab = DeviceTab(
            device=device,
            profile_manager=None,
            hardware_manager=_HardwareManager(),
            demo_mode=False,
        )

        tooltips = [
            button.get_tooltip_text()
            for button in collect_widgets(tab, Gtk.Button, include_self=True)
        ]

        assert "Inspect device" in tooltips
        assert "Hardware settings" in tooltips
        assert "Delete device" not in tooltips

    def test_numbered_hardware_id_header_keeps_path_in_tooltip(self):
        from keymasq.common.models import ButtonDefinition, DeviceType, EvdevDevice, HardwareConfig
        from keymasq.gui.widgets.device_tab import DeviceTab

        stable_path = (
            "/dev/input/by-id/"
            "usb-\u00a9Microsoft_Xbox_360_Wireless_Receiver_for_Windows_"
            "FD161BB0-if02-event-joystick"
        )
        hardware_id = "045e:02a1@2"
        device = HardwareConfig(
            vendor_id="045e",
            product_id="02a1",
            name="Xbox 360 2",
            evdev_devices=[
                EvdevDevice(
                    path=stable_path,
                    device_type=DeviceType.GAMEPAD,
                    id="if02_joystick",
                )
            ],
            buttons=[ButtonDefinition(id="btn_south", label="A", evdev="btn_south")],
            id=hardware_id,
        )

        tab = DeviceTab(device=device, profile_manager=None, demo_mode=True)

        assert tab._header_caption_label.get_text() == "045e:02a1 | 1 evdev, 1 buttons"
        assert tab._header_caption_label.get_tooltip_text() == (
            f"Hardware ID: {hardware_id}\nInterfaces:\n{stable_path}"
        )

    def test_header_caption_keeps_button_and_analog_counts_consistent(self):
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
            name="Gamepad",
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
                        AnalogAxisDefinition(role="x", evdev="abs_x", evdev_code=0),
                        AnalogAxisDefinition(role="y", evdev="abs_y", evdev_code=1),
                    ],
                ),
                AnalogInputDefinition(
                    id="left_trigger",
                    label="Left Trigger",
                    type="axis",
                    source="pad",
                    axes=[AnalogAxisDefinition(role="x", evdev="abs_z", evdev_code=2)],
                ),
            ],
        )

        tab = DeviceTab(device=device, profile_manager=None, demo_mode=True)

        expected_caption = "1234:5678 | 1 evdev, 1 buttons, 2 analog inputs"
        assert tab._header_caption_label.get_text() == expected_caption
        tab._update_header_caption()
        assert tab._header_caption_label.get_text() == expected_caption

    def test_live_header_caption_lists_interface_state_and_mappings(self, temp_config_dir):
        from keymasq.common.models import (
            ActionType,
            ButtonDefinition,
            DeviceProfileLayer,
            DeviceType,
            EvdevDevice,
            HardwareConfig,
            MappingAction,
            ProfileConfig,
        )
        from keymasq.gui.widgets.device_tab import DeviceTab
        from keymasq.session.profiles import ProfileManager

        profile_manager = ProfileManager()
        profile_manager.save_profile(
            ProfileConfig(
                name="Gaming",
                enabled=True,
                device_layers={
                    "1234:5678": DeviceProfileLayer(
                        hardware_id="1234:5678",
                        mappings={
                            "btn_back": MappingAction(
                                action_type=ActionType.KEYBOARD,
                                target="key_1",
                            ),
                            "btn_forward": MappingAction(
                                action_type=ActionType.KEYBOARD,
                                target="key_2",
                            ),
                        },
                    )
                },
            )
        )
        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Mouse",
            evdev_devices=[
                EvdevDevice(
                    path="/dev/input/event10",
                    device_type=DeviceType.MOUSE,
                    id="mouse",
                ),
                EvdevDevice(
                    path="/dev/input/event11",
                    device_type=DeviceType.MOUSE,
                    id="extra",
                ),
            ],
            buttons=[
                ButtonDefinition(id="btn_back", label="Back", evdev="btn_side"),
                ButtonDefinition(id="btn_forward", label="Forward", evdev="btn_extra"),
            ],
        )

        tab = DeviceTab(device=device, profile_manager=profile_manager, demo_mode=False)
        tab.apply_active_profile_response(
            {
                "status": "ok",
                "devices": {
                    device.hardware_id: {
                        "device_status": {
                            "state": "partial",
                            "configured_count": 2,
                            "connected_count": 2,
                            "grabbed_count": 1,
                            "runtime_ready": True,
                        }
                    }
                },
            }
        )

        assert (
            tab._header_caption_label.get_text()
            == "1234:5678 | 2 interfaces · 2 connected · 1 grabbed · 2 mappings"
        )
        assert "1/2" not in tab._header_caption_label.get_text()

    def test_keyboard_left_layout_does_not_request_seventh_column(self):
        from gi.repository import Gtk

        from keymasq.common.models import ButtonDefinition, HardwareConfig
        from keymasq.gui.widgets.device_tab import DeviceTab

        left_keyboard_ids = [
            "key_esc",
            "key_grave",
            "key_tab",
            "key_q",
            "key_w",
            "key_e",
            "key_r",
            "key_t",
            "key_capslock",
            "key_a",
            "key_s",
            "key_d",
            "key_f",
            "key_g",
            "key_leftshift",
            "key_z",
            "key_x",
            "key_c",
            "key_v",
            "key_b",
            "key_leftctrl",
            "key_leftmeta",
            "key_leftalt",
            "key_space",
        ]
        key_ids = [
            *left_keyboard_ids,
            "key_rightalt",
            "key_rightctrl",
            *(f"key_extra_{i}" for i in range(14)),
        ]
        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Test Keyboard",
            evdev_devices=[],
            buttons=[
                ButtonDefinition(id=key_id, label=key_id, evdev=key_id) for key_id in key_ids
            ],
        )

        tab = DeviceTab(device=device, profile_manager=None, demo_mode=True)

        scrolled = tab.get_last_child()
        assert isinstance(scrolled, Gtk.ScrolledWindow)
        content = scrolled.get_child()
        if isinstance(content, Gtk.Viewport):
            content = content.get_child()
        assert isinstance(content, Gtk.Box)

        child = content.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.Expander) and child.get_label() == "Keyboard (Left)":
                grid = child.get_child()
                assert isinstance(grid, Gtk.Grid)
                for row in range(5):
                    assert grid.get_child_at(6, row) is None
                assert tab._button_widgets["key_rightalt"].get_parent() is not grid
                assert tab._button_widgets["key_rightctrl"].get_parent() is not grid
                return
            child = child.get_next_sibling()

        raise AssertionError("Keyboard (Left) section not found")

    def test_keyboard_right_modifier_order(self):
        from gi.repository import Gtk

        from keymasq.common.models import ButtonDefinition, HardwareConfig
        from keymasq.gui.widgets.device_tab import DeviceTab

        right_keyboard_ids = [
            "key_backspace",
            "key_y",
            "key_u",
            "key_i",
            "key_o",
            "key_p",
            "key_enter",
            "key_h",
            "key_j",
            "key_k",
            "key_l",
            "key_n",
            "key_m",
            "key_rightshift",
            "key_rightmeta",
            "key_rightalt",
            "key_rightctrl",
        ]
        key_ids = [*right_keyboard_ids, *(f"key_extra_{i}" for i in range(23))]
        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Test Keyboard",
            evdev_devices=[],
            buttons=[
                ButtonDefinition(id=key_id, label=key_id, evdev=key_id) for key_id in key_ids
            ],
        )

        tab = DeviceTab(device=device, profile_manager=None, demo_mode=True)

        scrolled = tab.get_last_child()
        assert isinstance(scrolled, Gtk.ScrolledWindow)
        content = scrolled.get_child()
        if isinstance(content, Gtk.Viewport):
            content = content.get_child()
        assert isinstance(content, Gtk.Box)

        child = content.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.Expander) and child.get_label() == "Keyboard (Right)":
                grid = child.get_child()
                assert isinstance(grid, Gtk.Grid)
                expected = {
                    (0, 2): "key_n",
                    (1, 2): "key_m",
                    (2, 2): "key_rightshift",
                    (0, 3): "key_rightmeta",
                    (1, 3): "key_rightalt",
                    (2, 3): "key_rightctrl",
                }
                for (col, row), button_id in expected.items():
                    cell = grid.get_child_at(col, row)
                    assert cell is tab._button_widgets[button_id]
                return
            child = child.get_next_sibling()

        raise AssertionError("Keyboard (Right) section not found")

    def test_device_tab_initial_profile_selection(self, temp_config_dir):
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
                name="Gaming",
                enabled=True,
                device_layers={"1234:5678": DeviceProfileLayer(hardware_id="1234:5678")},
            )
        )

        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Test Mouse",
            evdev_devices=[],
            buttons=[
                ButtonDefinition(id="btn_back", label="Back", evdev="btn_side"),
            ],
        )

        tab = DeviceTab(
            device=device,
            profile_manager=profile_manager,
            demo_mode=True,
        )

        assert tab._selected_profile is not None
        assert tab._selected_profile.config.name == "Gaming"
        assert tab.settings_frame.get_sensitive() is True

    def test_device_tab_does_not_start_active_profile_polling(self, monkeypatch):
        from keymasq.common.models import ButtonDefinition, HardwareConfig
        from keymasq.gui.widgets import device_tab as device_tab_module
        from keymasq.gui.widgets.device_tab import DeviceTab

        def fail_timeout(*args, **kwargs):
            raise AssertionError("DeviceTab should not schedule active profile polling")

        monkeypatch.setattr(device_tab_module.GLib, "timeout_add", fail_timeout)

        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Test Mouse",
            evdev_devices=[],
            buttons=[ButtonDefinition(id="btn_back", label="Back", evdev="btn_side")],
        )

        DeviceTab(
            device=device,
            profile_manager=None,
            demo_mode=False,
        )

    def test_device_tab_refresh_profiles_picks_up_new_global_profile(self, temp_config_dir):
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
                name="Base",
                enabled=True,
                device_layers={"1234:5678": DeviceProfileLayer(hardware_id="1234:5678")},
            )
        )

        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Test Mouse",
            evdev_devices=[],
            buttons=[
                ButtonDefinition(id="btn_back", label="Back", evdev="btn_side"),
            ],
        )

        tab = DeviceTab(
            device=device,
            profile_manager=profile_manager,
            demo_mode=True,
        )

        profile_manager.save_profile(
            ProfileConfig(
                name="Gaming",
                enabled=True,
                device_layers={"1234:5678": DeviceProfileLayer(hardware_id="1234:5678")},
            )
        )

        tab.refresh_profiles(preferred_profile_name="Gaming")

        assert "Gaming" in tab._profile_names
        assert tab._selected_profile is not None
        assert tab._selected_profile.config.name == "Gaming"

    def test_device_tab_greys_out_overridden_mapping(self, temp_config_dir):
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
                            "btn_back": MappingAction(
                                action_type=ActionType.KEYBOARD, target="key_1"
                            )
                        },
                    )
                },
            )
        )
        profile_manager.save_profile(
            ProfileConfig(
                name="Overlay",
                enabled=True,
                is_permanent=True,
                priority=2,
                device_layers={
                    "1234:5678": DeviceProfileLayer(
                        hardware_id="1234:5678",
                        mappings={
                            "btn_back": MappingAction(
                                action_type=ActionType.KEYBOARD, target="key_2"
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
            buttons=[ButtonDefinition(id="btn_back", label="Back", evdev="btn_side")],
        )

        tab = DeviceTab(
            device=device,
            profile_manager=profile_manager,
            demo_mode=True,
        )
        tab._active_profile_names = ["Base", "Overlay"]

        tab.refresh_profiles(preferred_profile_name="Base")
        base_widget = tab._button_widgets["btn_back"]
        tab._update_button_display("btn_back")

        assert base_widget.has_css_class("button-card-mapped-inactive") is True

        tab.refresh_profiles(preferred_profile_name="Overlay")
        overlay_widget = tab._button_widgets["btn_back"]
        tab._update_button_display("btn_back")

        assert overlay_widget.has_css_class("button-card-mapped-active") is True

    def test_device_tab_always_grab_toggle_persists_selected_layer(self, temp_config_dir):
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

        tab = DeviceTab(device=device, profile_manager=profile_manager, demo_mode=True)
        tab.refresh_profiles(preferred_profile_name="Gaming", publish_selection=False)

        save_calls: list[bool] = []
        tab._save_profile = lambda: save_calls.append(True) or True

        tab.always_grab_check.set_active(True)

        layer = tab._selected_layer()
        assert layer is not None
        assert layer.always_grab_all is True
        assert save_calls == [True]

    def test_device_tab_profile_settings_lists_all_devices_for_grab_mode(self, temp_config_dir):
        from keymasq.common.models import ButtonDefinition, HardwareConfig, ProfileConfig
        from keymasq.gui.window import MainWindow

        window = MainWindow(demo_mode=True)
        assert window.profile_manager is not None
        window.profile_manager.save_profile(
            ProfileConfig(name="Gaming", enabled=True, is_permanent=True)
        )

        device1 = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Mouse One",
            evdev_devices=[],
            buttons=[ButtonDefinition(id="btn_back", label="Back", evdev="btn_side")],
        )
        device2 = HardwareConfig(
            vendor_id="2234",
            product_id="6678",
            name="Mouse Two",
            evdev_devices=[],
            buttons=[ButtonDefinition(id="btn_back", label="Back", evdev="btn_side")],
        )

        window._add_device_tab(device1)
        window._add_device_tab(device2)
        tab = window._child_for_hardware_id(device1.hardware_id)
        tab.refresh_profiles(preferred_profile_name="Gaming", publish_selection=False)

        tab._on_profile_settings_clicked(tab.settings_btn)

        assert set(tab.always_grab_checks) == {device1.hardware_id, device2.hardware_id}
        assert tab.always_grab_checks[device1.hardware_id].get_title() == "Always grab Mouse One"
        assert tab.always_grab_checks[device2.hardware_id].get_title() == "Always grab Mouse Two"

        tab.always_grab_checks[device2.hardware_id].set_active(True)

        assert tab._selected_profile is not None
        layer = tab._selected_profile.config.get_layer(device2.hardware_id)
        assert layer is not None
        assert layer.always_grab_all is True

    def test_device_tab_confirm_delete_device_updates_runtime_and_stack(
        self, temp_config_dir, monkeypatch
    ):
        from gi.repository import Adw, Gtk

        from keymasq.common.models import ButtonDefinition, HardwareConfig
        from keymasq.gui.widgets import device_tab as device_tab_module
        from keymasq.gui.widgets.device_tab import DeviceTab

        class _HardwareManager:
            def __init__(self) -> None:
                self.deleted: list[str] = []

            def delete_hardware(self, hardware_id: str) -> None:
                self.deleted.append(hardware_id)

        class _ProfileManager:
            def __init__(self) -> None:
                self.removed: list[str] = []

            def list_profiles(self) -> list[object]:
                return []

            def remove_device_layers(self, hardware_id: str) -> None:
                self.removed.append(hardware_id)

        class _Stack:
            def __init__(self) -> None:
                self.removed: list[object] = []

            def remove(self, child: object) -> None:
                self.removed.append(child)

        class _Root:
            def __init__(self) -> None:
                self.stack = _Stack()
                self.checked = 0

            def _check_empty_state(self) -> None:
                self.checked += 1

        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Test Mouse",
            evdev_devices=[],
            buttons=[ButtonDefinition(id="btn_back", label="Back", evdev="btn_side")],
        )

        hardware_manager = _HardwareManager()
        profile_manager = _ProfileManager()
        tab = DeviceTab(
            device=device,
            profile_manager=profile_manager,
            hardware_manager=hardware_manager,
            demo_mode=True,
        )

        reload_requests: list[dict] = []

        def fake_session_request_async(payload, callback):
            reload_requests.append(payload)
            callback({"status": "ok"})

        monkeypatch.setattr(
            device_tab_module,
            "session_request_async",
            fake_session_request_async,
        )

        root = _Root()
        tab.get_root = lambda: root  # type: ignore[method-assign]
        dialog = Adw.Dialog()
        closed: list[bool] = []
        dialog.close = lambda: closed.append(True)  # type: ignore[method-assign]
        delete_profiles_check = Gtk.CheckButton()
        delete_profiles_check.set_active(True)

        tab._on_confirm_delete_device(Gtk.Button(), dialog, delete_profiles_check)

        assert profile_manager.removed == ["1234:5678"]
        assert hardware_manager.deleted == ["1234:5678"]
        assert reload_requests == [
            {"command": "release_device", "hardware_id": "1234:5678", "immediate": True},
            {"command": "reload"},
        ]
        assert root.stack.removed == [tab]
        assert root.checked == 1
        assert closed == [True]

    def test_device_tab_delete_keeps_config_when_release_fails(
        self, temp_config_dir, monkeypatch
    ):
        from gi.repository import Adw, Gtk

        from keymasq.common.models import ButtonDefinition, HardwareConfig
        from keymasq.gui.widgets import device_tab as device_tab_module
        from keymasq.gui.widgets.device_tab import DeviceTab

        class _HardwareManager:
            def __init__(self) -> None:
                self.deleted: list[str] = []

            def delete_hardware(self, hardware_id: str) -> None:
                self.deleted.append(hardware_id)

        class _ProfileManager:
            def __init__(self) -> None:
                self.removed: list[str] = []

            def list_profiles(self) -> list[object]:
                return []

            def remove_device_layers(self, hardware_id: str) -> None:
                self.removed.append(hardware_id)

        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Test Mouse",
            evdev_devices=[],
            buttons=[ButtonDefinition(id="btn_back", label="Back", evdev="btn_side")],
        )

        hardware_manager = _HardwareManager()
        profile_manager = _ProfileManager()
        tab = DeviceTab(
            device=device,
            profile_manager=profile_manager,
            hardware_manager=hardware_manager,
            demo_mode=True,
        )

        requests: list[dict] = []

        def fake_session_request_async(payload, callback):
            requests.append(payload)
            callback({"status": "error", "error": "release failed"})

        monkeypatch.setattr(
            device_tab_module,
            "session_request_async",
            fake_session_request_async,
        )

        dialog = Adw.Dialog()
        closed: list[bool] = []
        dialog.close = lambda: closed.append(True)  # type: ignore[method-assign]
        delete_profiles_check = Gtk.CheckButton()
        delete_profiles_check.set_active(True)
        delete_button = Gtk.Button()
        error_label = Gtk.Label()
        error_label.set_visible(False)

        tab._on_confirm_delete_device(
            delete_button,
            dialog,
            delete_profiles_check,
            error_label,
        )

        assert delete_button.get_sensitive() is True
        assert error_label.get_visible() is True
        assert "release failed" in error_label.get_label()
        assert profile_manager.removed == []
        assert hardware_manager.deleted == []
        assert requests == [
            {"command": "release_device", "hardware_id": "1234:5678", "immediate": True}
        ]
        assert closed == []

    def test_device_tab_rename_device_updates_hardware_runtime_and_header(
        self, temp_config_dir, monkeypatch
    ):
        from keymasq.common.models import ButtonDefinition, HardwareConfig
        from keymasq.gui.widgets import device_tab as device_tab_module
        from keymasq.gui.widgets.device_tab import DeviceTab
        from keymasq.session.hardware import HardwareManager

        class _MainWindow:
            def __init__(self) -> None:
                self.renamed: list[tuple[str, str]] = []

            def update_device_display_name(self, hardware_id: str, name: str) -> None:
                self.renamed.append((hardware_id, name))

        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Test Mouse",
            evdev_devices=[],
            buttons=[ButtonDefinition(id="btn_back", label="Back", evdev="btn_side")],
        )

        hardware_manager = HardwareManager()
        hardware_manager.save_hardware(device)
        main_window = _MainWindow()
        tab = DeviceTab(
            device=device,
            profile_manager=None,
            hardware_manager=hardware_manager,
            main_window=main_window,
            demo_mode=False,
        )

        reload_requests: list[dict] = []
        monkeypatch.setattr(
            device_tab_module,
            "session_request_async",
            lambda payload, callback: reload_requests.append(payload),
        )

        assert tab._rename_device("  Work Mouse  ") is True

        assert device.name == "Work Mouse"
        reloaded = HardwareManager().get_hardware(device.hardware_id)
        assert reloaded is not None
        assert reloaded.name == "Work Mouse"
        assert reload_requests == [{"command": "reload"}]
        assert tab.device_name_label.get_text() == "Work Mouse"
        assert tab.always_grab_check.get_title() == "Always grab Work Mouse"
        assert main_window.renamed == [("1234:5678", "Work Mouse")]

        assert tab._rename_device("   ") is False
        assert reload_requests == [{"command": "reload"}]

    def test_device_tab_rename_without_hardware_manager_does_not_mutate(
        self, monkeypatch
    ):
        from keymasq.common.models import ButtonDefinition, HardwareConfig
        from keymasq.gui.widgets import device_tab as device_tab_module
        from keymasq.gui.widgets.device_tab import DeviceTab

        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Test Mouse",
            evdev_devices=[],
            buttons=[ButtonDefinition(id="btn_back", label="Back", evdev="btn_side")],
        )
        requests: list[dict] = []
        monkeypatch.setattr(
            device_tab_module,
            "session_request_async",
            lambda payload, callback: requests.append(payload),
        )

        tab = DeviceTab(
            device=device,
            profile_manager=None,
            hardware_manager=None,
            demo_mode=False,
        )

        assert tab.device_name_label.get_tooltip_text() is None
        assert tab._rename_device("Work Mouse") is False
        assert device.name == "Test Mouse"
        assert tab.device_name_label.get_text() == "Test Mouse"
        assert requests == []

    def test_device_tab_delete_button_updates_hardware_profiles_and_ui(
        self, temp_config_dir, monkeypatch
    ):
        from gi.repository import Adw

        from keymasq.common.models import ButtonDefinition, HardwareConfig
        from keymasq.gui.widgets import device_tab as device_tab_module
        from keymasq.gui.widgets.device_tab import DeviceTab

        class _HardwareManager:
            def __init__(self) -> None:
                self.saved: list[HardwareConfig] = []

            def save_hardware(self, device: HardwareConfig) -> None:
                self.saved.append(device)

        class _ProfileManager:
            def __init__(self) -> None:
                self.removed: list[tuple[str, str]] = []

            def list_profiles(self) -> list[object]:
                return []

            def remove_device_button_mappings(self, hardware_id: str, button_id: str) -> None:
                self.removed.append((hardware_id, button_id))

        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Test Mouse",
            evdev_devices=[],
            buttons=[
                ButtonDefinition(id="btn_back", label="Back", evdev="btn_side"),
                ButtonDefinition(id="btn_forward", label="Forward", evdev="btn_extra"),
            ],
        )

        hardware_manager = _HardwareManager()
        profile_manager = _ProfileManager()
        tab = DeviceTab(
            device=device,
            profile_manager=profile_manager,
            hardware_manager=hardware_manager,
            demo_mode=False,
        )

        reload_requests: list[dict] = []
        monkeypatch.setattr(
            device_tab_module,
            "session_request_async",
            lambda payload, callback: reload_requests.append(payload),
        )

        reloaded: list[bool] = []
        tab._reload_ui = lambda: reloaded.append(True)  # type: ignore[method-assign]

        dialog = Adw.Dialog()
        closed: list[bool] = []
        dialog.close = lambda: closed.append(True)  # type: ignore[method-assign]

        tab._delete_button(device.buttons[0], dialog)

        assert [button.id for button in tab.device.buttons] == ["btn_forward"]
        assert hardware_manager.saved[-1].buttons == tab.device.buttons
        assert profile_manager.removed == [("1234:5678", "btn_back")]
        assert reload_requests == [{"command": "reload"}]
        assert reloaded == [True]
        assert closed == [True]

    def test_device_tab_button_click_routes_protected_profileless_and_edit_paths(
        self, temp_config_dir
    ):
        from gi.repository import Gdk

        from keymasq.common.models import (
            ButtonDefinition,
            DeviceProfileLayer,
            HardwareConfig,
            ProfileConfig,
        )
        from keymasq.gui.widgets.device_tab import DeviceTab
        from keymasq.session.profiles import ProfileManager

        class _Click:
            def __init__(self, button: int) -> None:
                self._button = button

            def get_current_button(self) -> int:
                return self._button

        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Test Mouse",
            evdev_devices=[],
            buttons=[
                ButtonDefinition(id="btn_left", label="Left Click", evdev="btn_left"),
                ButtonDefinition(id="btn_back", label="Back", evdev="btn_side"),
            ],
        )

        no_profile_protected_tab = DeviceTab(device=device, profile_manager=None, demo_mode=True)
        protected_no_profile_calls: list[str] = []
        no_profile_protected_tab._show_no_profile_dialog = (
            lambda: protected_no_profile_calls.append("no-profile")
        )

        no_profile_protected_tab._on_button_clicked(
            _Click(Gdk.BUTTON_PRIMARY), 1, 0, 0, device.buttons[0], True
        )
        no_profile_protected_tab._on_button_clicked(
            _Click(Gdk.BUTTON_SECONDARY), 1, 0, 0, device.buttons[1], False
        )

        assert protected_no_profile_calls == ["no-profile"]

        allowed_tab = DeviceTab(device=device, profile_manager=None, demo_mode=True)
        allowed_tab._selected_profile = SimpleNamespace()
        allowed_calls: list[str] = []
        allowed_tab._show_protected_remap_warning_dialog = lambda button: allowed_calls.append(
            f"warn:{button.id}"
        )
        allowed_tab._show_function_editor = lambda button: allowed_calls.append(f"edit:{button.id}")

        allowed_tab._on_button_clicked(_Click(Gdk.BUTTON_PRIMARY), 1, 0, 0, device.buttons[0], True)

        assert allowed_calls == ["warn:btn_left"]

        no_profile_tab = DeviceTab(device=device, profile_manager=None, demo_mode=True)
        no_profile_calls: list[str] = []
        no_profile_tab._show_no_profile_dialog = lambda: no_profile_calls.append("no-profile")
        no_profile_tab._show_function_editor = lambda button: no_profile_calls.append(button.id)

        no_profile_tab._on_button_clicked(
            _Click(Gdk.BUTTON_PRIMARY), 1, 0, 0, device.buttons[1], False
        )

        assert no_profile_calls == ["no-profile"]

        profile_manager = ProfileManager()
        profile_manager.save_profile(
            ProfileConfig(
                name="Gaming",
                enabled=True,
                is_permanent=True,
                device_layers={"1234:5678": DeviceProfileLayer(hardware_id="1234:5678")},
            )
        )
        selected_tab = DeviceTab(device=device, profile_manager=profile_manager, demo_mode=True)
        selected_tab.refresh_profiles(preferred_profile_name="Gaming", publish_selection=False)
        selected_calls: list[str] = []
        selected_tab._show_function_editor = lambda button: selected_calls.append(button.id)

        selected_tab._on_button_clicked(
            _Click(Gdk.BUTTON_PRIMARY), 1, 0, 0, device.buttons[1], False
        )

        assert selected_calls == ["btn_back"]

    def test_device_tab_protected_buttons_show_info_indicator(self):
        from gi.repository import Gtk

        from keymasq.common.models import ButtonDefinition, HardwareConfig
        from keymasq.gui.widgets.device_tab import DeviceTab

        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Test Mouse",
            evdev_devices=[],
            buttons=[ButtonDefinition(id="btn_left", label="Left Click", evdev="btn_left")],
        )

        tab = DeviceTab(device=device, profile_manager=None, demo_mode=True)
        button = tab._button_widgets["btn_left"]
        content = button.get_child()
        assert isinstance(content, Gtk.Box)
        header = content.get_first_child()
        assert isinstance(header, Gtk.Box)
        info_icon = header.get_last_child()

        assert isinstance(info_icon, Gtk.Image)
        assert info_icon.get_icon_name() == "help-about-symbolic"
        assert info_icon.get_pixel_size() == 10
        assert info_icon.has_css_class("protected-button-info-icon") is True

    def test_device_tab_add_button_is_unified_and_dialog_defaults_to_one(self, temp_config_dir):
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gtk

        from keymasq.common.models import (
            ButtonDefinition,
            DeviceProfileLayer,
            DeviceType,
            EvdevDevice,
            HardwareConfig,
            ProfileConfig,
        )
        from keymasq.gui.widgets.device_tab import DeviceTab
        from keymasq.session.profiles import ProfileManager

        profile_manager = ProfileManager()
        profile_manager.save_profile(
            ProfileConfig(
                name="Typing",
                enabled=True,
                is_permanent=True,
                device_layers={"1234:5678": DeviceProfileLayer(hardware_id="1234:5678")},
            )
        )

        keyboard_buttons = [
            ButtonDefinition(
                id=f"key_{chr(ord('a') + i)}",
                label=chr(ord("A") + i),
                evdev=f"key_{chr(ord('a') + i)}",
            )
            for i in range(40)
        ]
        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Keyboard",
            evdev_devices=[EvdevDevice(path="/dev/input/event0", device_type=DeviceType.KEYBOARD)],
            buttons=keyboard_buttons,
        )

        tab = DeviceTab(device=device, profile_manager=profile_manager, demo_mode=False)
        presented: list[Adw.Dialog] = []

        def monkeypatch_present(self, root) -> None:
            presented.append(self)

        original_present = Adw.Dialog.present
        Adw.Dialog.present = monkeypatch_present  # type: ignore[method-assign]
        try:
            tab.get_root = lambda: Gtk.Window()  # type: ignore[method-assign]
            tab._on_add_keys_clicked(None)
        finally:
            Adw.Dialog.present = original_present  # type: ignore[method-assign]

        learn_tile = tab._create_learn_tile()
        add_button_content = learn_tile.get_child()
        assert isinstance(add_button_content, Gtk.Box)
        add_button_icon = add_button_content.get_first_child()
        add_button_label = add_button_icon.get_next_sibling() if add_button_icon else None
        assert isinstance(add_button_icon, Gtk.Image)
        assert add_button_icon.get_icon_name() == "list-add-symbolic"
        assert isinstance(add_button_label, Gtk.Label)
        assert add_button_label.get_text() == "Learn Keys"
        assert (
            learn_tile.get_tooltip_text()
            == "Capture additional physical buttons or keys for this device"
        )
        assert not hasattr(tab, "add_keys_btn")
        assert not hasattr(tab, "listen_btn")
        assert len(presented) == 1

        dialog = presented[0]
        content = dialog.get_child()
        assert isinstance(content, Gtk.Box)
        dialog_children = []
        child = content.get_first_child()
        while child is not None:
            dialog_children.append(child)
            child = child.get_next_sibling()

        count_row = dialog_children[1]
        assert isinstance(count_row, Gtk.Box)
        row_children = []
        child = count_row.get_first_child()
        while child is not None:
            row_children.append(child)
            child = child.get_next_sibling()

        assert isinstance(row_children[0], Gtk.Label)
        assert row_children[0].get_label() == "Number of inputs:"
        assert isinstance(row_children[1], Gtk.SpinButton)
        assert int(row_children[1].get_value()) == 1

    def test_device_tab_add_keys_capture_read_handles_duplicates_and_finishes(
        self, temp_config_dir
    ):
        from gi.repository import Adw, Gtk

        from keymasq.common.models import ButtonDefinition, DeviceType, EvdevDevice, HardwareConfig

        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Keyboard",
            evdev_devices=[EvdevDevice(path="/dev/input/event0", device_type=DeviceType.KEYBOARD)],
            buttons=[
                ButtonDefinition(id="key_a", label="A", evdev="key_a")
            ]
            + [
                ButtonDefinition(id=f"key_{index}", label=f"Key {index}", evdev=f"key_{index}")
                for index in range(40)
            ],
        )

        status = Gtk.Label()
        dialog = Adw.Dialog()
        finished: list[str] = []
        flow = _make_add_inputs_flow(
            device,
            lambda result: (
                device.buttons.extend(result.buttons),
                device.evdev_devices.extend(result.evdev_devices),
                finished.append("finished"),
            ),
        )
        flow._capturing = True
        flow._capture_active_hardware_id = "1234:5678"
        flow._pending_ids = ["key_added_1"]

        assert flow._on_capture_read(None, status, dialog) is False
        assert status.get_text() == ""

        duplicate = {"status": "ok", "captured": {"evdev": "key_a", "source": "kbd"}}
        assert flow._on_capture_read(duplicate, status, dialog) is False
        assert "already exists" in status.get_text()

        unsupported = {"status": "ok", "captured": {"evdev": "abs_x", "source": "kbd"}}
        assert flow._on_capture_read(unsupported, status, dialog) is False
        assert "Unsupported input" in status.get_text()

        captured = {
            "status": "ok",
            "captured": {
                "evdev": "btn_side",
                "source": "mouse-if1",
                "stable_path": "/dev/input/by-id/test-mouse",
            },
        }
        assert flow._on_capture_read(captured, status, dialog) is False

        assert finished == ["finished"]
        assert device.buttons[-1].id == "btn_side"
        assert device.buttons[-1].type == "mouse"
        assert device.evdev_devices[-1].path == "/dev/input/by-id/test-mouse"
        assert device.evdev_devices[-1].device_type == DeviceType.MOUSE
        assert status.get_text() == "Captured btn_side (0 remaining)"

    def test_device_tab_add_inputs_dialog_requires_unlock_before_capture(self, temp_config_dir):
        from gi.repository import Adw, Gtk

        from keymasq.common.models import ButtonDefinition, HardwareConfig
        from keymasq.gui.widgets.device_tab import DeviceTab

        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Mouse",
            evdev_devices=[],
            buttons=[ButtonDefinition(id="btn_left", label="Left Click", evdev="btn_left")],
        )

        unlock_callbacks: list[object] = []
        root = SimpleNamespace(
            _recording_unlock_required=True,
            _recording_unlocked=False,
            _recording_refresh_owner=False,
            present_unlock_dialog=lambda on_success=None: unlock_callbacks.append(on_success),
        )

        tab = DeviceTab(device=device, profile_manager=None, demo_mode=False)
        tab.get_root = lambda: root  # type: ignore[method-assign]
        presented: list[Adw.Dialog] = []

        def monkeypatch_present(self, parent) -> None:
            presented.append(self)

        original_present = Adw.Dialog.present
        Adw.Dialog.present = monkeypatch_present  # type: ignore[method-assign]
        try:
            tab._on_add_keys_clicked(None)
        finally:
            Adw.Dialog.present = original_present  # type: ignore[method-assign]

        assert len(presented) == 1
        content = presented[0].get_child()
        assert isinstance(content, Gtk.Box)
        dialog_children: list[Gtk.Widget] = []
        child = content.get_first_child()
        while child is not None:
            dialog_children.append(child)
            child = child.get_next_sibling()

        privilege_status = dialog_children[2]
        button_row = dialog_children[4]
        assert isinstance(privilege_status, Gtk.Label)
        assert isinstance(button_row, Gtk.Box)

        row_children: list[Gtk.Widget] = []
        child = button_row.get_first_child()
        while child is not None:
            row_children.append(child)
            child = child.get_next_sibling()

        cancel_btn, unlock_btn, start_btn = row_children
        assert isinstance(cancel_btn, Gtk.Button)
        assert isinstance(unlock_btn, Gtk.Button)
        assert isinstance(start_btn, Gtk.Button)
        assert start_btn.get_sensitive() is False
        assert unlock_btn.get_visible() is True
        assert "Unlock to add additional keys and mouse buttons." in privilege_status.get_text()
        assert "raw original-input capture" in (unlock_btn.get_tooltip_text() or "")

        unlock_btn.emit("clicked")
        assert len(unlock_callbacks) == 1

        root._recording_unlocked = True
        root._recording_refresh_owner = True
        callback = unlock_callbacks[0]
        assert callable(callback)
        callback()

        assert start_btn.get_sensitive() is True
        assert unlock_btn.get_visible() is False
        assert "Add inputs reads raw key events before remapping." in privilege_status.get_text()

    def test_device_tab_finish_add_keys_reloads_session_runtime(
        self, temp_config_dir, monkeypatch
    ):
        from keymasq.common.models import ButtonDefinition, HardwareConfig
        from keymasq.gui.widgets import device_tab as device_tab_module
        from keymasq.gui.widgets.device_tab import DeviceTab
        from keymasq.gui.widgets.device_tab.add_inputs_flow import AddInputsResult

        class _HardwareManager:
            def __init__(self) -> None:
                self.saved: list[HardwareConfig] = []

            def save_hardware(self, device: HardwareConfig) -> None:
                self.saved.append(device)

        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Mouse",
            evdev_devices=[],
            buttons=[ButtonDefinition(id="wheel_down", label="Scroll Down", evdev="rel_wheel")],
        )
        hardware_manager = _HardwareManager()
        tab = DeviceTab(
            device=device,
            profile_manager=None,
            hardware_manager=hardware_manager,
            demo_mode=True,
        )
        reload_requests: list[dict] = []
        monkeypatch.setattr(
            device_tab_module,
            "session_request_async",
            lambda payload, callback: reload_requests.append(payload),
        )
        reloaded: list[bool] = []
        tab._reload_ui = lambda: reloaded.append(True)  # type: ignore[method-assign]

        added = ButtonDefinition(id="btn_side", label="Back", evdev="btn_side")
        tab._on_add_inputs_complete(AddInputsResult(buttons=[added], evdev_devices=[]))

        assert hardware_manager.saved == [device]
        assert reload_requests == [{"command": "reload"}]
        assert reloaded == [True]
        assert device.buttons[-1] == added

    def test_device_tab_hardware_settings_adds_evdev_devices_and_reloads(
        self, monkeypatch, temp_config_dir
    ):
        from keymasq.common.models import ButtonDefinition, DeviceType, EvdevDevice, HardwareConfig
        from keymasq.gui.widgets import device_tab as device_tab_module
        from keymasq.gui.widgets.device_tab import DeviceTab

        class _HardwareManager:
            def __init__(self) -> None:
                self.saved: list[HardwareConfig] = []

            def save_hardware(self, device: HardwareConfig) -> None:
                self.saved.append(device)

        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Mouse",
            evdev_devices=[
                EvdevDevice(
                    path="/dev/input/by-id/usb-Test-event-mouse",
                    device_type=DeviceType.MOUSE,
                    id="mouse",
                )
            ],
            buttons=[ButtonDefinition(id="btn_left", label="Left Click", evdev="btn_left")],
        )
        hardware_manager = _HardwareManager()
        tab = DeviceTab(
            device=device,
            profile_manager=None,
            hardware_manager=hardware_manager,
            demo_mode=False,
        )
        reload_requests: list[dict] = []
        monkeypatch.setattr(
            device_tab_module,
            "session_request_async",
            lambda payload, callback: reload_requests.append(payload),
        )

        added = tab._add_hardware_evdev_devices(
            [
                EvdevDevice(
                    path="/dev/input/by-id/usb-Test-if02-event-kbd",
                    device_type=DeviceType.KEYBOARD,
                    id="mouse",
                    phys="usb-test/input1",
                    capabilities=["key_a"],
                )
            ]
        )
        duplicate = tab._add_hardware_evdev_devices(
            [
                EvdevDevice(
                    path="/dev/input/by-id/usb-Test-if02-event-kbd",
                    device_type=DeviceType.KEYBOARD,
                    id="kbd",
                    phys="usb-test/input1",
                    capabilities=["key_a"],
                )
            ]
        )

        assert added == 1
        assert duplicate == 0
        assert hardware_manager.saved == [device]
        assert reload_requests == [{"command": "reload"}]
        assert [evdev.id for evdev in device.evdev_devices] == ["mouse", "mouse_2"]
        assert device.evdev_devices[-1].device_type == DeviceType.KEYBOARD
        assert "2 evdev" in tab._header_caption_label.get_text()

    def test_device_tab_hardware_settings_switches_evdev_detection_to_product_id(
        self, monkeypatch, temp_config_dir
    ):
        from keymasq.common.models import ButtonDefinition, DeviceType, EvdevDevice, HardwareConfig
        from keymasq.gui.widgets import device_tab as device_tab_module
        from keymasq.gui.widgets.device_tab import DeviceTab

        class _HardwareManager:
            def __init__(self, device: HardwareConfig) -> None:
                self.device = device
                self.saved: list[HardwareConfig] = []

            def list_hardware(self) -> list[HardwareConfig]:
                return [self.device]

            def save_hardware(self, device: HardwareConfig) -> None:
                self.saved.append(device)

        evdev_device = EvdevDevice(
            path="/dev/input/by-id/usb-Test-event-mouse",
            device_type=DeviceType.MOUSE,
            id="mouse",
        )
        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Mouse",
            evdev_devices=[evdev_device],
            buttons=[ButtonDefinition(id="btn_left", label="Left Click", evdev="btn_left")],
        )
        hardware_manager = _HardwareManager(device)
        tab = DeviceTab(
            device=device,
            profile_manager=None,
            hardware_manager=hardware_manager,
            demo_mode=False,
        )
        tab._device_runtime_status = {
            "interfaces": [
                {
                    "id": "mouse",
                    "configured_path": "/dev/input/by-id/usb-Test-event-mouse",
                    "stable_path": "/dev/input/by-id/usb-Test-event-mouse",
                    "phys": "usb-test/input0",
                    "capabilities": ["btn_left"],
                }
            ]
        }
        reload_requests: list[dict] = []
        monkeypatch.setattr(
            device_tab_module,
            "session_request_async",
            lambda payload, callback: reload_requests.append(payload),
        )

        ok, message = tab._set_hardware_evdev_detection_method(evdev_device, "product")

        assert ok is True
        assert message == "Switched event device to Product ID detection."
        assert evdev_device.path == "keymasq:1234:5678"
        assert evdev_device.phys == "usb-test/input0"
        assert evdev_device.capabilities == ["btn_left"]
        assert hardware_manager.saved == [device]
        assert reload_requests == [{"command": "reload"}]

    def test_device_tab_hardware_settings_denies_product_id_detection_conflict(
        self, monkeypatch, temp_config_dir
    ):
        from keymasq.common.models import ButtonDefinition, DeviceType, EvdevDevice, HardwareConfig
        from keymasq.gui.widgets import device_tab as device_tab_module
        from keymasq.gui.widgets.device_tab import DeviceTab

        class _HardwareManager:
            def __init__(self, devices: list[HardwareConfig]) -> None:
                self.devices = devices
                self.saved: list[HardwareConfig] = []

            def list_hardware(self) -> list[HardwareConfig]:
                return self.devices

            def save_hardware(self, device: HardwareConfig) -> None:
                self.saved.append(device)

        evdev_device = EvdevDevice(
            path="/dev/input/by-id/usb-Test-if02-event-kbd",
            device_type=DeviceType.KEYBOARD,
            id="kbd",
            capabilities=["key_a"],
        )
        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Keyboard",
            evdev_devices=[evdev_device],
            buttons=[ButtonDefinition(id="key_a", label="A", evdev="key_a")],
            id="1234:5678@2",
        )
        existing = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Mouse",
            evdev_devices=[
                EvdevDevice(
                    path="keymasq:1234:5678",
                    device_type=DeviceType.MOUSE,
                    id="mouse",
                )
            ],
            buttons=[],
        )
        hardware_manager = _HardwareManager([device, existing])
        tab = DeviceTab(
            device=device,
            profile_manager=None,
            hardware_manager=hardware_manager,
            demo_mode=False,
        )
        reload_requests: list[dict] = []
        monkeypatch.setattr(
            device_tab_module,
            "session_request_async",
            lambda payload, callback: reload_requests.append(payload),
        )

        ok, message = tab._set_hardware_evdev_detection_method(evdev_device, "product")

        assert ok is False
        assert message == "Product ID detection is already used by 1234:5678."
        assert evdev_device.path == "/dev/input/by-id/usb-Test-if02-event-kbd"
        assert hardware_manager.saved == []
        assert reload_requests == []

    def test_device_tab_hardware_settings_switches_evdev_detection_to_stable_path(
        self, monkeypatch, temp_config_dir
    ):
        from keymasq.common.models import ButtonDefinition, DeviceType, EvdevDevice, HardwareConfig
        from keymasq.gui.widgets import device_tab as device_tab_module
        from keymasq.gui.widgets.device_tab import DeviceTab

        class _HardwareManager:
            def __init__(self) -> None:
                self.saved: list[HardwareConfig] = []

            def save_hardware(self, device: HardwareConfig) -> None:
                self.saved.append(device)

        evdev_device = EvdevDevice(
            path="keymasq:1234:5678",
            device_type=DeviceType.GAMEPAD,
            id="gamepad",
            capabilities=["btn_south"],
        )
        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Gamepad",
            evdev_devices=[evdev_device],
            buttons=[ButtonDefinition(id="btn_south", label="A", evdev="btn_south")],
        )
        hardware_manager = _HardwareManager()
        tab = DeviceTab(
            device=device,
            profile_manager=None,
            hardware_manager=hardware_manager,
            demo_mode=False,
        )
        tab._device_runtime_status = {
            "interfaces": [
                {
                    "id": "gamepad",
                    "configured_path": "keymasq:1234:5678",
                    "stable_path": "/dev/input/by-id/usb-Test-event-joystick",
                }
            ]
        }
        reload_requests: list[dict] = []
        monkeypatch.setattr(
            device_tab_module,
            "session_request_async",
            lambda payload, callback: reload_requests.append(payload),
        )

        ok, message = tab._set_hardware_evdev_detection_method(evdev_device, "stable")

        assert ok is True
        assert message == "Switched event device to Stable Path detection."
        assert evdev_device.path == "/dev/input/by-id/usb-Test-event-joystick"
        assert hardware_manager.saved == [device]
        assert reload_requests == [{"command": "reload"}]

    def test_device_tab_hardware_settings_migrates_event_path_to_runtime_stable_path(
        self, monkeypatch, temp_config_dir
    ):
        from keymasq.common.models import ButtonDefinition, DeviceType, EvdevDevice, HardwareConfig
        from keymasq.gui.widgets import device_tab as device_tab_module
        from keymasq.gui.widgets.device_tab import DeviceTab

        class _HardwareManager:
            def __init__(self) -> None:
                self.saved: list[HardwareConfig] = []

            def save_hardware(self, device: HardwareConfig) -> None:
                self.saved.append(device)

        evdev_device = EvdevDevice(
            path="/dev/input/event10",
            device_type=DeviceType.MOUSE,
            id="mouse",
        )
        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Mouse",
            evdev_devices=[evdev_device],
            buttons=[ButtonDefinition(id="btn_left", label="Left Click", evdev="btn_left")],
        )
        hardware_manager = _HardwareManager()
        tab = DeviceTab(
            device=device,
            profile_manager=None,
            hardware_manager=hardware_manager,
            demo_mode=False,
        )
        tab._device_runtime_status = {
            "interfaces": [
                {
                    "id": "mouse",
                    "configured_path": "/dev/input/event10",
                    "current_path": "/dev/input/event10",
                    "stable_path": "/dev/input/by-id/usb-Test-event-mouse",
                }
            ]
        }
        reload_requests: list[dict] = []
        monkeypatch.setattr(
            device_tab_module,
            "session_request_async",
            lambda payload, callback: reload_requests.append(payload),
        )

        available, tooltip = tab._stable_detection_status_for_evdev_device(evdev_device)
        ok, message = tab._set_hardware_evdev_detection_method(evdev_device, "stable")

        assert available is True
        assert tooltip == "Switch this event device to its /dev/input/by-id path."
        assert ok is True
        assert message == "Switched event device to Stable Path detection."
        assert evdev_device.path == "/dev/input/by-id/usb-Test-event-mouse"
        assert hardware_manager.saved == [device]
        assert reload_requests == [{"command": "reload"}]

    def test_device_tab_hardware_settings_stable_detection_reports_missing_by_id(
        self, monkeypatch, temp_config_dir
    ):
        from keymasq.common.models import ButtonDefinition, DeviceType, EvdevDevice, HardwareConfig
        from keymasq.gui.widgets import device_tab as device_tab_module
        from keymasq.gui.widgets.device_tab import DeviceTab

        class _HardwareManager:
            def __init__(self) -> None:
                self.saved: list[HardwareConfig] = []

            def save_hardware(self, device: HardwareConfig) -> None:
                self.saved.append(device)

        evdev_device = EvdevDevice(
            path="keymasq:1234:5678",
            device_type=DeviceType.GAMEPAD,
            id="gamepad",
            capabilities=["btn_south"],
        )
        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Gamepad",
            evdev_devices=[evdev_device],
            buttons=[ButtonDefinition(id="btn_south", label="A", evdev="btn_south")],
        )
        hardware_manager = _HardwareManager()
        tab = DeviceTab(
            device=device,
            profile_manager=None,
            hardware_manager=hardware_manager,
            demo_mode=False,
        )
        tab._device_runtime_status = {
            "interfaces": [
                {
                    "id": "gamepad",
                    "configured_path": "keymasq:1234:5678",
                    "stable_path": "/dev/input/event10",
                }
            ]
        }
        reload_requests: list[dict] = []
        monkeypatch.setattr(
            device_tab_module,
            "session_request_async",
            lambda payload, callback: reload_requests.append(payload),
        )

        available, tooltip = tab._stable_detection_status_for_evdev_device(evdev_device)
        ok, message = tab._set_hardware_evdev_detection_method(evdev_device, "stable")

        assert available is False
        assert tooltip == (
            "Stable Path is unavailable because this event device has no "
            "/dev/input/by-id path."
        )
        assert ok is False
        assert message == tooltip
        assert evdev_device.path == "keymasq:1234:5678"
        assert hardware_manager.saved == []
        assert reload_requests == []

    def test_append_unique_evdev_devices_allows_logical_path_with_distinct_metadata(self):
        from keymasq.common.models import DeviceType, EvdevDevice, HardwareConfig
        from keymasq.gui.widgets.device_tab.hardware_settings_dialog import (
            append_unique_evdev_devices,
        )

        config = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Gamepad",
            evdev_devices=[
                EvdevDevice(
                    path="keymasq:1234:5678",
                    device_type=DeviceType.GAMEPAD,
                    id="gamepad",
                    phys="usb-test/input0",
                    capabilities=["btn_south"],
                )
            ],
            buttons=[],
        )

        added = append_unique_evdev_devices(
            config,
            [
                EvdevDevice(
                    path="keymasq:1234:5678",
                    device_type=DeviceType.GAMEPAD,
                    id="gamepad",
                    phys="usb-test/input0",
                    capabilities=["btn_south"],
                ),
                EvdevDevice(
                    path="keymasq:1234:5678",
                    device_type=DeviceType.GAMEPAD,
                    id="gamepad",
                    phys="usb-test/input1",
                    capabilities=["btn_east"],
                ),
            ],
        )

        assert added == 1
        assert [device.id for device in config.evdev_devices] == ["gamepad", "gamepad_2"]
        assert config.evdev_devices[-1].phys == "usb-test/input1"

    def test_append_unique_evdev_devices_treats_real_path_as_duplicate(self):
        from keymasq.common.models import DeviceType, EvdevDevice, HardwareConfig
        from keymasq.gui.widgets.device_tab.hardware_settings_dialog import (
            append_unique_evdev_devices,
        )

        config = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Mouse",
            evdev_devices=[
                EvdevDevice(
                    path="/dev/input/event10",
                    device_type=DeviceType.MOUSE,
                    id="mouse",
                )
            ],
            buttons=[],
        )

        added = append_unique_evdev_devices(
            config,
            [
                EvdevDevice(
                    path="/dev/input/event10",
                    device_type=DeviceType.MOUSE,
                    id="mouse",
                    phys="usb-test/input0",
                    capabilities=["btn_left"],
                )
            ],
        )

        assert added == 0
        assert len(config.evdev_devices) == 1

    def test_hardware_settings_identity_row_opens_rename(self):
        from collections.abc import Callable
        from typing import Any, cast

        from gi.repository import Gtk

        from keymasq.common.models import ButtonDefinition, DeviceType, EvdevDevice, HardwareConfig
        from keymasq.gui.widgets.device_tab.hardware_settings_dialog import (
            HardwareSettingsDialog,
        )
        from tests.gui.support import collect_widgets

        class _HardwareManager:
            pass

        class _Click:
            def __init__(self) -> None:
                self.states: list[Gtk.EventSequenceState] = []

            def set_state(self, state: Gtk.EventSequenceState) -> bool:
                self.states.append(state)
                return True

        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Mouse",
            evdev_devices=[
                EvdevDevice(
                    path="/dev/input/by-id/usb-Test-event-mouse",
                    device_type=DeviceType.MOUSE,
                    id="mouse",
                )
            ],
            buttons=[ButtonDefinition(id="btn_left", label="Left Click", evdev="btn_left")],
        )
        rename_callbacks: list[Callable[[], None]] = []

        def on_rename(callback: Callable[[], None]) -> None:
            rename_callbacks.append(callback)

        dialog = HardwareSettingsDialog(
            None,
            device,
            cast(Any, _HardwareManager()),
            lambda _devices: 0,
            lambda: None,
            lambda _device, _delete_profiles: True,
            lambda _device, _method: (True, ""),
            lambda _device: (
                True,
                "Match this event device by its /dev/input/by-id path.",
            ),
            on_rename,
            can_delete_profile_mappings=True,
        )
        row = dialog._identity_row
        assert row is not None
        click = _Click()
        toggle_box = dialog._detection_method_toggle_box(device.evdev_devices[0])
        toggle_labels = [
            toggle.get_label()
            for toggle in collect_widgets(toggle_box, Gtk.ToggleButton, include_self=True)
        ]
        content = dialog.get_child()
        assert content is not None
        docs_buttons = [
            button
            for button in collect_widgets(content, Gtk.Button, include_self=True)
            if button.get_label() == "?" and button.has_css_class("actions-docs-button")
        ]

        dialog._on_identity_row_activated(row)
        dialog._on_identity_row_right_clicked(
            cast(Gtk.GestureClick, click),
            1,
            0.0,
            0.0,
        )
        dialog._on_identity_row_right_clicked(
            cast(Gtk.GestureClick, _Click()),
            2,
            0.0,
            0.0,
        )

        assert len(rename_callbacks) == 2
        assert click.states == [Gtk.EventSequenceState.CLAIMED]
        assert "Stable" in toggle_labels
        assert "Product" in toggle_labels
        assert len(docs_buttons) == 1
        assert docs_buttons[0].get_tooltip_text() == "Open Hardware documentation"

    def test_hardware_settings_disables_stable_detection_when_unavailable(self):
        from typing import Any, cast

        from gi.repository import Gtk

        from keymasq.common.models import ButtonDefinition, DeviceType, EvdevDevice, HardwareConfig
        from keymasq.gui.widgets.device_tab.hardware_settings_dialog import (
            HardwareSettingsDialog,
        )
        from tests.gui.support import collect_widgets

        class _HardwareManager:
            pass

        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Gamepad",
            evdev_devices=[
                EvdevDevice(
                    path="keymasq:1234:5678",
                    device_type=DeviceType.GAMEPAD,
                    id="gamepad",
                )
            ],
            buttons=[ButtonDefinition(id="btn_south", label="A", evdev="btn_south")],
        )
        stable_tooltip = (
            "Stable Path is unavailable because this event device has no "
            "/dev/input/by-id path."
        )
        dialog = HardwareSettingsDialog(
            None,
            device,
            cast(Any, _HardwareManager()),
            lambda _devices: 0,
            lambda: None,
            lambda _device, _delete_profiles: True,
            lambda _device, _method: (True, ""),
            lambda _device: (False, stable_tooltip),
            lambda _callback: None,
            can_delete_profile_mappings=True,
        )

        toggle_box = dialog._detection_method_toggle_box(device.evdev_devices[0])
        toggles = {
            toggle.get_label(): toggle
            for toggle in collect_widgets(toggle_box, Gtk.ToggleButton, include_self=True)
        }

        assert toggles["Stable"].get_sensitive() is False
        assert toggles["Stable"].get_tooltip_text() == stable_tooltip
        assert toggles["Product"].get_active() is True
        assert toggle_box.get_tooltip_text() == stable_tooltip

    def test_hardware_settings_refreshes_stable_detection_after_runtime_update(self):
        from typing import Any, cast

        from gi.repository import Gtk

        from keymasq.common.models import ButtonDefinition, DeviceType, EvdevDevice, HardwareConfig
        from keymasq.gui.widgets.device_tab import DeviceTab
        from keymasq.gui.widgets.device_tab.hardware_settings_dialog import (
            HardwareSettingsDialog,
        )
        from tests.gui.support import collect_widgets

        class _HardwareManager:
            pass

        evdev_device = EvdevDevice(
            path="keymasq:1234:5678",
            device_type=DeviceType.GAMEPAD,
            id="gamepad",
        )
        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Gamepad",
            evdev_devices=[evdev_device],
            buttons=[ButtonDefinition(id="btn_south", label="A", evdev="btn_south")],
        )
        tab = DeviceTab(
            device=device,
            profile_manager=None,
            hardware_manager=cast(Any, _HardwareManager()),
            demo_mode=False,
        )
        tab._device_runtime_status = {
            "interfaces": [
                {
                    "id": "gamepad",
                    "configured_path": "keymasq:1234:5678",
                    "stable_path": "/dev/input/event10",
                }
            ]
        }
        stable_tooltip = (
            "Stable Path is unavailable because this event device has no "
            "/dev/input/by-id path."
        )
        dialog = HardwareSettingsDialog(
            None,
            device,
            cast(Any, _HardwareManager()),
            lambda _devices: 0,
            lambda: None,
            lambda _device, _delete_profiles: True,
            lambda _device, _method: (True, ""),
            tab._stable_detection_status_for_evdev_device,
            lambda _callback: None,
            can_delete_profile_mappings=True,
        )
        tab._hardware_settings_dialog = dialog

        def dialog_toggles() -> dict[str, Gtk.ToggleButton]:
            content = dialog.get_child()
            assert content is not None
            return {
                toggle.get_label(): toggle
                for toggle in collect_widgets(content, Gtk.ToggleButton, include_self=True)
            }

        initial_toggles = dialog_toggles()
        assert initial_toggles["Stable"].get_sensitive() is False
        assert initial_toggles["Stable"].get_tooltip_text() == stable_tooltip

        tab.apply_active_profile_response(
            {
                "active_profiles": [],
                "devices": {
                    device.hardware_id: {
                        "profiles": [],
                        "device_status": {
                            "interfaces": [
                                {
                                    "id": "gamepad",
                                    "configured_path": "keymasq:1234:5678",
                                    "stable_path": "/dev/input/by-id/usb-Test-event-joystick",
                                }
                            ]
                        },
                    }
                },
            }
        )

        refreshed_toggles = dialog_toggles()
        assert refreshed_toggles["Stable"].get_sensitive() is True
        assert (
            refreshed_toggles["Stable"].get_tooltip_text()
            == "Switch this event device to its /dev/input/by-id path."
        )
        assert refreshed_toggles["Product"].get_active() is True

    def test_device_tab_hardware_settings_deletes_evdev_device_controls_and_mappings(
        self, monkeypatch, temp_config_dir
    ):
        from keymasq.common.models import (
            AnalogAxisDefinition,
            AnalogInputDefinition,
            ButtonDefinition,
            DeviceType,
            EvdevDevice,
            HardwareConfig,
        )
        from keymasq.gui.widgets import device_tab as device_tab_module
        from keymasq.gui.widgets.device_tab import DeviceTab

        class _HardwareManager:
            def __init__(self) -> None:
                self.saved: list[HardwareConfig] = []

            def save_hardware(self, device: HardwareConfig) -> None:
                self.saved.append(device)

        class _ProfileManager:
            def __init__(self) -> None:
                self.removed: list[tuple[str, str]] = []

            def list_profiles(self) -> list[object]:
                return []

            def remove_device_button_mappings(self, hardware_id: str, button_id: str) -> None:
                self.removed.append((hardware_id, button_id))

        keyboard_iface = EvdevDevice(
            path="/dev/input/by-id/usb-Test-if02-event-kbd",
            device_type=DeviceType.KEYBOARD,
            id="kbd",
            phys="usb-test/input1",
        )
        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Mouse Keyboard",
            evdev_devices=[
                EvdevDevice(
                    path="/dev/input/by-id/usb-Test-event-mouse",
                    device_type=DeviceType.MOUSE,
                    id="mouse",
                ),
                keyboard_iface,
            ],
            buttons=[
                ButtonDefinition(
                    id="btn_left",
                    label="Left Click",
                    evdev="btn_left",
                    source="mouse",
                ),
                ButtonDefinition(
                    id="key_a",
                    label="A",
                    evdev="key_a",
                    source="kbd",
                ),
            ],
            analog_inputs=[
                AnalogInputDefinition(
                    id="left_stick",
                    label="Left Stick",
                    type="stick",
                    source="kbd",
                    axes=[AnalogAxisDefinition(role="x", evdev="abs_x")],
                )
            ],
        )
        hardware_manager = _HardwareManager()
        profile_manager = _ProfileManager()
        tab = DeviceTab(
            device=device,
            profile_manager=profile_manager,
            hardware_manager=hardware_manager,
            demo_mode=False,
        )
        reload_requests: list[dict] = []
        monkeypatch.setattr(
            device_tab_module,
            "session_request_async",
            lambda payload, callback: reload_requests.append(payload),
        )
        reloaded: list[bool] = []
        tab._reload_ui = lambda: reloaded.append(True)  # type: ignore[method-assign]

        deleted = tab._delete_hardware_evdev_device(
            keyboard_iface,
            delete_profile_mappings=True,
        )

        assert deleted is True
        assert [evdev.id for evdev in device.evdev_devices] == ["mouse"]
        assert [button.id for button in device.buttons] == ["btn_left"]
        assert device.analog_inputs == []
        assert profile_manager.removed == [
            ("1234:5678", "key_a"),
            ("1234:5678", "left_stick"),
        ]
        assert hardware_manager.saved == [device]
        assert reload_requests == [{"command": "reload"}]
        assert reloaded == [True]

    def test_device_tab_add_keys_capture_read_accepts_wheel_input(self, temp_config_dir):
        from gi.repository import Adw, Gtk

        import evdev

        from keymasq.common.models import ButtonDefinition, HardwareConfig

        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Mouse",
            evdev_devices=[],
            buttons=[ButtonDefinition(id="btn_left", label="Left Click", evdev="btn_left")],
        )

        status = Gtk.Label()
        dialog = Adw.Dialog()
        finished: list[str] = []
        flow = _make_add_inputs_flow(
            device,
            lambda result: (device.buttons.extend(result.buttons), finished.append("finished")),
        )
        flow._capturing = True
        flow._capture_active_hardware_id = "1234:5678"
        flow._pending_ids = ["added_1"]

        captured = {
            "status": "ok",
            "captured": {
                "evdev": "rel_wheel",
                "code": evdev.ecodes.REL_WHEEL,
                "direction": "down",
                "value": -1,
                "source": "mouse",
            },
        }
        assert flow._on_capture_read(captured, status, dialog) is False

        assert finished == ["finished"]
        assert len(device.buttons) == 2
        assert device.buttons[-1].id == "wheel_down"
        assert device.buttons[-1].label == "Scroll Down"
        assert device.buttons[-1].evdev == "rel_wheel"
        assert device.buttons[-1].evdev_code == evdev.ecodes.REL_WHEEL
        assert device.buttons[-1].evdev_value == -1
        assert device.buttons[-1].type == "wheel"
        assert status.get_text() == "Captured Scroll Down (0 remaining)"

    def test_device_tab_add_keys_allows_opposite_wheel_direction(self, temp_config_dir):
        from gi.repository import Adw, Gtk

        import evdev

        from keymasq.common.models import ButtonDefinition, HardwareConfig

        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Mouse",
            evdev_devices=[],
            buttons=[
                ButtonDefinition(
                    id="wheel_up",
                    label="Scroll Up",
                    evdev="rel_wheel",
                    evdev_code=evdev.ecodes.REL_WHEEL,
                    evdev_value=1,
                )
            ],
        )

        status = Gtk.Label()
        dialog = Adw.Dialog()
        finished: list[str] = []
        flow = _make_add_inputs_flow(
            device,
            lambda result: (device.buttons.extend(result.buttons), finished.append("finished")),
        )
        flow._capturing = True
        flow._capture_active_hardware_id = "1234:5678"
        flow._pending_ids = ["added_1"]

        captured = {
            "status": "ok",
            "captured": {
                "evdev": "rel_wheel",
                "code": evdev.ecodes.REL_WHEEL,
                "direction": "down",
                "value": -1,
                "source": "mouse",
            },
        }
        assert flow._on_capture_read(captured, status, dialog) is False

        assert finished == ["finished"]
        assert [button.id for button in device.buttons] == ["wheel_up", "wheel_down"]

    def test_device_tab_add_keys_rejects_duplicate_wheel_direction(self, temp_config_dir):
        from gi.repository import Adw, Gtk

        import evdev

        from keymasq.common.models import ButtonDefinition, HardwareConfig

        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Mouse",
            evdev_devices=[],
            buttons=[
                ButtonDefinition(
                    id="wheel_down",
                    label="Scroll Down",
                    evdev="rel_wheel",
                    evdev_code=evdev.ecodes.REL_WHEEL,
                    evdev_value=-1,
                )
            ],
        )

        status = Gtk.Label()
        dialog = Adw.Dialog()
        flow = _make_add_inputs_flow(device)
        flow._capturing = True
        flow._capture_active_hardware_id = "1234:5678"
        flow._pending_ids = ["added_1"]

        captured = {
            "status": "ok",
            "captured": {
                "evdev": "rel_wheel",
                "code": evdev.ecodes.REL_WHEEL,
                "direction": "down",
                "value": -1,
                "source": "mouse",
            },
        }
        assert flow._on_capture_read(captured, status, dialog) is False

        assert len(device.buttons) == 1
        assert "already exists" in status.get_text()

    def test_device_tab_duplicate_key_esc_cancels_capture(self, temp_config_dir):
        from gi.repository import Adw, Gtk

        from keymasq.common.models import ButtonDefinition, DeviceType, EvdevDevice, HardwareConfig

        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Keyboard",
            evdev_devices=[EvdevDevice(path="/dev/input/event0", device_type=DeviceType.KEYBOARD)],
            buttons=[ButtonDefinition(id="key_esc", label="Esc", evdev="key_esc")]
            + [
                ButtonDefinition(id=f"key_{index}", label=f"Key {index}", evdev=f"key_{index}")
                for index in range(40)
            ],
        )

        status = Gtk.Label()
        dialog = Adw.Dialog()
        stopped: list[str] = []
        closed: list[str] = []
        dialog.close = lambda: closed.append("closed")  # type: ignore[method-assign]
        flow = _make_add_inputs_flow(device)
        flow.stop_capture = lambda: stopped.append("stopped")  # type: ignore[method-assign]
        flow._capturing = True
        flow._capture_active_hardware_id = "1234:5678"
        flow._pending_ids = ["key_added_1"]

        duplicate_esc = {"status": "ok", "captured": {"evdev": "key_esc", "source": "kbd"}}
        assert flow._on_capture_read(duplicate_esc, status, dialog) is False

        assert stopped == ["stopped"]
        assert closed == ["closed"]
        assert "already exists" in status.get_text()

    def test_mouse_device_tab_add_inputs_accepts_keyboard_keys(self, temp_config_dir):
        from gi.repository import Adw, Gtk

        from keymasq.common.models import ButtonDefinition, DeviceType, EvdevDevice, HardwareConfig

        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Mouse",
            evdev_devices=[EvdevDevice(path="/dev/input/event0", device_type=DeviceType.MOUSE)],
            buttons=[ButtonDefinition(id="btn_left", label="Left Click", evdev="btn_left")],
        )

        status = Gtk.Label()
        dialog = Adw.Dialog()
        finished: list[str] = []
        flow = _make_add_inputs_flow(
            device,
            lambda result: (
                device.buttons.extend(result.buttons),
                device.evdev_devices.extend(result.evdev_devices),
                finished.append("finished"),
            ),
        )
        flow._capturing = True
        flow._capture_active_hardware_id = "1234:5678"
        flow._pending_ids = ["input_added_1"]

        captured = {
            "status": "ok",
            "captured": {
                "evdev": "key_space",
                "source": "kbd-if1",
                "stable_path": "/dev/input/by-id/test-kbd",
            },
        }
        assert flow._on_capture_read(captured, status, dialog) is False

        assert finished == ["finished"]
        assert device.buttons[-1].id == "key_space"
        assert device.buttons[-1].type == "key"
        assert device.evdev_devices[-1].path == "/dev/input/by-id/test-kbd"
        assert device.evdev_devices[-1].device_type == DeviceType.KEYBOARD
        assert status.get_text() == "Captured key_space (0 remaining)"

    def test_device_tab_add_inputs_escape_closes_dialog_and_stops_capture(self, temp_config_dir):
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gdk, Gtk

        from keymasq.common.models import ButtonDefinition, HardwareConfig

        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Mouse",
            evdev_devices=[],
            buttons=[ButtonDefinition(id="btn_left", label="Left Click", evdev="btn_left")],
        )

        dialog = Adw.Dialog()
        closed: list[str] = []
        stopped: list[str] = []
        dialog.close = lambda: closed.append("closed")  # type: ignore[method-assign]
        flow = _make_add_inputs_flow(device)
        flow.stop_capture = lambda: stopped.append("stopped")  # type: ignore[method-assign]

        assert (
            flow._on_key_pressed(
                Gtk.EventControllerKey(),
                Gdk.KEY_Escape,
                0,
                Gdk.ModifierType(0),
                dialog,
            )
            is True
        )

        assert stopped == ["stopped"]
        assert closed == ["closed"]

    def test_device_tab_add_inputs_dialog_closed_stops_capture_and_removes_controller(
        self, temp_config_dir
    ):
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gtk

        from keymasq.common.models import ButtonDefinition, HardwareConfig

        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Mouse",
            evdev_devices=[],
            buttons=[ButtonDefinition(id="btn_left", label="Left Click", evdev="btn_left")],
        )

        root = Gtk.Window()
        added: list[object] = []
        removed: list[object] = []

        original_add_controller = root.add_controller
        original_remove_controller = root.remove_controller

        def add_controller(controller: object) -> None:
            added.append(controller)
            original_add_controller(controller)

        def remove_controller(controller: object) -> None:
            removed.append(controller)
            original_remove_controller(controller)

        root.add_controller = add_controller  # type: ignore[method-assign]
        root.remove_controller = remove_controller  # type: ignore[method-assign]
        dialog = Adw.Dialog()
        stopped: list[str] = []
        flow = _make_add_inputs_flow(device, parent=root)
        flow.stop_capture = lambda: stopped.append("stopped")  # type: ignore[method-assign]

        flow._install_escape_controller(dialog)
        flow._dialog = dialog
        controller = flow._escape_controller

        flow._on_dialog_closed(dialog)

        assert controller is not None
        assert added == [controller]
        assert removed == [controller]
        assert stopped == ["stopped"]
        assert flow._dialog is None

    def test_gamepad_device_tab_add_buttons_capture_sets_gamepad_type(self, temp_config_dir):
        from gi.repository import Adw, Gtk

        from keymasq.common.models import ButtonDefinition, DeviceType, EvdevDevice, HardwareConfig

        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Gamepad",
            evdev_devices=[
                EvdevDevice(
                    path="/dev/input/event0",
                    device_type=DeviceType.GAMEPAD,
                    id="joystick",
                )
            ],
            buttons=[
                ButtonDefinition(
                    id="btn_south",
                    label="A",
                    evdev="btn_south",
                    source="joystick",
                )
            ],
        )

        status = Gtk.Label()
        dialog = Adw.Dialog()
        finished: list[str] = []
        flow = _make_add_inputs_flow(
            device,
            lambda result: (device.buttons.extend(result.buttons), finished.append("finished")),
        )
        flow._capturing = True
        flow._capture_active_hardware_id = "1234:5678"
        flow._pending_ids = ["btn_added_1"]

        captured = {
            "status": "ok",
            "captured": {
                "evdev": "btn_tr",
                "code": 311,
                "source": "joystick",
                "stable_path": "/dev/input/by-id/test-gamepad",
            },
        }
        assert flow._on_capture_read(captured, status, dialog) is False

        assert finished == ["finished"]
        assert device.buttons[-1].id == "btn_tr"
        assert device.buttons[-1].type == "gamepad"
        assert device.buttons[-1].evdev_code == 311
        assert device.evdev_devices == [
            EvdevDevice(
                path="/dev/input/event0",
                device_type=DeviceType.GAMEPAD,
                id="joystick",
            )
        ]
        assert status.get_text() == "Captured btn_tr (0 remaining)"

    def test_gamepad_device_tab_rejects_alias_duplicate_by_code(self, temp_config_dir):
        from gi.repository import Adw, Gtk

        from keymasq.common.models import ButtonDefinition, DeviceType, EvdevDevice, HardwareConfig

        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Gamepad",
            evdev_devices=[
                EvdevDevice(
                    path="/dev/input/event0",
                    device_type=DeviceType.GAMEPAD,
                    id="joystick",
                )
            ],
            buttons=[
                ButtonDefinition(
                    id="btn_south",
                    label="A",
                    evdev="btn_south",
                    evdev_code=304,
                    source="joystick",
                )
            ],
        )

        status = Gtk.Label()
        dialog = Adw.Dialog()
        flow = _make_add_inputs_flow(device)
        flow._capturing = True
        flow._capture_active_hardware_id = "1234:5678"
        flow._pending_ids = ["btn_added_1"]

        captured = {
            "status": "ok",
            "captured": {
                "evdev": "btn_a",
                "code": 304,
                "source": "joystick",
                "stable_path": "/dev/input/by-id/test-gamepad",
            },
        }
        assert flow._on_capture_read(captured, status, dialog) is False

        assert len(device.buttons) == 1
        assert "already exists" in status.get_text()
