import re
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import (  # pyright: ignore[reportAttributeAccessIssue]
    Adw,  # pyright: ignore[reportAttributeAccessIssue]
    GLib,  # pyright: ignore[reportAttributeAccessIssue]
    Gtk,  # pyright: ignore[reportAttributeAccessIssue]
)

from keymasq.common.keyboard_layouts import (
    keyboard_layout_choices,
    normalize_keyboard_layout_id,
)
from keymasq.common.settings import GlobalSettings
from keymasq.common.virtual_devices import (
    MAX_VIRTUAL_GAMEPADS,
    MIN_VIRTUAL_GAMEPADS,
    clamp_virtual_gamepad_count,
)
from keymasq.gui.session_client import session_request_async
from keymasq.gui.widgets.fuzzy_search import install_listbox_fuzzy_filter
from keymasq.session.settings import load_global_settings, save_global_settings

_SEARCH_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def _count_value(value: object, default: int) -> int:
    try:
        return int(value if isinstance(value, (int, float, str)) else default)
    except (TypeError, ValueError):
        return default


def _layout_value(value: object, default: str) -> str:
    return normalize_keyboard_layout_id(value) if isinstance(value, str) and value else default


def present_keyboard_layout_settings(
    parent: Gtk.Widget | None,
    *,
    on_closed: Callable[[], None] | None = None,
) -> "SettingsDialog":
    """Open Settings directly on the keyboard layout page.

    Type macro editors link here so a user who sees the wrong layout can fix
    it without hunting through Settings. ``on_closed`` runs when the dialog
    closes, so the caller can re-read the layout.
    """
    root = parent.get_root() if parent is not None else None
    dialog = SettingsDialog(root if isinstance(root, Gtk.Window) else None)
    if on_closed is not None:
        closed_callback = on_closed

        def _run_on_closed(_dialog: Adw.Dialog) -> None:
            closed_callback()

        dialog.connect("closed", _run_on_closed)
    dialog.present(parent)
    dialog._open_keyboard_layout_page()
    return dialog


class SettingsDialog(Adw.PreferencesDialog):
    """Global settings. Sizes itself to the parent window and scrolls, so rows
    can be added without pushing others out of view."""

    def __init__(self, parent: Gtk.Window | None = None) -> None:
        super().__init__(title="Settings")
        self.set_search_enabled(True)
        self._parent = parent
        self._settings = load_global_settings()
        self._gamepad_count = self._settings.virtual_gamepad_count
        self._applied_gamepad_count = self._gamepad_count
        self._keyboard_layout = self._settings.keyboard_layout
        self._applied_keyboard_layout = self._keyboard_layout
        self._layout_names: dict[str, str] = dict(keyboard_layout_choices())
        self._layout_page: KeyboardLayoutPage | None = None
        self._status_text = ""
        self._save_seq = 0
        self._applied_save_seq = 0
        self._save_inflight = False
        self._save_applied = False
        self._latest_save_failed = False

        page = Adw.PreferencesPage()
        self.add(page)

        devices_group = Adw.PreferencesGroup(title="Devices")
        page.add(devices_group)

        masking_row = self._navigation_row(
            "Device masking",
            "Stop apps reading devices directly while Keymasq remaps them",
            "Manage device masking",
            self._on_hardware_masking_clicked,
        )
        devices_group.add(masking_row)
        self._masking_row = masking_row

        gamepad_row = Adw.ActionRow(title="Virtual gamepads")
        gamepad_row.set_subtitle("Number of standard virtual gamepads")
        count_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        count_box.set_valign(Gtk.Align.CENTER)
        self._minus_button = Gtk.Button(icon_name="list-remove-symbolic")
        self._minus_button.set_tooltip_text("Remove virtual gamepad")
        self._minus_button.connect("clicked", self._on_decrement_gamepads_clicked)
        self._count_label = Gtk.Label()
        self._count_label.set_width_chars(2)
        self._count_label.set_xalign(0.5)
        self._plus_button = Gtk.Button(icon_name="list-add-symbolic")
        self._plus_button.set_tooltip_text("Add virtual gamepad")
        self._plus_button.connect("clicked", self._on_increment_gamepads_clicked)
        count_box.append(self._minus_button)
        count_box.append(self._count_label)
        count_box.append(self._plus_button)
        gamepad_row.add_suffix(count_box)
        devices_group.add(gamepad_row)
        self._sync_gamepad_count_controls()

        virtual_devices_row = self._navigation_row(
            "Custom virtual devices",
            "Configure templates and additional outputs",
            "Manage custom virtual devices",
            self._on_virtual_devices_clicked,
        )
        devices_group.add(virtual_devices_row)
        self._virtual_devices_button = virtual_devices_row

        input_group = Adw.PreferencesGroup(title="Input")
        input_group.set_description(
            "Type macros are compiled for this keyboard layout. "
            "Pick the layout your desktop session uses."
        )
        page.add(input_group)
        self._layout_row = self._navigation_row(
            "Keyboard layout",
            "",
            "Choose the keyboard layout",
            self._on_keyboard_layout_row_activated,
        )
        self._sync_keyboard_layout_row()
        input_group.add(self._layout_row)

        macro_group = Adw.PreferencesGroup(title="Macros")
        page.add(macro_group)
        macro_row = self._navigation_row(
            "Macro recording",
            "Recording sources and opt-in state",
            "Open macro recording settings",
            self._on_macro_settings_clicked,
        )
        macro_group.add(macro_row)
        self._macro_settings_btn = macro_row

        session_request_async(
            {"command": "get_settings"},
            self._on_loaded,
            timeout=1.0,
        )

    @staticmethod
    def _navigation_row(
        title: str,
        subtitle: str,
        tooltip: str,
        on_activated: Callable[[Adw.ActionRow], None],
    ) -> Adw.ActionRow:
        row = Adw.ActionRow(title=title)
        row.set_subtitle(subtitle)
        row.set_tooltip_text(tooltip)
        row.set_activatable(True)
        row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        row.connect("activated", on_activated)
        return row

    def _set_status(self, text: str) -> None:
        self._status_text = text
        if not text:
            return
        toast = Adw.Toast(title=text)
        toast.set_priority(Adw.ToastPriority.HIGH)
        toast.set_timeout(3)
        self.add_toast(toast)

    def _count(self) -> int:
        return clamp_virtual_gamepad_count(self._gamepad_count)

    def _on_macro_settings_clicked(self, _row: Gtk.Widget) -> None:
        present_settings = getattr(self._parent, "present_recording_settings_dialog", None)
        if callable(present_settings):
            present_settings(reason="settings")
            return
        if self._parent is None:
            self._set_status("Macro recording settings are available from the main window")
            return

        from keymasq.gui.widgets.record_macro_dialog import RecordMacroDialog

        dialog = RecordMacroDialog(self._parent)
        dialog.present(self._parent)

    def _on_virtual_devices_clicked(self, _row: Gtk.Widget) -> None:
        from keymasq.gui.widgets.virtual_devices_dialog import VirtualDevicesDialog

        VirtualDevicesDialog(self).present(self)

    def _on_hardware_masking_clicked(self, _row: Gtk.Widget) -> None:
        from keymasq.gui.widgets.hardware_masking_dialog import HardwareMaskingDialog

        HardwareMaskingDialog(self._parent).present(self)

    def _on_loaded(self, response: dict[str, object] | None) -> bool:
        if self._save_inflight or self._save_applied:
            return False
        if isinstance(response, dict) and response.get("status") == "ok":
            try:
                raw_count = response.get(
                    "virtual_gamepad_count",
                    self._settings.virtual_gamepad_count,
                )
                count = _count_value(raw_count, self._settings.virtual_gamepad_count)
                self._gamepad_count = clamp_virtual_gamepad_count(count)
            except (TypeError, ValueError):
                self._gamepad_count = self._settings.virtual_gamepad_count
            self._applied_gamepad_count = self._gamepad_count
            self._sync_gamepad_count_controls()
            self._keyboard_layout = _layout_value(
                response.get("keyboard_layout"), self._settings.keyboard_layout
            )
            self._applied_keyboard_layout = self._keyboard_layout
            self._apply_session_layout_choices(response.get("keyboard_layouts"))
            self._sync_keyboard_layout_row()
        return False

    def _apply_session_layout_choices(self, raw_choices: object) -> None:
        """The session validates layouts, so its list is the one to offer."""
        if not isinstance(raw_choices, list):
            return
        names: dict[str, str] = {}
        for entry in raw_choices:
            if not isinstance(entry, dict):
                continue
            layout_id = entry.get("id")
            if isinstance(layout_id, str) and layout_id:
                names[layout_id] = str(entry.get("name") or layout_id)
        self._layout_names = names
        self._layout_row.set_sensitive(bool(names))
        if not names:
            self._set_status(
                "Keyboard layouts are unavailable: keymasq-session cannot load libxkbcommon"
            )

    def _on_increment_gamepads_clicked(self, _button: Gtk.Button) -> None:
        self._set_gamepad_count(self._gamepad_count + 1)

    def _on_decrement_gamepads_clicked(self, _button: Gtk.Button) -> None:
        self._set_gamepad_count(self._gamepad_count - 1)

    def _set_gamepad_count(self, count: int) -> None:
        normalized = clamp_virtual_gamepad_count(count)
        if normalized == self._gamepad_count:
            return
        self._gamepad_count = normalized
        self._sync_gamepad_count_controls()
        self._save_settings()

    def _sync_gamepad_count_controls(self) -> None:
        count = self._count()
        self._count_label.set_text(str(count))
        self._minus_button.set_sensitive(count > MIN_VIRTUAL_GAMEPADS)
        self._plus_button.set_sensitive(count < MAX_VIRTUAL_GAMEPADS)

    def _layout_label(self, layout_id: str) -> str:
        name = self._layout_names.get(layout_id)
        return f"{name} ({layout_id})" if name else layout_id

    def _sync_keyboard_layout_row(self) -> None:
        self._layout_row.set_subtitle(self._layout_label(self._keyboard_layout))
        if self._layout_page is not None:
            self._layout_page.mark_selected(self._keyboard_layout)

    def _on_keyboard_layout_row_activated(self, _row: Gtk.Widget) -> None:
        self._open_keyboard_layout_page()

    def _open_keyboard_layout_page(self) -> "KeyboardLayoutPage":
        page = KeyboardLayoutPage(
            self._layout_names,
            self._keyboard_layout,
            on_selected=self._on_keyboard_layout_page_selected,
        )
        page.connect("hidden", self._on_keyboard_layout_page_hidden)
        self._layout_page = page
        self.push_subpage(page)
        return page

    def _on_keyboard_layout_page_selected(self, layout_id: str) -> None:
        self.pop_subpage()
        self._set_keyboard_layout(layout_id)

    def _on_keyboard_layout_page_hidden(self, page: Adw.NavigationPage) -> None:
        if self._layout_page is page:
            self._layout_page = None

    def _set_keyboard_layout(self, layout_id: str) -> None:
        if layout_id == self._keyboard_layout:
            return
        self._keyboard_layout = layout_id
        self._sync_keyboard_layout_row()
        self._save_settings()

    def _save_settings(self) -> None:
        count = self._count()
        layout = self._keyboard_layout
        self._gamepad_count = count
        self._sync_gamepad_count_controls()
        self._save_seq += 1
        save_seq = self._save_seq
        self._save_inflight = True
        self._latest_save_failed = False
        self._status_text = ""

        def apply_response(response: dict[str, object]) -> tuple[int, str]:
            applied_count = clamp_virtual_gamepad_count(
                _count_value(response.get("virtual_gamepad_count", count), count)
            )
            applied_layout = _layout_value(response.get("keyboard_layout"), layout)
            return applied_count, applied_layout

        def status_text(response: dict[str, object]) -> str:
            return str(response.get("warning") or "")

        def on_response(response: dict[str, object] | None) -> bool:
            if isinstance(response, dict) and response.get("status") == "ok":
                applied_count, applied_layout = apply_response(response)
                if save_seq != self._save_seq:
                    if save_seq > self._applied_save_seq:
                        self._applied_gamepad_count = applied_count
                        self._applied_keyboard_layout = applied_layout
                        self._applied_save_seq = save_seq
                        self._save_applied = True
                    if self._latest_save_failed and save_seq == self._applied_save_seq:
                        self._gamepad_count = applied_count
                        self._keyboard_layout = applied_layout
                        self._sync_gamepad_count_controls()
                        self._sync_keyboard_layout_row()
                    return False
                self._save_inflight = False
                self._latest_save_failed = False
                self._applied_gamepad_count = applied_count
                self._applied_keyboard_layout = applied_layout
                self._applied_save_seq = save_seq
                self._save_applied = True
                self._gamepad_count = applied_count
                self._keyboard_layout = applied_layout
                self._sync_gamepad_count_controls()
                self._sync_keyboard_layout_row()
                self._set_status(status_text(response))
                return False
            if save_seq != self._save_seq:
                return False
            self._save_inflight = False
            if isinstance(response, dict):
                self._latest_save_failed = True
                message = str(response.get("message") or "Failed to apply settings")
                self._gamepad_count = self._applied_gamepad_count
                self._keyboard_layout = self._applied_keyboard_layout
                self._sync_gamepad_count_controls()
                self._sync_keyboard_layout_row()
                self._set_status(message)
                return False
            saved = save_global_settings(
                GlobalSettings(
                    virtual_gamepad_count=count,
                    keyboard_layout=layout,
                )
            )
            self._gamepad_count = saved.virtual_gamepad_count
            self._keyboard_layout = saved.keyboard_layout
            self._applied_gamepad_count = self._gamepad_count
            self._applied_keyboard_layout = self._keyboard_layout
            self._applied_save_seq = save_seq
            self._sync_gamepad_count_controls()
            self._sync_keyboard_layout_row()
            self._save_applied = True
            self._latest_save_failed = False
            return False

        session_request_async(
            {
                "command": "set_settings",
                "virtual_gamepad_count": count,
                "keyboard_layout": layout,
            },
            on_response,
            timeout=1.0,
        )


class KeyboardLayoutPage(Adw.NavigationPage):
    """Searchable list of every XKB layout and variant, one row each."""

    def __init__(
        self,
        layout_names: dict[str, str],
        selected: str,
        *,
        on_selected: Callable[[str], None],
    ) -> None:
        super().__init__(title="Keyboard layout")
        self._on_selected = on_selected
        self._rows: dict[str, Adw.ActionRow] = {}
        self._selected = selected

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.set_margin_top(6)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)

        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text("Search layouts, for example de us or dvorak")
        self.search_entry.connect("activate", self._on_search_activate)
        content.append(self.search_entry)

        self.listbox = Gtk.ListBox()
        self.listbox.add_css_class("boxed-list")
        self.listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self.listbox.connect("row-activated", self._on_row_activated)
        choices = list(layout_names.items())
        if selected not in layout_names:
            choices.insert(0, (selected, selected))
        for layout_id, name in choices:
            row = Adw.ActionRow(title=name)
            row.set_subtitle(layout_id)
            row.set_activatable(True)
            check = Gtk.Image.new_from_icon_name("object-select-symbolic")
            check.set_visible(layout_id == selected)
            row.add_suffix(check)
            row._layout_id = layout_id  # pyright: ignore[reportAttributeAccessIssue]
            row._order = len(self._rows)  # pyright: ignore[reportAttributeAccessIssue]
            row._check = check  # pyright: ignore[reportAttributeAccessIssue]
            # The id tokenizes into layout and variant, so "de us" finds de(us).
            row._search_text = f"{name} {layout_id}"  # pyright: ignore[reportAttributeAccessIssue]
            self.listbox.append(row)
            self._rows[layout_id] = row
        self.listbox.set_sort_func(self._sort_rows)
        install_listbox_fuzzy_filter(
            self.listbox,
            self.search_entry,
            after_filter_changed=self._on_query_changed,
        )

        self._scrolled = Gtk.ScrolledWindow()
        self._scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._scrolled.set_vexpand(True)
        self._scrolled.set_child(self.listbox)
        content.append(self._scrolled)

        toolbar.set_content(content)
        self.set_child(toolbar)
        self.connect("shown", self._on_shown)

    def mark_selected(self, layout_id: str) -> None:
        self._selected = layout_id
        for row_id, row in self._rows.items():
            row._check.set_visible(row_id == layout_id)  # pyright: ignore[reportAttributeAccessIssue]

    def visible_layout_ids(self) -> list[str]:
        return [
            row._layout_id  # pyright: ignore[reportAttributeAccessIssue]
            for row in self.listbox
            if isinstance(row, Adw.ActionRow) and row.get_child_visible()
        ]

    def _query_tokens(self) -> set[str]:
        return set(_SEARCH_TOKEN_RE.findall(self.search_entry.get_text().casefold()))

    def _rank(self, row: Gtk.ListBoxRow) -> tuple[int, int]:
        """Whole-token id matches first ("de us" puts de(us) on top), then
        whole-token name matches, then the remaining fuzzy matches."""
        order = getattr(row, "_order", 0)
        query = self._query_tokens()
        if not query:
            return (0, order)
        layout_id = str(getattr(row, "_layout_id", ""))
        id_tokens = set(_SEARCH_TOKEN_RE.findall(layout_id.casefold()))
        if query <= id_tokens:
            return (0, order)
        text_tokens = set(
            _SEARCH_TOKEN_RE.findall(str(getattr(row, "_search_text", "")).casefold())
        )
        if query <= text_tokens:
            return (1, order)
        return (2, order)

    def _sort_rows(self, first: Gtk.ListBoxRow, second: Gtk.ListBoxRow) -> int:
        first_rank, second_rank = self._rank(first), self._rank(second)
        return (first_rank > second_rank) - (first_rank < second_rank)

    def _on_shown(self, _page: Adw.NavigationPage) -> None:
        # Wait for the list to be allocated; focusing a row before that scrolls nowhere.
        GLib.idle_add(self._reveal_selected_row)

    def _reveal_selected_row(self) -> bool:
        selected_row = self._rows.get(self._selected)
        if selected_row is not None:
            # Grabbing focus scrolls the current layout into view; typing then
            # goes to the search entry.
            selected_row.grab_focus()
        self.search_entry.grab_focus()
        return False

    def _on_query_changed(self) -> None:
        self.listbox.invalidate_sort()
        # The page opens scrolled to the current layout; results start at the top.
        self._scrolled.get_vadjustment().set_value(0)

    def _on_search_activate(self, _entry: Gtk.SearchEntry) -> None:
        # Enter picks the best-ranked match.
        visible = self.visible_layout_ids()
        if visible:
            self._on_selected(visible[0])

    def _on_row_activated(self, _listbox: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        layout_id = getattr(row, "_layout_id", None)
        if isinstance(layout_id, str):
            self._on_selected(layout_id)
