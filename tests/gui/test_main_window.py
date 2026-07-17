# ruff: noqa: E402, I001
from types import SimpleNamespace

import pytest

pytest.importorskip("gi")

from keymasq.gui.window import (
    compositor,
    connection,
    device_tabs,
    gnome_setup,
    macro_recording,
    profiles,
    recording_unlock,
    tab_layout,
)


class TestMainWindow:
    def test_main_window_restores_selected_profile(self, temp_config_dir):
        from keymasq.common.model.hardware import ButtonDefinition, HardwareConfig
        from keymasq.common.model.profiles import ProfileConfig
        from keymasq.gui.preferences import save_selected_profile
        from keymasq.gui.window.core import MainWindow
        from keymasq.session.profile.manager import ProfileManager

        profile_manager = ProfileManager()
        profile_manager.save_profile(
            ProfileConfig(name="Alpha", enabled=True, is_permanent=True)
        )
        profile_manager.save_profile(
            ProfileConfig(name="Gaming", enabled=True, is_permanent=True)
        )
        save_selected_profile("Gaming")

        window = MainWindow(demo_mode=True)
        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Mouse One",
            evdev_devices=[],
            buttons=[ButtonDefinition(id="btn_back", label="Back", evdev="btn_side")],
        )
        device_tabs._add_device_tab(window, device)
        device_tab = tab_layout._child_for_hardware_id(window, device.hardware_id)

        assert window._selected_profile_name == "Gaming"
        assert window.combo_tab is not None
        assert window.combo_tab.selected_profile_name() == "Gaming"
        assert device_tab.selected_profile_name() == "Gaming"

    def test_main_window_missing_selected_profile_falls_back_to_first(
        self, temp_config_dir
    ):
        from keymasq.common.model.profiles import ProfileConfig
        from keymasq.gui.preferences import load_selected_profile, save_selected_profile
        from keymasq.gui.window.core import MainWindow
        from keymasq.session.profile.manager import ProfileManager

        profile_manager = ProfileManager()
        profile_manager.save_profile(
            ProfileConfig(name="Zulu", enabled=True, is_permanent=True)
        )
        profile_manager.save_profile(
            ProfileConfig(name="Alpha", enabled=True, is_permanent=True)
        )
        save_selected_profile("Missing")

        window = MainWindow(demo_mode=True)

        assert window._selected_profile_name == "Alpha"
        assert window.combo_tab is not None
        assert window.combo_tab.selected_profile_name() == "Alpha"
        assert load_selected_profile() == "Alpha"

    def test_main_window_does_not_seed_default_profile_for_first_device(self, temp_config_dir):
        from keymasq.common.model.hardware import ButtonDefinition, HardwareConfig
        from keymasq.gui.window.core import MainWindow

        window = MainWindow(demo_mode=True)

        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Mouse One",
            evdev_devices=[],
            buttons=[ButtonDefinition(id="btn_back", label="Back", evdev="btn_side")],
        )

        device_tabs._add_device_tab(window, device)

        tab = tab_layout._child_for_hardware_id(window, device.hardware_id)

        assert window.profile_manager.get_profile("Default") is None
        assert not (temp_config_dir / "profiles" / "Default.toml").exists()
        assert tab._selected_profile is None
        assert tab.settings_frame.get_sensitive() is False

    def test_gui_profile_reload_snapshot_does_not_seed_default(self, temp_config_dir):
        manager = profiles._load_profile_manager_snapshot(object())

        assert manager.list_profiles() == []
        assert not (temp_config_dir / "profiles" / "Default.toml").exists()

    def test_main_window_demo_mode(self, temp_config_dir):
        from keymasq.common.model.hardware import ButtonDefinition, HardwareConfig
        from keymasq.common.model.profiles import DeviceProfileLayer, ProfileConfig
        from keymasq.gui.window.core import MainWindow

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

        device_tabs._add_device_tab(window, device1)
        device_tabs._add_device_tab(window, device2)

        tab1 = tab_layout._child_for_hardware_id(window, device1.hardware_id)
        tab2 = tab_layout._child_for_hardware_id(window, device2.hardware_id)

        tab1.refresh_profiles(preferred_profile_name="Profile 2")
        page2 = tab_layout._page_for_hardware_id(window, device2.hardware_id)
        assert page2 is not None
        window.tab_view.set_selected_page(page2)

        assert tab2._selected_profile is not None
        assert tab2._selected_profile.config.name == "Profile 2"

    def test_main_window_demo_mode_uses_sample_startup_without_system_probe(
        self, temp_config_dir, monkeypatch
    ):
        from keymasq.gui.window import _runtime as window_runtime
        from keymasq.gui.window import core as window_core
        from keymasq.gui.window.core import MainWindow

        def fail_system_probe(*args, **kwargs):
            raise AssertionError("demo startup should not probe the real system")

        monkeypatch.setattr(window_runtime, "session_request", fail_system_probe)
        monkeypatch.setattr(window_core.HardwareManager, "list_hardware", fail_system_probe)
        monkeypatch.setattr(window_runtime, "run_gui_task", fail_system_probe)
        monkeypatch.setattr(
            window_runtime.GLib,
            "idle_add",
            lambda callback, *args: callback(*args),
        )

        window = MainWindow(demo_mode=True)
        demo_tab = tab_layout._child_for_hardware_id(window, "1234:5678")

        assert window._startup_probe_done is True
        assert demo_tab is not None
        assert demo_tab.device.name == "Demo Mouse"

    def test_main_window_add_device_action_does_not_require_unlock(
        self, temp_config_dir, monkeypatch
    ):
        from keymasq.gui.window.core import MainWindow

        window = MainWindow(demo_mode=True)
        add_calls: list[bool] = []
        unlock_calls: list[bool] = []
        button = object()

        window._recording_unlocked = False
        monkeypatch.setattr(
            device_tabs,
            "_on_add_device",
            lambda _window, _button: add_calls.append(True),
        )
        window.present_unlock_dialog = lambda on_success=None: unlock_calls.append(True)  # type: ignore[method-assign]

        device_tabs._on_add_device_clicked(window, button)

        assert add_calls == [True]
        assert unlock_calls == []

    def test_main_window_device_inspector_uses_unlock_flow(self, temp_config_dir, monkeypatch):
        from keymasq.common.model.hardware import ButtonDefinition, HardwareConfig
        from keymasq.gui.window.core import MainWindow

        window = MainWindow(demo_mode=True)
        window.demo_mode = False
        window._recording_unlock_required = True
        window._recording_unlocked = False
        window._recording_refresh_owner = False
        unlock_callbacks = []
        monkeypatch.setattr(
            recording_unlock,
            "present_unlock_dialog",
            lambda _window, on_success=None: unlock_callbacks.append(on_success),
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
        from keymasq.common.model.hardware import ButtonDefinition, HardwareConfig
        from keymasq.gui.widgets import device_inspector_window as inspector_module
        from keymasq.gui.window.core import MainWindow

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

    def test_main_window_combo_inspector_reuses_open_window(
        self,
        temp_config_dir,
        monkeypatch,
    ):
        from keymasq.gui.widgets import combo_inspector_window as inspector_module
        from keymasq.gui.window.core import MainWindow

        window = MainWindow(demo_mode=True)
        window.demo_mode = False
        present_calls: list[str] = []
        close_callbacks = []
        instances: list[object] = []

        class FakeInspector:
            def __init__(self, parent):
                self.parent = parent
                instances.append(self)

            def connect(self, signal, callback):
                if signal == "close-request":
                    close_callbacks.append((self, callback))
                return 1

            def present(self):
                present_calls.append("present")

            def close(self):
                pass

        monkeypatch.setattr(inspector_module, "ComboInspectorWindow", FakeInspector)

        window.open_combo_inspector()
        window.open_combo_inspector()

        assert present_calls == ["present", "present"]
        assert len(instances) == 1
        assert window._combo_inspector_window is instances[0]

        inspector = window._combo_inspector_window
        assert close_callbacks[-1][1](inspector) is False
        assert window._combo_inspector_window is None

        window.open_combo_inspector()

        assert len(instances) == 2
        assert window._combo_inspector_window is instances[1]
        assert present_calls == ["present", "present", "present"]

    def test_main_window_menu_reflects_saved_appearance(self, temp_config_dir):
        from keymasq.gui.preferences import save_appearance_mode
        from keymasq.gui.window.core import MainWindow

        save_appearance_mode("dark")

        window = MainWindow(demo_mode=True)

        assert set(window._appearance_buttons) == {"system", "light", "dark"}
        assert window._appearance_buttons["dark"].get_active() is True

    def test_main_window_unlock_uses_runtime_polkit_without_prompt(
        self, temp_config_dir, monkeypatch
    ):
        from keymasq.gui.window import _runtime as window_runtime
        from keymasq.gui.session_client import GuiTaskResult
        from keymasq.gui.window.core import MainWindow

        window = MainWindow(demo_mode=True)
        window.demo_mode = False
        commands: list[list[str]] = []
        alerts: list[object] = []
        success_calls: list[bool] = []

        monkeypatch.setattr(
            window_runtime,
            "resolve_keymasq_record_helper_path",
            lambda: "/usr/bin/keymasq-record",
        )
        monkeypatch.setattr(
            window_runtime,
            "run_gui_task",
            lambda worker, callback: callback(GuiTaskResult(value=worker())),
        )

        def fake_run(cmd, capture_output, text):
            commands.append(cmd)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(window_runtime.subprocess, "run", fake_run)
        monkeypatch.setattr(
            window_runtime,
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
            window_runtime.Adw.AlertDialog,
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
                str(window_runtime.os.getuid()),
                "--ttl",
                "60",
            ]
        ]
        assert all("unlock-persistent" not in cmd for cmd in commands)
        assert window._recording_refresh_lease_id == "lease-1"
        assert success_calls == [True]
        assert alerts == []

    def test_main_window_syncs_manual_profile_selection_across_tabs(self, temp_config_dir):
        from keymasq.common.model.hardware import ButtonDefinition, HardwareConfig
        from keymasq.common.model.profiles import DeviceProfileLayer, ProfileConfig
        from keymasq.gui.preferences import load_selected_profile
        from keymasq.gui.window.core import MainWindow

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

        device_tabs._add_device_tab(window, device1)
        device_tabs._add_device_tab(window, device2)

        tab1 = tab_layout._child_for_hardware_id(window, device1.hardware_id)
        tab2 = tab_layout._child_for_hardware_id(window, device2.hardware_id)

        tab1.profile_dropdown.set_selected(tab1._profile_names.index("Desktop"))

        assert window._selected_profile_name == "Desktop"
        assert load_selected_profile() == "Desktop"
        assert tab1._selected_profile is not None
        assert tab1._selected_profile.config.name == "Desktop"
        assert tab2._selected_profile is not None
        assert tab2._selected_profile.config.name == "Desktop"

    def test_created_profile_is_selected_and_reloaded(self, temp_config_dir, monkeypatch):
        from keymasq.common.model.hardware import ButtonDefinition, HardwareConfig
        from keymasq.common.model.profiles import ProfileConfig
        from keymasq.gui.widgets import profile_managed_tab as profile_tab_module
        from keymasq.gui.window.core import MainWindow
        from keymasq.session.profile.manager import ProfileManager

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
        device_tabs._add_device_tab(window, device)
        original_profile_manager = window.profile_manager
        profiles._set_profile_manager(window, ProfileManager())
        assert window.profile_manager.get_profile("Gaming") is None
        original_profile_manager.save_profile(
            ProfileConfig(name="Gaming", enabled=True, is_permanent=True)
        )

        tab = tab_layout._child_for_hardware_id(window, device.hardware_id)

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
        from keymasq.common.model.hardware import ButtonDefinition, HardwareConfig
        from keymasq.gui.window.core import MainWindow

        window = MainWindow(demo_mode=True)
        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Mouse One",
            evdev_devices=[],
            buttons=[ButtonDefinition(id="btn_back", label="Back", evdev="btn_side")],
        )

        device_tabs._add_device_tab(window, device)
        page = tab_layout._page_for_hardware_id(window, device.hardware_id)
        assert page is not None
        assert page.get_title() == "Mouse One"

        window.update_device_display_name(device.hardware_id, "Desk Mouse")

        assert page.get_title() == "Desk Mouse"

    def test_main_window_persists_user_tab_order(self, temp_config_dir):
        from keymasq.common.model.hardware import ButtonDefinition, HardwareConfig
        from keymasq.gui.preferences import load_tab_order
        from keymasq.gui.window.core import MainWindow

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
            device_tabs._add_device_tab(window, device)

        third_page = tab_layout._page_for_hardware_id(window, devices[2].hardware_id)
        assert third_page is not None

        window.tab_view.reorder_page(third_page, window.tab_view.get_n_pinned_pages())

        expected_order = [
            f"device:{devices[2].hardware_id}",
            f"device:{devices[0].hardware_id}",
            f"device:{devices[1].hardware_id}",
            "combos",
        ]
        assert tab_layout._current_tab_order(window) == expected_order
        assert load_tab_order() == expected_order

    def test_main_window_applies_saved_tab_order_on_load(self, temp_config_dir):
        from keymasq.common.model.hardware import ButtonDefinition, HardwareConfig
        from keymasq.gui.preferences import save_tab_layout
        from keymasq.gui.window.core import MainWindow

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
        device_tabs._apply_loaded_devices(window, devices)

        assert tab_layout._current_tab_order(window) == expected_order

        device_tabs._apply_loaded_devices(window, devices)

        assert tab_layout._current_tab_order(window) == expected_order
        assert window.tab_view.get_n_pages() == 4

    def test_main_window_persists_selected_tab(self, temp_config_dir):
        from keymasq.common.model.hardware import ButtonDefinition, HardwareConfig
        from keymasq.gui.preferences import load_selected_tab
        from keymasq.gui.window.core import MainWindow

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
        device_tabs._apply_loaded_devices(window, devices)

        second_page = tab_layout._page_for_hardware_id(window, devices[1].hardware_id)
        combo_page = tab_layout._page_for_child(window, window.combo_tab)
        assert second_page is not None
        assert combo_page is not None

        window.tab_view.set_selected_page(second_page)

        assert load_selected_tab() == devices[1].hardware_id

        window.tab_view.set_selected_page(combo_page)

        assert load_selected_tab() == "combos"

    def test_main_window_applies_saved_selected_device_tab_on_load(self, temp_config_dir):
        from keymasq.common.model.hardware import ButtonDefinition, HardwareConfig
        from keymasq.gui.preferences import save_selected_tab, save_tab_layout
        from keymasq.gui.window.core import MainWindow

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
        save_tab_layout(
            [f"device:{devices[0].hardware_id}", "combos", f"device:{devices[1].hardware_id}"],
            set(),
        )
        save_selected_tab(devices[1].hardware_id)

        window = MainWindow(demo_mode=True)
        device_tabs._apply_loaded_devices(window, devices)

        assert window.tab_view.get_selected_page() is tab_layout._page_for_hardware_id(
            window, devices[1].hardware_id
        )

    def test_main_window_applies_saved_selected_combo_tab_on_load(self, temp_config_dir):
        from keymasq.common.model.hardware import ButtonDefinition, HardwareConfig
        from keymasq.gui.preferences import save_selected_tab, save_tab_layout
        from keymasq.gui.window.core import MainWindow

        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Mouse One",
            evdev_devices=[],
            buttons=[ButtonDefinition(id="btn_back", label="Back", evdev="btn_side")],
        )
        save_tab_layout([f"device:{device.hardware_id}", "combos"], set())
        save_selected_tab("combos")

        window = MainWindow(demo_mode=True)
        device_tabs._apply_loaded_devices(window, [device])

        assert window.combo_tab is not None
        assert window.tab_view.get_selected_page() is tab_layout._page_for_child(
            window, window.combo_tab
        )

    def test_main_window_invalid_selected_tab_falls_back_to_saved_order(self, temp_config_dir):
        from keymasq.common.model.hardware import ButtonDefinition, HardwareConfig
        from keymasq.gui.preferences import load_selected_tab, save_selected_tab, save_tab_layout
        from keymasq.gui.window.core import MainWindow

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
        save_tab_layout(
            [f"device:{devices[1].hardware_id}", "combos", f"device:{devices[0].hardware_id}"],
            set(),
        )
        save_selected_tab("missing-hardware")

        window = MainWindow(demo_mode=True)
        device_tabs._apply_loaded_devices(window, devices)

        assert window.tab_view.get_selected_page() is tab_layout._page_for_hardware_id(
            window, devices[1].hardware_id
        )
        assert load_selected_tab() == devices[1].hardware_id

    def test_main_window_hides_and_restores_combo_tab_from_menu(self, temp_config_dir):
        from keymasq.common.model.hardware import ButtonDefinition, HardwareConfig
        from keymasq.gui.preferences import load_hidden_tabs, load_tab_order
        from keymasq.gui.window.core import MainWindow

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
            device_tabs._add_device_tab(window, device)

        assert window.combo_tab is not None
        combo_page = tab_layout._page_for_child(window, window.combo_tab)
        assert combo_page is not None
        assert window.tab_bar.get_start_action_widget() is not None
        assert window.tab_view.get_n_pages() == 3

        window.tab_view.reorder_page(combo_page, window.tab_view.get_n_pinned_pages() + 1)
        expected_order = [
            f"device:{devices[0].hardware_id}",
            "combos",
            f"device:{devices[1].hardware_id}",
        ]

        assert tab_layout._current_tab_order(window) == expected_order
        assert load_tab_order() == expected_order

        window.tab_view.close_page(combo_page)

        assert window.combo_tab is None
        assert tab_layout._current_tab_order(window) == [
            f"device:{devices[0].hardware_id}",
            f"device:{devices[1].hardware_id}",
        ]
        assert load_tab_order() == expected_order
        assert load_hidden_tabs() == {"combos"}

        window.show_combo_tab()

        assert window.combo_tab is not None
        restored_page = tab_layout._page_for_child(window, window.combo_tab)
        assert restored_page is not None
        assert window.tab_view.get_selected_page() is restored_page
        assert tab_layout._current_tab_order(window) == expected_order
        assert load_tab_order() == expected_order
        assert load_hidden_tabs() == set()

    def test_main_window_tab_close_requests_device_delete(self, temp_config_dir):
        from keymasq.common.model.hardware import ButtonDefinition, HardwareConfig
        from keymasq.gui.window.core import MainWindow

        window = MainWindow(demo_mode=True)
        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Mouse One",
            evdev_devices=[],
            buttons=[ButtonDefinition(id="btn_back", label="Back", evdev="btn_side")],
        )
        device_tabs._add_device_tab(window, device)

        tab = tab_layout._child_for_hardware_id(window, device.hardware_id)
        page = tab_layout._page_for_hardware_id(window, device.hardware_id)
        assert tab is not None
        assert page is not None

        delete_requests: list[bool] = []
        tab.present_delete_device_dialog = lambda: delete_requests.append(True)  # type: ignore[attr-defined, method-assign]

        window.tab_view.close_page(page)

        assert delete_requests == [True]
        assert tab_layout._page_for_hardware_id(window, device.hardware_id) is page

    def test_main_window_startup_probe_applies_compositor_state_and_devices(self, temp_config_dir):
        from keymasq.common.model.hardware import ButtonDefinition, HardwareConfig
        from keymasq.common.model.profiles import DeviceProfileLayer, ProfileConfig, WindowRule
        from keymasq.gui.session_client import GuiTaskResult
        from keymasq.gui.window.core import MainWindow

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

        finished = compositor._on_startup_probe_finished(
            window,
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
            ),
        )

        device_tab = tab_layout._child_for_hardware_id(window, device.hardware_id)

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

    def test_main_window_startup_probe_ignores_result_after_destroy(self, temp_config_dir):
        from keymasq.gui.session_client import GuiTaskResult
        from keymasq.gui.window.core import MainWindow

        window = MainWindow(demo_mode=True)
        window._destroyed = True

        finished = compositor._on_startup_probe_finished(
            window,
            GuiTaskResult(
                value=(
                    {
                        "compositor_id": "hyprland",
                        "support_details": {"supported": True, "warning": ""},
                        "supported": True,
                        "capabilities": ["window_tags"],
                    },
                    [],
                )
            ),
        )

        assert finished is False
        assert window._startup_probe_done is False
        assert window._compositor_id is None

    def test_main_window_startup_probe_asks_session_for_compositor(
        self, temp_config_dir, monkeypatch
    ):
        from keymasq.gui.window import _runtime as window_runtime
        from keymasq.gui.window.core import MainWindow

        window = MainWindow(demo_mode=True)
        window.demo_mode = False
        requests: list[dict] = []

        def fake_session_request(payload, timeout=5.0):
            requests.append(payload)
            return {
                "compositor_id": "niri",
                "compositor_name": "Niri",
                "supported": True,
                "capabilities": ["window_tags"],
                "details": {"supported": True, "warning": ""},
                "listener_active": True,
                "listener_name": "niri",
                "compositor_dispatch_available": True,
            }

        monkeypatch.setattr(window_runtime, "session_request", fake_session_request)
        monkeypatch.setattr(window.hardware_manager, "list_hardware", lambda: [])

        state, devices = compositor._probe_startup_state(window)

        assert requests == [{"command": "get_compositor"}]
        assert state == {
            "compositor_id": "niri",
            "support_details": {"supported": True, "warning": ""},
            "supported": True,
            "capabilities": ["window_tags"],
        }
        assert devices == []

    def test_main_window_startup_probe_without_session_reports_unknown_compositor(
        self, temp_config_dir, monkeypatch
    ):
        from keymasq.gui.window import _runtime as window_runtime
        from keymasq.gui.window.core import MainWindow

        window = MainWindow(demo_mode=True)
        window.demo_mode = False
        monkeypatch.setattr(
            window_runtime, "session_request", lambda payload, timeout=5.0: None
        )
        monkeypatch.setattr(window.hardware_manager, "list_hardware", lambda: [])

        state, devices = compositor._probe_startup_state(window)

        assert state == {
            "compositor_id": None,
            "support_details": {"supported": False, "warning": ""},
            "supported": False,
            "capabilities": [],
        }
        assert devices == []

    def test_main_window_compositor_state_updates_shared_session_cache(self, temp_config_dir):
        from keymasq.gui.compositor_state import session_compositor_id
        from keymasq.gui.window.core import MainWindow

        window = MainWindow(demo_mode=True)

        compositor._apply_compositor_state(
            window,
            {
                "compositor_id": "hyprland",
                "support_details": {"supported": True, "warning": ""},
                "supported": True,
                "capabilities": ["window_tags"],
            },
        )

        assert session_compositor_id() == "hyprland"

        compositor._update_compositor_dispatch_state(
            window,
            {"status": "ok", "compositor_id": "niri", "listener_name": "niri"},
        )

        assert session_compositor_id() == "niri"

        compositor._update_compositor_dispatch_state(
            window,
            {"status": "ok", "listener_name": "niri"},
        )

        assert window._compositor_id == "niri"
        assert session_compositor_id() == "niri"

        compositor._update_compositor_dispatch_state(
            window,
            {"status": "ok", "compositor_id": None, "listener_name": ""},
        )

        assert window._compositor_id is None
        assert session_compositor_id() is None

    def test_main_window_profiles_changed_event_updates_tabs_without_polling(self, temp_config_dir):
        from keymasq.common.model.hardware import ButtonDefinition, HardwareConfig
        from keymasq.common.model.profiles import DeviceProfileLayer, ProfileConfig
        from keymasq.gui.window.core import MainWindow

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

        device_tabs._add_device_tab(window, device)
        tab = tab_layout._child_for_hardware_id(window, device.hardware_id)
        tab.profile_dropdown.set_selected(tab._profile_names.index("Gaming"))

        connection._handle_session_event(
            window,
            {
                "event": "profiles_changed",
                "status": "ok",
                "active_profiles": ["Gaming"],
                "devices": {"2234:6678": {"profiles": ["Gaming"]}},
            },
        )

        assert tab._active_profile_names == ["Gaming"]
        assert tab.active_profiles_title_label.get_text() == "Applied profiles:"
        assert (
            tab.active_profiles_label.get_tooltip_text() == "Applied profiles. Layer order: Gaming"
        )
        assert tab.status_label.get_text() == "active"
        assert window.combo_tab is not None
        assert window.combo_tab.active_profiles_title_label.get_text() == "Active profiles:"
        assert window.combo_tab._active_profile_names == ["Gaming"]
        assert window.combo_tab.status_label.get_text() == "active"

    def test_main_window_device_runtime_status_updates_tab_title_and_header(
        self,
        temp_config_dir,
        monkeypatch,
    ):
        from keymasq.common.model.hardware import ButtonDefinition, EvdevDevice, HardwareConfig
        from keymasq.common.model.core import DeviceType
        from keymasq.gui.window import _runtime as window_runtime
        from keymasq.gui.window.core import MainWindow

        monkeypatch.setattr(window_runtime, "run_gui_task", lambda worker, callback: None)
        monkeypatch.setattr(
            window_runtime, "session_request_async", lambda *args, **kwargs: None
        )
        monkeypatch.setattr(
            window_runtime, "register_session_event_callback", lambda *args: None
        )
        monkeypatch.setattr(
            window_runtime, "unregister_session_event_callback", lambda *args: None
        )
        monkeypatch.setattr(window_runtime.GLib, "timeout_add", lambda *args: 0)
        monkeypatch.setattr(window_runtime.GLib, "timeout_add_seconds", lambda *args: 0)

        window = MainWindow(demo_mode=False)
        device = HardwareConfig(
            vendor_id="2234",
            product_id="6678",
            name="Pad One",
            evdev_devices=[
                EvdevDevice(
                    path="/dev/input/by-id/pad-event-joystick",
                    device_type=DeviceType.GAMEPAD,
                    id="gamepad",
                )
            ],
            buttons=[ButtonDefinition(id="btn_south", label="South", evdev="btn_south")],
        )

        device_tabs._add_device_tab(window, device)
        tab = tab_layout._child_for_hardware_id(window, device.hardware_id)
        page = tab_layout._page_for_hardware_id(window, device.hardware_id)
        assert tab is not None
        assert page is not None

        profiles._apply_profile_runtime_state(
            window,
            {
                "status": "ok",
                "active_profiles": [],
                "devices": {
                    device.hardware_id: {
                        "profiles": [],
                        "device_status": {
                            "state": "grabbed",
                            "configured_count": 1,
                            "connected_count": 1,
                            "requested_count": 1,
                            "grabbed_count": 1,
                            "runtime_ready": True,
                            "interfaces": [
                                {
                                    "id": "gamepad",
                                    "configured_path": "/dev/input/by-id/pad-event-joystick",
                                    "type": "gamepad",
                                    "connected": True,
                                    "requested": True,
                                    "grabbed": True,
                                    "current_path": "/dev/input/event10",
                                    "stable_path": "/dev/input/by-id/pad-event-joystick",
                                }
                            ],
                        },
                    }
                },
            },
        )

        assert page.get_title() == "🟢 Pad One"
        assert tab._device_status_label.get_text() == "Grabbed"
        assert "1 interface · 1 connected · 1 grabbed" in tab._header_caption_label.get_text()
        assert "1/1" not in tab._header_caption_label.get_text()
        tooltip = tab._header_caption_label.get_tooltip_text() or ""
        assert "connected, grabbed" in tooltip

    def test_main_window_profiles_changed_event_reloads_profile_models(
        self,
        temp_config_dir,
        monkeypatch,
    ):
        from keymasq.common.model.hardware import ButtonDefinition, HardwareConfig
        from keymasq.common.model.profiles import DeviceProfileLayer, ProfileConfig
        from keymasq.gui.window import _runtime as window_runtime
        from keymasq.gui.session_client import GuiTaskResult
        from keymasq.gui.window.core import MainWindow
        from keymasq.session.profile.manager import ProfileManager

        monkeypatch.setattr(
            window_runtime,
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

        device_tabs._add_device_tab(window, device)
        external_profiles = ProfileManager()
        external_profiles.save_profile(
            ProfileConfig(
                name="Gaming",
                enabled=True,
                is_permanent=True,
                device_layers={"2234:6678": DeviceProfileLayer(hardware_id="2234:6678")},
            )
        )

        connection._handle_session_event(
            window,
            {
                "event": "profiles_changed",
                "status": "ok",
                "active_profiles": ["Gaming"],
                "devices": {"2234:6678": {"profiles": ["Gaming"]}},
            },
        )

        tab = tab_layout._child_for_hardware_id(window, device.hardware_id)
        assert "Gaming" in tab._profile_names
        assert window.combo_tab is not None
        assert "Gaming" in window.combo_tab._profile_names

    def test_main_window_destroy_removes_repeating_timeout_sources(
        self, temp_config_dir, monkeypatch
    ):
        from keymasq.gui.window import _runtime as window_runtime
        from keymasq.gui.window.core import MainWindow

        removed: list[int] = []
        registered: list[tuple[str, object]] = []
        unregistered: list[tuple[str, object]] = []

        monkeypatch.setattr(window_runtime, "run_gui_task", lambda worker, callback: None)
        monkeypatch.setattr(
            window_runtime, "session_request_async", lambda *args, **kwargs: None
        )
        monkeypatch.setattr(
            window_runtime,
            "register_session_event_callback",
            lambda event, callback: registered.append((event, callback)),
        )
        monkeypatch.setattr(
            window_runtime,
            "unregister_session_event_callback",
            lambda event, callback: unregistered.append((event, callback)),
        )
        monkeypatch.setattr(window_runtime.GLib, "timeout_add", lambda interval, cb: 11)
        monkeypatch.setattr(
            window_runtime.GLib,
            "timeout_add_seconds",
            lambda interval, cb: 22,
        )
        monkeypatch.setattr(
            window_runtime.GLib,
            "source_remove",
            lambda source_id: removed.append(source_id),
        )

        window = MainWindow(demo_mode=False)
        window._on_destroy()

        assert window._session_event_callback is not None
        assert registered == [("*", window._session_event_callback)]
        assert removed == [11, 22]
        assert unregistered == [("*", window._session_event_callback)]

    def test_main_window_macro_recording_dialog_refreshes_session_status(
        self,
        temp_config_dir,
        monkeypatch,
    ):
        from keymasq.gui.window import _runtime as window_runtime
        from keymasq.gui.window.core import MainWindow

        requests: list[tuple[dict[str, object], float]] = []

        def fake_session_request_async(payload, callback, timeout=5.0):
            requests.append((payload, timeout))
            callback(
                {
                    "status": "ok",
                    "macro_recording_enabled": True,
                    "macro_recording_source": "persistent",
                    "macro_recording_expires_at": 0,
                }
            )

        monkeypatch.setattr(
            window_runtime, "session_request_async", fake_session_request_async
        )

        window = MainWindow(demo_mode=True)
        requests.clear()
        window._macro_recording_enabled = False
        called: list[bool] = []

        window.present_macro_recording_enable_dialog(on_success=lambda: called.append(True))

        assert requests == [({"command": "get_status"}, 1.0)]
        assert window._macro_recording_enabled is True
        assert called == [True]

    def test_main_window_status_error_keeps_last_runtime_profile_state(self, temp_config_dir):
        from keymasq.common.model.hardware import ButtonDefinition, HardwareConfig
        from keymasq.common.model.profiles import DeviceProfileLayer, ProfileConfig
        from keymasq.gui.window.core import MainWindow

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

        device_tabs._add_device_tab(window, device)
        tab = tab_layout._child_for_hardware_id(window, device.hardware_id)
        tab.refresh_profiles(preferred_profile_name="Desktop")
        profiles._apply_profile_runtime_state(
            window,
            {
                "status": "ok",
                "active_profiles": ["Desktop"],
                "devices": {"2234:6678": {"profiles": ["Desktop"]}},
                "window": {},
            },
        )

        assert tab.status_label.get_text() == "active"

        window._status_query_id = 1
        window._status_query_inflight = True
        finished = connection._on_status_response(window, {"status": "error"}, 1)

        assert finished is False
        assert tab.status_label.get_text() == "active"

    def test_main_window_partial_runtime_state_preserves_omitted_keys(self, temp_config_dir):
        from keymasq.gui.window.core import MainWindow

        window = MainWindow(demo_mode=True)
        profiles._apply_profile_runtime_state(
            window,
            {
                "status": "ok",
                "active_profiles": ["Desktop"],
                "devices": {"2234:6678": {"profiles": ["Desktop"]}},
                "window": {"class": "steam"},
            },
        )

        window._status_query_id = 1
        window._status_query_inflight = True
        finished = connection._on_status_response(
            window,
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
        assert window._profile_runtime_state["devices"] == {"2234:6678": {"profiles": ["Desktop"]}}
        assert window._profile_runtime_state["window"] == {"class": "steam"}

    def test_main_window_successful_initial_status_queues_profile_reload_once(
        self,
        temp_config_dir,
        monkeypatch,
    ):
        from keymasq.gui.window.core import MainWindow

        window = MainWindow(demo_mode=True)
        window.demo_mode = False
        reloads: list[object] = []
        monkeypatch.setattr(
            profiles,
            "_queue_profile_reload",
            lambda target: reloads.append(target),
        )

        window._status_query_id = 1
        window._status_query_inflight = True
        finished = connection._on_status_response(
            window,
            {"status": "ok", "keymasqd_connected": False},
            1,
        )

        assert finished is False
        assert reloads == [window]
        assert window._initial_status_profile_reload_done is True

        window._status_query_id = 2
        window._status_query_inflight = True
        connection._on_status_response(
            window,
            {"status": "ok", "keymasqd_connected": True},
            2,
        )

        assert reloads == [window]

    def test_main_window_recording_auth_event_opens_locked_recording_dialog(self, monkeypatch):
        from keymasq.gui.window.core import MainWindow

        window = MainWindow(demo_mode=True)
        captured: dict[str, object] = {}
        monkeypatch.setattr(
            macro_recording,
            "present_recording_settings_dialog",
            lambda _window, reason="settings": captured.setdefault("reason", reason),
        )

        connection._handle_session_event(window, {"event": "recording_auth_requested"})

        assert captured["reason"] == "recording_locked"

    def test_main_window_recording_started_closes_tracked_dialogs(self):
        from keymasq.gui.window.core import MainWindow

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

        connection._handle_session_event(window, {"event": "recording_started"})

        assert closed == ["settings", "macros"]
        assert overlay_events == [{"visible": True}, {"event": "recording_started"}]

    def test_main_window_recording_stopped_tracks_single_save_macro_dialog(self, monkeypatch):
        import keymasq.gui.widgets.save_macro_dialog as save_macro_dialog_module
        from keymasq.gui.window.core import MainWindow

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

        macro_recording._on_recording_stopped(
            window, {"event": "recording_stopped", "pending_save_token": "pending-1"}
        )
        first_dialog = window._save_macro_dialog
        macro_recording._on_recording_stopped(
            window, {"event": "recording_stopped", "pending_save_token": "pending-1"}
        )

        assert len(created) == 1
        assert first_dialog is window._save_macro_dialog
        assert presented == [window, window]

        assert window.present_pending_macro_save_dialog() is True
        assert presented == [window, window, window]

        macro_recording._on_save_macro_dialog_closed(window, first_dialog)
        assert window._save_macro_dialog is None

    def test_main_window_ignores_status_response_after_destroy(self, temp_config_dir):
        from keymasq.gui.window.core import MainWindow

        window = MainWindow(demo_mode=True)
        window._status_query_id = 1
        window._status_query_inflight = True
        window._on_destroy()

        finished = connection._on_status_response(
            window, {"status": "ok", "keymasqd_connected": True}, 1
        )

        assert finished is False
        assert window._status_query_inflight is True

    def test_main_window_uses_gnome_setup_dialog_instead_of_warning_banner(
        self, temp_config_dir, monkeypatch
    ):
        from keymasq.gui.window import _runtime as window_runtime
        from keymasq.gui.window.core import MainWindow

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
            window_runtime.Adw.Dialog,
            "present",
            lambda self, parent: presented.append((self, parent)),
        )

        compositor._update_compositor_warning_banner(window)
        gnome_setup._present_gnome_setup_dialog(window)

        assert window.warning_banner.get_revealed() is False
        assert len(presented) == 1
        assert window._gnome_setup_dialog is not None

    def test_main_window_clicking_gnome_limited_status_reopens_setup_dialog(
        self, temp_config_dir, monkeypatch
    ):
        from gi.repository import Gtk  # pyright: ignore[reportAttributeAccessIssue]

        from keymasq.gui.window import _runtime as window_runtime
        from keymasq.gui.window.core import MainWindow

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
            window_runtime.Adw.Dialog,
            "present",
            lambda self, parent: presented.append((self, parent)),
        )

        compositor._update_compositor_status(window)
        gnome_setup._on_compositor_status_released(window, Gtk.GestureClick(), 1, 0.0, 0.0)

        assert len(presented) == 1
        assert window._gnome_setup_dialog is not None
        assert "GNOME (limited)" in window.compositor_status.get_label()

    def test_gnome_setup_dialog_enable_bridge_uses_session_ipc(self, temp_config_dir, monkeypatch):
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
        from keymasq.gui.window import _runtime as window_runtime
        from keymasq.gui.window.core import MainWindow

        window = MainWindow(demo_mode=True)
        requests: list[dict] = []
        polls: list[object] = []

        monkeypatch.setattr(
            window_runtime,
            "session_request_async",
            lambda payload, callback, timeout=5.0: requests.append(payload),
        )
        monkeypatch.setattr(
            window_runtime.GLib,
            "timeout_add",
            lambda *_args: polls.append(True) or 9,
        )

        gnome_setup._on_gnome_setup_action_completed(window, "enable_bridge")

        assert requests == []
        assert polls == [True]
        assert window._gnome_setup_poll_source_id == 9

    def test_gnome_setup_dialog_closes_when_gnome_support_becomes_ready(
        self, temp_config_dir, monkeypatch
    ):
        from keymasq.gui.window import _runtime as window_runtime
        from keymasq.gui.window.core import MainWindow

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
            window_runtime.Adw.Dialog,
            "present",
            lambda self, parent: None,
        )
        monkeypatch.setattr(
            window_runtime.GLib,
            "source_remove",
            lambda source_id: removed.append(source_id),
        )

        gnome_setup._present_gnome_setup_dialog(window)
        assert window._gnome_setup_dialog is not None
        window._gnome_setup_poll_source_id = 77

        connection._on_status_response(
            window,
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
        from keymasq.gui.preferences import save_selected_tab
        from keymasq.gui.window.core import MainWindow

        save_selected_tab("combos")
        window = MainWindow(demo_mode=False)

        device_tabs._apply_loaded_devices(window, [])

        assert window._placeholder_title is not None
        assert window._placeholder_subtitle is not None
        assert window._placeholder_title.get_label() == "No devices configured"
        assert window._placeholder_subtitle.get_label() == "Click + to add a new device"
        placeholder_page = tab_layout._page_for_child(window, window.placeholder)
        assert placeholder_page is not None
        assert window.tab_view.get_selected_page() is placeholder_page
        icon = placeholder_page.get_icon()
        assert icon is not None
        assert icon.to_string() == resolve_icon_name(*device_icon_names(False))

        tab_layout._close_tab_page(window, placeholder_page)
        window._placeholder_page = None
        window._placeholder_title.set_label("Loading devices...")
        window._placeholder_subtitle.set_label(
            "Checking compositor support and loading saved hardware"
        )

        device_tabs._check_empty_state(window)

        assert tab_layout._page_for_child(window, window.placeholder) is not None
        assert window.tab_view.get_selected_page() is tab_layout._page_for_child(
            window, window.placeholder
        )
        assert window._placeholder_title.get_label() == "No devices configured"
        assert window._placeholder_subtitle.get_label() == "Click + to add a new device"

        demo_window = MainWindow(demo_mode=True)
        device_tabs._apply_loaded_devices(demo_window, [])

        demo_tab = tab_layout._child_for_hardware_id(demo_window, "1234:5678")

        assert tab_layout._page_for_child(demo_window, demo_window.placeholder) is None
        assert demo_tab.device.name == "Demo Mouse"

    def test_main_window_status_response_updates_labels_for_all_status_paths(
        self, temp_config_dir, monkeypatch
    ):
        from keymasq.gui.window.core import MainWindow

        window = MainWindow(demo_mode=True)
        issues: list[str | None] = []
        unlock_updates: list[dict | None] = []

        monkeypatch.setattr(
            connection,
            "_set_connection_issue",
            lambda _window, issue: issues.append(issue),
        )
        monkeypatch.setattr(
            recording_unlock,
            "_update_unlock_state",
            lambda _window, data: unlock_updates.append(data),
        )

        window._status_query_id = 3
        window._status_query_inflight = True
        assert (
            connection._on_status_response(window, {"status": "ok", "keymasqd_connected": True}, 2)
            is False
        )
        assert window._status_query_inflight is True
        assert unlock_updates == []
        assert issues == []

        assert (
            connection._on_status_response(window, {"status": "ok", "keymasqd_connected": True}, 3)
            is False
        )
        assert window.session_status.get_label() == "session: 🟢"
        assert window.keymasqd_status.get_label() == "keymasqd: 🟢"
        assert unlock_updates[-1] == {"status": "ok", "keymasqd_connected": True}
        assert issues[-1] is None

        window._status_query_inflight = True
        assert (
            connection._on_status_response(window, {"status": "ok", "keymasqd_connected": False}, 3)
            is False
        )
        assert window.session_status.get_label() == "session: 🟡"
        assert window.keymasqd_status.get_label() == "keymasqd: 🔴"
        assert issues[-1] == "keymasqd"

        window._status_query_inflight = True
        assert connection._on_status_response(window, None, 3) is False
        assert window.session_status.get_label() == "session: 🔴"
        assert window.keymasqd_status.get_label() == "keymasqd: ⚪"
        assert unlock_updates[-1] is None
        assert issues[-1] == "session"
