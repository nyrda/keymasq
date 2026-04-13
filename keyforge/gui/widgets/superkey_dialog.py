from collections.abc import Sequence

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, GObject, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keyforge.common.models import (
    ActionType,
    MappingAction,
    SuperkeyAction,
    SuperkeyConfig,
    SuperkeyMode,
    mapping_action_to_superkey_action,
    superkey_action_to_mapping_action,
)
from keyforge.gui.widgets.action_labels import describe_mapping_action_verbose
from keyforge.session.profiles import ProfileManager
from keyforge.session.superkeys import SuperkeyManager


def _append_action_state_markers(label: str, action: object) -> str:
    rapidfire_enabled = bool(getattr(action, "rapidfire_enabled", False))
    return f"{label} ⚡" if rapidfire_enabled else label


def _describe_pattern_superkey_action(
    action: object,
    *,
    exec_limit: int,
    exec_prefix: str,
    macro_prefix: str,
    target_separator: str,
    title_case_target_type: bool,
) -> str:
    if not isinstance(action, SuperkeyAction):
        return "Unknown action"

    def type_label(text: str) -> str:
        return text if title_case_target_type else text.lower()

    label: str
    if action.action_type.value == "exec":
        cmd = action.cmd or ""
        rendered = cmd[:exec_limit] + "..." if len(cmd) > exec_limit else cmd
        label = f"{exec_prefix}{rendered}"
    elif action.action_type.value == "macro":
        label = f"{macro_prefix}{action.macro_name or ''}"
    elif action.action_type == ActionType.KEYBOARD:
        label = f"{type_label('Keyboard')}{target_separator}{action.target or ''}"
    elif action.action_type == ActionType.MOUSE:
        label = f"{type_label('Mouse')}{target_separator}{action.target or ''}"
    elif action.action_type == ActionType.GAMEPAD:
        label = f"{type_label('Gamepad')}{target_separator}{action.target or ''}"
    elif action.action_type == ActionType.MOUSE_MOVE_REL:
        label = (
            f"{type_label('Mouse Move (rel)')}"
            f"{target_separator}{action.move_x}, {action.move_y}"
        )
    elif action.action_type == ActionType.MOUSE_MOVE_ABS:
        label = (
            f"{type_label('Mouse Move (abs)')}"
            f"{target_separator}{action.move_x}, {action.move_y}"
        )
    elif action.action_type == ActionType.COMPOSITOR_DISPATCH:
        dispatcher = action.compositor_dispatcher or "dispatch"
        args = str(action.compositor_args or "").strip()
        suffix = f" {args}" if args else ""
        label = f"{type_label('Compositor')}{target_separator}{dispatcher}{suffix}"
    elif action.action_type == ActionType.START_MACRO_RECORDING:
        label = type_label("Toggle Macro Recording")
    elif action.action_type == ActionType.STOP_MACRO_RECORDING:
        label = type_label("Stop Macro Recording")
    elif action.action_type == ActionType.CANCEL_MACRO_PLAYBACK:
        label = type_label("Cancel Macro Playback")
    elif action.action_type == ActionType.PROFILE_ENABLE:
        label = f"{type_label('Enable Profile')}{target_separator}{action.profile_name or ''}"
    elif action.action_type == ActionType.PROFILE_DISABLE:
        label = f"{type_label('Disable Profile')}{target_separator}{action.profile_name or ''}"
    elif action.action_type == ActionType.PROFILE_TOGGLE:
        label = f"{type_label('Toggle Profile')}{target_separator}{action.profile_name or ''}"
    else:
        label = describe_mapping_action_verbose(superkey_action_to_mapping_action(action))
    return _append_action_state_markers(label, action)


class ActionListDialog(Adw.Dialog):
    __gsignals__ = {
        "actions-selected": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(
        self,
        parent: Gtk.Window,
        title: str,
        list_mode: str,
        current_actions: list[SuperkeyAction] | list[MappingAction] | None = None,
        action_key: str | None = None,
    ):
        super().__init__(title=title, content_width=720, content_height=520)
        self._parent = parent
        self._list_mode = list_mode
        self._action_key = action_key or ""
        self._actions = list(current_actions or [])

        self._build_ui()
        self._populate_actions()

    def _build_ui(self) -> None:
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        main_box.set_margin_top(12)
        main_box.set_margin_bottom(12)
        main_box.set_margin_start(12)
        main_box.set_margin_end(12)

        description = Gtk.Label(
            label=(
                "Actions run in order and release in reverse order."
                if self._list_mode == "pattern"
                else "Actions receive the source key's normal down/repeat/up cycle in order."
            )
        )
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

    def _populate_actions(self) -> None:
        while child := self.list_box.get_first_child():
            self.list_box.remove(child)

        if not self._actions:
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

        for index, action in enumerate(self._actions):
            row = Gtk.ListBoxRow()
            row._action_index = index

            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            box.set_margin_top(8)
            box.set_margin_bottom(8)
            box.set_margin_start(8)
            box.set_margin_end(8)

            position = Gtk.Label(label=f"{index + 1}.")
            position.add_css_class("dim-label")
            box.append(position)

            summary = Gtk.Label(label=self._describe_action(action), xalign=0)
            summary.set_hexpand(True)
            summary.set_wrap(True)
            box.append(summary)

            row.set_child(box)
            self.list_box.append(row)

        self._update_buttons(self.list_box.get_selected_row())

    def _describe_action(self, action: object) -> str:
        if self._list_mode == "pattern":
            return _describe_pattern_superkey_action(
                action,
                exec_limit=40,
                exec_prefix="Exec -> ",
                macro_prefix="Macro -> ",
                target_separator=" -> ",
                title_case_target_type=True,
            )
        return (
            _append_action_state_markers(describe_mapping_action_verbose(action), action)
            if isinstance(action, MappingAction)
            else "Unknown action"
        )

    def _selected_index(self) -> int | None:
        row = self.list_box.get_selected_row()
        if row is None or not hasattr(row, "_action_index"):
            return None
        return int(row._action_index)

    def _update_buttons(self, row: Gtk.ListBoxRow | None) -> None:
        index = self._selected_index() if row is not None else None
        has_selection = index is not None
        self.edit_btn.set_sensitive(has_selection)
        self.remove_btn.set_sensitive(has_selection)
        self.up_btn.set_sensitive(has_selection and index > 0)
        self.down_btn.set_sensitive(has_selection and index < len(self._actions) - 1)

    def _on_selection_changed(self, _list_box, row: Gtk.ListBoxRow | None) -> None:
        self._update_buttons(row)

    def _open_child_editor(
        self,
        current_action: object | None = None,
        index: int | None = None,
    ) -> None:
        if self._list_mode == "pattern":
            from keyforge.gui.widgets.key_selector_dialog import KeySelectorDialog

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
                allow_rapidfire=allow_rapidfire,
                allow_tap=False,
                allow_macro_options=True,
            )
            dialog.connect("key-selected", self._on_pattern_action_selected, index)
            dialog.present()
            return

        from keyforge.gui.widgets.key_selector_dialog import KeySelectorDialog

        dialog = KeySelectorDialog(
            self._parent,
            self.get_title() or "Action",
            current_action if isinstance(current_action, MappingAction) else None,
            allow_passthrough=False,
            allow_clear_mapping=False,
            allow_suppress=False,
            allow_superkey=False,
        )
        dialog.connect("key-selected", self._on_mapping_action_selected, index)
        dialog.present()

    def _on_add_clicked(self, _button: Gtk.Button) -> None:
        self._open_child_editor()

    def _on_edit_clicked(self, _button: Gtk.Button) -> None:
        index = self._selected_index()
        if index is None:
            return
        self._open_child_editor(self._actions[index], index)

    def _on_up_clicked(self, _button: Gtk.Button) -> None:
        index = self._selected_index()
        if index is None or index == 0:
            return
        self._actions[index - 1], self._actions[index] = (
            self._actions[index],
            self._actions[index - 1],
        )
        self._populate_actions()
        self.list_box.select_row(self.list_box.get_row_at_index(index - 1))

    def _on_down_clicked(self, _button: Gtk.Button) -> None:
        index = self._selected_index()
        if index is None or index >= len(self._actions) - 1:
            return
        self._actions[index + 1], self._actions[index] = (
            self._actions[index],
            self._actions[index + 1],
        )
        self._populate_actions()
        self.list_box.select_row(self.list_box.get_row_at_index(index + 1))

    def _on_remove_clicked(self, _button: Gtk.Button) -> None:
        index = self._selected_index()
        if index is None:
            return
        self._actions.pop(index)
        self._populate_actions()

    def _on_pattern_action_selected(
        self,
        _dialog,
        action: MappingAction | None,
        index: int | None,
    ) -> None:
        converted = mapping_action_to_superkey_action(action) if action is not None else None
        if converted is None:
            if index is not None and index < len(self._actions):
                self._actions.pop(index)
        elif index is None:
            self._actions.append(converted)
        else:
            self._actions[index] = converted
        self._populate_actions()

    def _on_mapping_action_selected(
        self,
        _dialog,
        action: MappingAction | None,
        index: int | None,
    ) -> None:
        if action is None:
            if index is not None and index < len(self._actions):
                self._actions.pop(index)
        elif index is None:
            self._actions.append(action)
        else:
            self._actions[index] = action
        self._populate_actions()

    def _on_cancel_clicked(self, _button: Gtk.Button) -> None:
        self.close()

    def _on_save_clicked(self, _button: Gtk.Button) -> None:
        self.emit("actions-selected", list(self._actions))
        self.close()


class SuperkeyDialog(Adw.Dialog):
    __gsignals__ = {
        "superkey-saved": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "superkey-deleted": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, parent: Gtk.Window, profile_manager: ProfileManager | None = None):
        super().__init__(title="Manage Super Keys", content_width=920, content_height=640)
        self._parent = parent
        self.manager = SuperkeyManager()
        self.profile_manager = profile_manager
        self._current_config: SuperkeyConfig | None = None
        self._modified = False
        self._mode_items = [SuperkeyMode.PATTERN, SuperkeyMode.OVERLOAD]

        self._build_ui()
        self._load_superkeys()
        self._setup_shortcuts()

    def _setup_shortcuts(self) -> None:
        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", self._on_key_pressed)
        self.add_controller(key_controller)

    def _on_key_pressed(self, _controller, keyval, _keycode, _state) -> bool:
        if keyval == Gdk.KEY_Escape:
            self.close()
            return True
        return False

    def _build_ui(self) -> None:
        main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)

        main_box.append(self._build_left_panel())
        main_box.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        main_box.append(self._build_right_panel())

        self.set_child(main_box)

    def _build_left_panel(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_size_request(220, -1)

        label = Gtk.Label(label="Super Keys")
        label.add_css_class("title-4")
        box.append(label)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self.list_box = Gtk.ListBox()
        self.list_box.set_vexpand(True)
        self.list_box.connect("row-selected", self._on_superkey_selected)
        scrolled.set_child(self.list_box)

        box.append(scrolled)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

        new_btn = Gtk.Button(label="New")
        new_btn.connect("clicked", self._on_new_clicked)
        btn_box.append(new_btn)

        self.delete_btn = Gtk.Button(label="Delete")
        self.delete_btn.set_sensitive(False)
        self.delete_btn.add_css_class("destructive-action")
        self.delete_btn.connect("clicked", self._on_delete_clicked)
        btn_box.append(self.delete_btn)

        box.append(btn_box)
        return box

    def _build_right_panel(self) -> Gtk.Widget:
        self.right_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.right_box.set_margin_top(12)
        self.right_box.set_margin_bottom(12)
        self.right_box.set_margin_start(12)
        self.right_box.set_margin_end(12)
        self.right_box.set_hexpand(True)
        self.right_box.set_sensitive(False)

        fields_grid = Gtk.Grid()
        fields_grid.set_column_spacing(12)
        fields_grid.set_row_spacing(8)

        name_label = Gtk.Label(label="Name:")
        name_label.set_xalign(0)
        name_label.set_size_request(96, -1)
        fields_grid.attach(name_label, 0, 0, 1, 1)

        self.name_entry = Gtk.Entry()
        self.name_entry.set_hexpand(True)
        self.name_entry.connect("changed", self._on_modified)
        fields_grid.attach(self.name_entry, 1, 0, 1, 1)

        desc_label = Gtk.Label(label="Description:")
        desc_label.set_xalign(0)
        desc_label.set_size_request(96, -1)
        fields_grid.attach(desc_label, 0, 1, 1, 1)

        self.desc_entry = Gtk.Entry()
        self.desc_entry.set_hexpand(True)
        self.desc_entry.connect("changed", self._on_modified)
        fields_grid.attach(self.desc_entry, 1, 1, 1, 1)

        mode_label = Gtk.Label(label="Mode:")
        mode_label.set_xalign(0)
        mode_label.set_size_request(96, -1)
        fields_grid.attach(mode_label, 0, 2, 1, 1)

        self.mode_dropdown = Gtk.DropDown.new_from_strings(["Pattern", "Overload"])
        self.mode_dropdown.connect("notify::selected", self._on_mode_changed)
        fields_grid.attach(self.mode_dropdown, 1, 2, 1, 1)

        self.right_box.append(fields_grid)
        self.right_box.append(Gtk.Separator())

        self.actions_group = Adw.PreferencesGroup()
        self.actions_group.set_title("Actions")

        self.tap_row = self._build_action_row("Tap", "tap", "pattern")
        self.actions_group.add(self.tap_row)

        self.double_tap_row = self._build_action_row("Double Tap", "double_tap", "pattern")
        self.actions_group.add(self.double_tap_row)

        self.hold_row = self._build_action_row("Hold", "hold", "pattern")
        self.actions_group.add(self.hold_row)

        self.tap_hold_row = self._build_action_row("Tap + Hold", "tap_hold", "pattern")
        self.actions_group.add(self.tap_hold_row)

        self.overload_row = self._build_action_row("Overload Actions", "overload", "overload")
        self.actions_group.add(self.overload_row)

        self.right_box.append(self.actions_group)
        self.right_box.append(Gtk.Separator())

        self.timing_group = Adw.PreferencesGroup()
        self.timing_group.set_title("Timing")

        self.tap_timeout_row, self.tap_timeout_spin = self._build_timing_row(
            "Tap Timeout",
            "Maximum time for a tap (ms)",
            200,
            50,
            1000,
        )
        self.timing_group.add(self.tap_timeout_row)

        self.double_tap_window_row, self.double_tap_window_spin = self._build_timing_row(
            "Double Tap Window",
            "Maximum time between taps (ms)",
            300,
            50,
            1000,
        )
        self.timing_group.add(self.double_tap_window_row)

        self.hold_threshold_row, self.hold_threshold_spin = self._build_timing_row(
            "Hold Threshold",
            "Time to consider a hold (ms)",
            300,
            50,
            2000,
        )
        self.timing_group.add(self.hold_threshold_row)

        self.right_box.append(self.timing_group)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        btn_box.set_halign(Gtk.Align.END)
        btn_box.set_margin_top(12)

        top_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        top_row.set_halign(Gtk.Align.END)

        self.revert_btn = Gtk.Button(label="Revert")
        self.revert_btn.set_sensitive(False)
        self.revert_btn.connect("clicked", self._on_revert_clicked)
        top_row.append(self.revert_btn)

        self.save_btn = Gtk.Button(label="Save")
        self.save_btn.add_css_class("suggested-action")
        self.save_btn.set_sensitive(False)
        self.save_btn.connect("clicked", self._on_save_clicked)
        top_row.append(self.save_btn)

        btn_box.append(top_row)

        bottom_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        bottom_row.set_halign(Gtk.Align.END)

        close_btn = Gtk.Button(label="Close")
        close_btn.connect("clicked", self._on_close_clicked)
        bottom_row.append(close_btn)

        btn_box.append(bottom_row)

        self.right_box.append(btn_box)
        self._update_mode_visibility()
        return self.right_box

    def _build_timing_row(
        self,
        title: str,
        subtitle: str,
        default: int,
        lower: int,
        upper: int,
    ) -> tuple[Adw.ActionRow, Gtk.SpinButton]:
        row = Adw.ActionRow()
        row.set_title(title)
        row.set_subtitle(subtitle)

        adjustment = Gtk.Adjustment(
            value=default,
            lower=lower,
            upper=upper,
            step_increment=10,
        )
        spin = Gtk.SpinButton(adjustment=adjustment, climb_rate=0, digits=0)
        spin.set_numeric(True)
        spin.set_width_chars(5)
        spin.set_max_width_chars(5)
        spin.set_alignment(1.0)
        spin.connect("value-changed", self._on_modified)
        row.add_suffix(spin)
        return row, spin

    def _build_action_row(self, title: str, action_key: str, row_mode: str) -> Adw.ActionRow:
        row = Adw.ActionRow()
        row.set_title(title)
        row._action_key = action_key
        row._row_mode = row_mode
        row._action_items = []

        label = Gtk.Label(label="(none)")
        label.set_xalign(1.0)
        label.set_wrap(True)
        row.add_suffix(label)
        row._action_label = label

        edit_btn = Gtk.Button(label="Edit")
        edit_btn.add_css_class("flat")
        edit_btn.connect("clicked", self._on_edit_action_clicked, row)
        row.add_suffix(edit_btn)

        clear_btn = Gtk.Button(icon_name="edit-clear-symbolic")
        clear_btn.add_css_class("flat")
        clear_btn.connect("clicked", self._on_clear_action_clicked, row)
        row.add_suffix(clear_btn)
        return row

    def _current_mode(self) -> SuperkeyMode:
        return self._mode_items[self.mode_dropdown.get_selected()]

    def _update_mode_visibility(self) -> None:
        mode = self._current_mode()
        pattern_visible = mode == SuperkeyMode.PATTERN
        for row in (self.tap_row, self.double_tap_row, self.hold_row, self.tap_hold_row):
            row.set_visible(pattern_visible)
        self.overload_row.set_visible(not pattern_visible)
        self.timing_group.set_visible(pattern_visible)

    def _load_superkeys(self) -> None:
        while row := self.list_box.get_row_at_index(0):
            self.list_box.remove(row)

        names = self.manager.list_superkeys()
        for name in names:
            label = Gtk.Label(label=name, xalign=0)
            label.set_margin_start(6)
            label.set_margin_end(6)
            label.set_margin_top(6)
            label.set_margin_bottom(6)
            self.list_box.append(label)

        if names:
            self.list_box.select_row(self.list_box.get_row_at_index(0))

    def _on_superkey_selected(self, _list_box, row) -> None:
        if row is None:
            self._current_config = None
            self.right_box.set_sensitive(False)
            self.delete_btn.set_sensitive(False)
            return

        label = row.get_child()
        name = label.get_label()
        self._current_config = self.manager.get_superkey(name)
        if self._current_config:
            self._populate_editor()
            self.right_box.set_sensitive(True)
            self.delete_btn.set_sensitive(True)

        self._modified = False
        self._update_buttons()

    def _populate_editor(self) -> None:
        if not self._current_config:
            return

        self.name_entry.set_text(self._current_config.name)
        self.desc_entry.set_text(self._current_config.description or "")
        self.mode_dropdown.set_selected(self._mode_items.index(self._current_config.mode))

        self._populate_action_row(self.tap_row, list(self._current_config.tap_actions))
        self._populate_action_row(
            self.double_tap_row,
            list(self._current_config.double_tap_actions),
        )
        self._populate_action_row(self.hold_row, list(self._current_config.hold_actions))
        self._populate_action_row(self.tap_hold_row, list(self._current_config.tap_hold_actions))
        self._populate_action_row(self.overload_row, list(self._current_config.overload_actions))

        self.tap_timeout_spin.set_value(self._current_config.tap_timeout_ms)
        self.double_tap_window_spin.set_value(self._current_config.double_tap_window_ms)
        self.hold_threshold_spin.set_value(self._current_config.hold_threshold_ms)
        self._update_mode_visibility()

    def _populate_action_row(
        self,
        row: Adw.ActionRow,
        actions: list[SuperkeyAction] | list[MappingAction],
    ) -> None:
        row._action_items = list(actions)
        row._action_label.set_label(self._describe_action_list(row._action_items, row._row_mode))
        tooltip = self._describe_action_tooltip(row._action_items, row._row_mode)
        row.set_tooltip_text(tooltip)
        row._action_label.set_tooltip_text(tooltip)

    def _describe_action_list(self, actions: Sequence[object], row_mode: str) -> str:
        if not actions:
            return "(none)"

        if row_mode == "pattern":
            labels = [
                _describe_pattern_superkey_action(
                    action,
                    exec_limit=20,
                    exec_prefix="exec ",
                    macro_prefix="macro ",
                    target_separator=" ",
                    title_case_target_type=False,
                )
                for action in actions[:2]
            ]
        else:
            labels = [
                describe_mapping_action_verbose(action)
                if isinstance(action, MappingAction)
                else "Unknown action"
                for action in actions[:2]
            ]

        if len(actions) > 2:
            labels.append("...")

        suffix = ", ".join(labels)
        noun = "action" if len(actions) == 1 else "actions"
        return f"{len(actions)} {noun}: {suffix}"

    def _describe_action_tooltip(self, actions: Sequence[object], row_mode: str) -> str:
        if not actions:
            return "(none)"

        lines: list[str] = []
        for index, action in enumerate(actions, start=1):
            if row_mode == "pattern":
                description = _describe_pattern_superkey_action(
                    action,
                    exec_limit=20,
                    exec_prefix="exec ",
                    macro_prefix="macro ",
                    target_separator=" ",
                    title_case_target_type=False,
                )
            else:
                description = (
                    describe_mapping_action_verbose(action)
                    if isinstance(action, MappingAction)
                    else "Unknown action"
                )
                description = _append_action_state_markers(description, action)
            lines.append(f"{index}. {description}")
        return "\n".join(lines)

    def _on_mode_changed(self, _dropdown, _param) -> None:
        self._update_mode_visibility()
        self._on_modified()

    def _on_modified(self, *_args) -> None:
        self._modified = True
        self._update_buttons()

    def _update_buttons(self) -> None:
        if not hasattr(self, "revert_btn") or not hasattr(self, "save_btn"):
            return
        self.revert_btn.set_sensitive(self._modified)
        self.save_btn.set_sensitive(self._modified)

    def _on_new_clicked(self, _button) -> None:
        self.list_box.select_row(None)
        self._current_config = SuperkeyConfig(name="New Super Key")
        self._populate_editor()
        self.right_box.set_sensitive(True)
        self.delete_btn.set_sensitive(False)
        self._modified = True
        self._update_buttons()
        self.name_entry.grab_focus()

    def start_new_superkey(self) -> None:
        self._on_new_clicked(None)

    def _on_delete_clicked(self, _button) -> None:
        if not self._current_config:
            return

        name = self._current_config.name
        if self.profile_manager:
            affected = self.profile_manager.find_profiles_using_superkey(name)
            if affected:
                dialog = Adw.AlertDialog()
                dialog.set_heading("Delete Super Key?")
                dialog.set_body(
                    f"'{name}' is used in {len(affected)} profile(s). "
                    "It will be replaced with Suppress in those profiles."
                )
                dialog.add_response("cancel", "Cancel")
                dialog.add_response("delete", "Delete")
                dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
                dialog.connect("response", self._on_delete_confirm, name)
                dialog.present(self._parent)
                return

        self._do_delete(name)

    def _on_delete_confirm(self, _dialog, response, name: str) -> None:
        if response == "delete":
            self._do_delete(name)

    def _do_delete(self, name: str) -> None:
        if self.profile_manager:
            self.profile_manager.replace_superkey_with_suppress(name)

        self.manager.delete_superkey(name)
        self._load_superkeys()
        self.emit("superkey-deleted", name)

    def _on_revert_clicked(self, _button) -> None:
        if self._current_config:
            self._populate_editor()
        self._modified = False
        self._update_buttons()

    def _on_save_clicked(self, _button) -> None:
        name = self.name_entry.get_text().strip()
        if not name:
            return

        old_name = self._current_config.name if self._current_config else None
        mode = self._current_mode()
        config = SuperkeyConfig(
            name=name,
            description=self.desc_entry.get_text().strip() or None,
            mode=mode,
            tap_actions=list(self.tap_row._action_items) if mode == SuperkeyMode.PATTERN else [],
            double_tap_actions=(
                list(self.double_tap_row._action_items) if mode == SuperkeyMode.PATTERN else []
            ),
            hold_actions=list(self.hold_row._action_items) if mode == SuperkeyMode.PATTERN else [],
            tap_hold_actions=(
                list(self.tap_hold_row._action_items) if mode == SuperkeyMode.PATTERN else []
            ),
            overload_actions=(
                list(self.overload_row._action_items) if mode == SuperkeyMode.OVERLOAD else []
            ),
            tap_timeout_ms=self.tap_timeout_spin.get_value_as_int(),
            double_tap_window_ms=self.double_tap_window_spin.get_value_as_int(),
            hold_threshold_ms=self.hold_threshold_spin.get_value_as_int(),
        )

        if old_name and old_name != name:
            self.manager.rename_superkey(old_name, name)

        self.manager.save_superkey(config)
        self._current_config = config
        self._modified = False
        self._update_buttons()
        self._load_superkeys()

        idx = 0
        while True:
            row = self.list_box.get_row_at_index(idx)
            if row is None:
                break
            label = row.get_child()
            if label and label.get_label() == name:
                self.list_box.select_row(row)
                break
            idx += 1

        self.emit("superkey-saved", name)

    def _on_edit_action_clicked(self, _button, row: Adw.ActionRow) -> None:
        title = (
            f"Edit {row.get_title()} Actions"
            if row._row_mode == "pattern"
            else "Edit Overload Actions"
        )
        dialog = ActionListDialog(
            self._parent,
            title,
            row._row_mode,
            row._action_items,
            action_key=row._action_key,
        )
        dialog.connect("actions-selected", self._on_actions_selected, row)
        dialog.present()

    def _on_actions_selected(self, _dialog, actions: list[object], row: Adw.ActionRow) -> None:
        row._action_items = list(actions)
        row._action_label.set_label(self._describe_action_list(row._action_items, row._row_mode))
        tooltip = self._describe_action_tooltip(row._action_items, row._row_mode)
        row.set_tooltip_text(tooltip)
        row._action_label.set_tooltip_text(tooltip)
        self._on_modified()

    def _on_clear_action_clicked(self, _button, row: Adw.ActionRow) -> None:
        row._action_items = []
        row._action_label.set_label("(none)")
        row.set_tooltip_text("(none)")
        row._action_label.set_tooltip_text("(none)")
        self._on_modified()

    def _on_close_clicked(self, _button: Gtk.Button) -> None:
        self.close()
