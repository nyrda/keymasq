# ruff: noqa: F403, F405, I001
from tests.gui.support import *

class TestMainWindow:
    def test_main_window_seeds_default_profile_for_first_device(self, temp_config_dir):
        from keyforge.common.models import ButtonDefinition, HardwareConfig
        from keyforge.gui.window import MainWindow

        window = MainWindow(demo_mode=True)

        device = HardwareConfig(
            vendor_id="1234",
            product_id="5678",
            name="Mouse One",
            evdev_devices=[],
            buttons=[ButtonDefinition(id="btn_back", label="Back", evdev="btn_side")],
        )

        window._add_device_tab(device)

        tab = window.stack.get_page(window.stack.get_child_by_name(device.hardware_id)).get_child()

        assert window.profile_manager.get_profile("Default") is not None
        assert tab._selected_profile is not None
        assert tab._selected_profile.config.name == "Default"
        assert tab.settings_frame.get_sensitive() is True

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


