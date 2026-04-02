import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, GObject, Gtk

from keyforge.common.models import ActionType, SuperkeyAction, SuperkeyConfig
from keyforge.session.profiles import ProfileManager
from keyforge.session.superkeys import SuperkeyManager


class SuperkeyDialog(Adw.Dialog):
    __gsignals__ = {
        "superkey-saved": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "superkey-deleted": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, parent: Gtk.Window, profile_manager: ProfileManager | None = None):
        super().__init__(title="Manage Super Keys", content_width=800, content_height=600)
        self._parent = parent
        self.manager = SuperkeyManager()
        self.profile_manager = profile_manager
        self._current_config: SuperkeyConfig | None = None
        self._modified = False

        self._build_ui()
        self._load_superkeys()
        self._setup_shortcuts()

    def _setup_shortcuts(self) -> None:
        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", self._on_key_pressed)
        self.add_controller(key_controller)

    def _on_key_pressed(self, controller, keyval, keycode, state) -> bool:
        if keyval == Gdk.KEY_Escape:
            self.close()
            return True
        return False

    def _build_ui(self):
        main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)

        left_panel = self._build_left_panel()
        main_box.append(left_panel)

        separator = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        main_box.append(separator)

        right_panel = self._build_right_panel()
        main_box.append(right_panel)

        self.set_child(main_box)

    def _build_left_panel(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_size_request(200, -1)

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

        self.right_box.append(fields_grid)

        self.right_box.append(Gtk.Separator())

        actions_group = Adw.PreferencesGroup()
        actions_group.set_title("Actions")

        self.tap_row = self._build_action_row("Tap", "tap")
        actions_group.add(self.tap_row)

        self.double_tap_row = self._build_action_row("Double Tap", "double_tap")
        actions_group.add(self.double_tap_row)

        self.hold_row = self._build_action_row("Hold", "hold")
        actions_group.add(self.hold_row)

        self.tap_hold_row = self._build_action_row("Tap + Hold", "tap_hold")
        actions_group.add(self.tap_hold_row)

        self.right_box.append(actions_group)

        self.right_box.append(Gtk.Separator())

        timing_group = Adw.PreferencesGroup()
        timing_group.set_title("Timing")

        self.tap_timeout_row, self.tap_timeout_spin = self._build_timing_row(
            "Tap Timeout",
            "Maximum time for a tap (ms)",
            200,
            50,
            1000,
        )
        timing_group.add(self.tap_timeout_row)

        self.double_tap_window_row, self.double_tap_window_spin = self._build_timing_row(
            "Double Tap Window",
            "Maximum time between taps (ms)",
            300,
            50,
            1000,
        )
        timing_group.add(self.double_tap_window_row)

        self.hold_threshold_row, self.hold_threshold_spin = self._build_timing_row(
            "Hold Threshold",
            "Time to consider a hold (ms)",
            300,
            50,
            2000,
        )
        timing_group.add(self.hold_threshold_row)

        self.right_box.append(timing_group)

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
        close_btn.connect("clicked", lambda _: self.close())
        bottom_row.append(close_btn)

        btn_box.append(bottom_row)

        self.right_box.append(btn_box)

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

    def _build_action_row(self, title: str, action_type: str) -> Adw.ActionRow:
        row = Adw.ActionRow()
        row.set_title(title)

        label = Gtk.Label(label="(none)")
        row.add_suffix(label)
        row._action_label = label
        row._action_type = action_type
        row._action_data = None

        edit_btn = Gtk.Button(label="Edit")
        edit_btn.add_css_class("flat")
        edit_btn.connect("clicked", self._on_edit_action_clicked, row)
        row.add_suffix(edit_btn)

        clear_btn = Gtk.Button(icon_name="edit-clear-symbolic")
        clear_btn.add_css_class("flat")
        clear_btn.connect("clicked", self._on_clear_action_clicked, row)
        row.add_suffix(clear_btn)

        return row

    def _load_superkeys(self):
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

    def _on_superkey_selected(self, list_box, row):
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

    def _populate_editor(self):
        if not self._current_config:
            return

        self.name_entry.set_text(self._current_config.name)
        self.desc_entry.set_text(self._current_config.description or "")

        self._populate_action_row(self.tap_row, self._current_config.tap_action)
        self._populate_action_row(self.double_tap_row, self._current_config.double_tap_action)
        self._populate_action_row(self.hold_row, self._current_config.hold_action)
        self._populate_action_row(self.tap_hold_row, self._current_config.tap_hold_action)

        self.tap_timeout_spin.set_value(self._current_config.tap_timeout_ms)
        self.double_tap_window_spin.set_value(self._current_config.double_tap_window_ms)
        self.hold_threshold_spin.set_value(self._current_config.hold_threshold_ms)

    def _populate_action_row(self, row: Adw.ActionRow, action: SuperkeyAction | None):
        if action is None:
            row._action_label.set_label("(none)")
            row._action_data = None
        else:
            desc = self._describe_action(action)
            row._action_label.set_label(desc)
            row._action_data = action

    def _describe_action(self, action: SuperkeyAction) -> str:
        type_str = action.action_type.value
        if action.action_type == ActionType.EXEC:
            return (
                f"exec: {action.cmd[:30]}..."
                if action.cmd and len(action.cmd) > 30
                else f"exec: {action.cmd or ''}"
            )
        if action.action_type == ActionType.MACRO:
            return f"macro: {action.macro_name or ''}"
        return f"{type_str}: {action.target or ''}"

    def _on_modified(self, *args):
        self._modified = True
        self._update_buttons()

    def _update_buttons(self):
        self.revert_btn.set_sensitive(self._modified)
        self.save_btn.set_sensitive(self._modified)

    def _on_new_clicked(self, button):
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

    def _on_delete_clicked(self, button):
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

    def _on_delete_confirm(self, dialog, response, name):
        if response == "delete":
            self._do_delete(name)

    def _do_delete(self, name: str):
        if self.profile_manager:
            self.profile_manager.replace_superkey_with_suppress(name)

        self.manager.delete_superkey(name)
        self._load_superkeys()
        self.emit("superkey-deleted", name)

    def _on_revert_clicked(self, button):
        if self._current_config:
            self._populate_editor()
        self._modified = False
        self._update_buttons()

    def _on_save_clicked(self, button):
        name = self.name_entry.get_text().strip()
        if not name:
            return

        old_name = self._current_config.name if self._current_config else None

        config = SuperkeyConfig(
            name=name,
            description=self.desc_entry.get_text().strip() or None,
            tap_action=self.tap_row._action_data,
            double_tap_action=self.double_tap_row._action_data,
            hold_action=self.hold_row._action_data,
            tap_hold_action=self.tap_hold_row._action_data,
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

    def _on_edit_action_clicked(self, button, row):
        from keyforge.gui.widgets.key_selector_dialog import SuperkeyActionDialog

        dialog = SuperkeyActionDialog(
            self._parent,
            row._action_type,
            row._action_data,
        )
        dialog.connect("action-selected", self._on_action_selected, row)
        dialog.present()

    def _on_action_selected(self, dialog, action: SuperkeyAction | None, row):
        row._action_data = action
        if action:
            row._action_label.set_label(self._describe_action(action))
        else:
            row._action_label.set_label("(none)")
        self._on_modified()

    def _on_clear_action_clicked(self, button, row):
        row._action_data = None
        row._action_label.set_label("(none)")
        self._on_modified()
