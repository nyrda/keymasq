import logging

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, GObject, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq import __version__
from keymasq.common.models import (
    ActionType,
    MappingAction,
    SuperkeyAction,
    SuperkeyConfig,
    SuperkeyMode,
    mapping_action_to_superkey_action,
    superkey_action_to_mapping_action,
)
from keymasq.gui.widgets.action_labels import describe_mapping_action_verbose
from keymasq.gui.widgets.dialog_sizing import parent_constrained_dialog_width
from keymasq.gui.widgets.fuzzy_search import install_listbox_fuzzy_filter
from keymasq.session.profiles import ProfileManager
from keymasq.session.superkeys import SuperkeyManager

log = logging.getLogger("keymasq.gui.widgets.superkey_dialog")


def _docs_version() -> str:
    version = __version__.strip()
    if not version:
        return "master"
    if "dev" in version:
        return "master"
    return f"v{version.removeprefix('v')}"


def _superkeys_docs_url() -> str:
    return f"https://keymasq.tools/docs/{_docs_version()}/SUPERKEYS/"


def _superkey_search_text(config: SuperkeyConfig | None, name: str) -> str:
    if config is None:
        return name
    return " ".join(
        [
            str(config.name or ""),
            str(config.description or ""),
            config.mode.value,
            str(len(config.tap_actions)),
            str(len(config.double_tap_actions)),
            str(len(config.hold_actions)),
            str(len(config.tap_hold_actions)),
            str(len(config.overload_actions)),
            str(len(config.overload_down_actions)),
            str(len(config.overload_up_actions)),
            "actions",
        ]
    )


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
    elif action.action_type == ActionType.GAMEPAD_AXIS:
        label = (
            f"{type_label('Gamepad Axis')}{target_separator}"
            f"{action.target or ''}={int(action.axis_value)}"
        )
    elif action.action_type == ActionType.MOUSE_MOVE_REL:
        label = (
            f"{type_label('Mouse Move (rel)')}{target_separator}{action.move_x}, {action.move_y}"
        )
    elif action.action_type == ActionType.MOUSE_MOVE_ABS:
        label = (
            f"{type_label('Mouse Move (abs)')}{target_separator}{action.move_x}, {action.move_y}"
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
    elif action.action_type == ActionType.EMERGENCY_RESET:
        label = type_label("Emergency Runtime Reset")
    elif action.action_type == ActionType.PROFILE_ENABLE:
        label = (
            f"{type_label('Enable Profile')}{target_separator}{action.profile_name or ''}"
            f"{_profile_lifetime_suffix(action)}"
        )
    elif action.action_type == ActionType.PROFILE_DISABLE:
        label = f"{type_label('Disable Profile')}{target_separator}{action.profile_name or ''}"
    elif action.action_type == ActionType.PROFILE_TOGGLE:
        label = (
            f"{type_label('Toggle Profile')}{target_separator}{action.profile_name or ''}"
            f"{_profile_lifetime_suffix(action)}"
        )
    else:
        label = describe_mapping_action_verbose(superkey_action_to_mapping_action(action))
    return _append_action_state_markers(label, action)


def _profile_lifetime_suffix(action: object) -> str:
    policy = getattr(action, "profile_deactivation", None)
    if policy is None:
        return ""
    if policy.on_trigger_end and policy.after_actions is None and policy.timeout_ms is None:
        return " (while held)"
    if not policy.on_trigger_end and policy.timeout_ms is None and policy.after_actions == 1:
        return " (one-shot)"
    if not policy.on_trigger_end and policy.timeout_ms is None and policy.after_actions:
        return f" ({int(policy.after_actions)} actions)"
    if not policy.on_trigger_end and policy.after_actions is None and policy.timeout_ms:
        return f" ({int(policy.timeout_ms)} ms)"
    return " (custom)"


def _describe_superkey_dialog_action(action: object, row_mode: str) -> str:
    if row_mode == "pattern":
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
        super().__init__(
            title=title,
            content_width=parent_constrained_dialog_width(parent, 720),
            content_height=520,
        )
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
                else (
                    "Actions receive the source key's normal down/repeat/up cycle in order."
                    if self._action_key == "overload"
                    else (
                        "Actions go through their press/release cycle when the key goes down."
                        if self._action_key == "overload_down"
                        else "Actions go through their press/release cycle when the key comes up."
                    )
                )
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
        return _describe_superkey_dialog_action(action, self._list_mode)

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
            from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

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

        from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

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
        self._close_warning_dialog: Adw.AlertDialog | None = None
        self._selection_warning_dialog: Adw.AlertDialog | None = None
        self._active_selection_key: tuple[str, str | None] | None = None
        self._pending_selection_key: tuple[str, str | None] | None = None
        self._suppress_selection_guard = False
        self._mode_items = [SuperkeyMode.PATTERN, SuperkeyMode.OVERLOAD]
        self.new_superkey_row: Gtk.ListBoxRow | None = None

        self._build_ui()
        self._load_superkeys()
        self._setup_shortcuts()

    def _setup_shortcuts(self) -> None:
        key_controller = Gtk.EventControllerKey()
        key_controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key_controller.connect("key-pressed", self._on_key_pressed)
        self.add_controller(key_controller)

    def _on_key_pressed(self, _controller, keyval, _keycode, _state) -> bool:
        if keyval in (Gdk.KEY_f, Gdk.KEY_F) and _state & Gdk.ModifierType.CONTROL_MASK:
            self._show_search()
            return True
        if keyval == Gdk.KEY_Escape and self.search_entry.get_visible():
            self._hide_search()
            return True
        if keyval == Gdk.KEY_Escape:
            self._request_close()
            return True
        return False

    def do_close_attempt(self) -> None:
        self._request_close()

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

        header = Gtk.CenterBox()

        self.search_button = Gtk.Button()
        self.search_button.set_icon_name("system-search-symbolic")
        self.search_button.set_tooltip_text("Search Super Keys")
        self.search_button.connect("clicked", self._on_search_clicked)
        header.set_start_widget(self.search_button)

        label = Gtk.Label(label="Super Keys")
        label.add_css_class("title-4")
        label.set_halign(Gtk.Align.CENTER)
        header.set_center_widget(label)

        header_spacer = Gtk.Box()
        header_spacer.set_size_request(34, -1)
        header.set_end_widget(header_spacer)
        box.append(header)

        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text("Search Super Keys")
        self.search_entry.set_tooltip_text("Filter Super Keys by name, description, or mode")
        self.search_entry.set_visible(False)
        self.search_entry.connect("stop-search", self._on_search_stop)
        box.append(self.search_entry)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self.list_box = Gtk.ListBox()
        self.list_box.set_vexpand(True)
        self.list_box.connect("row-selected", self._on_superkey_selected)
        install_listbox_fuzzy_filter(
            self.list_box,
            self.search_entry,
            before_filter_changed=self._before_search_filter_changed,
            after_filter_changed=self._after_search_filter_changed,
        )
        scrolled.set_child(self.list_box)

        box.append(scrolled)

        footer = Gtk.CenterBox()

        self.superkeys_docs_btn = Gtk.Button(label="?")
        self.superkeys_docs_btn.add_css_class("flat")
        self.superkeys_docs_btn.add_css_class("actions-docs-button")
        self.superkeys_docs_btn.set_tooltip_text("Open Super Keys documentation")
        self.superkeys_docs_btn.connect("clicked", self._on_superkeys_docs_clicked)
        footer.set_start_widget(self.superkeys_docs_btn)

        add_button = Gtk.Button(icon_name="list-add-symbolic")
        add_button.set_tooltip_text("Add a new Super Key")
        add_button.connect("clicked", self._on_new_clicked)
        footer.set_center_widget(add_button)

        box.append(footer)
        return box

    def _show_search(self) -> None:
        self.search_entry.set_visible(True)
        self.search_entry.grab_focus()
        self.search_entry.select_region(0, -1)

    def _hide_search(self) -> None:
        self.search_entry.set_text("")
        self.search_entry.set_visible(False)

    def _on_search_clicked(self, _button: Gtk.Button) -> None:
        self._show_search()

    def _on_search_stop(self, _entry: Gtk.SearchEntry) -> None:
        self._hide_search()

    def _before_search_filter_changed(self) -> None:
        self._suppress_selection_guard = True

    def _after_search_filter_changed(self) -> None:
        try:
            self._restore_active_selection()
        finally:
            self._suppress_selection_guard = False

    def _build_right_panel(self) -> Gtk.Widget:
        self.right_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.right_box.set_margin_top(12)
        self.right_box.set_margin_bottom(12)
        self.right_box.set_margin_start(12)
        self.right_box.set_margin_end(12)
        self.right_box.set_hexpand(True)

        self.editor_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.editor_box.set_sensitive(False)

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

        self.editor_box.append(fields_grid)
        self.editor_box.append(Gtk.Separator())

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

        self.overload_row = self._build_action_row("Main Actions", "overload", "overload")
        self.overload_row._static_description = "Held while pressed, released when you let go"
        self.overload_row.set_tooltip_text(
            "Main Actions start before On Press and stay held until after On Release, "
            "so they can provide a held modifier or context for both press/release lists."
        )
        self._refresh_child_rows(self.overload_row)
        self.actions_group.add(self.overload_row)

        self.overload_down_row = self._build_action_row("On Press", "overload_down", "overload")
        self._refresh_child_rows(self.overload_down_row)

        self.overload_up_row = self._build_action_row("On Release", "overload_up", "overload")
        self._refresh_child_rows(self.overload_up_row)

        self.overload_pulse_group = Adw.PreferencesGroup()
        self.overload_pulse_group.set_title("On Press / Release")
        self.overload_pulse_group.add(self.overload_down_row)
        self.overload_pulse_group.add(self.overload_up_row)

        self.editor_box.append(self.actions_group)
        self.editor_box.append(self.overload_pulse_group)
        self.editor_box.append(Gtk.Separator())

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

        self.editor_box.append(self.timing_group)

        editor_scrolled = Gtk.ScrolledWindow()
        editor_scrolled.set_vexpand(True)
        editor_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        editor_scrolled.set_child(self.editor_box)
        self.right_box.append(editor_scrolled)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        footer.set_hexpand(True)
        footer.set_margin_top(12)

        self.delete_btn = Gtk.Button(label="Delete")
        self.delete_btn.set_sensitive(False)
        self.delete_btn.add_css_class("destructive-action")
        self.delete_btn.connect("clicked", self._on_delete_clicked)
        footer.append(self.delete_btn)

        footer_spacer = Gtk.Box()
        footer_spacer.set_hexpand(True)
        footer.append(footer_spacer)

        self.save_btn = Gtk.Button(label="Save")
        self.save_btn.add_css_class("suggested-action")
        self.save_btn.set_sensitive(False)
        self.save_btn.connect("clicked", self._on_save_clicked)
        footer.append(self.save_btn)

        self.revert_btn = Gtk.Button(label="Revert")
        self.revert_btn.set_sensitive(False)
        self.revert_btn.connect("clicked", self._on_revert_clicked)
        footer.append(self.revert_btn)

        close_btn = Gtk.Button(label="Close")
        close_btn.connect("clicked", self._on_close_clicked)
        footer.append(close_btn)
        self.close_btn = close_btn

        self.right_box.append(footer)
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

    def _build_action_row(self, title: str, action_key: str, row_mode: str) -> Adw.ExpanderRow:
        row = Adw.ExpanderRow()
        row.set_title(title)
        row._action_key = action_key
        row._row_mode = row_mode
        row._action_items = []
        row._child_rows = []
        row._static_description = None
        row.set_subtitle("(none)")
        row.set_enable_expansion(False)

        edit_btn = Gtk.Button(label="Edit")
        edit_btn.add_css_class("flat")
        edit_btn.connect("clicked", self._on_edit_action_clicked, row)
        row.add_suffix(edit_btn)

        clear_btn = Gtk.Button(icon_name="edit-clear-symbolic")
        clear_btn.add_css_class("flat")
        clear_btn.connect("clicked", self._on_clear_action_clicked, row)
        row.add_suffix(clear_btn)
        return row

    def _build_new_superkey_row(self) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row._is_new_superkey = True
        row.add_css_class("superkey-add-row")
        row.set_tooltip_text("Add a new Super Key")
        label = Gtk.Label(label="+ Add", xalign=0)
        label.add_css_class("dim-label")
        row.set_child(label)
        return row

    def _build_saved_superkey_row(
        self,
        name: str,
        search_text: str | None = None,
    ) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row._superkey_name = name
        row._search_text = search_text or name
        label = Gtk.Label(label=name, xalign=0)
        label.set_margin_start(6)
        label.set_margin_end(6)
        label.set_margin_top(6)
        label.set_margin_bottom(6)
        row.set_child(label)
        return row

    def _current_mode(self) -> SuperkeyMode:
        return self._mode_items[self.mode_dropdown.get_selected()]

    def _update_mode_visibility(self) -> None:
        mode = self._current_mode()
        pattern_visible = mode == SuperkeyMode.PATTERN
        for row in (self.tap_row, self.double_tap_row, self.hold_row, self.tap_hold_row):
            row.set_visible(pattern_visible)
        overload_visible = not pattern_visible
        self.overload_row.set_visible(overload_visible)
        self.overload_down_row.set_visible(overload_visible)
        self.overload_up_row.set_visible(overload_visible)
        self.overload_pulse_group.set_visible(overload_visible)
        self.timing_group.set_visible(pattern_visible)

    def _load_superkeys(self) -> None:
        while row := self.list_box.get_row_at_index(0):
            self.list_box.remove(row)

        names = self.manager.list_superkeys()
        configs = self.manager.get_all_superkeys()
        for name in names:
            self.list_box.append(
                self._build_saved_superkey_row(
                    name,
                    _superkey_search_text(configs.get(name), name),
                )
            )

        self.new_superkey_row = self._build_new_superkey_row()
        self.list_box.append(self.new_superkey_row)
        self.list_box.invalidate_filter()

        if names:
            self.list_box.select_row(self.list_box.get_row_at_index(0))
        else:
            self.list_box.select_row(self.new_superkey_row)

    def _on_superkey_selected(self, _list_box, row) -> None:
        if self._suppress_selection_guard:
            return

        if row is None:
            self._current_config = None
            self.editor_box.set_sensitive(False)
            self.delete_btn.set_sensitive(False)
            self._modified = False
            self._active_selection_key = None
            self._update_buttons()
            return

        selection_key = self._selection_key_for_row(row)
        if (
            self._modified
            and selection_key is not None
            and self._active_selection_key is not None
            and selection_key != self._active_selection_key
        ):
            self._pending_selection_key = selection_key
            self._restore_active_selection()
            self._show_unsaved_selection_warning()
            return

        if getattr(row, "_is_new_superkey", False):
            self._begin_new_superkey()
            return

        name = getattr(row, "_superkey_name", None)
        if not name:
            self._current_config = None
            self.editor_box.set_sensitive(False)
            self.delete_btn.set_sensitive(False)
            self._modified = False
            self._active_selection_key = None
            self._update_buttons()
            return
        self._current_config = self.manager.get_superkey(name)
        if self._current_config:
            self._populate_editor()
            self.editor_box.set_sensitive(True)
            self.delete_btn.set_sensitive(True)
            self._active_selection_key = ("name", name)
        else:
            self.editor_box.set_sensitive(False)
            self.delete_btn.set_sensitive(False)
            self._active_selection_key = None

        self._modified = False
        self._update_buttons()

    def _selection_key_for_row(
        self,
        row: Gtk.ListBoxRow | None,
    ) -> tuple[str, str | None] | None:
        if row is None:
            return None
        if getattr(row, "_is_new_superkey", False):
            return ("new", None)
        name = getattr(row, "_superkey_name", None)
        if isinstance(name, str):
            return ("name", name)
        return None

    def _row_for_selection_key(
        self,
        key: tuple[str, str | None] | None,
    ) -> Gtk.ListBoxRow | None:
        if key is None:
            return None
        kind, name = key
        if kind == "new":
            return self.new_superkey_row
        idx = 0
        while True:
            row = self.list_box.get_row_at_index(idx)
            if row is None:
                return None
            if getattr(row, "_superkey_name", None) == name:
                return row
            idx += 1

    def _restore_active_selection(self) -> None:
        row = self._row_for_selection_key(self._active_selection_key)
        if row is None:
            return
        self._suppress_selection_guard = True
        try:
            self.list_box.select_row(row)
        finally:
            self._suppress_selection_guard = False

    def _select_selection_key(self, key: tuple[str, str | None] | None) -> None:
        row = self._row_for_selection_key(key)
        if row is not None:
            self.list_box.select_row(row)

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
        self._populate_action_row(
            self.overload_down_row,
            list(self._current_config.overload_down_actions),
        )
        self._populate_action_row(
            self.overload_up_row,
            list(self._current_config.overload_up_actions),
        )

        self.tap_timeout_spin.set_value(self._current_config.tap_timeout_ms)
        self.double_tap_window_spin.set_value(self._current_config.double_tap_window_ms)
        self.hold_threshold_spin.set_value(self._current_config.hold_threshold_ms)
        self._update_mode_visibility()

    def _populate_action_row(
        self,
        row: Adw.ExpanderRow,
        actions: list[SuperkeyAction] | list[MappingAction],
    ) -> None:
        row._action_items = list(actions)
        self._refresh_child_rows(row)

    def _describe_single_action(self, action: object, row_mode: str) -> str:
        return _describe_superkey_dialog_action(action, row_mode)

    def _refresh_child_rows(self, row: Adw.ExpanderRow) -> None:
        for child in row._child_rows:
            row.remove(child)
        row._child_rows = []

        actions = list(row._action_items)
        subtitle_parts: list[str] = []
        if row._static_description:
            subtitle_parts.append(row._static_description)
        if actions:
            noun = "action" if len(actions) == 1 else "actions"
            subtitle_parts.append(f"{len(actions)} {noun}")
        else:
            subtitle_parts.append("(none)")
        row.set_subtitle("\n".join(subtitle_parts))

        if not actions:
            row.set_enable_expansion(False)
            row.set_expanded(False)
            return

        row.set_enable_expansion(True)
        row.set_expanded(True)
        for index, action in enumerate(actions, start=1):
            child = Adw.ActionRow()
            child.set_use_markup(False)
            child.set_title_lines(0)
            description = self._describe_single_action(action, row._row_mode)
            child.set_title(f"{index}. {description}")
            row.add_row(child)
            row._child_rows.append(child)

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
        self.set_can_close(not self._modified)

    def _begin_new_superkey(self) -> None:
        self._current_config = SuperkeyConfig(name="New Super Key")
        self._populate_editor()
        self.editor_box.set_sensitive(True)
        self.delete_btn.set_sensitive(False)
        self._active_selection_key = ("new", None)
        self._modified = True
        self._update_buttons()
        self.name_entry.grab_focus()

    def _on_new_clicked(self, _button) -> None:
        if (
            self.new_superkey_row is not None
            and self.list_box.get_selected_row() is not self.new_superkey_row
        ):
            self.list_box.select_row(self.new_superkey_row)
        else:
            self._begin_new_superkey()

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

    def _save_current_superkey(self) -> bool:
        name = self.name_entry.get_text().strip()
        if not name:
            return False

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
            overload_down_actions=(
                list(self.overload_down_row._action_items)
                if mode == SuperkeyMode.OVERLOAD
                else []
            ),
            overload_up_actions=(
                list(self.overload_up_row._action_items)
                if mode == SuperkeyMode.OVERLOAD
                else []
            ),
            tap_timeout_ms=self.tap_timeout_spin.get_value_as_int(),
            double_tap_window_ms=self.double_tap_window_spin.get_value_as_int(),
            hold_threshold_ms=self.hold_threshold_spin.get_value_as_int(),
        )

        try:
            if (
                old_name
                and old_name != name
                and self.manager.get_superkey(old_name) is not None
                and not self.manager.rename_superkey(old_name, name)
            ):
                self._show_save_error(f"Super Key '{name}' already exists")
                return False
            self.manager.save_superkey(config)
        except ValueError as exc:
            self._show_save_error(str(exc))
            return False
        self._current_config = config
        self._modified = False
        self._update_buttons()
        self._load_superkeys()

        idx = 0
        while True:
            row = self.list_box.get_row_at_index(idx)
            if row is None:
                break
            if getattr(row, "_superkey_name", None) == name:
                self.list_box.select_row(row)
                break
            idx += 1

        self.emit("superkey-saved", name)
        return True

    def _show_save_error(self, message: str) -> None:
        dialog = Adw.AlertDialog()
        dialog.set_heading("Unable To Save Super Key")
        dialog.set_body(message)
        dialog.add_response("ok", "OK")
        dialog.set_default_response("ok")
        dialog.set_close_response("ok")
        dialog.present(self)

    def _on_save_clicked(self, _button) -> None:
        self._save_current_superkey()

    def _on_edit_action_clicked(self, _button, row: Adw.ExpanderRow) -> None:
        title = (
            f"Edit {row.get_title()} Actions"
            if row._row_mode == "pattern"
            else f"Edit {row.get_title()}"
        )
        dialog = ActionListDialog(
            self._parent,
            title,
            row._row_mode,
            row._action_items,
            action_key=row._action_key,
        )
        dialog.connect("actions-selected", self._on_actions_selected, row)
        dialog.present(self._parent)

    def _on_actions_selected(self, _dialog, actions: list[object], row: Adw.ExpanderRow) -> None:
        row._action_items = list(actions)
        self._refresh_child_rows(row)
        self._on_modified()

    def _on_clear_action_clicked(self, _button, row: Adw.ExpanderRow) -> None:
        row._action_items = []
        self._refresh_child_rows(row)
        self._on_modified()

    def _on_close_clicked(self, _button: Gtk.Button) -> None:
        self._request_close()

    def _request_close(self) -> None:
        if not self._modified:
            self.force_close()
            return
        self._show_unsaved_close_warning()

    def _show_unsaved_close_warning(self) -> None:
        if self._close_warning_dialog is not None:
            return

        dialog = Adw.AlertDialog()
        dialog.set_heading("Unsaved Super Key Changes")
        dialog.set_body("Save your changes before closing, or discard them?")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("discard", "Discard")
        dialog.add_response("save", "Save")
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.connect("response", self._on_unsaved_close_response)
        self._close_warning_dialog = dialog
        dialog.present(self)

    def _on_unsaved_close_response(self, _dialog: Adw.AlertDialog, response: str) -> None:
        self._close_warning_dialog = None
        if response == "discard":
            self._modified = False
            self._update_buttons()
            self.force_close()
            return
        if response == "save" and self._save_current_superkey():
            self.force_close()

    def _show_unsaved_selection_warning(self) -> None:
        if self._selection_warning_dialog is not None:
            return

        dialog = Adw.AlertDialog()
        dialog.set_heading("Unsaved Super Key Changes")
        dialog.set_body("Save your changes before switching, or discard them?")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("discard", "Discard")
        dialog.add_response("save", "Save")
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.connect("response", self._on_unsaved_selection_response)
        self._selection_warning_dialog = dialog
        dialog.present(self)

    def _on_unsaved_selection_response(self, _dialog: Adw.AlertDialog, response: str) -> None:
        pending_key = self._pending_selection_key
        self._selection_warning_dialog = None
        self._pending_selection_key = None
        if response == "discard":
            self._modified = False
            self._update_buttons()
            self._select_selection_key(pending_key)
            return
        if response == "save" and self._save_current_superkey():
            self._select_selection_key(pending_key)

    def _on_superkeys_docs_clicked(self, _button: Gtk.Button) -> None:
        url = _superkeys_docs_url()
        try:
            launcher = Gtk.UriLauncher.new(url)
            launcher.launch(None, None, None)
        except Exception as exc:
            log.warning("Could not open Super Keys documentation %s: %s", url, exc)
