# ruff: noqa: F403, F405, I001
from tests.gui.support import *

def test_notify_session_reload_returns_false_without_shell_fallback(monkeypatch):
    from keymasq.gui import session_reload

    monkeypatch.setattr(session_reload, "session_request", lambda payload, timeout=5.0: None)

    assert session_reload.notify_session_reload(timeout=0.1) is False


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
    assert dialog.map_btn.get_visible() is True
    assert dialog.map_btn.get_sensitive() is True

    dialog._on_map_clicked(dialog.map_btn)

    assert len(results) == 1
    assert results[0].action_type == ActionType.SUPERKEY
    assert results[0].superkey_name == "volume_rocker"


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


def test_key_selector_dialog_gamepad_output_selector_lives_in_title(monkeypatch):
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.gui.widgets import key_selector_dialog as dialog_module
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

    monkeypatch.setattr(dialog_module, "load_virtual_gamepad_count", lambda: 2)
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
    monkeypatch.setattr(dialog_module, "load_virtual_gamepad_count", lambda: 1)
    monkeypatch.setattr(
        dialog_module,
        "HardwareManager",
        lambda: SimpleNamespace(list_hardware=lambda: [hardware]),
    )

    dialog = KeySelectorDialog(Gtk.Box(), "Extra Button 14")

    assert ("045e:028e@2", "Living Room Pad (045e:028e@2)") in dialog._gamepad_output_choices()


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


def test_key_selector_dialog_only_shows_hyprland_actions_for_active_hyprland_listener():
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


def test_key_selector_dialog_only_shows_niri_dispatch_for_active_niri_listener():
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

    hidden_dialog = KeySelectorDialog(
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
    assert hidden_dialog.stack.get_child_by_name("niri") is None


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


def test_key_selector_dialog_only_shows_kde_dispatch_for_active_kde_listener():
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

    hidden_dialog = KeySelectorDialog(
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
    assert hidden_dialog.stack.get_child_by_name("kde") is None


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

    owner = _Owner()
    widget = build_media_tab(owner, media_groups=MEDIA_KEY_GROUPS)

    assert isinstance(widget, Gtk.ScrolledWindow)
    buttons = collect_buttons(widget)
    assert len(buttons) == 10
    assert isinstance(buttons[0].get_child(), Gtk.Box)

    buttons[2].emit("clicked")

    assert owner.clicked == ["key_volumeup"]


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

    monkeypatch.setattr(dialog_module, "__version__", "1.2.3.dev1")
    assert dialog_module._actions_docs_url("mouse") == (
        "https://keymasq.tools/docs/master/ACTIONS/#mouse"
    )
