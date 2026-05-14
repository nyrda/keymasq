import logging
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GObject, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.models import (
    AnalogActionThreshold,
    AnalogControlConfig,
    AnalogMouseMotionConfig,
    MappingAction,
)
from keymasq.gui.widgets.action_labels import describe_mapping_action_verbose
from keymasq.gui.widgets.superkey_dialog import ActionListDialog
from keymasq.session.analog_controls import (
    AnalogControlManager,
    analog_control_arrow_template,
    analog_control_mouse_wheel_template,
    analog_control_wasd_template,
)
from keymasq.session.profiles import ProfileManager

log = logging.getLogger("keymasq.gui.widgets.analog_control_dialog")

_MODE_ITEMS = ("mouse", "digital", "both")
_MODE_LABELS = ("Mouse Movement", "Digital Actions", "Mouse + Digital")
_AXIS_ITEMS = ("x", "y")


class AnalogControlDialog(Adw.Dialog):
    __gsignals__ = {
        "analog-control-saved": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "analog-control-deleted": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, parent: Gtk.Window, profile_manager: ProfileManager | None = None):
        super().__init__(title="Manage Analog Controls", content_width=920, content_height=640)
        self._parent = parent
        self.profile_manager = profile_manager
        self.manager = AnalogControlManager()
        self._current_config: AnalogControlConfig | None = None
        self._current_name: str | None = None
        self._thresholds: list[AnalogActionThreshold] = []
        self._modified = False
        self.new_control_row: Gtk.ListBoxRow | None = None

        self._build_ui()
        self._load_controls()

    def _build_ui(self) -> None:
        main = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        main.append(self._build_left_panel())
        main.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        main.append(self._build_right_panel())
        self.set_child(main)

    def _build_left_panel(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_size_request(220, -1)

        label = Gtk.Label(label="Analog Controls")
        label.add_css_class("title-4")
        label.set_halign(Gtk.Align.START)
        box.append(label)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self.list_box = Gtk.ListBox()
        self.list_box.set_vexpand(True)
        self.list_box.connect("row-selected", self._on_control_selected)
        scrolled.set_child(self.list_box)
        box.append(scrolled)
        return box

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

        self._attach_labeled(fields_grid, "Name:", 0, self._build_name_entry())
        self._attach_labeled(fields_grid, "Description:", 1, self._build_description_entry())

        self.mode_dropdown = Gtk.DropDown.new_from_strings(list(_MODE_LABELS))
        self.mode_dropdown.connect("notify::selected", self._on_mode_changed)
        self._attach_labeled(fields_grid, "Mode:", 2, self.mode_dropdown)

        input_type = Gtk.Label(label="Stick")
        input_type.set_xalign(0)
        self._attach_labeled(fields_grid, "Input Type:", 3, input_type)

        self.editor_box.append(fields_grid)
        self.editor_box.append(Gtk.Separator())

        self.mouse_group = self._build_mouse_group()
        self.digital_group = self._build_digital_group()
        self.template_group = self._build_template_group()
        self.editor_box.append(self.mouse_group)
        self.editor_box.append(self.digital_group)
        self.editor_box.append(self.template_group)

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

        self.right_box.append(footer)
        self._update_mode_visibility()
        return self.right_box

    def _build_name_entry(self) -> Gtk.Entry:
        self.name_entry = Gtk.Entry()
        self.name_entry.set_hexpand(True)
        self.name_entry.connect("changed", self._on_modified)
        return self.name_entry

    def _build_description_entry(self) -> Gtk.Entry:
        self.description_entry = Gtk.Entry()
        self.description_entry.set_hexpand(True)
        self.description_entry.connect("changed", self._on_modified)
        return self.description_entry

    def _attach_labeled(
        self,
        grid: Gtk.Grid,
        label_text: str,
        row: int,
        widget: Gtk.Widget,
    ) -> None:
        label = Gtk.Label(label=label_text)
        label.set_xalign(0)
        label.set_size_request(104, -1)
        grid.attach(label, 0, row, 1, 1)
        grid.attach(widget, 1, row, 1, 1)

    def _build_mouse_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title="Mouse Movement")

        self.speed_row = self._spin_row("Speed", 900, 0, 5000, 25, 0)
        group.add(self.speed_row)
        self.deadzone_row = self._spin_row("Deadzone", 0.15, 0, 0.95, 0.01, 2)
        group.add(self.deadzone_row)

        self.curve_row = Adw.ComboRow(title="Curve")
        self.curve_model = Gtk.StringList.new(["Soft", "Linear", "Fast"])
        self.curve_row.set_model(self.curve_model)
        self.curve_row.connect("notify::selected", self._on_modified)
        group.add(self.curve_row)

        self.invert_x_row = Adw.SwitchRow(title="Invert X")
        self.invert_x_row.connect("notify::active", self._on_modified)
        self.invert_y_row = Adw.SwitchRow(title="Invert Y")
        self.invert_y_row.connect("notify::active", self._on_modified)
        group.add(self.invert_x_row)
        group.add(self.invert_y_row)
        return group

    def _build_digital_group(self) -> Adw.PreferencesGroup:
        self.digital_group = Adw.PreferencesGroup(title="Digital Action Ranges")
        self.add_range_row = Adw.ActionRow(
            title="+ Add Range",
            subtitle="Create a new editable activation and release range",
        )
        self.add_range_row.set_activatable(True)
        self.add_range_row.connect("activated", self._on_add_range_clicked)
        self.digital_group.add(self.add_range_row)
        return self.digital_group

    def _build_template_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Range Templates",
            description=(
                "Templates append editable digital ranges. "
                "They do not create special runtime modes."
            ),
        )
        for title, subtitle, callback in (
            (
                "Apply WASD Template",
                "Adds four keyboard ranges for left-stick movement",
                self._on_template_wasd,
            ),
            ("Apply Arrow Keys Template", "Adds four arrow-key ranges", self._on_template_arrows),
            (
                "Apply Mouse Wheel Template",
                "Adds two rapidfire wheel ranges",
                self._on_template_mouse_wheel,
            ),
        ):
            row = Adw.ActionRow(title=title, subtitle=subtitle)
            row.set_activatable(True)
            row.connect("activated", callback)
            group.add(row)
        return group

    def _spin_row(
        self,
        title: str,
        value: float,
        lower: float,
        upper: float,
        step: float,
        digits: int,
    ) -> Adw.SpinRow:
        row = Adw.SpinRow(
            title=title,
            adjustment=Gtk.Adjustment(
                value=value,
                lower=lower,
                upper=upper,
                step_increment=step,
            ),
            digits=digits,
        )
        row.connect("notify::value", self._on_modified)
        return row

    def _build_new_control_row(self) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row._is_new_analog_control = True
        row.add_css_class("superkey-add-row")
        row.set_tooltip_text("Add a new Analog Control")
        label = Gtk.Label(label="+ Add", xalign=0)
        label.add_css_class("dim-label")
        row.set_child(label)
        return row

    def _build_saved_control_row(self, name: str) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row._analog_control_name = name
        label = Gtk.Label(label=name, xalign=0)
        label.set_margin_start(6)
        label.set_margin_end(6)
        label.set_margin_top(6)
        label.set_margin_bottom(6)
        row.set_child(label)
        return row

    def _current_mode(self) -> str:
        selected = int(self.mode_dropdown.get_selected())
        if selected < 0 or selected >= len(_MODE_ITEMS):
            return "mouse"
        return _MODE_ITEMS[selected]

    def _update_mode_visibility(self) -> None:
        if not hasattr(self, "mouse_group"):
            return
        mode = self._current_mode()
        digital_visible = mode in {"digital", "both"}
        self.mouse_group.set_visible(mode in {"mouse", "both"})
        self.digital_group.set_visible(digital_visible)
        self.template_group.set_visible(digital_visible)

    def _load_controls(self) -> None:
        while row := self.list_box.get_row_at_index(0):
            self.list_box.remove(row)

        names = self.manager.list_analog_controls()
        for name in names:
            self.list_box.append(self._build_saved_control_row(name))

        self.new_control_row = self._build_new_control_row()
        self.list_box.append(self.new_control_row)

        if names:
            self.list_box.select_row(self.list_box.get_row_at_index(0))
        else:
            self.list_box.select_row(self.new_control_row)

    def _on_control_selected(self, _list_box, row) -> None:
        if row is None:
            self._current_config = None
            self._current_name = None
            self.editor_box.set_sensitive(False)
            self.delete_btn.set_sensitive(False)
            self._modified = False
            self._update_buttons()
            return

        if getattr(row, "_is_new_analog_control", False):
            self._begin_new_control()
            return

        name = getattr(row, "_analog_control_name", None)
        if not isinstance(name, str):
            return
        config = self.manager.get_analog_control(name)
        if config is None:
            return
        self._load_config(config)
        self.delete_btn.set_sensitive(True)
        self._modified = False
        self._update_buttons()

    def _load_config(self, config: AnalogControlConfig) -> None:
        self._current_config = config
        self._current_name = config.name
        self._thresholds = [self._copy_threshold(threshold) for threshold in config.thresholds]
        self.editor_box.set_sensitive(True)
        self.name_entry.set_text(config.name)
        self.description_entry.set_text(config.description or "")
        self.mode_dropdown.set_selected(self._mode_index(config))
        self.speed_row.set_value(config.mouse_motion.speed)
        self.deadzone_row.set_value(config.mouse_motion.deadzone)
        self.curve_row.set_selected(
            {"soft": 0, "linear": 1, "fast": 2}.get(config.mouse_motion.curve, 0)
        )
        self.invert_x_row.set_active(config.mouse_motion.invert_x)
        self.invert_y_row.set_active(config.mouse_motion.invert_y)
        self._refresh_thresholds()
        self._update_mode_visibility()

    def _mode_index(self, config: AnalogControlConfig) -> int:
        has_mouse = bool(config.mouse_motion.enabled)
        has_digital = bool(config.thresholds)
        if has_mouse and has_digital:
            return _MODE_ITEMS.index("both")
        if has_digital:
            return _MODE_ITEMS.index("digital")
        return _MODE_ITEMS.index("mouse")

    def _refresh_thresholds(self) -> None:
        for row in getattr(self, "_threshold_rows", []):
            self.digital_group.remove(row)
        self._threshold_rows = []

        for index, threshold in enumerate(self._thresholds):
            row = self._build_threshold_row(index, threshold)
            self.digital_group.add(row)
            self._threshold_rows.append(row)

    def _build_threshold_row(
        self,
        index: int,
        threshold: AnalogActionThreshold,
    ) -> Adw.ExpanderRow:
        row = Adw.ExpanderRow()
        row.set_title(f"Range {index + 1}: {threshold.axis.upper()}")
        row.set_subtitle(self._threshold_subtitle(threshold))
        row.set_enable_expansion(True)

        edit_btn = Gtk.Button(label="Edit Actions")
        edit_btn.add_css_class("flat")
        edit_btn.connect("clicked", self._on_edit_threshold_actions_clicked, index)
        row.add_suffix(edit_btn)

        remove_btn = Gtk.Button(icon_name="edit-delete-symbolic")
        remove_btn.add_css_class("flat")
        remove_btn.add_css_class("destructive-action")
        remove_btn.connect("clicked", self._on_remove_threshold_clicked, index)
        row.add_suffix(remove_btn)

        axis_row = Adw.ActionRow(title="Axis")
        axis_buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        axis_buttons.add_css_class("linked")
        axis_group: Gtk.ToggleButton | None = None
        for axis in _AXIS_ITEMS:
            button = Gtk.ToggleButton(label=axis.upper())
            if axis_group is None:
                axis_group = button
            else:
                button.set_group(axis_group)
            button.set_active(threshold.axis == axis)
            button.connect("toggled", self._on_threshold_axis_toggled, index, axis, row)
            axis_buttons.append(button)
        axis_row.add_suffix(axis_buttons)
        row.add_row(axis_row)

        for title, attr in (
            ("Activation Min", "trigger_min"),
            ("Activation Max", "trigger_max"),
            ("Release Min", "release_min"),
            ("Release Max", "release_max"),
        ):
            row.add_row(
                self._threshold_spin_row(
                    title,
                    float(getattr(threshold, attr)),
                    self._make_threshold_value_callback(index, attr),
                )
            )

        if threshold.actions:
            for action_index, action in enumerate(threshold.actions, start=1):
                child = Adw.ActionRow()
                child.set_use_markup(False)
                child.set_title(f"{action_index}. {describe_mapping_action_verbose(action)}")
                row.add_row(child)
        return row

    def _threshold_spin_row(
        self,
        title: str,
        value: float,
        callback: Callable[[Adw.SpinRow, object], None],
    ) -> Adw.SpinRow:
        spin = Adw.SpinRow(
            title=title,
            adjustment=Gtk.Adjustment(
                value=value,
                lower=-1.0,
                upper=1.0,
                step_increment=0.05,
            ),
            digits=2,
        )
        spin.connect("notify::value", callback)
        return spin

    def _make_threshold_value_callback(
        self,
        index: int,
        attr: str,
    ) -> Callable[[Adw.SpinRow, object], None]:
        def _callback(spin: Adw.SpinRow, _param: object) -> None:
            self._on_threshold_value_changed(spin, index, attr)

        return _callback

    def _threshold_subtitle(self, threshold: AnalogActionThreshold) -> str:
        count = len(threshold.actions)
        noun = "action" if count == 1 else "actions"
        return (
            f"activate {threshold.trigger_min:.2f}..{threshold.trigger_max:.2f} · "
            f"release {threshold.release_min:.2f}..{threshold.release_max:.2f} · "
            f"{count} {noun}"
        )

    def _begin_new_control(self) -> None:
        self._load_config(AnalogControlConfig(name="New Analog Control"))
        self.delete_btn.set_sensitive(False)
        self._modified = True
        self._update_buttons()
        self.name_entry.grab_focus()

    def _on_add_clicked(self, _button=None) -> None:
        if (
            self.new_control_row is not None
            and self.list_box.get_selected_row() is not self.new_control_row
        ):
            self.list_box.select_row(self.new_control_row)
        else:
            self._begin_new_control()

    def _on_add_range_clicked(self, *_args) -> None:
        self._thresholds.append(
            AnalogActionThreshold(
                axis="x",
                trigger_min=0.65,
                trigger_max=1.0,
                release_min=0.55,
                release_max=1.0,
                actions=[],
            )
        )
        if self._current_mode() == "mouse":
            self.mode_dropdown.set_selected(_MODE_ITEMS.index("both"))
        self._refresh_thresholds()
        self._on_modified()

    def _on_remove_threshold_clicked(self, _button, index: int) -> None:
        if 0 <= index < len(self._thresholds):
            self._thresholds.pop(index)
        self._refresh_thresholds()
        self._on_modified()

    def _on_edit_threshold_actions_clicked(self, _button, index: int) -> None:
        if not 0 <= index < len(self._thresholds):
            return
        dialog = ActionListDialog(
            self._parent,
            f"Edit Range {index + 1} Actions",
            "overload",
            list(self._thresholds[index].actions),
            action_key="analog_threshold",
        )
        dialog.connect("actions-selected", self._on_threshold_actions_selected, index)
        dialog.present(self._parent)

    def _on_threshold_actions_selected(
        self,
        _dialog,
        actions: list[object],
        index: int,
    ) -> None:
        if not 0 <= index < len(self._thresholds):
            return
        self._thresholds[index].actions = [
            action for action in actions if isinstance(action, MappingAction)
        ]
        self._refresh_thresholds()
        self._on_modified()

    def _on_threshold_axis_toggled(
        self,
        button: Gtk.ToggleButton,
        index: int,
        axis: str,
        row: Adw.ExpanderRow,
    ) -> None:
        if not button.get_active():
            return
        if not 0 <= index < len(self._thresholds):
            return
        self._thresholds[index].axis = axis
        row.set_title(f"Range {index + 1}: {axis.upper()}")
        row.set_subtitle(self._threshold_subtitle(self._thresholds[index]))
        self._on_modified()

    def _on_threshold_value_changed(self, spin: Adw.SpinRow, index: int, attr: str) -> None:
        if not 0 <= index < len(self._thresholds):
            return
        setattr(self._thresholds[index], attr, spin.get_value())
        self._on_modified()

    def _on_template_wasd(self, *_args) -> None:
        self._apply_template(analog_control_wasd_template())

    def _on_template_arrows(self, *_args) -> None:
        self._apply_template(analog_control_arrow_template())

    def _on_template_mouse_wheel(self, *_args) -> None:
        self._apply_template(analog_control_mouse_wheel_template())

    def _apply_template(self, thresholds: list[AnalogActionThreshold]) -> None:
        self._thresholds.extend(self._copy_threshold(threshold) for threshold in thresholds)
        if self._current_mode() == "mouse":
            self.mode_dropdown.set_selected(_MODE_ITEMS.index("both"))
        self._refresh_thresholds()
        self._on_modified()

    def _on_mode_changed(self, _dropdown, _param) -> None:
        self._update_mode_visibility()
        self._on_modified()

    def _on_modified(self, *_args) -> None:
        if not hasattr(self, "save_btn"):
            return
        self._modified = True
        self._update_buttons()

    def _update_buttons(self) -> None:
        if not hasattr(self, "save_btn"):
            return
        self.save_btn.set_sensitive(self._modified)
        self.revert_btn.set_sensitive(self._modified)

    def _on_revert_clicked(self, _button) -> None:
        if self._current_config is not None:
            self._load_config(self._current_config)
        self._modified = False
        self._update_buttons()

    def _on_save_clicked(self, _button) -> None:
        self._save_current_control()

    def _on_close_clicked(self, _button: Gtk.Button) -> None:
        self.close()

    def _save_current_control(self) -> bool:
        name = self.name_entry.get_text().strip()
        if not name:
            return False

        mode = self._current_mode()
        old_name = self._current_name
        config = AnalogControlConfig(
            name=name,
            description=self.description_entry.get_text().strip() or None,
            input_type="stick",
            mouse_motion=AnalogMouseMotionConfig(
                enabled=mode in {"mouse", "both"},
                speed=self.speed_row.get_value(),
                deadzone=self.deadzone_row.get_value(),
                curve=["soft", "linear", "fast"][int(self.curve_row.get_selected())],
                invert_x=self.invert_x_row.get_active(),
                invert_y=self.invert_y_row.get_active(),
            ),
            thresholds=list(self._thresholds) if mode in {"digital", "both"} else [],
        )
        try:
            self.manager.save_analog_control(config, replacing_name=old_name)
        except ValueError as exc:
            self._show_save_error(str(exc))
            return False

        self._current_name = name
        self._current_config = config
        self._modified = False
        self._update_buttons()
        self._load_controls()
        self._select_control(name)
        self.emit("analog-control-saved", name)
        return True

    def _show_save_error(self, message: str) -> None:
        dialog = Adw.AlertDialog()
        dialog.set_heading("Unable To Save Analog Control")
        dialog.set_body(message)
        dialog.add_response("ok", "OK")
        dialog.set_default_response("ok")
        dialog.set_close_response("ok")
        dialog.present(self)

    def _select_control(self, name: str) -> None:
        idx = 0
        while True:
            row = self.list_box.get_row_at_index(idx)
            if row is None:
                return
            if getattr(row, "_analog_control_name", None) == name:
                self.list_box.select_row(row)
                return
            idx += 1

    def _on_delete_clicked(self, _button) -> None:
        if not self._current_name:
            return
        name = self._current_name
        if self.profile_manager is not None:
            self.profile_manager.replace_analog_control_with_suppress(name)
        if self.manager.delete_analog_control(name):
            self.emit("analog-control-deleted", name)
        self._current_name = None
        self._current_config = None
        self._thresholds = []
        self.editor_box.set_sensitive(False)
        self.delete_btn.set_sensitive(False)
        self._modified = False
        self._update_buttons()
        self._load_controls()

    def _copy_threshold(self, threshold: AnalogActionThreshold) -> AnalogActionThreshold:
        return AnalogActionThreshold(
            axis=threshold.axis,
            trigger_min=threshold.trigger_min,
            trigger_max=threshold.trigger_max,
            release_min=threshold.release_min,
            release_max=threshold.release_max,
            actions=list(threshold.actions),
        )
