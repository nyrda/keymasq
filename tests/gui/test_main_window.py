# ruff: noqa: F403, F405, I001
from tests.gui.support import *

class TestMainWindow:
    def test_main_window_seeds_default_profile_for_first_device(self, temp_config_dir):
        from keymasq.common.models import ButtonDefinition, HardwareConfig
        from keymasq.gui.window import MainWindow

        window = MainWindow(demo_mode=True)

        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Mouse One",
            evdev_devices=[],
            buttons=[ButtonDefinition(id="btn_back", label="Back", evdev="btn_side")],
        )

        window._add_device_tab(device)

        tab = window._child_for_hardware_id(device.hardware_id)

        assert window.profile_manager.get_profile("Default") is not None
        assert tab._selected_profile is not None
        assert tab._selected_profile.config.name == "Default"
        assert tab.settings_frame.get_sensitive() is True

    def test_main_window_demo_mode(self, temp_config_dir):
        from keymasq.common.models import (
            ButtonDefinition,
            DeviceProfileLayer,
            HardwareConfig,
            ProfileConfig,
        )
        from keymasq.gui.window import MainWindow

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

        tab1 = window._child_for_hardware_id(device1.hardware_id)
        tab2 = window._child_for_hardware_id(device2.hardware_id)

        tab1.refresh_profiles(preferred_profile_name="Profile 2")
        page2 = window._page_for_hardware_id(device2.hardware_id)
        assert page2 is not None
        window.tab_view.set_selected_page(page2)

        assert tab2._selected_profile is not None
        assert tab2._selected_profile.config.name == "Profile 2"

    def test_main_window_add_device_action_does_not_require_unlock(self, temp_config_dir):
        from keymasq.gui.window import MainWindow

        window = MainWindow(demo_mode=True)
        add_calls: list[bool] = []
        unlock_calls: list[bool] = []
        button = object()

        window._recording_unlocked = False
        window._on_add_device = lambda _button: add_calls.append(True)  # type: ignore[method-assign]
        window.present_unlock_dialog = lambda on_success=None: unlock_calls.append(True)  # type: ignore[method-assign]

        window._on_add_device_clicked(button)

        assert add_calls == [True]
        assert unlock_calls == []

    def test_main_window_device_inspector_uses_unlock_flow(self, temp_config_dir):
        from keymasq.common.models import ButtonDefinition, HardwareConfig
        from keymasq.gui.window import MainWindow

        window = MainWindow(demo_mode=True)
        window.demo_mode = False
        window._recording_unlock_required = True
        window._recording_unlocked = False
        window._recording_refresh_owner = False
        unlock_callbacks = []
        window.present_unlock_dialog = (  # type: ignore[method-assign]
            lambda on_success=None: unlock_callbacks.append(on_success)
        )
        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Mouse",
            evdev_devices=[],
            buttons=[ButtonDefinition(id="btn_back", label="Back", evdev="btn_side")],
        )

        window.open_device_inspector(device)

        assert len(unlock_callbacks) == 1
        assert window._device_inspector_windows == {}

    def test_main_window_device_inspector_reuses_open_window(
        self,
        temp_config_dir,
        monkeypatch,
    ):
        from keymasq.common.models import ButtonDefinition, HardwareConfig
        from keymasq.gui.widgets import device_inspector_window as inspector_module
        from keymasq.gui.window import MainWindow

        window = MainWindow(demo_mode=True)
        window.demo_mode = False
        window._recording_unlock_required = False
        present_calls = []
        close_callbacks = []

        class FakeInspector:
            def __init__(self, parent, device):
                self.parent = parent
                self.device = device

            def connect(self, signal, callback):
                if signal == "close-request":
                    close_callbacks.append((self, callback))
                return 1

            def present(self):
                present_calls.append(self.device.hardware_id)

            def close(self):
                pass

        monkeypatch.setattr(inspector_module, "DeviceInspectorWindow", FakeInspector)

        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Mouse",
            evdev_devices=[],
            buttons=[ButtonDefinition(id="btn_back", label="Back", evdev="btn_side")],
        )

        window.open_device_inspector(device)
        window.open_device_inspector(device)

        assert present_calls == [device.hardware_id, device.hardware_id]
        assert len(window._device_inspector_windows) == 1

        inspector = window._device_inspector_windows[device.hardware_id]
        assert close_callbacks[-1][1](inspector) is False
        assert window._device_inspector_windows == {}

        window.open_device_inspector(device)

        assert len(window._device_inspector_windows) == 1
        assert present_calls == [
            device.hardware_id,
            device.hardware_id,
            device.hardware_id,
        ]

    def test_main_window_menu_reflects_saved_appearance(self, temp_config_dir):
        from keymasq.gui.preferences import save_appearance_mode
        from keymasq.gui.window import MainWindow

        save_appearance_mode("dark")

        window = MainWindow(demo_mode=True)

        assert set(window._appearance_buttons) == {"system", "light", "dark"}
        assert window._appearance_buttons["dark"].get_active() is True

    def test_main_window_unlock_uses_runtime_polkit_without_prompt(
        self, temp_config_dir, monkeypatch
    ):
        from keymasq.gui import window as window_module
        from keymasq.gui.session_client import GuiTaskResult
        from keymasq.gui.window import MainWindow

        window = MainWindow(demo_mode=True)
        window.demo_mode = False
        commands: list[list[str]] = []
        alerts: list[object] = []
        success_calls: list[bool] = []

        monkeypatch.setattr(
            window_module,
            "resolve_keymasq_record_helper_path",
            lambda: "/usr/bin/keymasq-record",
        )
        monkeypatch.setattr(
            window_module,
            "run_gui_task",
            lambda worker, callback: callback(GuiTaskResult(value=worker())),
        )

        def fake_run(cmd, capture_output, text):
            commands.append(cmd)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(window_module.subprocess, "run", fake_run)
        monkeypatch.setattr(
            window_module,
            "session_request",
            lambda payload, timeout=3.0: {
                "status": "ok",
                "lease_id": "lease-1",
                "recording_unlocked": True,
                "recording_unlock_required": True,
                "recording_unlock_source": "runtime",
                "recording_unlock_expires_at": 123,
                "recording_refresh_owner": True,
            },
        )
        monkeypatch.setattr(
            window_module.Adw.AlertDialog,
            "present",
            lambda self, root: alerts.append(self),
        )

        window.present_unlock_dialog(on_success=lambda: success_calls.append(True))

        assert commands == [
            [
                "pkexec",
                "/usr/bin/keymasq-record",
                "unlock-runtime",
                "--uid",
                str(window_module.os.getuid()),
                "--ttl",
                "60",
            ]
        ]
        assert all("unlock-persistent" not in cmd for cmd in commands)
        assert window._recording_refresh_lease_id == "lease-1"
        assert success_calls == [True]
        assert alerts == []

    def test_main_window_syncs_manual_profile_selection_across_tabs(self, temp_config_dir):
        from keymasq.common.models import (
            ButtonDefinition,
            DeviceProfileLayer,
            HardwareConfig,
            ProfileConfig,
        )
        from keymasq.gui.window import MainWindow

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

        tab1 = window._child_for_hardware_id(device1.hardware_id)
        tab2 = window._child_for_hardware_id(device2.hardware_id)

        tab1.profile_dropdown.set_selected(tab1._profile_names.index("Desktop"))

        assert window._selected_profile_name == "Desktop"
        assert tab1._selected_profile is not None
        assert tab1._selected_profile.config.name == "Desktop"
        assert tab2._selected_profile is not None
        assert tab2._selected_profile.config.name == "Desktop"

    def test_created_profile_is_selected_and_reloaded(self, temp_config_dir, monkeypatch):
        from keymasq.common.models import ButtonDefinition, HardwareConfig, ProfileConfig
        from keymasq.gui.widgets import profile_managed_tab as profile_tab_module
        from keymasq.gui.window import MainWindow
        from keymasq.session.profiles import ProfileManager

        reload_calls = []
        monkeypatch.setattr(
            profile_tab_module,
            "notify_session_reload_async",
            lambda *args, **kwargs: reload_calls.append((args, kwargs)),
        )

        window = MainWindow(demo_mode=True)
        device = HardwareConfig(
            vendor_id="2234",
            product_id="6678",
            name="Mouse One",
            evdev_devices=[],
            buttons=[ButtonDefinition(id="btn_back", label="Back", evdev="btn_side")],
        )
        window._add_device_tab(device)
        original_profile_manager = window.profile_manager
        window._set_profile_manager(ProfileManager(auto_create_default_if_empty=True))
        assert window.profile_manager.get_profile("Gaming") is None
        original_profile_manager.save_profile(
            ProfileConfig(name="Gaming", enabled=True, is_permanent=True)
        )

        tab = window._child_for_hardware_id(device.hardware_id)

        tab._on_profile_created(None, "Gaming")

        assert reload_calls
        assert window._selected_profile_name == "Gaming"
        assert tab._selected_profile is not None
        assert tab._selected_profile.config.name == "Gaming"
        assert tab._profile_settings_dialog is not None
        assert window.combo_tab is not None
        assert window.combo_tab._selected_profile is not None
        assert window.combo_tab._selected_profile.config.name == "Gaming"

    def test_main_window_update_device_display_name_updates_stack_page(self, temp_config_dir):
        from keymasq.common.models import ButtonDefinition, HardwareConfig
        from keymasq.gui.window import MainWindow

        window = MainWindow(demo_mode=True)
        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Mouse One",
            evdev_devices=[],
            buttons=[ButtonDefinition(id="btn_back", label="Back", evdev="btn_side")],
        )

        window._add_device_tab(device)
        page = window._page_for_hardware_id(device.hardware_id)
        assert page is not None
        assert page.get_title() == "Mouse One"

        window.update_device_display_name(device.hardware_id, "Desk Mouse")

        assert page.get_title() == "Desk Mouse"

    def test_main_window_persists_user_tab_order(self, temp_config_dir):
        from keymasq.common.models import ButtonDefinition, HardwareConfig
        from keymasq.gui.preferences import load_tab_order
        from keymasq.gui.window import MainWindow

        window = MainWindow(demo_mode=True)
        devices = [
            HardwareConfig(
                vendor_id="1234",
                product_id=product_id,
                name=name,
                evdev_devices=[],
                buttons=[ButtonDefinition(id="btn_back", label="Back", evdev="btn_side")],
            )
            for product_id, name in (
                ("5678", "Mouse One"),
                ("5679", "Mouse Two"),
                ("5680", "Mouse Three"),
            )
        ]
        for device in devices:
            window._add_device_tab(device)

        third_page = window._page_for_hardware_id(devices[2].hardware_id)
        assert third_page is not None

        window.tab_view.reorder_page(third_page, window.tab_view.get_n_pinned_pages())

        expected_order = [
            f"device:{devices[2].hardware_id}",
            f"device:{devices[0].hardware_id}",
            f"device:{devices[1].hardware_id}",
            "combos",
        ]
        assert window._current_tab_order() == expected_order
        assert load_tab_order() == expected_order

    def test_main_window_applies_saved_tab_order_on_load(self, temp_config_dir):
        from keymasq.common.models import ButtonDefinition, HardwareConfig
        from keymasq.gui.preferences import save_tab_layout
        from keymasq.gui.window import MainWindow

        devices = [
            HardwareConfig(
                vendor_id="1234",
                product_id=product_id,
                name=name,
                evdev_devices=[],
                buttons=[ButtonDefinition(id="btn_back", label="Back", evdev="btn_side")],
            )
            for product_id, name in (
                ("5678", "Mouse One"),
                ("5679", "Mouse Two"),
                ("5680", "Mouse Three"),
            )
        ]
        expected_order = [
            f"device:{devices[1].hardware_id}",
            "combos",
            f"device:{devices[0].hardware_id}",
            f"device:{devices[2].hardware_id}",
        ]
        save_tab_layout(expected_order[:-1], set())

        window = MainWindow(demo_mode=True)
        window._apply_loaded_devices(devices)

        assert window._current_tab_order() == expected_order

        window._apply_loaded_devices(devices)

        assert window._current_tab_order() == expected_order
        assert window.tab_view.get_n_pages() == 4

    def test_main_window_hides_and_restores_combo_tab_from_menu(self, temp_config_dir):
        from keymasq.common.models import ButtonDefinition, HardwareConfig
        from keymasq.gui.preferences import load_hidden_tabs, load_tab_order
        from keymasq.gui.window import MainWindow

        window = MainWindow(demo_mode=True)
        devices = [
            HardwareConfig(
                vendor_id="1234",
                product_id=product_id,
                name=name,
                evdev_devices=[],
                buttons=[ButtonDefinition(id="btn_back", label="Back", evdev="btn_side")],
            )
            for product_id, name in (
                ("5678", "Mouse One"),
                ("5679", "Mouse Two"),
            )
        ]
        for device in devices:
            window._add_device_tab(device)

        assert window.combo_tab is not None
        combo_page = window._page_for_child(window.combo_tab)
        assert combo_page is not None
        assert window.tab_bar.get_start_action_widget() is not None
        assert window.tab_view.get_n_pages() == 3

        window.tab_view.reorder_page(combo_page, window.tab_view.get_n_pinned_pages() + 1)
        expected_order = [
            f"device:{devices[0].hardware_id}",
            "combos",
            f"device:{devices[1].hardware_id}",
        ]

        assert window._current_tab_order() == expected_order
        assert load_tab_order() == expected_order

        window.tab_view.close_page(combo_page)

        assert window.combo_tab is None
        assert window._current_tab_order() == [
            f"device:{devices[0].hardware_id}",
            f"device:{devices[1].hardware_id}",
        ]
        assert load_tab_order() == expected_order
        assert load_hidden_tabs() == {"combos"}

        window.show_combo_tab()

        assert window.combo_tab is not None
        restored_page = window._page_for_child(window.combo_tab)
        assert restored_page is not None
        assert window.tab_view.get_selected_page() is restored_page
        assert window._current_tab_order() == expected_order
        assert load_tab_order() == expected_order
        assert load_hidden_tabs() == set()

    def test_main_window_tab_close_requests_device_delete(self, temp_config_dir):
        from keymasq.common.models import ButtonDefinition, HardwareConfig
        from keymasq.gui.window import MainWindow

        window = MainWindow(demo_mode=True)
        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Mouse One",
            evdev_devices=[],
            buttons=[ButtonDefinition(id="btn_back", label="Back", evdev="btn_side")],
        )
        window._add_device_tab(device)

        tab = window._child_for_hardware_id(device.hardware_id)
        page = window._page_for_hardware_id(device.hardware_id)
        assert tab is not None
        assert page is not None

        delete_requests: list[bool] = []
        tab.present_delete_device_dialog = lambda: delete_requests.append(True)  # type: ignore[attr-defined, method-assign]

        window.tab_view.close_page(page)

        assert delete_requests == [True]
        assert window._page_for_hardware_id(device.hardware_id) is page

    def test_main_window_startup_probe_applies_compositor_state_and_devices(self, temp_config_dir):
        from keymasq.common.models import (
            ButtonDefinition,
            DeviceProfileLayer,
            HardwareConfig,
            ProfileConfig,
            WindowRule,
        )
        from keymasq.gui.session_client import GuiTaskResult
        from keymasq.gui.window import MainWindow

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
            GuiTaskResult(
                value=(
                    {
                        "compositor_id": "hyprland",
                        "support_details": {"supported": True, "warning": ""},
                        "supported": True,
                        "capabilities": ["window_tags"],
                    },
                    [device],
                )
            )
        )

        device_tab = window._child_for_hardware_id(device.hardware_id)

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

    def test_main_window_profiles_changed_event_updates_tabs_without_polling(self, temp_config_dir):
        from keymasq.common.models import (
            ButtonDefinition,
            DeviceProfileLayer,
            HardwareConfig,
            ProfileConfig,
        )
        from keymasq.gui.window import MainWindow

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
        tab = window._child_for_hardware_id(device.hardware_id)
        tab.profile_dropdown.set_selected(tab._profile_names.index("Gaming"))

        window._handle_session_event(
            {
                "event": "profiles_changed",
                "status": "ok",
                "active_profiles": ["Gaming"],
                "devices": {"2234:6678": {"profiles": ["Gaming"]}},
            }
        )

        assert tab._active_profile_names == ["Gaming"]
        assert tab.active_profiles_title_label.get_text() == "Applied profiles:"
        assert (
            tab.active_profiles_label.get_tooltip_text()
            == "Applied profiles. Layer order: Gaming"
        )
        assert tab.status_label.get_text() == "active"
        assert window.combo_tab is not None
        assert window.combo_tab.active_profiles_title_label.get_text() == "Active profiles:"
        assert window.combo_tab._active_profile_names == ["Gaming"]
        assert window.combo_tab.status_label.get_text() == "active"

    def test_main_window_profiles_changed_event_reloads_profile_models(
        self,
        temp_config_dir,
        monkeypatch,
    ):
        from keymasq.common.models import (
            ButtonDefinition,
            DeviceProfileLayer,
            HardwareConfig,
            ProfileConfig,
        )
        from keymasq.gui import window as window_module
        from keymasq.gui.session_client import GuiTaskResult
        from keymasq.gui.window import MainWindow
        from keymasq.session.profiles import ProfileManager

        monkeypatch.setattr(
            window_module,
            "run_gui_task",
            lambda worker, callback, **kwargs: callback(GuiTaskResult(value=worker())),
        )

        window = MainWindow(demo_mode=True)
        window.profile_manager.save_profile(
            ProfileConfig(
                name="Desktop",
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
        external_profiles = ProfileManager(auto_create_default_if_empty=True)
        external_profiles.save_profile(
            ProfileConfig(
                name="Gaming",
                enabled=True,
                is_permanent=True,
                device_layers={"2234:6678": DeviceProfileLayer(hardware_id="2234:6678")},
            )
        )

        window._handle_session_event(
            {
                "event": "profiles_changed",
                "status": "ok",
                "active_profiles": ["Gaming"],
                "devices": {"2234:6678": {"profiles": ["Gaming"]}},
            }
        )

        tab = window._child_for_hardware_id(device.hardware_id)
        assert "Gaming" in tab._profile_names
        assert window.combo_tab is not None
        assert "Gaming" in window.combo_tab._profile_names

    def test_main_window_destroy_removes_repeating_timeout_sources(
        self, temp_config_dir, monkeypatch
    ):
        from keymasq.gui import window as window_module
        from keymasq.gui.window import MainWindow

        removed: list[int] = []
        registered: list[tuple[str, object]] = []
        unregistered: list[tuple[str, object]] = []

        monkeypatch.setattr(window_module, "run_gui_task", lambda worker, callback: None)
        monkeypatch.setattr(window_module, "session_request_async", lambda *args, **kwargs: None)
        monkeypatch.setattr(
            window_module,
            "register_session_event_callback",
            lambda event, callback: registered.append((event, callback)),
        )
        monkeypatch.setattr(
            window_module,
            "unregister_session_event_callback",
            lambda event, callback: unregistered.append((event, callback)),
        )
        monkeypatch.setattr(window_module.GLib, "timeout_add", lambda interval, cb: 11)
        monkeypatch.setattr(window_module.GLib, "timeout_add_seconds", lambda interval, cb: 22)
        monkeypatch.setattr(
            window_module.GLib,
            "source_remove",
            lambda source_id: removed.append(source_id),
        )

        window = MainWindow(demo_mode=False)
        window._on_destroy()

        assert registered == [("*", window._on_session_event)]
        assert removed == [11, 22]
        assert unregistered == [("*", window._on_session_event)]

    def test_main_window_status_error_keeps_last_runtime_profile_state(self, temp_config_dir):
        from keymasq.common.models import (
            ButtonDefinition,
            DeviceProfileLayer,
            HardwareConfig,
            ProfileConfig,
        )
        from keymasq.gui.window import MainWindow

        window = MainWindow(demo_mode=True)
        window.profile_manager.save_profile(
            ProfileConfig(
                name="Desktop",
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
        tab = window._child_for_hardware_id(device.hardware_id)
        tab.refresh_profiles(preferred_profile_name="Desktop")
        window._apply_profile_runtime_state(
            {
                "status": "ok",
                "active_profiles": ["Desktop"],
                "devices": {"2234:6678": {"profiles": ["Desktop"]}},
                "window": {},
            }
        )

        assert tab.status_label.get_text() == "active"

        window._status_query_id = 1
        window._status_query_inflight = True
        finished = window._on_status_response({"status": "error"}, 1)

        assert finished is False
        assert tab.status_label.get_text() == "active"

    def test_main_window_partial_runtime_state_preserves_omitted_keys(self, temp_config_dir):
        from keymasq.gui.window import MainWindow

        window = MainWindow(demo_mode=True)
        window._apply_profile_runtime_state(
            {
                "status": "ok",
                "active_profiles": ["Desktop"],
                "devices": {"2234:6678": {"profiles": ["Desktop"]}},
                "window": {"class": "steam"},
            }
        )

        window._status_query_id = 1
        window._status_query_inflight = True
        finished = window._on_status_response(
            {
                "status": "ok",
                "keymasqd_connected": True,
                "recording_unlocked": False,
                "recording_unlock_required": True,
                "recording_unlock_source": "none",
                "recording_unlock_expires_at": 0,
            },
            1,
        )

        assert finished is False
        assert window._profile_runtime_state["active_profiles"] == ["Desktop"]
        assert window._profile_runtime_state["devices"] == {
            "2234:6678": {"profiles": ["Desktop"]}
        }
        assert window._profile_runtime_state["window"] == {"class": "steam"}

    def test_main_window_recording_auth_event_opens_locked_recording_dialog(self, monkeypatch):
        from keymasq.gui.window import MainWindow

        window = MainWindow(demo_mode=True)
        captured: dict[str, object] = {}
        monkeypatch.setattr(
            window,
            "present_recording_settings_dialog",
            lambda reason="settings": captured.setdefault("reason", reason),
        )

        window._handle_session_event({"event": "recording_auth_requested"})

        assert captured["reason"] == "recording_locked"

    def test_main_window_macro_save_pending_event_presents_existing_save_dialog(self):
        from keymasq.gui.window import MainWindow

        window = MainWindow(demo_mode=True)
        presented: list[bool] = []

        class DummyDialog:
            def present(self, parent) -> None:
                presented.append(True)

        window._save_macro_dialog = DummyDialog()  # type: ignore[assignment]

        window._handle_session_event({"event": "macro_save_pending"})

        assert presented == [True]

    def test_main_window_recording_started_closes_tracked_dialogs(self):
        from keymasq.gui.window import MainWindow

        window = MainWindow(demo_mode=True)
        closed: list[str] = []
        overlay_events: list[dict] = []

        class DummyDialog:
            def __init__(self, name: str) -> None:
                self.name = name

            def close(self) -> None:
                closed.append(self.name)

        class DummyOverlay:
            def set_visible(self, visible: bool) -> None:
                overlay_events.append({"visible": visible})

            def on_started(self, event: dict) -> None:
                overlay_events.append(event)

        window._record_macro_dialog = DummyDialog("settings")  # type: ignore[assignment]
        window._macro_manager_dialog = DummyDialog("macros")  # type: ignore[assignment]
        window._recording_overlay = DummyOverlay()  # type: ignore[assignment]

        window._handle_session_event({"event": "recording_started"})

        assert closed == ["settings", "macros"]
        assert overlay_events == [{"visible": True}, {"event": "recording_started"}]

    def test_main_window_recording_stopped_tracks_single_save_macro_dialog(self, monkeypatch):
        import keymasq.gui.widgets.save_macro_dialog as save_macro_dialog_module
        from keymasq.gui.window import MainWindow

        window = MainWindow(demo_mode=True)
        created: list[dict] = []
        presented: list[object] = []

        class DummySaveMacroDialog:
            def __init__(self, parent, event: dict) -> None:
                self.parent = parent
                self.event = event
                created.append(event)

            def connect(self, signal: str, callback) -> None:
                self.signal = signal
                self.callback = callback

            def present(self, parent) -> None:
                presented.append(parent)

        monkeypatch.setattr(save_macro_dialog_module, "SaveMacroDialog", DummySaveMacroDialog)

        window._on_recording_stopped(
            {"event": "recording_stopped", "pending_save_token": "pending-1"}
        )
        first_dialog = window._save_macro_dialog
        window._on_recording_stopped(
            {"event": "recording_stopped", "pending_save_token": "pending-1"}
        )

        assert len(created) == 1
        assert first_dialog is window._save_macro_dialog
        assert presented == [window, window]

        assert window.present_pending_macro_save_dialog() is True
        assert presented == [window, window, window]

        window._on_save_macro_dialog_closed(first_dialog)
        assert window._save_macro_dialog is None

    def test_main_window_ignores_status_response_after_destroy(self, temp_config_dir):
        from keymasq.gui.window import MainWindow

        window = MainWindow(demo_mode=True)
        window._status_query_id = 1
        window._status_query_inflight = True
        window._on_destroy()

        finished = window._on_status_response({"status": "ok", "keymasqd_connected": True}, 1)

        assert finished is False
        assert window._status_query_inflight is True

    def test_main_window_uses_gnome_setup_dialog_instead_of_warning_banner(
        self, temp_config_dir, monkeypatch
    ):
        from keymasq.gui import window as window_module
        from keymasq.gui.window import MainWindow

        window = MainWindow(demo_mode=True)
        window.demo_mode = False
        window._startup_probe_done = True
        window._compositor_id = "gnome"
        window._compositor_supported = True
        window._compositor_support_details = {
            "supported": True,
            "gnome_bridge_state": "protocol_stale",
            "gnome_bridge_action": "logout",
            "warning": "GNOME bridge update detected. Log out and back in.",
        }
        presented: list[object] = []

        monkeypatch.setattr(
            window_module.Adw.Dialog,
            "present",
            lambda self, parent: presented.append((self, parent)),
        )

        window._update_compositor_warning_banner()
        window._present_gnome_setup_dialog()

        assert window.warning_banner.get_revealed() is False
        assert len(presented) == 1
        assert window._gnome_setup_dialog is not None

    def test_main_window_clicking_gnome_limited_status_reopens_setup_dialog(
        self, temp_config_dir, monkeypatch
    ):
        from gi.repository import Gtk  # pyright: ignore[reportAttributeAccessIssue]

        from keymasq.gui import window as window_module
        from keymasq.gui.window import MainWindow

        window = MainWindow(demo_mode=True)
        window.demo_mode = False
        window._startup_probe_done = True
        window._compositor_id = "gnome"
        window._compositor_supported = False
        window._compositor_support_details = {
            "supported": False,
            "gnome_bridge_state": "bridge_disabled",
            "gnome_bridge_action": "enable_bridge",
            "warning": "GNOME bridge disabled.",
        }
        presented: list[object] = []

        monkeypatch.setattr(
            window_module.Adw.Dialog,
            "present",
            lambda self, parent: presented.append((self, parent)),
        )

        window._update_compositor_status()
        window._on_compositor_status_released(Gtk.GestureClick(), 1, 0.0, 0.0)

        assert len(presented) == 1
        assert window._gnome_setup_dialog is not None
        assert "GNOME (limited)" in window.compositor_status.get_label()

    def test_gnome_setup_dialog_enable_bridge_uses_session_ipc(
        self, temp_config_dir, monkeypatch
    ):
        from gi.repository import Gtk  # pyright: ignore[reportAttributeAccessIssue]

        from keymasq.gui.widgets import gnome_setup_dialog as dialog_module
        from keymasq.gui.widgets.gnome_setup_dialog import GnomeSetupDialog

        requests: list[tuple[dict[str, object], float]] = []
        completed: list[str] = []

        def fake_session_request_async(payload, callback, timeout=5.0):
            requests.append((payload, timeout))
            callback(
                {
                    "status": "ok",
                    "message": "GNOME bridge enabled. Waiting for Keymasq to connect.",
                }
            )

        monkeypatch.setattr(dialog_module, "session_request_async", fake_session_request_async)

        parent = Gtk.Window()
        dialog = GnomeSetupDialog(
            parent,
            {
                "gnome_bridge_state": "bridge_disabled",
                "gnome_bridge_action": "enable_bridge",
            },
            on_action_completed=completed.append,
        )

        dialog._on_primary_clicked(Gtk.Button(), "enable_bridge")

        assert requests == [
            (
                {
                    "command": "run_compositor_setup_action",
                    "compositor": "gnome",
                    "action": "enable_bridge",
                },
                5.0,
            )
        ]
        assert completed == ["enable_bridge"]

    def test_gnome_setup_dialog_restart_session_treats_missing_response_as_restart(
        self,
        temp_config_dir,
        monkeypatch,
    ):
        from gi.repository import Gtk  # pyright: ignore[reportAttributeAccessIssue]

        from keymasq.gui.widgets import gnome_setup_dialog as dialog_module
        from keymasq.gui.widgets.gnome_setup_dialog import GnomeSetupDialog

        completed: list[str] = []

        def fake_session_request_async(_payload, callback, timeout=5.0):
            callback(None)

        monkeypatch.setattr(dialog_module, "session_request_async", fake_session_request_async)

        parent = Gtk.Window()
        dialog = GnomeSetupDialog(
            parent,
            {
                "gnome_bridge_state": "protocol_newer",
                "gnome_bridge_action": "restart_session",
            },
            on_action_completed=completed.append,
        )

        dialog._on_primary_clicked(Gtk.Button(), "restart_session")

        assert completed == ["restart_session"]
        assert dialog._status_label is not None  # pyright: ignore[reportPrivateUsage]
        assert (  # pyright: ignore[reportPrivateUsage]
            dialog._status_label.get_text() == "keymasq-session is restarting..."
        )

    def test_gnome_setup_dialog_bridge_disabled_uses_short_prompt(self, temp_config_dir):
        from gi.repository import Gtk  # pyright: ignore[reportAttributeAccessIssue]

        from keymasq.gui.widgets.gnome_setup_dialog import GnomeSetupDialog

        parent = Gtk.Window()
        dialog = GnomeSetupDialog(
            parent,
            {
                "gnome_bridge_state": "bridge_disabled",
                "gnome_bridge_action": "enable_bridge",
            },
        )

        labels: list[str] = []
        buttons: list[str] = []

        def collect(widget) -> None:
            if isinstance(widget, Gtk.Button):
                label = widget.get_label()
                if label:
                    buttons.append(label)
            elif isinstance(widget, Gtk.Label):
                text = widget.get_text()
                if text:
                    labels.append(text)

            child = widget.get_first_child()
            while child is not None:
                collect(child)
                child = child.get_next_sibling()

        child = dialog.get_child()
        assert child is not None
        collect(child)

        assert "Enable GNOME Bridge" in labels
        assert (
            "Enable the bridge for window-aware profiles, GNOME window actions, "
            "and native pointer positioning."
        ) in labels
        assert buttons == ["Not Now", "Enable Bridge"]

    def test_gnome_setup_dialog_shell_rescan_uses_finish_setup_prompt(self, temp_config_dir):
        from gi.repository import Gtk  # pyright: ignore[reportAttributeAccessIssue]

        from keymasq.gui.widgets.gnome_setup_dialog import GnomeSetupDialog

        parent = Gtk.Window()
        dialog = GnomeSetupDialog(
            parent,
            {
                "gnome_bridge_state": "shell_not_rescanned",
                "gnome_bridge_action": "logout",
            },
        )

        labels: list[str] = []
        buttons: list[str] = []

        def collect(widget) -> None:
            if isinstance(widget, Gtk.Button):
                label = widget.get_label()
                if label:
                    buttons.append(label)
            elif isinstance(widget, Gtk.Label):
                text = widget.get_text()
                if text:
                    labels.append(text)

            child = widget.get_first_child()
            while child is not None:
                collect(child)
                child = child.get_next_sibling()

        child = dialog.get_child()
        assert child is not None
        collect(child)

        assert "Finish GNOME Setup" in labels
        assert (
            "Keymasq needs a GNOME extension for window-aware profiles, GNOME window "
            "actions, and native pointer positioning. The extension is installed; log "
            "out and back in once so GNOME can load it."
        ) in labels
        assert buttons == ["Setup Guide", "Log Out"]

    def test_gnome_setup_action_starts_status_poll_without_gui_refresh_command(
        self, temp_config_dir, monkeypatch
    ):
        from keymasq.gui import window as window_module
        from keymasq.gui.window import MainWindow

        window = MainWindow(demo_mode=True)
        requests: list[dict] = []
        polls: list[object] = []

        monkeypatch.setattr(
            window_module,
            "session_request_async",
            lambda payload, callback, timeout=5.0: requests.append(payload),
        )
        monkeypatch.setattr(
            window_module.GLib,
            "timeout_add",
            lambda *_args: polls.append(True) or 9,
        )

        window._on_gnome_setup_action_completed("enable_bridge")

        assert requests == []
        assert polls == [True]
        assert window._gnome_setup_poll_source_id == 9

    def test_gnome_setup_dialog_closes_when_gnome_support_becomes_ready(
        self, temp_config_dir, monkeypatch
    ):
        from keymasq.gui import window as window_module
        from keymasq.gui.window import MainWindow

        window = MainWindow(demo_mode=True)
        window.demo_mode = False
        window._startup_probe_done = True
        window._compositor_id = "gnome"
        window._compositor_supported = False
        window._compositor_support_details = {
            "supported": False,
            "gnome_bridge_state": "bridge_disabled",
            "gnome_bridge_action": "enable_bridge",
            "warning": "GNOME bridge disabled.",
        }
        removed: list[int] = []

        monkeypatch.setattr(
            window_module.Adw.Dialog,
            "present",
            lambda self, parent: None,
        )
        monkeypatch.setattr(
            window_module.GLib,
            "source_remove",
            lambda source_id: removed.append(source_id),
        )

        window._present_gnome_setup_dialog()
        assert window._gnome_setup_dialog is not None
        window._gnome_setup_poll_source_id = 77

        window._on_status_response(
            {
                "status": "ok",
                "keymasqd_connected": True,
                "compositor_id": "gnome",
                "compositor_details": {
                    "supported": True,
                    "gnome_bridge_state": "ready",
                    "gnome_bridge_action": "",
                    "warning": "",
                },
            },
            window._status_query_id,
        )

        assert removed == [77]
        assert window._gnome_setup_dialog is None

    def test_main_window_apply_loaded_devices_updates_empty_state_and_demo_devices(
        self, temp_config_dir
    ):
        from keymasq.gui.icons import resolve_icon_name, device_icon_names
        from keymasq.gui.window import MainWindow

        window = MainWindow(demo_mode=False)

        window._apply_loaded_devices([])

        assert window._placeholder_title is not None
        assert window._placeholder_subtitle is not None
        assert window._placeholder_title.get_label() == "No devices configured"
        assert window._placeholder_subtitle.get_label() == "Click + to add a new device"
        placeholder_page = window._page_for_child(window.placeholder)
        assert placeholder_page is not None
        icon = placeholder_page.get_icon()
        assert icon is not None
        assert icon.to_string() == resolve_icon_name(*device_icon_names(False))

        window._close_tab_page(placeholder_page)
        window._placeholder_page = None
        window._placeholder_title.set_label("Loading devices...")
        window._placeholder_subtitle.set_label(
            "Checking compositor support and loading saved hardware"
        )

        window._check_empty_state()

        assert window._page_for_child(window.placeholder) is not None
        assert window._placeholder_title.get_label() == "No devices configured"
        assert window._placeholder_subtitle.get_label() == "Click + to add a new device"

        demo_window = MainWindow(demo_mode=True)
        demo_window._apply_loaded_devices([])

        demo_tab = demo_window._child_for_hardware_id("1234:5678")

        assert demo_window._page_for_child(demo_window.placeholder) is None
        assert demo_tab.device.name == "Demo Mouse"

    def test_main_window_status_response_updates_labels_for_all_status_paths(
        self, temp_config_dir, monkeypatch
    ):
        from keymasq.gui.window import MainWindow

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
        assert window._on_status_response({"status": "ok", "keymasqd_connected": True}, 2) is False
        assert window._status_query_inflight is True
        assert unlock_updates == []
        assert issues == []

        assert window._on_status_response({"status": "ok", "keymasqd_connected": True}, 3) is False
        assert window.session_status.get_label() == "session: 🟢"
        assert window.keymasqd_status.get_label() == "keymasqd: 🟢"
        assert unlock_updates[-1] == {"status": "ok", "keymasqd_connected": True}
        assert issues[-1] is None

        window._status_query_inflight = True
        assert (
            window._on_status_response({"status": "ok", "keymasqd_connected": False}, 3) is False
        )
        assert window.session_status.get_label() == "session: 🟡"
        assert window.keymasqd_status.get_label() == "keymasqd: 🔴"
        assert issues[-1] == "keymasqd"

        window._status_query_inflight = True
        assert window._on_status_response(None, 3) is False
        assert window.session_status.get_label() == "session: 🔴"
        assert window.keymasqd_status.get_label() == "keymasqd: ⚪"
        assert unlock_updates[-1] is None
        assert issues[-1] == "session"
