import pytest

from tests.gui.support import collect_listbox_row_labels

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")


def test_sortable_combo_list_sorts_headers_and_search_empty_state() -> None:
    from gi.repository import Gtk

    from keymasq.gui.widgets.combo_list import (
        SORT_ACTION,
        SORT_NAME,
        SORT_TRIGGER,
        SortableComboList,
    )

    items = [
        {"name": "Bravo", "trigger": "C", "action": "C", "search": "bravo key c"},
        {"name": "Alpha", "trigger": "A", "action": "B", "search": "alpha key a"},
        {"name": "Charlie", "trigger": "B", "action": "A", "search": "charlie key b"},
    ]

    def create_row(item: dict[str, str]) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row._search_text = item["search"]  # type: ignore[attr-defined]
        row.set_child(Gtk.Label(label=item["name"]))
        return row

    combo_list = SortableComboList[dict[str, str]](
        search_placeholder="Search combos",
        search_tooltip="Filter combos",
        empty_text="No combos.",
        no_match_text="No matching combos.",
        get_items=lambda: items,
        sort_keys={
            SORT_NAME: lambda item: item["name"],
            SORT_TRIGGER: lambda item: item["trigger"],
            SORT_ACTION: lambda item: item["action"],
        },
        create_row=create_row,
    )

    combo_list.render()

    assert collect_listbox_row_labels(combo_list.listbox) == ["Bravo", "Alpha", "Charlie"]
    assert combo_list.name_header_btn.get_label() == "Name"

    combo_list.name_header_btn.emit("clicked")
    assert collect_listbox_row_labels(combo_list.listbox) == ["Alpha", "Bravo", "Charlie"]
    assert combo_list.name_header_btn.get_label() == "Name \u25b4"

    combo_list.name_header_btn.emit("clicked")
    assert collect_listbox_row_labels(combo_list.listbox) == ["Charlie", "Bravo", "Alpha"]
    assert combo_list.name_header_btn.get_label() == "Name \u25be"

    combo_list.trigger_header_btn.emit("clicked")
    assert collect_listbox_row_labels(combo_list.listbox) == ["Alpha", "Charlie", "Bravo"]
    assert combo_list.trigger_header_btn.get_label() == "Trigger \u25b4"
    assert combo_list.name_header_btn.get_label() == "Name"

    combo_list.action_header_btn.emit("clicked")
    assert collect_listbox_row_labels(combo_list.listbox) == ["Charlie", "Alpha", "Bravo"]
    assert combo_list.action_header_btn.get_label() == "Action \u25b4"

    combo_list.search_entry.set_text("missing")
    assert combo_list.visible_count() == 0
    assert combo_list.section_label.get_text() == "No matching combos."
    assert combo_list.listbox.get_visible() is False
    assert combo_list.column_header.get_visible() is False

    combo_list.search_entry.set_text("")
    items.clear()
    combo_list.render()
    assert combo_list.section_label.get_text() == "No combos."
    assert combo_list.listbox.get_visible() is False
