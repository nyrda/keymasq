# ruff: noqa: F403, F405, I001
from tests.gui.support import *


def test_fuzzy_query_matches_substrings_and_abbreviations() -> None:
    from keymasq.gui.widgets.fuzzy_search import fuzzy_query_matches

    assert fuzzy_query_matches("copy", "copy_ctrl_c keyboard 4 events")
    assert fuzzy_query_matches("ctc", "copy_ctrl_c keyboard 4 events")
    assert fuzzy_query_matches("kbd 4", "copy_ctrl_c keyboard kbd 4 events")
    assert fuzzy_query_matches("xbox", "Xbox 360 Wireless Receiver")
    assert fuzzy_query_matches("café", "Café macro")
    assert fuzzy_query_matches("клава", "Клава input")
    assert fuzzy_query_matches("键盘", "外接键盘")
    assert not fuzzy_query_matches("gamepad", "copy_ctrl_c keyboard 4 events")
    assert not fuzzy_query_matches(
        "dygma",
        (
            "Xbox 360 Wireless Receiver 045e:02a1 gamepad "
            "/dev/input/by-id/usb-Microsoft_Xbox_360_Wireless_Receiver"
        ),
    )


def test_macro_manager_search_entry_filters_against_row_metadata(monkeypatch) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import GLib, Gtk

    from keymasq.gui.widgets.fuzzy_search import fuzzy_query_matches
    from keymasq.gui.widgets.macro_manager_dialog import MacroManagerDialog

    monkeypatch.setattr(GLib, "idle_add", lambda callback, *args: 0)
    dialog = MacroManagerDialog(Gtk.Window())

    dialog._on_macros_loaded(
        {
            "macros": [
                {
                    "name": "copy_ctrl_c",
                    "duration_us": 125_000,
                    "device_types": ["keyboard"],
                    "event_count": 4,
                },
                {
                    "name": "gamepad_combo",
                    "duration_us": 43_300_000,
                    "device_types": ["gamepad"],
                    "event_count": 407,
                },
            ]
        }
    )

    row = dialog._listbox.get_row_at_index(0)
    assert row is not None
    assert dialog._search_entry.get_placeholder_text() == "Search macros"
    assert fuzzy_query_matches("ctc", row._search_text)
    assert fuzzy_query_matches("keyboard 4", row._search_text)
    assert not fuzzy_query_matches("gamepad", row._search_text)


def test_macro_manager_search_is_revealed_by_button_and_ctrl_f(monkeypatch) -> None:
    gi.require_version("Gdk", "4.0")
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gdk, GLib, Gtk

    from keymasq.gui.widgets.macro_manager_dialog import MacroManagerDialog

    monkeypatch.setattr(GLib, "idle_add", lambda callback, *args: 0)
    dialog = MacroManagerDialog(Gtk.Window())

    assert dialog._search_entry.get_visible() is False
    assert dialog._search_button is not None
    assert dialog._search_button.get_icon_name() == "system-search-symbolic"

    dialog._search_button.emit("clicked")

    assert dialog._search_entry.get_visible() is True

    dialog._hide_search()
    assert dialog._search_entry.get_visible() is False

    handled = dialog._on_key_pressed(
        Gtk.EventControllerKey(),
        Gdk.KEY_f,
        0,
        Gdk.ModifierType.CONTROL_MASK,
    )

    assert handled is True
    assert dialog._search_entry.get_visible() is True


def test_key_selector_macro_search_clears_hidden_selection(monkeypatch) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    import keymasq.gui.widgets.key_selector_dialog as key_selector_dialog_module
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

    monkeypatch.setattr(key_selector_dialog_module.GLib, "idle_add", lambda callback, *args: 0)
    monkeypatch.setattr(
        key_selector_dialog_module,
        "session_request_async",
        lambda _payload, _callback: None,
    )

    dialog = KeySelectorDialog(Gtk.Window(), "Back")
    dialog.stack.set_visible_child_name("macro")
    dialog._macro_list = [
        {
            "name": "copy_ctrl_c",
            "duration_us": 125_000,
            "device_types": ["keyboard"],
            "event_count": 4,
        },
        {
            "name": "gamepad_combo",
            "duration_us": 43_300_000,
            "device_types": ["gamepad"],
            "event_count": 407,
        },
    ]
    dialog._selected_macro = "copy_ctrl_c"
    dialog._macro_search_entry.set_text("gamepad")
    dialog.map_btn.set_sensitive(True)

    dialog._populate_macro_listbox()

    assert dialog._selected_macro is None
    assert dialog.map_btn.get_sensitive() is False


def test_key_selector_macro_refresh_clears_missing_selection(monkeypatch) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    import keymasq.gui.widgets.key_selector_dialog as key_selector_dialog_module
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

    monkeypatch.setattr(key_selector_dialog_module.GLib, "idle_add", lambda callback, *args: 0)
    monkeypatch.setattr(
        key_selector_dialog_module,
        "session_request_async",
        lambda _payload, _callback: None,
    )

    dialog = KeySelectorDialog(Gtk.Window(), "Back")
    dialog.stack.set_visible_child_name("macro")
    dialog._macro_list = [
        {
            "name": "gamepad_combo",
            "duration_us": 43_300_000,
            "device_types": ["gamepad"],
            "event_count": 407,
        }
    ]
    dialog._selected_macro = "deleted_macro"
    dialog.map_btn.set_sensitive(True)

    dialog._populate_macro_listbox()

    assert dialog._selected_macro is None
    assert dialog.map_btn.get_sensitive() is False


def test_key_selector_macro_slots_use_card_layout(monkeypatch) -> None:
    gi.require_version("Adw", "1")
    gi.require_version("Gtk", "4.0")
    from gi.repository import Adw, Gtk

    import keymasq.gui.widgets.key_selector_dialog as key_selector_dialog_module
    from keymasq.common.models import ActionType, MappingAction
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

    def collect_widgets(widget, widget_type):
        matches = []
        child = widget.get_first_child()
        while child is not None:
            if isinstance(child, widget_type):
                matches.append(child)
            matches.extend(collect_widgets(child, widget_type))
            child = child.get_next_sibling()
        return matches

    monkeypatch.setattr(key_selector_dialog_module.GLib, "idle_add", lambda callback, *args: 0)
    monkeypatch.setattr(
        key_selector_dialog_module,
        "session_request_async",
        lambda _payload, _callback: None,
    )

    class Parent(Gtk.Window):
        def macro_recording_enabled(self) -> bool:
            return True

    results: list[MappingAction] = []
    dialog = KeySelectorDialog(Parent(), "Back")
    monkeypatch.setattr(dialog, "close", lambda: None)
    dialog.connect("key-selected", lambda _dialog, action: results.append(action))
    dialog.stack.set_visible_child_name("macro")

    macro_tab = dialog.stack.get_child_by_name("macro")
    assert macro_tab is not None
    assert not hasattr(dialog, "_macro_slot_stack")
    assert dialog.map_btn.get_visible() is True
    assert dialog.map_btn.get_sensitive() is False
    assert dialog._cancel_macro_playback_btn is not None
    assert dialog._cancel_macro_playback_btn.get_visible() is True
    assert dialog._cancel_macro_playback_btn.get_next_sibling() is dialog.map_btn
    cancel_content = dialog._cancel_macro_playback_btn.get_child()
    assert isinstance(cancel_content, Adw.ButtonContent)
    assert cancel_content.get_label() == "Cancel Macro Playback"

    toggle_labels = {
        toggle.get_label()
        for toggle in collect_widgets(macro_tab, Gtk.ToggleButton)
        if toggle.get_label()
    }
    assert "Rows" not in toggle_labels
    assert "Cards" not in toggle_labels

    labels = {
        label.get_label()
        for label in collect_widgets(macro_tab, Gtk.Label)
        if label.get_label()
    }
    assert "Macro Slots" not in labels
    assert "Macro Library" in labels
    assert {f"Slot {slot}" for slot in range(1, 5)} <= labels
    assert "Slot 5" not in labels
    assert "Slot 6" not in labels
    assert collect_widgets(macro_tab, Gtk.SearchEntry)
    assert collect_widgets(macro_tab, Gtk.ListBox)

    slot_4_record = next(
        button
        for button in collect_widgets(macro_tab, Gtk.Button)
        if button.get_tooltip_text() == "Toggle macro recording into slot 4"
    )
    slot_4_record.emit("clicked")

    assert len(results) == 1
    assert results[0].action_type == ActionType.START_MACRO_RECORDING
    assert results[0].macro_recording_slot == 4

    slot_4_play = next(
        button
        for button in collect_widgets(macro_tab, Gtk.Button)
        if button.get_tooltip_text() == "Play the macro recorded in slot 4"
    )
    slot_4_play.emit("clicked")

    assert len(results) == 2
    assert results[1].action_type == ActionType.PLAY_MACRO_SLOT
    assert results[1].macro_recording_slot == 4


def test_key_selector_macro_slots_show_disabled_placeholder(monkeypatch) -> None:
    gi.require_version("Adw", "1")
    gi.require_version("Gtk", "4.0")
    from gi.repository import Adw, Gtk

    import keymasq.gui.widgets.key_selector_dialog as key_selector_dialog_module
    from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

    def collect_widgets(widget, widget_type):
        matches = []
        child = widget.get_first_child()
        while child is not None:
            if isinstance(child, widget_type):
                matches.append(child)
            matches.extend(collect_widgets(child, widget_type))
            child = child.get_next_sibling()
        return matches

    monkeypatch.setattr(key_selector_dialog_module.GLib, "idle_add", lambda callback, *args: 0)
    monkeypatch.setattr(
        key_selector_dialog_module,
        "session_request_async",
        lambda _payload, _callback: None,
    )
    opened: list[str] = []

    class Parent(Gtk.Window):
        def macro_recording_enabled(self) -> bool:
            return False

        def present_recording_settings_dialog(self, reason: str = "settings") -> None:
            opened.append(reason)

    dialog = KeySelectorDialog(Parent(), "Back")
    closed: list[bool] = []
    monkeypatch.setattr(dialog, "close", lambda: closed.append(True))
    dialog.stack.set_visible_child_name("macro")

    macro_tab = dialog.stack.get_child_by_name("macro")
    assert macro_tab is not None
    labels = {
        label.get_label()
        for label in collect_widgets(macro_tab, Gtk.Label)
        if label.get_label()
    }
    assert "Macro recording is disabled" in labels
    assert "Macro Library" in labels
    assert "Slot 1" not in labels

    slot_buttons = [
        button
        for button in collect_widgets(macro_tab, Gtk.Button)
        if button.get_tooltip_text() == "Toggle macro recording into slot 1"
    ]
    assert slot_buttons == []

    settings_btn = next(
        button
        for button in collect_widgets(macro_tab, Gtk.Button)
        if button.get_tooltip_text() == "Open macro recording settings"
    )
    settings_content = settings_btn.get_child()
    assert isinstance(settings_content, Adw.ButtonContent)
    assert settings_content.get_label() == "Open Settings"
    settings_btn.emit("clicked")

    assert opened == ["settings"]
    assert closed == [True]


def test_hardware_setup_search_and_raw_toggle_controls(monkeypatch) -> None:
    gi.require_version("Gdk", "4.0")
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gdk, Gtk

    from keymasq.gui.wizards.hardware_setup import HardwareSetupDialog

    monkeypatch.setattr(HardwareSetupDialog, "_detect_devices", lambda self: None)
    dialog = HardwareSetupDialog(Gtk.Window(), SimpleNamespace())

    assert isinstance(dialog.raw_evdev_check, Gtk.CheckButton)
    assert dialog.device_search_button.get_icon_name() == "system-search-symbolic"
    assert dialog.device_search_entry.get_visible() is False

    handled = dialog._on_key_pressed(
        Gtk.EventControllerKey(),
        Gdk.KEY_f,
        0,
        Gdk.ModifierType.CONTROL_MASK,
    )

    assert handled is True
    assert dialog.device_search_entry.get_visible() is True


def test_hardware_setup_search_clears_hidden_selected_device(monkeypatch) -> None:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.gui.wizards.hardware_setup import HardwareSetupDialog

    monkeypatch.setattr(HardwareSetupDialog, "_detect_devices", lambda self: None)
    dialog = HardwareSetupDialog(Gtk.Window(), SimpleNamespace())
    dialog.detected_devices = {
        "keyboard": {
            "name": "Keyboard",
            "vendor_id": "1111",
            "product_id": "2222",
            "interfaces": [],
        }
    }

    row = Gtk.ListBoxRow()
    row.hardware_id = "keyboard"
    row._search_text = "keyboard 1111 2222"
    dialog.device_list.append(row)
    dialog.device_list.select_row(row)
    dialog.next_btn.set_sensitive(True)

    assert dialog.selected_device is dialog.detected_devices["keyboard"]

    dialog.device_search_entry.set_text("mouse")
    dialog._after_device_search_filter_changed()

    assert dialog.selected_device is None
    assert dialog.next_btn.get_sensitive() is False


def test_superkey_dialog_search_is_revealed_by_button_and_ctrl_f(
    temp_config_dir,
) -> None:
    gi.require_version("Gdk", "4.0")
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gdk, Gtk

    from keymasq.gui.widgets.superkey_dialog import SuperkeyDialog

    dialog = SuperkeyDialog(Gtk.Window())

    assert dialog.search_button.get_icon_name() == "system-search-symbolic"
    assert dialog.search_entry.get_visible() is False

    dialog.search_button.emit("clicked")
    assert dialog.search_entry.get_visible() is True

    dialog._hide_search()
    handled = dialog._on_key_pressed(
        Gtk.EventControllerKey(),
        Gdk.KEY_f,
        0,
        Gdk.ModifierType.CONTROL_MASK,
    )

    assert handled is True
    assert dialog.search_entry.get_visible() is True


def test_analog_control_dialog_search_is_revealed_by_button_and_ctrl_f(
    temp_config_dir,
) -> None:
    gi.require_version("Gdk", "4.0")
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gdk, Gtk

    from keymasq.gui.widgets.analog_control_dialog import AnalogControlDialog

    dialog = AnalogControlDialog(Gtk.Window())

    assert dialog.search_button.get_icon_name() == "system-search-symbolic"
    assert dialog.search_entry.get_visible() is False

    dialog.search_button.emit("clicked")
    assert dialog.search_entry.get_visible() is True

    dialog._hide_search()
    handled = dialog._on_key_pressed(
        Gtk.EventControllerKey(),
        Gdk.KEY_f,
        0,
        Gdk.ModifierType.CONTROL_MASK,
    )

    assert handled is True
    assert dialog.search_entry.get_visible() is True
