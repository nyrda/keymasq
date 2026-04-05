from types import SimpleNamespace

import pytest

gi = pytest.importorskip("gi")


class TestDemoDevice:
    def test_demo_device_creation(self):
        from keyforge.common.models import ButtonDefinition, DeviceType, EvdevDevice, HardwareConfig

        demo_device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Demo Mouse",
            evdev_devices=[
                EvdevDevice(path="/dev/input/event0", device_type=DeviceType.MOUSE),
            ],
            buttons=[
                ButtonDefinition(id="btn_left", label="Left Click", evdev="btn_left", zone="left"),
                ButtonDefinition(
                    id="btn_right", label="Right Click", evdev="btn_right", zone="right"
                ),
            ],
        )

        assert demo_device.name == "Demo Mouse"
        assert demo_device.hardware_id == "1234:5678"
        assert len(demo_device.buttons) == 2

    def test_demo_profile_mapping(self):
        from keyforge.common.models import (
            ActionType,
            DeviceProfileLayer,
            MappingAction,
            ProfileConfig,
        )

        profile = ProfileConfig(
            name="Demo Gaming Profile",
            enabled=True,
            device_layers={
                "1234:5678": DeviceProfileLayer(
                    hardware_id="1234:5678",
                    mappings={
                        "btn_back": MappingAction(action_type=ActionType.KEYBOARD, target="key_1"),
                        "btn_forward": MappingAction(
                            action_type=ActionType.KEYBOARD, target="key_2"
                        ),
                    },
                )
            },
        )

        assert profile.name == "Demo Gaming Profile"
        assert "btn_back" in profile.device_layers["1234:5678"].mappings
        assert profile.device_layers["1234:5678"].mappings["btn_back"].target == "key_1"


class TestRecordMacroDialog:
    def test_record_dialog_uses_unlock_and_owner_state(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keyforge.gui.widgets.record_macro_dialog import RecordMacroDialog

        monkeypatch.setattr(RecordMacroDialog, "_load_initial_state_async", lambda self: None)

        dialog = RecordMacroDialog(Gtk.Window())

        dialog._apply_unlock_state(
            {
                "status": "ok",
                "recording_unlocked": False,
                "recording_refresh_owner": False,
            }
        )
        assert dialog._unlock_btn.get_visible() is True
        assert dialog._unlock_btn.get_label() == "Unlock"
        assert dialog._unlock_status.get_label() == "Unlock required"

        dialog._apply_unlock_state(
            {
                "status": "ok",
                "recording_unlocked": True,
                "recording_refresh_owner": False,
            }
        )
        assert dialog._unlock_btn.get_visible() is True
        assert dialog._unlock_btn.get_label() == "Claim Unlock"
        assert dialog._unlock_status.get_label() == "Unlock active in another session"

        dialog._apply_unlock_state(
            {
                "status": "ok",
                "recording_unlocked": True,
                "recording_refresh_owner": True,
            }
        )
        assert dialog._unlock_btn.get_visible() is False
        assert dialog._unlock_status.get_label() == "Unlock active"


class TestDeviceTabWidget:
    def test_device_tab_creation(self):
        from keyforge.common.models import ButtonDefinition, HardwareConfig
        from keyforge.gui.widgets.device_tab import DeviceTab

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

    def test_device_tab_initial_profile_selection(self, temp_config_dir):
        from keyforge.common.models import (
            ButtonDefinition,
            DeviceProfileLayer,
            HardwareConfig,
            ProfileConfig,
        )
        from keyforge.gui.widgets.device_tab import DeviceTab
        from keyforge.session.profiles import ProfileManager

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

    def test_device_tab_refresh_profiles_picks_up_new_global_profile(self, temp_config_dir):
        from keyforge.common.models import (
            ButtonDefinition,
            DeviceProfileLayer,
            HardwareConfig,
            ProfileConfig,
        )
        from keyforge.gui.widgets.device_tab import DeviceTab
        from keyforge.session.profiles import ProfileManager

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
        from keyforge.common.models import (
            ActionType,
            ButtonDefinition,
            DeviceProfileLayer,
            HardwareConfig,
            MappingAction,
            ProfileConfig,
        )
        from keyforge.gui.widgets.device_tab import DeviceTab
        from keyforge.session.profiles import ProfileManager

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
        from keyforge.common.models import (
            ButtonDefinition,
            DeviceProfileLayer,
            HardwareConfig,
            ProfileConfig,
        )
        from keyforge.gui.widgets.device_tab import DeviceTab
        from keyforge.session.profiles import ProfileManager

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

    def test_device_tab_confirm_delete_device_updates_runtime_and_stack(
        self, temp_config_dir, monkeypatch
    ):
        from gi.repository import Adw, Gtk

        from keyforge.common.models import ButtonDefinition, HardwareConfig
        from keyforge.gui.widgets import device_tab as device_tab_module
        from keyforge.gui.widgets.device_tab import DeviceTab

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

    def test_device_tab_button_click_routes_protected_profileless_and_edit_paths(
        self, temp_config_dir
    ):
        from gi.repository import Gdk

        from keyforge.common.models import (
            ButtonDefinition,
            DeviceProfileLayer,
            HardwareConfig,
            ProfileConfig,
        )
        from keyforge.gui.widgets.device_tab import DeviceTab
        from keyforge.session.profiles import ProfileManager

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

        protected_tab = DeviceTab(device=device, profile_manager=None, demo_mode=True)
        protected_calls: list[str] = []
        protected_tab._show_protected_dialog = lambda button: protected_calls.append(button.id)
        protected_tab._show_no_profile_dialog = lambda: protected_calls.append("no-profile")
        protected_tab._show_function_editor = lambda button: protected_calls.append(
            f"edit:{button.id}"
        )

        protected_tab._on_button_clicked(
            _Click(Gdk.BUTTON_PRIMARY), 1, 0, 0, device.buttons[0], True
        )
        protected_tab._on_button_clicked(
            _Click(Gdk.BUTTON_SECONDARY), 1, 0, 0, device.buttons[1], False
        )

        assert protected_calls == ["btn_left"]

        allowed_tab = DeviceTab(
            device=device,
            profile_manager=None,
            demo_mode=True,
            main_window=SimpleNamespace(left_right_click_remap_allowed=lambda: True),
        )
        allowed_tab._selected_profile = SimpleNamespace()
        allowed_calls: list[str] = []
        allowed_tab._show_protected_dialog = lambda button: allowed_calls.append(
            f"blocked:{button.id}"
        )
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

    def test_device_tab_listen_toggle_and_reload_ui_remove_key_controller(self, temp_config_dir):
        from keyforge.common.models import (
            ButtonDefinition,
            DeviceProfileLayer,
            DeviceType,
            EvdevDevice,
            HardwareConfig,
            ProfileConfig,
        )
        from keyforge.gui.widgets.device_tab import DeviceTab
        from keyforge.session.profiles import ProfileManager

        class _Root:
            def __init__(self) -> None:
                self.added: list[object] = []
                self.removed: list[object] = []

            def add_controller(self, controller: object) -> None:
                self.added.append(controller)

            def remove_controller(self, controller: object) -> None:
                self.removed.append(controller)

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
        root = _Root()
        tab.get_root = lambda: root  # type: ignore[method-assign]

        tab.listen_btn.set_active(True)
        tab._on_listen_toggled(tab.listen_btn)
        controller = tab._listen_controller

        assert tab.listen_btn.get_label() == "Listening..."
        assert controller is not None
        assert root.added == [controller]

        tab.listen_btn.set_active(False)
        tab._on_listen_toggled(tab.listen_btn)

        assert tab._listen_controller is None
        assert root.removed == [controller]

        tab.listen_btn.set_active(True)
        tab._on_listen_toggled(tab.listen_btn)
        controller = tab._listen_controller

        tab._reload_ui()

        assert controller is not None
        assert controller in root.removed
        assert tab._listening_keys is False
        assert not hasattr(tab, "listen_btn") or tab.listen_btn.get_label() == "Listen Keys"

    def test_device_tab_add_keys_capture_read_handles_duplicates_and_finishes(
        self, temp_config_dir
    ):
        from gi.repository import Adw, Gtk

        from keyforge.common.models import ButtonDefinition, DeviceType, EvdevDevice, HardwareConfig
        from keyforge.gui.widgets.device_tab import DeviceTab

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

    def test_gamepad_device_tab_add_buttons_capture_sets_gamepad_type(self, temp_config_dir):
        from gi.repository import Adw, Gtk

        from keyforge.common.models import ButtonDefinition, DeviceType, EvdevDevice, HardwareConfig
        from keyforge.gui.widgets.device_tab import DeviceTab

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

        from keyforge.common.models import ButtonDefinition, DeviceType, EvdevDevice, HardwareConfig
        from keyforge.gui.widgets.device_tab import DeviceTab

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


class TestHardwareSetupDialog:
    def test_refresh_configure_modes_offers_gamepad_first(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from keyforge.gui.wizards.hardware_setup import HardwareSetupDialog

        monkeypatch.setattr(HardwareSetupDialog, "_detect_devices", lambda self: None)

        dialog = HardwareSetupDialog(Gtk.Window(), SimpleNamespace())
        dialog.selected_device = {
            "interfaces": [
                {
                    "device_type": "gamepad",
                    "device_types": ["gamepad"],
                },
                {
                    "device_type": "keyboard",
                    "device_types": ["keyboard"],
                },
            ]
        }

        dialog._refresh_configure_modes()

        assert dialog._configure_mode_values == ["gamepad", "keyboard"]
        assert dialog._configure_mode == "gamepad"
        assert dialog.describe_subtitle.get_label() == "Review the detected controller controls"

    def test_save_gamepad_config_builds_buttons_from_capabilities(self, monkeypatch):
        gi.require_version("Gtk", "4.0")
        import evdev
        from gi.repository import Gtk

        from keyforge.common.models import DeviceType
        from keyforge.gui.wizards.hardware_setup import HardwareSetupDialog

        class _HardwareManager:
            def __init__(self) -> None:
                self.saved = []

            def save_hardware(self, config) -> None:
                self.saved.append(config)

        monkeypatch.setattr(HardwareSetupDialog, "_detect_devices", lambda self: None)

        hardware_manager = _HardwareManager()
        dialog = HardwareSetupDialog(Gtk.Window(), hardware_manager)
        dialog.selected_device = {
            "vendor_id": "1234",
            "product_id": "5678",
            "name": "Test Pad",
        }
        dialog.discovered_interfaces = {
            "joystick": {
                "id": "joystick",
                "stable_path": "/dev/input/by-id/test-pad",
                "path": "/dev/input/event10",
                "name": "Test Pad",
                "device_type": DeviceType.GAMEPAD,
                "device_types": ["gamepad"],
                "capabilities": ["btn_start", "btn_south", "btn_east"],
                "raw_capabilities": {
                    evdev.ecodes.EV_KEY: [
                        evdev.ecodes.BTN_START,
                        evdev.ecodes.BTN_SOUTH,
                        evdev.ecodes.BTN_EAST,
                    ]
                },
            }
        }
        emitted = []
        dialog.emit = lambda signal, config: emitted.append((signal, config))
        dialog.close = lambda: None

        dialog._save_gamepad_config()

        assert len(hardware_manager.saved) == 1
        saved = hardware_manager.saved[0]
        assert saved.evdev_devices[0].device_type == DeviceType.GAMEPAD
        assert [button.id for button in saved.buttons] == ["btn_start", "btn_east", "btn_south"]
        assert [button.label for button in saved.buttons] == ["Start", "B", "A"]
        assert [button.evdev_code for button in saved.buttons] == [
            evdev.ecodes.BTN_START,
            evdev.ecodes.BTN_EAST,
            evdev.ecodes.BTN_SOUTH,
        ]
        assert all(button.type == "gamepad" for button in saved.buttons)
        assert emitted == [("device-created", saved)]


def test_notify_session_reload_returns_false_without_shell_fallback(monkeypatch):
    from keyforge.gui import session_reload

    monkeypatch.setattr(session_reload, "session_request", lambda payload, timeout=5.0: None)

    assert session_reload.notify_session_reload(timeout=0.1) is False


def test_resolve_keyforge_record_helper_path(tmp_path, monkeypatch):
    from keyforge.common import paths

    helper = tmp_path / "keyforge-record"
    helper.write_text("#!/bin/sh\n")
    helper.chmod(0o755)
    monkeypatch.setattr(paths, "KEYFORGE_RECORD_HELPER_PATH", helper)

    assert paths.resolve_keyforge_record_helper_path() == str(helper)


def test_device_tab_builds_captured_window_rules():
    from keyforge.common.models import ButtonDefinition, HardwareConfig
    from keyforge.gui.widgets.device_tab import DeviceTab

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
    from keyforge.common.models import ButtonDefinition, HardwareConfig, WindowRule
    from keyforge.gui.widgets.device_tab import DeviceTab

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
    from keyforge.common.models import (
        ButtonDefinition,
        DeviceProfileLayer,
        HardwareConfig,
        ProfileConfig,
    )
    from keyforge.gui.widgets.device_tab import DeviceTab
    from keyforge.session.profiles import ProfileManager

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


def test_device_tab_explicit_passthrough_is_shown_as_active_mask(temp_config_dir):
    from keyforge.common.models import (
        ActionType,
        ButtonDefinition,
        DeviceProfileLayer,
        HardwareConfig,
        MappingAction,
        ProfileConfig,
    )
    from keyforge.gui.widgets.device_tab import DeviceTab
    from keyforge.session.profiles import ProfileManager

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
    from keyforge.common.models import (
        ButtonDefinition,
        DeviceProfileLayer,
        HardwareConfig,
        ProfileConfig,
    )
    from keyforge.gui.widgets.device_tab import DeviceTab
    from keyforge.session.profiles import ProfileManager

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
    from keyforge.common.models import (
        ButtonDefinition,
        DeviceProfileLayer,
        HardwareConfig,
        ProfileConfig,
    )
    from keyforge.gui.widgets.device_tab import DeviceTab
    from keyforge.session.profiles import ProfileManager

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
    assert gaming.config.window_rules == []


def test_describe_mapping_action_compact_includes_runtime_markers():
    from keyforge.common.models import ActionType, MappingAction
    from keyforge.gui.widgets.action_labels import describe_mapping_action_compact

    action = MappingAction(
        action_type=ActionType.KEYBOARD,
        target="key_a",
        rapidfire_enabled=True,
        tap_enabled=True,
    )

    assert describe_mapping_action_compact(action, include_state=True) == "→ key_a ⚡ ↓"


def test_key_selector_dialog_distinguishes_explicit_passthrough_and_no_override():
    from gi.repository import Gtk

    from keyforge.common.models import ActionType, MappingAction
    from keyforge.gui.widgets.key_selector_dialog import KeySelectorDialog

    explicit_results: list[MappingAction | None] = []
    explicit_dialog = KeySelectorDialog(Gtk.Box(), "Back")
    explicit_dialog.connect("key-selected", lambda _dialog, action: explicit_results.append(action))
    explicit_dialog._on_special_clicked(None, "explicit_passthrough")

    assert len(explicit_results) == 1
    assert isinstance(explicit_results[0], MappingAction)
    assert explicit_results[0].action_type == ActionType.PASSTHROUGH

    clear_results: list[MappingAction | None] = []
    clear_dialog = KeySelectorDialog(Gtk.Box(), "Back")
    clear_dialog.connect("key-selected", lambda _dialog, action: clear_results.append(action))
    clear_dialog._on_special_clicked(None, "clear_mapping")

    assert clear_results == [None]


def test_key_selector_dialog_keyboard_mapping_uses_rapidfire_or_tap_state():
    from gi.repository import Gtk

    from keyforge.common.models import ActionType, MappingAction
    from keyforge.gui.widgets.key_selector_dialog import KeySelectorDialog

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


def test_key_selector_dialog_map_code_handles_valid_and_invalid_input():
    from gi.repository import Gtk

    from keyforge.common.models import ActionType, MappingAction
    from keyforge.gui.widgets.key_selector_dialog import KeySelectorDialog

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


def test_key_selector_dialog_profile_tab_populates_and_maps_selected_action(monkeypatch):
    from gi.repository import Gtk

    from keyforge.common.models import ActionType, MappingAction
    from keyforge.gui.widgets import key_selector_dialog as dialog_module
    from keyforge.gui.widgets.key_selector_dialog import KeySelectorDialog

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


def test_key_selector_dialog_only_shows_hyprland_dispatch_for_active_hyprland_listener():
    from gi.repository import Gtk

    from keyforge.common.models import ActionType, MappingAction
    from keyforge.gui.widgets.key_selector_dialog import KeySelectorDialog

    active_dialog = KeySelectorDialog(
        Gtk.Box(),
        "Back",
        compositor_action_status={
            "listener_name": "hyprland",
            "compositor_dispatch_available": True,
        },
    )
    assert active_dialog.stack.get_child_by_name("hyprland") is not None

    hidden_dialog = KeySelectorDialog(
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
    assert hidden_dialog.stack.get_child_by_name("hyprland") is None


def test_key_selector_dialog_shows_gnome_dispatch_for_active_gnome_listener():
    from gi.repository import Gtk

    from keyforge.common.models import ActionType, MappingAction
    from keyforge.gui.widgets.key_selector_dialog import KeySelectorDialog

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


def test_key_selector_dialog_mouse_capture_and_move_mapping_paths(monkeypatch):
    from gi.repository import Gtk

    from keyforge.common.models import ActionType, MappingAction
    from keyforge.gui.widgets import key_selector_dialog as dialog_module
    from keyforge.gui.widgets.key_selector_dialog import KeySelectorDialog

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

    dialog.mouse_move_abs_check.set_active(True)
    dialog._on_mouse_move_mode_changed(dialog.mouse_move_abs_check)
    dialog._on_capture_position_clicked(Gtk.Button())

    assert dialog.mouse_move_x_spin.get_value_as_int() == 640
    assert dialog.mouse_move_y_spin.get_value_as_int() == 480
    assert dialog.mouse_move_capture_status.get_text() == "Captured: 640, 480"

    dialog._on_mouse_move_map_clicked(None)

    assert len(results) == 1
    assert results[0].action_type == ActionType.MOUSE_MOVE_ABS
    assert results[0].move_x == 640
    assert results[0].move_y == 480

    error_dialog = KeySelectorDialog(Gtk.Box(), "Back")
    error_dialog._on_capture_position_response(
        {"status": "error", "message": "Unknown command: get_cursor_position"}
    )

    assert (
        error_dialog.mouse_move_capture_status.get_text()
        == "Please restart Keyforge Session, then try again"
    )


def test_shared_navigation_picker_builds_dropdown():
    from gi.repository import Gtk

    from keyforge.gui.widgets.input_picker_shared import build_navigation_tab

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


class TestMainWindow:
    def test_main_window_demo_mode(self, temp_config_dir):
        from keyforge.common.models import (
            ButtonDefinition,
            DeviceProfileLayer,
            HardwareConfig,
            ProfileConfig,
        )
        from keyforge.gui.window import MainWindow

        window = MainWindow(demo_mode=True)

        assert window.demo_mode is True
        assert window.hardware_manager is not None
        assert window.profile_manager is not None

        window.profile_manager.save_profile(
            ProfileConfig(
                name="Profile 1",
                enabled=True,
                is_permanent=True,
                device_layers={"1234:5678": DeviceProfileLayer(hardware_id="1234:5678")},
            )
        )
        window.profile_manager.save_profile(
            ProfileConfig(
                name="Profile 2",
                enabled=True,
                is_permanent=True,
                device_layers={
                    "1234:5678": DeviceProfileLayer(hardware_id="1234:5678"),
                    "1234:5679": DeviceProfileLayer(hardware_id="1234:5679"),
                },
            )
        )

        device1 = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Mouse One",
            evdev_devices=[],
            buttons=[ButtonDefinition(id="btn_back", label="Back", evdev="btn_side")],
        )
        device2 = HardwareConfig(
            vendor_id="1234",
            product_id="5679",
            name="Mouse Two",
            evdev_devices=[],
            buttons=[ButtonDefinition(id="btn_back", label="Back", evdev="btn_side")],
        )

        window._add_device_tab(device1)
        window._add_device_tab(device2)

        tab1 = window.stack.get_page(
            window.stack.get_child_by_name(device1.hardware_id)
        ).get_child()
        tab2 = window.stack.get_page(
            window.stack.get_child_by_name(device2.hardware_id)
        ).get_child()

        tab1.refresh_profiles(preferred_profile_name="Profile 2")
        window.stack.set_visible_child(tab2)

        assert tab2._selected_profile is not None
        assert tab2._selected_profile.config.name == "Profile 2"

    def test_main_window_syncs_manual_profile_selection_across_tabs(self, temp_config_dir):
        from keyforge.common.models import (
            ButtonDefinition,
            DeviceProfileLayer,
            HardwareConfig,
            ProfileConfig,
        )
        from keyforge.gui.window import MainWindow

        window = MainWindow(demo_mode=True)
        window.profile_manager.save_profile(
            ProfileConfig(
                name="Desktop",
                enabled=True,
                is_permanent=True,
                device_layers={
                    "2234:6678": DeviceProfileLayer(hardware_id="2234:6678"),
                    "2234:6679": DeviceProfileLayer(hardware_id="2234:6679"),
                },
            )
        )
        window.profile_manager.save_profile(
            ProfileConfig(
                name="Gaming",
                enabled=True,
                is_permanent=True,
                device_layers={
                    "2234:6678": DeviceProfileLayer(hardware_id="2234:6678"),
                    "2234:6679": DeviceProfileLayer(hardware_id="2234:6679"),
                },
            )
        )

        device1 = HardwareConfig(
            vendor_id="2234",
            product_id="6678",
            name="Mouse One",
            evdev_devices=[],
            buttons=[ButtonDefinition(id="btn_back", label="Back", evdev="btn_side")],
        )
        device2 = HardwareConfig(
            vendor_id="2234",
            product_id="6679",
            name="Mouse Two",
            evdev_devices=[],
            buttons=[ButtonDefinition(id="btn_back", label="Back", evdev="btn_side")],
        )

        window._add_device_tab(device1)
        window._add_device_tab(device2)

        tab1 = window.stack.get_page(
            window.stack.get_child_by_name(device1.hardware_id)
        ).get_child()
        tab2 = window.stack.get_page(
            window.stack.get_child_by_name(device2.hardware_id)
        ).get_child()

        tab1.profile_dropdown.set_selected(tab1._profile_names.index("Desktop"))

        assert window._selected_profile_name == "Desktop"
        assert tab1._selected_profile is not None
        assert tab1._selected_profile.config.name == "Desktop"
        assert tab2._selected_profile is not None
        assert tab2._selected_profile.config.name == "Desktop"

    def test_main_window_startup_probe_applies_compositor_state_and_devices(self, temp_config_dir):
        from keyforge.common.models import (
            ButtonDefinition,
            DeviceProfileLayer,
            HardwareConfig,
            ProfileConfig,
            WindowRule,
        )
        from keyforge.gui.window import MainWindow

        window = MainWindow(demo_mode=True)
        window.profile_manager.save_profile(
            ProfileConfig(
                name="Desktop",
                enabled=True,
                is_permanent=False,
                window_rules=[WindowRule(field="tag", pattern="work")],
                device_layers={"2234:6678": DeviceProfileLayer(hardware_id="2234:6678")},
            )
        )
        window._selected_profile_name = "Desktop"

        device = HardwareConfig(
            vendor_id="2234",
            product_id="6678",
            name="Mouse One",
            evdev_devices=[],
            buttons=[ButtonDefinition(id="btn_back", label="Back", evdev="btn_side")],
        )

        finished = window._on_startup_probe_finished(
            (
                {
                    "compositor_id": "hyprland",
                    "support_details": {"supported": True, "warning": ""},
                    "supported": True,
                    "capabilities": ["window_tags"],
                },
                [device],
            )
        )

        device_tab = window.stack.get_page(
            window.stack.get_child_by_name(device.hardware_id)
        ).get_child()

        assert finished is False
        assert window._startup_probe_done is True
        assert window._compositor_id == "hyprland"
        assert window._compositor_capabilities == ["window_tags"]
        assert window.compositor_status.get_label() == "compositor: 🟢 Hyprland"
        assert window.combo_tab is not None
        assert window.combo_tab._compositor_capabilities == ["window_tags"]
        assert window.combo_tab._selected_profile is not None
        assert window.combo_tab._selected_profile.config.name == "Desktop"
        assert window.combo_tab.status_label.get_text() == "waiting"
        assert device_tab._selected_profile is not None
        assert device_tab._selected_profile.config.name == "Desktop"

    def test_main_window_shows_warning_banner_even_when_compositor_supported(self):
        from keyforge.gui.window import MainWindow

        window = MainWindow(demo_mode=True)
        window.demo_mode = False
        window._startup_probe_done = True
        window._compositor_id = "gnome"
        window._compositor_supported = True
        window._compositor_support_details = {
            "supported": True,
            "warning": "GNOME bridge update detected. Log out and back in.",
        }

        window._update_compositor_warning_banner()

        assert window.warning_banner.get_revealed() is True
        assert "Log out and back in" in window.warning_banner.get_title()

    def test_main_window_apply_loaded_devices_updates_empty_state_and_demo_devices(
        self, temp_config_dir
    ):
        from keyforge.gui.window import MainWindow

        window = MainWindow(demo_mode=False)

        window._apply_loaded_devices([])

        assert window._placeholder_title is not None
        assert window._placeholder_subtitle is not None
        assert window._placeholder_title.get_label() == "No devices configured"
        assert window._placeholder_subtitle.get_label() == "Unlock capture to add a new device"

        demo_window = MainWindow(demo_mode=True)
        demo_window._apply_loaded_devices([])

        demo_tab = demo_window.stack.get_page(
            demo_window.stack.get_child_by_name("1234:5678")
        ).get_child()

        assert demo_window.placeholder not in demo_window.stack
        assert demo_tab.device.name == "Demo Mouse"

    def test_main_window_status_response_updates_labels_for_all_status_paths(
        self, temp_config_dir, monkeypatch
    ):
        from keyforge.gui.window import MainWindow

        window = MainWindow(demo_mode=True)
        issues: list[str | None] = []
        unlock_updates: list[dict | None] = []

        monkeypatch.setattr(window, "_set_connection_issue", lambda issue: issues.append(issue))
        monkeypatch.setattr(
            window,
            "_update_unlock_state",
            lambda data: unlock_updates.append(data),
        )

        window._status_query_id = 3
        window._status_query_inflight = True
        assert window._on_status_response({"status": "ok", "keyforged_connected": True}, 2) is False
        assert window._status_query_inflight is True
        assert unlock_updates == []
        assert issues == []

        assert window._on_status_response({"status": "ok", "keyforged_connected": True}, 3) is False
        assert window.session_status.get_label() == "session: 🟢"
        assert window.keyforged_status.get_label() == "keyforged: 🟢"
        assert unlock_updates[-1] == {"status": "ok", "keyforged_connected": True}
        assert issues[-1] is None

        window._status_query_inflight = True
        assert (
            window._on_status_response({"status": "ok", "keyforged_connected": False}, 3) is False
        )
        assert window.session_status.get_label() == "session: 🟡"
        assert window.keyforged_status.get_label() == "keyforged: 🔴"
        assert issues[-1] == "keyforged"

        window._status_query_inflight = True
        assert window._on_status_response(None, 3) is False
        assert window.session_status.get_label() == "session: 🔴"
        assert window.keyforged_status.get_label() == "keyforged: ⚪"
        assert unlock_updates[-1] is None
        assert issues[-1] == "session"


class TestComboTabWidget:
    def test_combo_tab_syncs_with_device_tab_selection(self, temp_config_dir):
        from keyforge.common.models import (
            ButtonDefinition,
            DeviceProfileLayer,
            HardwareConfig,
            ProfileConfig,
        )
        from keyforge.gui.window import MainWindow

        window = MainWindow(demo_mode=True)
        window.profile_manager.save_profile(
            ProfileConfig(
                name="Desktop",
                enabled=True,
                is_permanent=True,
                device_layers={"2234:6678": DeviceProfileLayer(hardware_id="2234:6678")},
            )
        )
        window.profile_manager.save_profile(
            ProfileConfig(
                name="Gaming",
                enabled=True,
                is_permanent=True,
                device_layers={"2234:6678": DeviceProfileLayer(hardware_id="2234:6678")},
            )
        )

        device = HardwareConfig(
            vendor_id="2234",
            product_id="6678",
            name="Mouse One",
            evdev_devices=[],
            buttons=[ButtonDefinition(id="btn_back", label="Back", evdev="btn_side")],
        )

        window._add_device_tab(device)
        tab = window.stack.get_page(window.stack.get_child_by_name(device.hardware_id)).get_child()

        tab.profile_dropdown.set_selected(tab._profile_names.index("Gaming"))

        assert window.combo_tab is not None
        assert window.combo_tab._selected_profile is not None
        assert window.combo_tab._selected_profile.config.name == "Gaming"

    def test_combo_tab_profile_selection_syncs_back_to_device_tabs(self, temp_config_dir):
        from keyforge.common.models import (
            ButtonDefinition,
            DeviceProfileLayer,
            HardwareConfig,
            ProfileConfig,
        )
        from keyforge.gui.window import MainWindow

        window = MainWindow(demo_mode=True)
        window.profile_manager.save_profile(
            ProfileConfig(
                name="Desktop",
                enabled=True,
                is_permanent=True,
                device_layers={"2234:6678": DeviceProfileLayer(hardware_id="2234:6678")},
            )
        )
        window.profile_manager.save_profile(
            ProfileConfig(
                name="Gaming",
                enabled=True,
                is_permanent=True,
                device_layers={"2234:6678": DeviceProfileLayer(hardware_id="2234:6678")},
            )
        )

        device = HardwareConfig(
            vendor_id="2234",
            product_id="6678",
            name="Mouse One",
            evdev_devices=[],
            buttons=[ButtonDefinition(id="btn_back", label="Back", evdev="btn_side")],
        )

        window._add_device_tab(device)
        tab = window.stack.get_page(window.stack.get_child_by_name(device.hardware_id)).get_child()

        assert window.combo_tab is not None
        window.combo_tab.profile_dropdown.set_selected(
            window.combo_tab._profile_names.index("Gaming")
        )

        assert window._selected_profile_name == "Gaming"
        assert tab._selected_profile is not None
        assert tab._selected_profile.config.name == "Gaming"

    def test_combo_tab_add_edit_delete_combo(self, temp_config_dir):
        from keyforge.common.models import (
            ActionType,
            ComboConfig,
            ComboEvent,
            ComboStep,
            MappingAction,
            ProfileConfig,
        )
        from keyforge.gui.widgets.combo_tab import ComboTab
        from keyforge.session.profiles import ProfileManager

        profile_manager = ProfileManager()
        profile_manager.save_profile(
            ProfileConfig(
                name="Desktop",
                enabled=True,
                is_permanent=True,
            )
        )

        tab = ComboTab(profile_manager=profile_manager, demo_mode=False)
        tab.refresh_profiles(preferred_profile_name="Desktop", publish_selection=False)

        combo = ComboConfig(
            id="combo-1",
            name="Quick Save",
            steps=[
                ComboStep(
                    events=[
                        ComboEvent(
                            evdev="key_leftctrl",
                            hardware_id="1234:5678",
                            source="kbd",
                        ),
                        ComboEvent(evdev="key_s", hardware_id="1234:5678", source="kbd"),
                    ]
                )
            ],
            action=MappingAction(action_type=ActionType.KEYBOARD, target="key_f5"),
        )
        tab._on_combo_saved(None, combo)

        assert len(tab._selected_combos()) == 1
        assert tab.combo_listbox.get_first_child() is not None
        reloaded = profile_manager.get_profile("Desktop")
        assert reloaded is not None
        assert reloaded.config.combos[0].steps[0].events[0].hardware_id == "1234:5678"
        reloaded_manager = ProfileManager()
        persisted = reloaded_manager.get_profile("Desktop")
        assert persisted is not None
        assert persisted.config.combos[0].steps[0].events[0].hardware_id == "1234:5678"

        updated = ComboConfig(
            id=combo.id,
            name="Quick Load",
            steps=[
                ComboStep(
                    events=[
                        ComboEvent(
                            evdev="key_leftctrl",
                            hardware_id="1234:5678",
                            source="kbd",
                        ),
                        ComboEvent(evdev="key_l", hardware_id="1234:5678", source="kbd"),
                    ]
                )
            ],
            action=MappingAction(action_type=ActionType.KEYBOARD, target="key_f9"),
        )
        tab._on_combo_saved(None, updated)

        assert len(tab._selected_combos()) == 1
        assert tab._selected_combos()[0].name == "Quick Load"

        tab._on_delete_combo_clicked(None, combo.id)

        assert tab._selected_combos() == []
        assert tab.section_label.get_text() == "No combos in this profile."

    def test_combo_tab_marks_active_profile_from_session_payload(self, temp_config_dir):
        from keyforge.common.models import ProfileConfig
        from keyforge.gui.widgets.combo_tab import ComboTab
        from keyforge.session.profiles import ProfileManager

        profile_manager = ProfileManager()
        profile_manager.save_profile(
            ProfileConfig(
                name="Desktop",
                enabled=True,
                is_permanent=True,
            )
        )

        tab = ComboTab(profile_manager=profile_manager, demo_mode=True)
        tab.refresh_profiles(preferred_profile_name="Desktop", publish_selection=False)
        tab._on_active_profile_response({"active_profiles": ["Desktop"]})

        assert tab._active_profile_names == ["Desktop"]
        assert tab.status_label.get_text() == "active"

    def test_combo_tab_respects_compositor_tag_rule_capability(self, temp_config_dir):
        from keyforge.common.models import ProfileConfig, WindowRule
        from keyforge.gui.widgets.combo_tab import ComboTab
        from keyforge.session.profiles import ProfileManager

        profile_manager = ProfileManager()
        profile_manager.save_profile(
            ProfileConfig(
                name="Desktop",
                enabled=True,
                is_permanent=False,
                window_rules=[WindowRule(field="tag", pattern="work")],
            )
        )

        unsupported = ComboTab(profile_manager=profile_manager, demo_mode=True)
        unsupported.refresh_profiles(preferred_profile_name="Desktop", publish_selection=False)

        supported = ComboTab(
            profile_manager=profile_manager,
            demo_mode=True,
            compositor_capabilities=["window_tags"],
        )
        supported.refresh_profiles(preferred_profile_name="Desktop", publish_selection=False)

        assert unsupported.status_label.get_text() == "unsupported rules"
        assert supported.status_label.get_text() == "waiting"

    def test_combo_tab_empty_state_uses_section_header_text(self, temp_config_dir):
        from keyforge.common.models import ProfileConfig
        from keyforge.gui.widgets.combo_tab import ComboTab
        from keyforge.session.profiles import ProfileManager

        profile_manager = ProfileManager()
        profile_manager.save_profile(
            ProfileConfig(
                name="Desktop",
                enabled=True,
                is_permanent=True,
            )
        )

        tab = ComboTab(profile_manager=profile_manager, demo_mode=True)
        tab.refresh_profiles(preferred_profile_name="Desktop", publish_selection=False)

        assert tab.section_label.get_text() == "No combos in this profile."
        assert tab.combo_listbox.get_visible() is False

    def test_combo_tab_sorts_rows_and_opens_editor_for_activated_row(self, temp_config_dir):
        from gi.repository import Gtk

        from keyforge.common.models import (
            ActionType,
            ComboConfig,
            ComboEvent,
            ComboStep,
            MappingAction,
            ProfileConfig,
        )
        from keyforge.gui.widgets.combo_tab import ComboTab
        from keyforge.session.profiles import ProfileManager

        def combo(combo_id: str, name: str, trigger_key: str, action_key: str) -> ComboConfig:
            return ComboConfig(
                id=combo_id,
                name=name,
                steps=[
                    ComboStep(
                        events=[
                            ComboEvent(
                                evdev=trigger_key,
                                hardware_id="1234:5678",
                                source="kbd",
                            )
                        ]
                    )
                ],
                action=MappingAction(action_type=ActionType.KEYBOARD, target=action_key),
            )

        profile_manager = ProfileManager()
        profile_manager.save_profile(
            ProfileConfig(
                name="Desktop",
                enabled=True,
                is_permanent=True,
                combos=[
                    combo("combo-c", "Charlie", "key_c", "key_3"),
                    combo("combo-a", "alpha", "key_a", "key_1"),
                    combo("combo-b", "Bravo", "key_b", "key_2"),
                ],
            )
        )

        tab = ComboTab(profile_manager=profile_manager, demo_mode=True)
        tab.refresh_profiles(preferred_profile_name="Desktop", publish_selection=False)

        tab._on_column_header_clicked(tab._name_header_btn, 1)
        name_rows = []
        row = tab.combo_listbox.get_first_child()
        while row is not None:
            name_rows.append(row.get_child().get_first_child().get_label())
            row = row.get_next_sibling()

        tab._on_column_header_clicked(tab._name_header_btn, 1)
        reversed_rows = []
        row = tab.combo_listbox.get_first_child()
        while row is not None:
            reversed_rows.append(row.get_child().get_first_child().get_label())
            row = row.get_next_sibling()

        opened: list[str] = []
        tab._open_combo_editor = lambda selected=None: opened.append(
            selected.id if selected else "new"
        )
        first_row = tab.combo_listbox.get_first_child()
        missing_row = Gtk.ListBoxRow()
        missing_row._combo_id = "missing"  # type: ignore[attr-defined]

        tab._on_row_activated(tab.combo_listbox, first_row)
        tab._on_row_activated(tab.combo_listbox, missing_row)

        assert name_rows == ["alpha", "Bravo", "Charlie"]
        assert reversed_rows == ["Charlie", "Bravo", "alpha"]
        assert tab._name_header_btn.get_label() == "Name ▾"
        assert opened == ["combo-c"]

    def test_combo_tab_add_combo_requires_selected_profile(self, temp_config_dir):
        from gi.repository import Gtk

        from keyforge.gui.widgets.combo_tab import ComboTab

        tab = ComboTab(profile_manager=None, demo_mode=True)
        opened: list[str] = []
        tab._open_combo_editor = lambda combo=None: opened.append("opened")

        tab._on_add_combo_clicked(Gtk.Button())

        assert tab.section_label.get_text() == "Combos"
        assert tab._selected_profile is None
        assert opened == []


class TestComboEditorDialog:
    def test_combo_editor_capture_response_adds_step(self):
        from gi.repository import Gtk

        from keyforge.gui.widgets.combo_editor_dialog import ComboEditorDialog

        parent = Gtk.Box()
        dialog = ComboEditorDialog(parent, profile_name="Desktop")

        dialog._on_capture_combo_response(
            {
                "status": "ok",
                "events": [{"evdev": "key_a", "hardware_id": "1234:5678", "source": "kbd"}],
            }
        )

        assert [event.evdev for event in dialog._draft.steps[0].events] == ["key_a"]
        assert dialog._draft.steps[0].events[0].hardware_id == "1234:5678"
        assert dialog._draft.steps[0].timeout_ms is None
        assert dialog.capture_status.get_text() == "Added step: A"

    def test_combo_editor_capture_request_uses_profile_name(self, monkeypatch):
        from gi.repository import Gtk

        import keyforge.gui.widgets.combo_editor_dialog as combo_editor_dialog_module
        from keyforge.gui.widgets.combo_editor_dialog import ComboEditorDialog

        calls: list[tuple[dict, float]] = []

        def fake_session_request_async(payload, callback, timeout=5.0):
            calls.append((payload, timeout))
            if payload.get("command") == "capture_combo":
                callback(
                    {
                        "status": "ok",
                        "events": [
                            {
                                "evdev": "key_leftctrl",
                                "hardware_id": "1234:5678",
                                "source": "kbd",
                            },
                            {"evdev": "key_s", "hardware_id": "1234:5678", "source": "kbd"},
                        ],
                    }
                )

        monkeypatch.setattr(
            combo_editor_dialog_module,
            "session_request_async",
            fake_session_request_async,
        )

        parent = Gtk.Box()
        dialog = ComboEditorDialog(parent, profile_name="Desktop")
        dialog._recording_unlocked = True
        dialog._update_capture_controls()

        dialog._on_add_step_clicked(None)

        assert calls == [
            ({"command": "get_status"}, 1.0),
            ({"command": "capture_combo", "profile_name": "Desktop", "timeout_s": 15.0}, 20.0),
        ]
        assert dialog._capture_inflight is False
        assert [event.evdev for event in dialog._draft.steps[0].events] == ["ctrl", "key_s"]
        assert dialog._draft.steps[0].timeout_ms is None

    def test_combo_editor_new_steps_after_first_default_to_600ms(self):
        from gi.repository import Gtk

        from keyforge.gui.widgets.combo_editor_dialog import ComboEditorDialog

        parent = Gtk.Box()
        dialog = ComboEditorDialog(parent, profile_name="Desktop")

        dialog._on_capture_combo_response(
            {
                "status": "ok",
                "events": [{"evdev": "key_a", "hardware_id": "1234:5678", "source": "kbd"}],
            }
        )
        dialog._on_capture_combo_response(
            {
                "status": "ok",
                "events": [{"evdev": "key_b", "hardware_id": "1234:5678", "source": "kbd"}],
            }
        )

        assert dialog._draft.steps[0].timeout_ms is None
        assert dialog._draft.steps[1].timeout_ms == 600

    def test_combo_editor_save_disabled_until_complete(self):
        from gi.repository import Gtk

        from keyforge.common.models import ActionType, ComboEvent, ComboStep, MappingAction
        from keyforge.gui.widgets.combo_editor_dialog import ComboEditorDialog

        parent = Gtk.Box()
        dialog = ComboEditorDialog(parent)

        assert dialog.save_button.get_sensitive() is False

        dialog._draft.steps.append(
            ComboStep(events=[ComboEvent(evdev="key_a", hardware_id="1234:5678")])
        )
        dialog._refresh_trigger_display()
        dialog._update_save_button()

        assert dialog.save_button.get_sensitive() is False

        dialog._on_action_selected(
            None,
            MappingAction(action_type=ActionType.KEYBOARD, target="key_f5"),
        )

        assert dialog.save_button.get_sensitive() is True

    def test_combo_editor_emits_saved_combo(self):
        from gi.repository import Gtk

        from keyforge.common.models import ActionType, ComboEvent, ComboStep, MappingAction
        from keyforge.gui.widgets.combo_editor_dialog import ComboEditorDialog

        parent = Gtk.Box()
        dialog = ComboEditorDialog(parent)
        captured = []
        dialog.connect("combo-saved", lambda _dialog, combo: captured.append(combo))

        dialog.name_entry.set_text("Quick Save")
        dialog._draft.steps.append(
            ComboStep(
                events=[
                    ComboEvent(evdev="key_leftctrl", hardware_id="1234:5678", source="kbd"),
                    ComboEvent(evdev="key_s", hardware_id="1234:5678", source="kbd"),
                ]
            )
        )
        dialog._refresh_trigger_display()
        dialog._on_action_selected(
            None,
            MappingAction(action_type=ActionType.KEYBOARD, target="key_f5"),
        )
        dialog._on_save_clicked(None)

        assert len(captured) == 1
        assert captured[0].name == "Quick Save"
        assert [event.evdev for event in captured[0].steps[0].events] == [
            "key_leftctrl",
            "key_s",
        ]

    def test_combo_editor_generates_default_name_when_name_is_empty(self):
        from gi.repository import Gtk

        from keyforge.common.models import ActionType, ComboEvent, ComboStep, MappingAction
        from keyforge.gui.widgets.combo_editor_dialog import ComboEditorDialog

        parent = Gtk.Box()
        dialog = ComboEditorDialog(parent)
        captured = []
        dialog.connect("combo-saved", lambda _dialog, combo: captured.append(combo))

        dialog._draft.steps.append(
            ComboStep(
                events=[
                    ComboEvent(evdev="key_leftctrl", hardware_id="1234:5678", source="kbd"),
                    ComboEvent(evdev="key_s", hardware_id="1234:5678", source="kbd"),
                ]
            )
        )
        dialog._refresh_trigger_display()
        dialog._on_action_selected(
            None,
            MappingAction(action_type=ActionType.KEYBOARD, target="key_f5"),
        )
        dialog._on_save_clicked(None)

        assert len(captured) == 1
        assert captured[0].name == "Ctrl+S -> F5"

    def test_combo_editor_step_timeout_controls_and_save(self):
        from gi.repository import Gtk

        from keyforge.common.models import (
            ActionType,
            ComboConfig,
            ComboEvent,
            ComboStep,
            MappingAction,
        )
        from keyforge.gui.widgets.combo_editor_dialog import ComboEditorDialog

        def child_widgets(widget):
            children = []
            child = widget.get_first_child()
            while child is not None:
                children.append(child)
                child = child.get_next_sibling()
            return children

        parent = Gtk.Box()
        dialog = ComboEditorDialog(
            parent,
            ComboConfig(
                id="combo-1",
                name="Quick Save",
                steps=[
                    ComboStep(events=[ComboEvent(evdev="key_a", hardware_id="1234:5678")]),
                    ComboStep(
                        events=[ComboEvent(evdev="key_b", hardware_id="1234:5678")],
                        timeout_ms=700,
                    ),
                ],
                action=MappingAction(action_type=ActionType.KEYBOARD, target="key_f5"),
            ),
        )

        first_row = dialog.steps_box.get_first_child()
        second_row = first_row.get_next_sibling()

        assert not any(isinstance(child, Gtk.SpinButton) for child in child_widgets(first_row))
        second_spins = [
            child for child in child_widgets(second_row) if isinstance(child, Gtk.SpinButton)
        ]
        assert len(second_spins) == 1
        second_spins[0].set_value(850)

        assert dialog._draft.steps[0].timeout_ms is None
        assert dialog._draft.steps[1].timeout_ms == 850

    def test_combo_editor_exact_duplicate_is_rejected(self):
        from gi.repository import Gtk

        from keyforge.common.models import (
            ActionType,
            ComboConfig,
            ComboEvent,
            ComboStep,
            MappingAction,
        )
        from keyforge.gui.widgets.combo_editor_dialog import ComboEditorDialog

        parent = Gtk.Box()
        dialog = ComboEditorDialog(
            parent,
            ComboConfig(
                id="combo-2",
                name="Long Combo",
                steps=[
                    ComboStep(
                        events=[
                            ComboEvent(evdev="key_leftctrl", hardware_id="1234:5678", source="kbd"),
                            ComboEvent(evdev="key_x", hardware_id="1234:5678", source="kbd"),
                        ]
                    ),
                    ComboStep(
                        events=[ComboEvent(evdev="key_1", hardware_id="1234:5678", source="kbd")],
                        timeout_ms=600,
                    ),
                ],
                action=MappingAction(action_type=ActionType.KEYBOARD, target="key_f5"),
            ),
            sibling_combos=[
                ComboConfig(
                    id="combo-1",
                    name="Duplicate Combo",
                    steps=[
                        ComboStep(
                            events=[
                                ComboEvent(
                                    evdev="key_leftctrl",
                                    hardware_id="1234:5678",
                                    source="kbd",
                                ),
                                ComboEvent(evdev="key_x", hardware_id="1234:5678", source="kbd"),
                            ]
                        ),
                        ComboStep(
                            events=[
                                ComboEvent(evdev="key_1", hardware_id="1234:5678", source="kbd")
                            ]
                        ),
                    ],
                    action=MappingAction(action_type=ActionType.KEYBOARD, target="key_f6"),
                )
            ],
        )

        assert dialog.validation_label.get_visible() is True
        assert "same trigger already exists" in dialog.validation_label.get_text().lower()
        assert dialog.save_button.get_sensitive() is False

    def test_combo_editor_prefix_shadow_does_not_block_save(self):
        from gi.repository import Gtk

        from keyforge.common.models import (
            ActionType,
            ComboConfig,
            ComboEvent,
            ComboStep,
            MappingAction,
        )
        from keyforge.gui.widgets.combo_editor_dialog import ComboEditorDialog

        parent = Gtk.Box()
        dialog = ComboEditorDialog(
            parent,
            ComboConfig(
                id="combo-2",
                name="Long Combo",
                steps=[
                    ComboStep(
                        events=[
                            ComboEvent(evdev="key_leftctrl", hardware_id="1234:5678", source="kbd"),
                            ComboEvent(evdev="key_x", hardware_id="1234:5678", source="kbd"),
                        ]
                    ),
                    ComboStep(
                        events=[ComboEvent(evdev="key_1", hardware_id="1234:5678", source="kbd")],
                        timeout_ms=600,
                    ),
                ],
                action=MappingAction(action_type=ActionType.KEYBOARD, target="key_f5"),
            ),
            sibling_combos=[
                ComboConfig(
                    id="combo-1",
                    name="Short Combo",
                    steps=[
                        ComboStep(
                            events=[
                                ComboEvent(
                                    evdev="key_leftctrl",
                                    hardware_id="1234:5678",
                                    source="kbd",
                                ),
                                ComboEvent(evdev="key_x", hardware_id="1234:5678", source="kbd"),
                            ]
                        )
                    ],
                    action=MappingAction(action_type=ActionType.KEYBOARD, target="key_f6"),
                )
            ],
        )

        assert dialog.save_button.get_sensitive() is True


class TestProfileCreateDialog:
    def test_new_profile_defaults_to_permanent(self, temp_config_dir):
        from keyforge.common.models import ProfileConfig
        from keyforge.gui.wizards.profile_create import ProfileCreateDialog
        from keyforge.session.profiles import ProfileManager

        profile_manager = ProfileManager()
        profile_manager.save_profile(
            ProfileConfig(
                name="Base",
                enabled=True,
                is_permanent=True,
                priority=4,
            )
        )
        dialog = ProfileCreateDialog(None, profile_manager)
        dialog.name_entry.set_text("Gaming")
        dialog._on_create(None)

        created = profile_manager.get_profile("Gaming")

        assert created is not None
        assert created.config.is_permanent is True
        assert created.config.priority == 5


class TestApplication:
    def test_application_args(self):
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--demo", action="store_true")
        parser.add_argument("--version", action="version", version="test")

        args = parser.parse_args(["--demo"])
        assert args.demo is True

        args = parser.parse_args([])
        assert args.demo is False


class TestButtonWidget:
    def test_button_widget_creation(self):
        from keyforge.common.models import ButtonDefinition

        button = ButtonDefinition(
            id="btn_left",
            label="Left Click",
            evdev="btn_left",
            zone="left",
        )

        assert button.id == "btn_left"
        assert button.label == "Left Click"
        assert button.evdev == "btn_left"
        assert button.zone == "left"

    def test_button_widget_wheel(self):
        from keyforge.common.models import ButtonDefinition

        button = ButtonDefinition(
            id="wheel_up",
            label="Scroll Up",
            evdev="rel_wheel",
            evdev_value=1,
            type="wheel",
        )

        assert button.id == "wheel_up"
        assert button.evdev_value == 1
        assert button.type == "wheel"


class TestProfileActions:
    def test_action_types(self):
        from keyforge.common.models import ActionType

        assert ActionType.PASSTHROUGH.value == "passthrough"
        assert ActionType.KEYBOARD.value == "keyboard"
        assert ActionType.MOUSE.value == "mouse"
        assert ActionType.EXEC.value == "exec"
        assert ActionType.COMPOSITOR_DISPATCH.value == "compositor_dispatch"
        assert ActionType.SUPPRESS.value == "suppress"

    def test_mapping_action_keyboard(self):
        from keyforge.common.models import ActionType, MappingAction

        action = MappingAction(
            action_type=ActionType.KEYBOARD,
            target="key_space",
        )

        assert action.action_type == ActionType.KEYBOARD
        assert action.target == "key_space"

    def test_mapping_action_with_rapidfire(self):
        from keyforge.common.models import ActionType, MappingAction

        action = MappingAction(
            action_type=ActionType.KEYBOARD,
            target="btn_left",
            rapidfire_enabled=True,
            rapidfire_hold_ms=50,
            rapidfire_wait_ms=30,
        )

        assert action.action_type == ActionType.KEYBOARD
        assert action.rapidfire_enabled is True
        assert action.rapidfire_hold_ms == 50
        assert action.rapidfire_wait_ms == 30
