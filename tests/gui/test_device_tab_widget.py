# ruff: noqa: F403, F405, I001
from tests.gui.support import *

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
        tab = window.stack.get_page(window.stack.get_child_by_name(device1.hardware_id)).get_child()
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
        monkeypatch.setattr(
            device_tab_module,
            "session_request_async",
            lambda payload, callback: reload_requests.append(payload),
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
        assert reload_requests == [{"command": "reload"}]
        assert root.stack.removed == [tab]
        assert root.checked == 1
        assert closed == [True]

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
        from keymasq.gui.widgets.device_tab import DeviceTab

        class _HardwareManager:
            def __init__(self) -> None:
                self.saved: list[HardwareConfig] = []

            def save_hardware(self, device: HardwareConfig) -> None:
                self.saved.append(device)

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

        hardware_manager = _HardwareManager()
        tab = DeviceTab(
            device=device,
            profile_manager=None,
            hardware_manager=hardware_manager,
            demo_mode=True,
        )
        status = Gtk.Label()
        dialog = Adw.Dialog()
        finished: list[str] = []
        tab._finish_add_keys = lambda parent_dialog: finished.append("finished")
        stopped: list[str] = []
        tab._stop_add_keys_capture = lambda: stopped.append("stopped")
        tab._add_keys_capturing = True
        tab._capture_active_hardware_id = "1234:5678"
        tab._add_keys_pending_ids = ["key_added_1"]

        assert tab._on_add_keys_capture_read(None, status, dialog) is False
        assert status.get_text() == ""

        duplicate = {"status": "ok", "captured": {"evdev": "key_a", "source": "kbd"}}
        assert tab._on_add_keys_capture_read(duplicate, status, dialog) is False
        assert "already exists" in status.get_text()

        unsupported = {"status": "ok", "captured": {"evdev": "abs_x", "source": "kbd"}}
        assert tab._on_add_keys_capture_read(unsupported, status, dialog) is False
        assert "Unsupported input" in status.get_text()

        captured = {
            "status": "ok",
            "captured": {
                "evdev": "btn_side",
                "source": "mouse-if1",
                "stable_path": "/dev/input/by-id/test-mouse",
            },
        }
        assert tab._on_add_keys_capture_read(captured, status, dialog) is False

        assert finished == ["finished"]
        assert stopped == []
        assert tab.device.buttons[-1].id == "btn_side"
        assert tab.device.buttons[-1].type == "mouse"
        assert tab.device.evdev_devices[-1].path == "/dev/input/by-id/test-mouse"
        assert tab.device.evdev_devices[-1].device_type == DeviceType.MOUSE
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
        from gi.repository import Adw

        from keymasq.common.models import ButtonDefinition, HardwareConfig
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
        stopped: list[bool] = []
        tab._stop_add_keys_capture = lambda: stopped.append(True)  # type: ignore[method-assign]
        dialog = Adw.Dialog()
        closed: list[bool] = []
        dialog.close = lambda: closed.append(True)  # type: ignore[method-assign]

        tab._finish_add_keys(dialog)

        assert stopped == [True]
        assert hardware_manager.saved == [device]
        assert reload_requests == [{"command": "reload"}]
        assert closed == [True]
        assert reloaded == [True]

    def test_device_tab_add_keys_capture_read_accepts_wheel_input(self, temp_config_dir):
        from gi.repository import Adw, Gtk

        import evdev

        from keymasq.common.models import ButtonDefinition, HardwareConfig
        from keymasq.gui.widgets.device_tab import DeviceTab

        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Mouse",
            evdev_devices=[],
            buttons=[ButtonDefinition(id="btn_left", label="Left Click", evdev="btn_left")],
        )

        tab = DeviceTab(device=device, profile_manager=None, demo_mode=True)
        status = Gtk.Label()
        dialog = Adw.Dialog()
        tab._add_keys_capturing = True
        tab._capture_active_hardware_id = "1234:5678"
        tab._add_keys_pending_ids = ["added_1"]
        finished: list[str] = []
        tab._finish_add_keys = lambda parent_dialog: finished.append("finished")

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
        assert tab._on_add_keys_capture_read(captured, status, dialog) is False

        assert finished == ["finished"]
        assert len(tab.device.buttons) == 2
        assert tab.device.buttons[-1].id == "wheel_down"
        assert tab.device.buttons[-1].label == "Scroll Down"
        assert tab.device.buttons[-1].evdev == "rel_wheel"
        assert tab.device.buttons[-1].evdev_code == evdev.ecodes.REL_WHEEL
        assert tab.device.buttons[-1].evdev_value == -1
        assert tab.device.buttons[-1].type == "wheel"
        assert status.get_text() == "Captured Scroll Down (0 remaining)"

    def test_device_tab_add_keys_allows_opposite_wheel_direction(self, temp_config_dir):
        from gi.repository import Adw, Gtk

        import evdev

        from keymasq.common.models import ButtonDefinition, HardwareConfig
        from keymasq.gui.widgets.device_tab import DeviceTab

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

        tab = DeviceTab(device=device, profile_manager=None, demo_mode=True)
        status = Gtk.Label()
        dialog = Adw.Dialog()
        finished: list[str] = []
        tab._finish_add_keys = lambda parent_dialog: finished.append("finished")
        tab._add_keys_capturing = True
        tab._capture_active_hardware_id = "1234:5678"
        tab._add_keys_pending_ids = ["added_1"]

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
        assert tab._on_add_keys_capture_read(captured, status, dialog) is False

        assert finished == ["finished"]
        assert [button.id for button in tab.device.buttons] == ["wheel_up", "wheel_down"]

    def test_device_tab_add_keys_rejects_duplicate_wheel_direction(self, temp_config_dir):
        from gi.repository import Adw, Gtk

        import evdev

        from keymasq.common.models import ButtonDefinition, HardwareConfig
        from keymasq.gui.widgets.device_tab import DeviceTab

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

        tab = DeviceTab(device=device, profile_manager=None, demo_mode=True)
        status = Gtk.Label()
        dialog = Adw.Dialog()
        tab._add_keys_capturing = True
        tab._capture_active_hardware_id = "1234:5678"
        tab._add_keys_pending_ids = ["added_1"]

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
        assert tab._on_add_keys_capture_read(captured, status, dialog) is False

        assert len(tab.device.buttons) == 1
        assert "already exists" in status.get_text()

    def test_device_tab_duplicate_key_esc_cancels_capture(self, temp_config_dir):
        from gi.repository import Adw, Gtk

        from keymasq.common.models import ButtonDefinition, DeviceType, EvdevDevice, HardwareConfig
        from keymasq.gui.widgets.device_tab import DeviceTab

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

        tab = DeviceTab(device=device, profile_manager=None, demo_mode=True)
        status = Gtk.Label()
        dialog = Adw.Dialog()
        stopped: list[str] = []
        closed: list[str] = []
        tab._stop_add_keys_capture = lambda: stopped.append("stopped")
        dialog.close = lambda: closed.append("closed")  # type: ignore[method-assign]
        tab._add_keys_capturing = True
        tab._capture_active_hardware_id = "1234:5678"
        tab._add_keys_pending_ids = ["key_added_1"]

        duplicate_esc = {"status": "ok", "captured": {"evdev": "key_esc", "source": "kbd"}}
        assert tab._on_add_keys_capture_read(duplicate_esc, status, dialog) is False

        assert stopped == ["stopped"]
        assert closed == ["closed"]
        assert "already exists" in status.get_text()

    def test_mouse_device_tab_add_inputs_accepts_keyboard_keys(self, temp_config_dir):
        from gi.repository import Adw, Gtk

        from keymasq.common.models import ButtonDefinition, DeviceType, EvdevDevice, HardwareConfig
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
            evdev_devices=[EvdevDevice(path="/dev/input/event0", device_type=DeviceType.MOUSE)],
            buttons=[ButtonDefinition(id="btn_left", label="Left Click", evdev="btn_left")],
        )

        tab = DeviceTab(
            device=device,
            profile_manager=None,
            hardware_manager=_HardwareManager(),
            demo_mode=True,
        )
        status = Gtk.Label()
        dialog = Adw.Dialog()
        finished: list[str] = []
        tab._finish_add_keys = lambda parent_dialog: finished.append("finished")
        tab._add_keys_capturing = True
        tab._capture_active_hardware_id = "1234:5678"
        tab._add_keys_pending_ids = ["input_added_1"]

        captured = {
            "status": "ok",
            "captured": {
                "evdev": "key_space",
                "source": "kbd-if1",
                "stable_path": "/dev/input/by-id/test-kbd",
            },
        }
        assert tab._on_add_keys_capture_read(captured, status, dialog) is False

        assert finished == ["finished"]
        assert tab.device.buttons[-1].id == "key_space"
        assert tab.device.buttons[-1].type == "key"
        assert tab.device.evdev_devices[-1].path == "/dev/input/by-id/test-kbd"
        assert tab.device.evdev_devices[-1].device_type == DeviceType.KEYBOARD
        assert status.get_text() == "Captured key_space (0 remaining)"

    def test_device_tab_add_inputs_escape_closes_dialog_and_stops_capture(self, temp_config_dir):
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gdk, Gtk

        from keymasq.common.models import ButtonDefinition, HardwareConfig
        from keymasq.gui.widgets.device_tab import DeviceTab

        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Mouse",
            evdev_devices=[],
            buttons=[ButtonDefinition(id="btn_left", label="Left Click", evdev="btn_left")],
        )

        tab = DeviceTab(device=device, profile_manager=None, demo_mode=True)
        dialog = Adw.Dialog()
        closed: list[str] = []
        stopped: list[str] = []
        dialog.close = lambda: closed.append("closed")  # type: ignore[method-assign]
        tab._stop_add_keys_capture = lambda: stopped.append("stopped")

        assert (
            tab._on_add_inputs_key_pressed(
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
        from keymasq.gui.widgets.device_tab import DeviceTab

        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Mouse",
            evdev_devices=[],
            buttons=[ButtonDefinition(id="btn_left", label="Left Click", evdev="btn_left")],
        )

        tab = DeviceTab(device=device, profile_manager=None, demo_mode=True)
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
        tab.get_root = lambda: root  # type: ignore[method-assign]
        dialog = Adw.Dialog()
        stopped: list[str] = []
        tab._stop_add_keys_capture = lambda: stopped.append("stopped")

        tab._install_add_inputs_escape_controller(dialog)
        tab._add_inputs_dialog = dialog
        controller = tab._add_inputs_escape_controller

        tab._on_add_inputs_dialog_closed(dialog)

        assert controller is not None
        assert added == [controller]
        assert removed == [controller]
        assert stopped == ["stopped"]
        assert tab._add_inputs_dialog is None

    def test_gamepad_device_tab_add_buttons_capture_sets_gamepad_type(self, temp_config_dir):
        from gi.repository import Adw, Gtk

        from keymasq.common.models import ButtonDefinition, DeviceType, EvdevDevice, HardwareConfig
        from keymasq.gui.widgets.device_tab import DeviceTab

        class _HardwareManager:
            def __init__(self) -> None:
                self.saved: list[HardwareConfig] = []

            def save_hardware(self, device: HardwareConfig) -> None:
                self.saved.append(device)

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

        tab = DeviceTab(
            device=device,
            profile_manager=None,
            hardware_manager=_HardwareManager(),
            demo_mode=True,
        )
        status = Gtk.Label()
        dialog = Adw.Dialog()
        finished: list[str] = []
        tab._finish_add_keys = lambda parent_dialog: finished.append("finished")
        tab._add_keys_capturing = True
        tab._capture_active_hardware_id = "1234:5678"
        tab._add_keys_pending_ids = ["btn_added_1"]

        captured = {
            "status": "ok",
            "captured": {
                "evdev": "btn_tr",
                "code": 311,
                "source": "joystick",
                "stable_path": "/dev/input/by-id/test-gamepad",
            },
        }
        assert tab._on_add_keys_capture_read(captured, status, dialog) is False

        assert finished == ["finished"]
        assert tab.device.buttons[-1].id == "btn_tr"
        assert tab.device.buttons[-1].type == "gamepad"
        assert tab.device.buttons[-1].evdev_code == 311
        assert tab.device.evdev_devices == [
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
        from keymasq.gui.widgets.device_tab import DeviceTab

        class _HardwareManager:
            def save_hardware(self, device: HardwareConfig) -> None:
                return

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

        tab = DeviceTab(
            device=device,
            profile_manager=None,
            hardware_manager=_HardwareManager(),
            demo_mode=True,
        )
        status = Gtk.Label()
        dialog = Adw.Dialog()
        tab._add_keys_capturing = True
        tab._capture_active_hardware_id = "1234:5678"
        tab._add_keys_pending_ids = ["btn_added_1"]

        captured = {
            "status": "ok",
            "captured": {
                "evdev": "btn_a",
                "code": 304,
                "source": "joystick",
                "stable_path": "/dev/input/by-id/test-gamepad",
            },
        }
        assert tab._on_add_keys_capture_read(captured, status, dialog) is False

        assert len(tab.device.buttons) == 1
        assert "already exists" in status.get_text()
