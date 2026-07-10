"""Ordered action-sequence state and its focused GTK editor."""

from dataclasses import dataclass, field
from enum import Enum, auto

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import (  # pyright: ignore[reportAttributeAccessIssue]
    Adw,  # pyright: ignore[reportAttributeAccessIssue]
    GObject,  # pyright: ignore[reportAttributeAccessIssue]
    Gtk,  # pyright: ignore[reportAttributeAccessIssue]
)

from keymasq.common.model.actions import MappingAction
from keymasq.common.model.superkeys import (
    SuperkeyAction,
    mapping_action_to_superkey_action,
    superkey_action_to_mapping_action,
)
from keymasq.gui.widgets.action_sequence_labels import (
    describe_mapping_editor_action,
    describe_superkey_editor_action,
)
from keymasq.gui.widgets.dialog_sizing import parent_constrained_dialog_width


class ActionSequenceMode(Enum):
    """Action model edited by an action-sequence dialog."""

    SUPERKEY_PATTERN = auto()
    MAPPING = auto()


@dataclass(slots=True)
class OrderedActionState[ActionT]:
    """Mutable, UI-independent ordering state for an action bundle."""

    items: list[ActionT] = field(default_factory=list)

    def replace(self, index: int | None, action: ActionT | None) -> None:
        if action is None:
            if index is not None and 0 <= index < len(self.items):
                self.items.pop(index)
            return
        if index is None:
            self.items.append(action)
        elif 0 <= index < len(self.items):
            self.items[index] = action

    def move_up(self, index: int) -> int:
        if index <= 0 or index >= len(self.items):
            return index
        self.items[index - 1], self.items[index] = self.items[index], self.items[index - 1]
        return index - 1

    def move_down(self, index: int) -> int:
        if index < 0 or index >= len(self.items) - 1:
            return index
        self.items[index + 1], self.items[index] = self.items[index], self.items[index + 1]
        return index + 1

    def snapshot(self) -> list[ActionT]:
        return list(self.items)


class ActionSequenceRow(Gtk.ListBoxRow):
    """Typed row carrying the index of one action in the editor state."""

    def __init__(self, action_index: int, summary: str) -> None:
        super().__init__()
        self.action_index = action_index
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(8)
        box.set_margin_end(8)
        position = Gtk.Label(label=f"{action_index + 1}.")
        position.add_css_class("dim-label")
        box.append(position)
        summary_label = Gtk.Label(label=summary, xalign=0)
        summary_label.set_hexpand(True)
        summary_label.set_wrap(True)
        box.append(summary_label)
        self.set_child(box)


class ActionSequenceDialog(Adw.Dialog):
    __gsignals__ = {
        "actions-selected": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(
        self,
        parent: Gtk.Window,
        title: str,
        mode: ActionSequenceMode,
        current_actions: list[SuperkeyAction] | list[MappingAction] | None = None,
        action_key: str | None = None,
    ) -> None:
        super().__init__(
            title=title,
            content_width=parent_constrained_dialog_width(parent, 720),
            content_height=520,
        )
        self._parent = parent
        self._mode = mode
        self._action_key = action_key or ""
        self._state: OrderedActionState[SuperkeyAction | MappingAction] = OrderedActionState(
            list(current_actions or [])
        )

        self._build_ui()
        self._populate_actions()

    def _build_ui(self) -> None:
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        main_box.set_margin_top(12)
        main_box.set_margin_bottom(12)
        main_box.set_margin_start(12)
        main_box.set_margin_end(12)

        description = Gtk.Label(label=self._description())
        description.add_css_class("dim-label")
        description.set_wrap(True)
        description.set_xalign(0)
        main_box.append(description)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        main_box.append(scrolled)

        self.list_box = Gtk.ListBox()
        self.list_box.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.list_box.connect("row-selected", self._on_selection_changed)
        scrolled.set_child(self.list_box)

        button_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        button_row.set_halign(Gtk.Align.START)
        add_btn = Gtk.Button(label="Add")
        add_btn.connect("clicked", self._on_add_clicked)
        button_row.append(add_btn)

        self.edit_btn = Gtk.Button(label="Edit")
        self.edit_btn.set_sensitive(False)
        self.edit_btn.connect("clicked", self._on_edit_clicked)
        button_row.append(self.edit_btn)

        self.up_btn = Gtk.Button(label="Up")
        self.up_btn.set_sensitive(False)
        self.up_btn.connect("clicked", self._on_up_clicked)
        button_row.append(self.up_btn)

        self.down_btn = Gtk.Button(label="Down")
        self.down_btn.set_sensitive(False)
        self.down_btn.connect("clicked", self._on_down_clicked)
        button_row.append(self.down_btn)

        self.remove_btn = Gtk.Button(label="Remove")
        self.remove_btn.set_sensitive(False)
        self.remove_btn.add_css_class("destructive-action")
        self.remove_btn.connect("clicked", self._on_remove_clicked)
        button_row.append(self.remove_btn)
        main_box.append(button_row)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer.set_halign(Gtk.Align.END)
        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", self._on_cancel_clicked)
        footer.append(cancel_btn)
        save_btn = Gtk.Button(label="Done")
        save_btn.add_css_class("suggested-action")
        save_btn.connect("clicked", self._on_save_clicked)
        footer.append(save_btn)
        main_box.append(footer)
        self.set_child(main_box)

    def _description(self) -> str:
        if self._mode is ActionSequenceMode.SUPERKEY_PATTERN:
            return "Actions run in order and release in reverse order."
        if self._action_key == "overload":
            return "Actions receive the source key's normal down/repeat/up cycle in order."
        if self._action_key == "overload_down":
            return "Actions go through their press/release cycle when the key goes down."
        return "Actions go through their press/release cycle when the key comes up."

    def _populate_actions(self) -> None:
        while child := self.list_box.get_first_child():
            self.list_box.remove(child)

        if not self._state.items:
            row = Gtk.ListBoxRow()
            row.set_selectable(False)
            label = Gtk.Label(label="No actions configured")
            label.add_css_class("dim-label")
            label.set_margin_top(16)
            label.set_margin_bottom(16)
            label.set_margin_start(8)
            label.set_margin_end(8)
            row.set_child(label)
            self.list_box.append(row)
            self._update_buttons(None)
            return

        for index, action in enumerate(self._state.items):
            self.list_box.append(ActionSequenceRow(index, self._describe_action(action)))

        self._update_buttons(self.list_box.get_selected_row())

    def _describe_action(self, action: SuperkeyAction | MappingAction) -> str:
        if self._mode is ActionSequenceMode.SUPERKEY_PATTERN:
            return (
                describe_superkey_editor_action(action)
                if isinstance(action, SuperkeyAction)
                else "Unknown action"
            )
        return (
            describe_mapping_editor_action(action)
            if isinstance(action, MappingAction)
            else "Unknown action"
        )

    def _selected_index(self) -> int | None:
        row = self.list_box.get_selected_row()
        if not isinstance(row, ActionSequenceRow):
            return None
        return row.action_index

    def _update_buttons(self, row: Gtk.ListBoxRow | None) -> None:
        index = self._selected_index() if row is not None else None
        has_selection = index is not None
        self.edit_btn.set_sensitive(has_selection)
        self.remove_btn.set_sensitive(has_selection)
        self.up_btn.set_sensitive(has_selection and index > 0)
        self.down_btn.set_sensitive(has_selection and index < len(self._state.items) - 1)

    def _on_selection_changed(self, _list_box, row: Gtk.ListBoxRow | None) -> None:
        self._update_buttons(row)

    def _open_child_editor(
        self,
        current_action: SuperkeyAction | MappingAction | None = None,
        index: int | None = None,
    ) -> None:
        from keymasq.gui.widgets.key_selector.dialog import KeySelectorDialog

        if self._mode is ActionSequenceMode.SUPERKEY_PATTERN:
            allow_rapidfire = self._action_key in {"hold", "tap_hold"}
            dialog = KeySelectorDialog(
                self._parent,
                self._action_key or "tap",
                (
                    superkey_action_to_mapping_action(current_action)
                    if isinstance(current_action, SuperkeyAction)
                    else None
                ),
                allow_passthrough=False,
                allow_clear_mapping=False,
                allow_suppress=False,
                allow_superkey=False,
                allow_repeat=False,
                allow_rapidfire=allow_rapidfire,
                allow_tap=False,
                allow_macro_options=True,
            )
            dialog.connect("key-selected", self._on_pattern_action_selected, index)
            dialog.present(self._parent)
            return

        dialog = KeySelectorDialog(
            self._parent,
            self.get_title() or "Action",
            current_action if isinstance(current_action, MappingAction) else None,
            allow_passthrough=False,
            allow_clear_mapping=False,
            allow_suppress=False,
            allow_superkey=False,
            allow_repeat=False,
        )
        dialog.connect("key-selected", self._on_mapping_action_selected, index)
        dialog.present(self._parent)

    def _on_add_clicked(self, _button: Gtk.Button) -> None:
        self._open_child_editor()

    def _on_edit_clicked(self, _button: Gtk.Button) -> None:
        index = self._selected_index()
        if index is not None:
            self._open_child_editor(self._state.items[index], index)

    def _on_up_clicked(self, _button: Gtk.Button) -> None:
        index = self._selected_index()
        if index is None:
            return
        selected = self._state.move_up(index)
        if selected == index:
            return
        self._populate_actions()
        self.list_box.select_row(self.list_box.get_row_at_index(selected))

    def _on_down_clicked(self, _button: Gtk.Button) -> None:
        index = self._selected_index()
        if index is None:
            return
        selected = self._state.move_down(index)
        if selected == index:
            return
        self._populate_actions()
        self.list_box.select_row(self.list_box.get_row_at_index(selected))

    def _on_remove_clicked(self, _button: Gtk.Button) -> None:
        self._state.replace(self._selected_index(), None)
        self._populate_actions()

    def _on_pattern_action_selected(
        self,
        _dialog,
        action: MappingAction | None,
        index: int | None,
    ) -> None:
        converted = mapping_action_to_superkey_action(action) if action is not None else None
        self._state.replace(index, converted)
        self._populate_actions()

    def _on_mapping_action_selected(
        self,
        _dialog,
        action: MappingAction | None,
        index: int | None,
    ) -> None:
        self._state.replace(index, action)
        self._populate_actions()

    def _on_cancel_clicked(self, _button: Gtk.Button) -> None:
        self.close()

    def _on_save_clicked(self, _button: Gtk.Button) -> None:
        self.emit("actions-selected", self._state.snapshot())
        self.close()
