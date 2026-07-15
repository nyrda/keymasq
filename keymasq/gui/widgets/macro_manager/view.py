"""Macro manager widget construction and search presentation."""

# pyright: reportAttributeAccessIssue=false, reportUnknownMemberType=false

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gdk, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.model.actions import MAX_MACRO_RECORDING_SLOTS
from keymasq.gui.widgets.fuzzy_search import install_listbox_fuzzy_filter


class ManagerViewMixin:
    """Build the manager chrome and coordinate search visibility."""

    def _build_ui(self) -> None:
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        main_box.set_margin_top(12)
        main_box.set_margin_bottom(12)
        main_box.set_margin_start(12)
        main_box.set_margin_end(12)

        frame = Gtk.Frame()
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        title_label = Gtk.Label(label="Macros")
        title_label.add_css_class("title-3")
        title_label.set_halign(Gtk.Align.CENTER)
        title_label.set_margin_top(12)
        title_label.set_margin_bottom(12)
        inner.append(title_label)
        inner.append(Gtk.Separator())

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        content.set_margin_top(12)
        content.set_margin_bottom(8)
        content.set_margin_start(12)
        content.set_margin_end(12)

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        toolbar.set_margin_bottom(4)

        slot_model = Gtk.StringList()
        for slot in range(1, MAX_MACRO_RECORDING_SLOTS + 1):
            slot_model.append(f"Slot {slot}")
        slot_dropdown = Gtk.DropDown()
        slot_dropdown.set_model(slot_model)
        slot_dropdown.set_selected(0)
        slot_dropdown.set_tooltip_text("Temporary recording slot")
        slot_dropdown.connect("notify::selected", self._on_recording_slot_changed)
        toolbar.append(slot_dropdown)
        self._slot_dropdown = slot_dropdown

        record_btn = Gtk.Button()
        record_btn.set_child(self._make_button_content("media-record-symbolic", "Record", "error"))
        record_btn.set_tooltip_text("Record a new macro")
        record_btn.connect("clicked", self._on_record_new)
        toolbar.append(record_btn)
        self._record_btn = record_btn

        empty_btn = Gtk.Button()
        empty_btn.set_child(self._make_button_content("document-new-symbolic", "Empty"))
        empty_btn.set_tooltip_text("Create an empty macro to edit")
        empty_btn.connect("clicked", self._on_create_empty_macro)
        toolbar.append(empty_btn)

        type_btn = Gtk.Button()
        type_btn.set_child(self._make_button_content("input-keyboard-symbolic", "Type"))
        type_btn.set_tooltip_text("Create a macro that types text")
        type_btn.connect("clicked", self._on_create_type_macro)
        toolbar.append(type_btn)

        toolbar_spacer = Gtk.Box()
        toolbar_spacer.set_hexpand(True)
        toolbar.append(toolbar_spacer)

        search_btn = Gtk.Button()
        search_btn.set_icon_name("system-search-symbolic")
        search_btn.set_tooltip_text("Search macros")
        search_btn.connect("clicked", self._on_search_clicked)
        toolbar.append(search_btn)
        self._search_button = search_btn

        settings_btn = Gtk.Button()
        settings_btn.set_child(self._make_button_content("emblem-system-symbolic", "Settings"))
        settings_btn.set_tooltip_text("Recording settings")
        settings_btn.connect("clicked", self._on_record_settings)
        toolbar.append(settings_btn)

        content.append(toolbar)

        self._search_entry = Gtk.SearchEntry()
        self._search_entry.set_placeholder_text("Search macros")
        self._search_entry.set_tooltip_text("Filter macros by name, device type, or event count")
        self._search_entry.set_visible(False)
        self._search_entry.connect("changed", self._on_search_changed)
        self._search_entry.connect("stop-search", self._on_search_stop)
        content.append(self._search_entry)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_min_content_height(240)
        scrolled.set_max_content_height(400)
        scrolled.set_vexpand(True)

        self._listbox = Gtk.ListBox()
        self._listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self._listbox.add_css_class("boxed-list")
        install_listbox_fuzzy_filter(self._listbox, self._search_entry)
        scrolled.set_child(self._listbox)
        content.append(scrolled)

        self._empty_label = Gtk.Label(label="No macros recorded yet")
        self._empty_label.add_css_class("dim-label")
        self._empty_label.set_margin_top(8)
        self._empty_label.set_margin_bottom(8)
        self._empty_label.set_visible(False)
        content.append(self._empty_label)

        inner.append(content)
        inner.append(Gtk.Separator())

        footer = Gtk.CenterBox(orientation=Gtk.Orientation.HORIZONTAL)
        footer.set_margin_top(8)
        footer.set_margin_bottom(8)
        footer.set_margin_start(12)
        footer.set_margin_end(12)

        docs_btn = Gtk.Button(label="?")
        docs_btn.add_css_class("flat")
        docs_btn.add_css_class("actions-docs-button")
        docs_btn.set_tooltip_text("Open Macros documentation")
        docs_btn.connect("clicked", self._on_macros_docs_clicked)
        footer.set_start_widget(docs_btn)
        self.macros_docs_btn = docs_btn

        self.playback_stop_hint = Gtk.Label(label="Interrupt macro playback: Ctrl+Alt+Esc")
        self.playback_stop_hint.add_css_class("dim-label")
        self.playback_stop_hint.add_css_class("caption")
        self.playback_stop_hint.set_halign(Gtk.Align.CENTER)
        footer.set_center_widget(self.playback_stop_hint)

        close_btn = Gtk.Button(label="Close")
        close_btn.connect("clicked", self._on_close_clicked)
        footer.set_end_widget(close_btn)

        inner.append(footer)
        frame.set_child(inner)
        main_box.append(frame)
        self.set_child(main_box)
        self._sync_record_button_state()

    def _on_key_pressed(self, _controller, keyval, _keycode, state) -> bool:
        if keyval in (Gdk.KEY_f, Gdk.KEY_F) and state & Gdk.ModifierType.CONTROL_MASK:
            self._show_search()
            return True
        return False

    def _show_search(self) -> None:
        self._catalog.show_search()
        self._search_entry.set_visible(True)
        self._search_entry.grab_focus()
        self._search_entry.select_region(0, -1)

    def _hide_search(self) -> None:
        self._catalog.hide_search()
        self._search_entry.set_text("")
        self._search_entry.set_visible(False)

    def _on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        self._catalog.set_query(entry.get_text())

    def _on_search_clicked(self, _button: Gtk.Button) -> None:
        self._show_search()

    def _on_search_stop(self, _entry: Gtk.SearchEntry) -> None:
        self._hide_search()

    def _on_close_clicked(self, _button: Gtk.Button) -> None:
        self.close()

    def _make_button_content(
        self,
        icon_name: str,
        label: str,
        icon_css_class: str | None = None,
    ) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        icon = Gtk.Image.new_from_icon_name(icon_name)
        if icon_css_class:
            icon.add_css_class(icon_css_class)
        box.append(icon)
        box.append(Gtk.Label(label=label))
        return box
