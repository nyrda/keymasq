"""Concrete common shell for managed-resource editor dialogs."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.gui.widgets.fuzzy_search import install_listbox_fuzzy_filter
from keymasq.gui.widgets.managed_editor.state import EditorSelection, EditorState


@dataclass(frozen=True, slots=True)
class ManagedEditorLabels:
    """Resource-specific text displayed by the common shell."""

    sidebar_title: str
    search_placeholder: str
    search_tooltip: str
    documentation_tooltip: str
    add_tooltip: str


@dataclass(frozen=True, slots=True)
class ManagedEditorCallbacks:
    """Domain actions initiated by common shell widgets."""

    selection_changed: Callable[[EditorSelection | None], None]
    open_documentation: Callable[[], None]
    add_item: Callable[[], None]
    delete_item: Callable[[], None]
    save_item: Callable[[], None]
    revert_item: Callable[[], None]
    close_editor: Callable[[], None]


@dataclass(frozen=True, slots=True)
class ManagedEditorRow:
    """Typed metadata for a row owned by a managed editor shell."""

    selection: EditorSelection | None
    search_text: str


class LabeledForm:
    """Small builder for consistently aligned label/widget grids."""

    __slots__ = ("_next_row", "grid", "label_width")

    def __init__(self, *, label_width: int = 96) -> None:
        self.grid = Gtk.Grid()
        self.grid.set_column_spacing(12)
        self.grid.set_row_spacing(8)
        self.label_width = label_width
        self._next_row = 0

    def append(self, label: str, widget: Gtk.Widget) -> Gtk.Label:
        field_label = Gtk.Label(label=label)
        field_label.set_xalign(0)
        field_label.set_size_request(self.label_width, -1)
        self.grid.attach(field_label, 0, self._next_row, 1, 1)
        self.grid.attach(widget, 1, self._next_row, 1, 1)
        self._next_row += 1
        return field_label


class ManagedEditorShell:
    """Owns the shared widget tree and typed list-row registry."""

    __slots__ = (
        "_callbacks",
        "_rows_by_selection",
        "_rows_by_widget",
        "_state",
        "add_button",
        "close_button",
        "delete_button",
        "documentation_button",
        "editor_container",
        "editor_scrolled",
        "list_box",
        "revert_button",
        "right_box",
        "root",
        "save_button",
        "search_button",
        "search_entry",
    )

    def __init__(
        self,
        *,
        state: EditorState,
        labels: ManagedEditorLabels,
        callbacks: ManagedEditorCallbacks,
    ) -> None:
        self._state = state
        self._callbacks = callbacks
        self._rows_by_selection: dict[EditorSelection, Gtk.ListBoxRow] = {}
        self._rows_by_widget: dict[Gtk.ListBoxRow, ManagedEditorRow] = {}

        self.root = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.root.append(self._build_sidebar(labels))
        self.root.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        self.root.append(self._build_editor_panel())

    def _build_sidebar(self, labels: ManagedEditorLabels) -> Gtk.Widget:
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        sidebar.set_margin_top(12)
        sidebar.set_margin_bottom(12)
        sidebar.set_margin_start(12)
        sidebar.set_margin_end(12)
        sidebar.set_size_request(220, -1)

        header = Gtk.CenterBox()
        self.search_button = Gtk.Button(icon_name="system-search-symbolic")
        self.search_button.set_tooltip_text(labels.search_placeholder)
        self.search_button.connect("clicked", self._on_search_clicked)
        header.set_start_widget(self.search_button)

        title = Gtk.Label(label=labels.sidebar_title)
        title.add_css_class("title-4")
        title.set_halign(Gtk.Align.CENTER)
        header.set_center_widget(title)

        header_spacer = Gtk.Box()
        header_spacer.set_size_request(34, -1)
        header.set_end_widget(header_spacer)
        sidebar.append(header)

        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text(labels.search_placeholder)
        self.search_entry.set_tooltip_text(labels.search_tooltip)
        self.search_entry.set_visible(False)
        self.search_entry.connect("stop-search", self._on_search_stopped)
        sidebar.append(self.search_entry)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.list_box = Gtk.ListBox()
        self.list_box.set_vexpand(True)
        self.list_box.connect("row-selected", self._on_row_selected)
        install_listbox_fuzzy_filter(
            self.list_box,
            self.search_entry,
            row_text=self.search_text_for_row,
            before_filter_changed=self._before_filter_changed,
            after_filter_changed=self._after_filter_changed,
        )
        scrolled.set_child(self.list_box)
        sidebar.append(scrolled)

        footer = Gtk.CenterBox()
        self.documentation_button = Gtk.Button(label="?")
        self.documentation_button.add_css_class("flat")
        self.documentation_button.add_css_class("actions-docs-button")
        self.documentation_button.set_tooltip_text(labels.documentation_tooltip)
        self.documentation_button.connect("clicked", self._on_documentation_clicked)
        footer.set_start_widget(self.documentation_button)

        self.add_button = Gtk.Button(icon_name="list-add-symbolic")
        self.add_button.set_tooltip_text(labels.add_tooltip)
        self.add_button.connect("clicked", self._on_add_clicked)
        footer.set_center_widget(self.add_button)
        sidebar.append(footer)
        return sidebar

    def _build_editor_panel(self) -> Gtk.Widget:
        self.right_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.right_box.set_margin_top(12)
        self.right_box.set_margin_bottom(12)
        self.right_box.set_margin_start(12)
        self.right_box.set_margin_end(12)
        self.right_box.set_hexpand(True)

        self.editor_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.editor_container.set_sensitive(False)
        self.editor_scrolled = Gtk.ScrolledWindow()
        self.editor_scrolled.set_vexpand(True)
        self.editor_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.editor_scrolled.set_child(self.editor_container)
        self.right_box.append(self.editor_scrolled)
        self.right_box.append(self._build_action_footer())
        return self.right_box

    def _build_action_footer(self) -> Gtk.Widget:
        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        footer.set_hexpand(True)
        footer.set_margin_top(12)

        self.delete_button = Gtk.Button(label="Delete")
        self.delete_button.set_sensitive(False)
        self.delete_button.add_css_class("destructive-action")
        self.delete_button.connect("clicked", self._on_delete_clicked)
        footer.append(self.delete_button)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        footer.append(spacer)

        self.save_button = Gtk.Button(label="Save")
        self.save_button.add_css_class("suggested-action")
        self.save_button.set_sensitive(False)
        self.save_button.connect("clicked", self._on_save_clicked)
        footer.append(self.save_button)

        self.revert_button = Gtk.Button(label="Revert")
        self.revert_button.set_sensitive(False)
        self.revert_button.connect("clicked", self._on_revert_clicked)
        footer.append(self.revert_button)

        self.close_button = Gtk.Button(label="Close")
        self.close_button.connect("clicked", self._on_close_clicked)
        footer.append(self.close_button)
        return footer

    def append_editor_widget(self, widget: Gtk.Widget) -> None:
        self.editor_container.append(widget)

    def append_text_row(
        self,
        selection: EditorSelection,
        *,
        label: str,
        search_text: str,
        tooltip: str | None = None,
        css_classes: Iterable[str] = (),
    ) -> Gtk.ListBoxRow:
        if selection in self._rows_by_selection:
            raise ValueError(f"Duplicate managed editor selection: {selection!r}")

        row = Gtk.ListBoxRow()
        if tooltip is not None:
            row.set_tooltip_text(tooltip)
        if selection.is_new_item:
            row.add_css_class("managed-editor-add-row")
        for css_class in css_classes:
            row.add_css_class(css_class)

        text_label = Gtk.Label(label=label, xalign=0)
        if selection.is_new_item:
            text_label.add_css_class("dim-label")
        else:
            text_label.set_margin_start(6)
            text_label.set_margin_end(6)
            text_label.set_margin_top(6)
            text_label.set_margin_bottom(6)
        row.set_child(text_label)

        self._rows_by_selection[selection] = row
        self._rows_by_widget[row] = ManagedEditorRow(selection, search_text)
        self.list_box.append(row)
        return row

    def append_heading_row(
        self,
        label: str,
        *,
        search_text: str | None = None,
    ) -> Gtk.ListBoxRow:
        """Append a non-selectable group heading included in fuzzy filtering."""

        row = Gtk.ListBoxRow()
        row.set_selectable(False)
        row.set_activatable(False)

        text_label = Gtk.Label(label=label, xalign=0)
        text_label.add_css_class("caption")
        text_label.add_css_class("dim-label")
        text_label.set_margin_start(6)
        text_label.set_margin_end(6)
        text_label.set_margin_top(10)
        text_label.set_margin_bottom(2)
        row.set_child(text_label)

        self._rows_by_widget[row] = ManagedEditorRow(None, search_text or label)
        self.list_box.append(row)
        return row

    def clear_rows(self) -> None:
        while row := self.list_box.get_row_at_index(0):
            self.list_box.remove(row)
        self._rows_by_selection.clear()
        self._rows_by_widget.clear()

    def selection_for_row(self, row: Gtk.ListBoxRow | None) -> EditorSelection | None:
        if row is None:
            return None
        record = self._rows_by_widget.get(row)
        return record.selection if record is not None else None

    def row_for_selection(self, selection: EditorSelection | None) -> Gtk.ListBoxRow | None:
        if selection is None:
            return None
        return self._rows_by_selection.get(selection)

    def search_text_for_row(self, row: Gtk.ListBoxRow) -> str:
        record = self._rows_by_widget.get(row)
        return record.search_text if record is not None else ""

    def select(self, selection: EditorSelection | None) -> bool:
        row = self.row_for_selection(selection)
        if selection is not None and row is None:
            return False
        self.list_box.select_row(row)
        return True

    def restore_active_selection(self) -> bool:
        row = self.row_for_selection(self._state.active_selection)
        if self._state.active_selection is not None and row is None:
            return False
        self._state.begin_selection_sync()
        try:
            self.list_box.select_row(row)
        finally:
            self._state.end_selection_sync()
        return True

    def show_search(self) -> None:
        self.search_entry.set_visible(True)
        self.search_entry.grab_focus()
        self.search_entry.select_region(0, -1)

    def hide_search(self) -> None:
        self.search_entry.set_text("")
        self.search_entry.set_visible(False)

    def _before_filter_changed(self) -> None:
        self._state.begin_selection_sync()

    def _after_filter_changed(self) -> None:
        try:
            self.restore_active_selection()
        finally:
            self._state.end_selection_sync()

    def _on_row_selected(
        self,
        _list_box: Gtk.ListBox,
        row: Gtk.ListBoxRow | None,
    ) -> None:
        if self._state.selection_guard_suppressed:
            return
        self._callbacks.selection_changed(self.selection_for_row(row))

    def _on_search_clicked(self, _button: Gtk.Button) -> None:
        self.show_search()

    def _on_search_stopped(self, _entry: Gtk.SearchEntry) -> None:
        self.hide_search()

    def _on_documentation_clicked(self, _button: Gtk.Button) -> None:
        self._callbacks.open_documentation()

    def _on_add_clicked(self, _button: Gtk.Button) -> None:
        self._callbacks.add_item()

    def _on_delete_clicked(self, _button: Gtk.Button) -> None:
        self._callbacks.delete_item()

    def _on_save_clicked(self, _button: Gtk.Button) -> None:
        self._callbacks.save_item()

    def _on_revert_clicked(self, _button: Gtk.Button) -> None:
        self._callbacks.revert_item()

    def _on_close_clicked(self, _button: Gtk.Button) -> None:
        self._callbacks.close_editor()
