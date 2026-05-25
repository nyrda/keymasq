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


def test_hardware_setup_search_and_raw_toggle_controls(monkeypatch) -> None:
    gi.require_version("Gdk", "4.0")
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gdk, Gtk

    from keymasq.gui.wizards.hardware_setup import HardwareSetupDialog

    monkeypatch.setattr(HardwareSetupDialog, "_detect_devices", lambda self: None)
    dialog = HardwareSetupDialog(Gtk.Window(), SimpleNamespace())

    assert isinstance(dialog.raw_evdev_check, Gtk.ToggleButton)
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
