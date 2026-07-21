from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.gui.widgets.fuzzy_search import fuzzy_query_matches, install_listbox_fuzzy_filter

SORT_NONE = 0
SORT_NAME = 1
SORT_TRIGGER = 2
SORT_ACTION = 3

_ARROW_UP = " \u25b4"
_ARROW_DOWN = " \u25be"
_SORT_LABELS = {
    SORT_NAME: "Name",
    SORT_TRIGGER: "Trigger",
    SORT_ACTION: "Action",
}


class SortableComboList[ItemT]:
    def __init__(
        self,
        *,
        search_placeholder: str,
        search_tooltip: str,
        empty_text: str,
        no_match_text: str,
        get_items: Callable[[], Iterable[ItemT]],
        sort_keys: Mapping[int, Callable[[ItemT], str]],
        create_row: Callable[[ItemT], Gtk.ListBoxRow],
        search_matches: Callable[[str, Gtk.ListBoxRow], bool] | None = None,
        is_available: Callable[[], bool] | None = None,
        row_activated: Callable[[Gtk.ListBox, Gtk.ListBoxRow], None] | None = None,
        trailing_header_width: int | None = None,
    ) -> None:
        self._empty_text = empty_text
        self._no_match_text = no_match_text
        self._get_items = get_items
        self._sort_keys = dict(sort_keys)
        self._create_row = create_row
        self._search_matches = search_matches or self._default_search_matches
        self._is_available = is_available or (lambda: True)
        self._sort_column = SORT_NONE
        self._sort_ascending = True

        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text(search_placeholder)
        self.search_entry.set_tooltip_text(search_tooltip)
        self.search_entry.set_visible(False)
        self.search_entry.connect("stop-search", self._on_search_stop)

        self.section_label = Gtk.Label(label="")
        self.section_label.add_css_class("heading")
        self.section_label.set_hexpand(True)
        self.section_label.set_halign(Gtk.Align.START)
        self.section_label.set_visible(False)

        self.column_header = self._create_column_header(trailing_header_width)

        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self.listbox.add_css_class("boxed-list")
        if row_activated is not None:
            self.listbox.connect("row-activated", row_activated)
        install_listbox_fuzzy_filter(
            self.listbox,
            self.search_entry,
            row_matches=self._search_matches,
            after_filter_changed=self._after_search_filter_changed,
        )

    def _create_column_header(self, trailing_header_width: int | None) -> Gtk.Box:
        col_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        col_header.add_css_class("combo-column-header")

        self.name_header_btn = self._create_header_button(SORT_NAME, expand=True)
        col_header.append(self.name_header_btn)

        self.trigger_header_btn = self._create_header_button(SORT_TRIGGER)
        col_header.append(self.trigger_header_btn)

        self.action_header_btn = self._create_header_button(SORT_ACTION)
        self.action_header_btn.set_size_request(180, -1)
        col_header.append(self.action_header_btn)

        if trailing_header_width is not None:
            spacer = Gtk.Box()
            spacer.set_size_request(trailing_header_width, -1)
            col_header.append(spacer)

        self.update_column_header_labels()
        return col_header

    def _create_header_button(self, column: int, *, expand: bool = False) -> Gtk.Button:
        button = Gtk.Button(label=_SORT_LABELS[column])
        button.add_css_class("flat")
        button.add_css_class("combo-col-btn")
        button.set_halign(Gtk.Align.START)
        button.set_hexpand(expand)
        button.connect("clicked", self._on_column_header_clicked, column)
        return button

    def sorted_items(self) -> list[ItemT]:
        items = list(self._get_items())
        sort_key = self._sort_keys.get(self._sort_column)
        if sort_key is None:
            return items
        result = sorted(items, key=lambda item: sort_key(item).casefold())
        if not self._sort_ascending:
            result.reverse()
        return result

    def update_column_header_labels(self) -> None:
        for column, button in (
            (SORT_NAME, self.name_header_btn),
            (SORT_TRIGGER, self.trigger_header_btn),
            (SORT_ACTION, self.action_header_btn),
        ):
            base = _SORT_LABELS[column]
            if self._sort_column == column:
                arrow = _ARROW_UP if self._sort_ascending else _ARROW_DOWN
                button.set_label(f"{base}{arrow}")
            else:
                button.set_label(base)

    def render(self) -> None:
        while row := self.listbox.get_first_child():
            self.listbox.remove(row)

        if not self._is_available():
            self._hide_unavailable()
            return

        items = self.sorted_items()
        for item in items:
            self.listbox.append(self._create_row(item))
        self.update_state(has_combos=bool(items))

    def iter_rows(self):
        row = self.listbox.get_first_child()
        while row is not None:
            yield row
            row = row.get_next_sibling()

    def visible_count(self) -> int:
        query = self.search_entry.get_text()
        return sum(1 for row in self.iter_rows() if self._search_matches(query, row))

    @staticmethod
    def _default_search_matches(query: str, row: Gtk.ListBoxRow) -> bool:
        return fuzzy_query_matches(query, getattr(row, "_search_text", ""))

    def update_state(self, *, has_combos: bool | None = None) -> None:
        if not self._is_available():
            self._hide_unavailable()
            return

        has_combos = has_combos if has_combos is not None else any(self.iter_rows())
        visible_count = self.visible_count() if has_combos else 0
        has_visible_rows = visible_count > 0
        self.listbox.set_visible(has_visible_rows)
        self.column_header.set_visible(has_visible_rows)
        if has_visible_rows:
            self.section_label.set_visible(False)
        elif has_combos and self.search_entry.get_text().strip():
            self.section_label.set_text(self._no_match_text)
            self.section_label.set_visible(True)
        else:
            self.section_label.set_text(self._empty_text)
            self.section_label.set_visible(True)

    def show_search(self) -> None:
        if not self._is_available():
            return
        self.search_entry.set_visible(True)
        self.search_entry.grab_focus()
        self.search_entry.select_region(0, -1)

    def hide_search(self) -> None:
        if self.search_entry.get_text():
            self.search_entry.set_text("")
        self.search_entry.set_visible(False)

    def on_column_header_clicked(self, column: int) -> None:
        if self._sort_column == column:
            self._sort_ascending = not self._sort_ascending
        else:
            self._sort_column = column
            self._sort_ascending = True
        self.update_column_header_labels()
        self.render()

    def _hide_unavailable(self) -> None:
        self.hide_search()
        self.section_label.set_visible(False)
        self.listbox.set_visible(False)
        self.column_header.set_visible(False)

    def _after_search_filter_changed(self) -> None:
        self.update_state()

    def _on_search_stop(self, _entry: Gtk.SearchEntry) -> None:
        self.hide_search()

    def _on_column_header_clicked(self, _button: Gtk.Button, column: int) -> None:
        self.on_column_header_clicked(column)
