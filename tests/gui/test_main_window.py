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

        tab = window.stack.get_page(window.stack.get_child_by_name(device.hardware_id)).get_child()

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

    def test_main_window_unlock_dialog_copy_mentions_additional_keys(self, temp_config_dir):
        from gi.repository import Adw, Gtk

        from keymasq.gui.window import MainWindow

        window = MainWindow(demo_mode=True)
        presented: list[Adw.Dialog] = []

        def monkeypatch_present(self, root) -> None:
            presented.append(self)

        original_present = Adw.Dialog.present
        Adw.Dialog.present = monkeypatch_present  # type: ignore[method-assign]
        try:
            window._present_unlock_dialog()
        finally:
            Adw.Dialog.present = original_present  # type: ignore[method-assign]

        assert len(presented) == 1
        content = presented[0].get_child()
        assert isinstance(content, Gtk.Box)
        message = content.get_first_child()
        assert isinstance(message, Gtk.Label)
        assert "additional keys and buttons" in message.get_label()
        assert "device setup" not in message.get_label()

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
        tab = window.stack.get_page(window.stack.get_child_by_name(device.hardware_id)).get_child()
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
        assert tab.status_label.get_text() == "active"
        assert window.combo_tab is not None
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

        tab = window.stack.get_page(window.stack.get_child_by_name(device.hardware_id)).get_child()
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
        tab = window.stack.get_page(window.stack.get_child_by_name(device.hardware_id)).get_child()
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

    def test_main_window_shows_warning_banner_even_when_compositor_supported(self):
        from keymasq.gui.window import MainWindow

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
        from keymasq.gui.icons import resolve_icon_name, device_icon_names
        from keymasq.gui.window import MainWindow

        window = MainWindow(demo_mode=False)

        window._apply_loaded_devices([])

        assert window._placeholder_title is not None
        assert window._placeholder_subtitle is not None
        assert window._placeholder_title.get_label() == "No devices configured"
        assert window._placeholder_subtitle.get_label() == "Click + to add a new device"
        placeholder_page = window.stack.get_page(window.placeholder)
        assert placeholder_page.get_icon_name() == resolve_icon_name(*device_icon_names(False))

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
