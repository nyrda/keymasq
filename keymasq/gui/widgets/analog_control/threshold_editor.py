from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import (  # pyright: ignore[reportAttributeAccessIssue]
    Adw,  # pyright: ignore[reportAttributeAccessIssue]
    GObject,  # pyright: ignore[reportAttributeAccessIssue]
    Gtk,  # pyright: ignore[reportAttributeAccessIssue]
)

from keymasq.common.models import AnalogActionThreshold, MappingAction
from keymasq.gui.widgets.action_labels import describe_mapping_action_verbose
from keymasq.gui.widgets.analog_control.options import (
    _AXIS_ITEMS,
    _compute_hysteresis,
    _from_percent,
    _to_percent,
)
from keymasq.gui.widgets.threshold_range_bar import ThresholdRangeBar
from keymasq.session.analog_controls import (
    analog_control_arrow_template,
    analog_control_mouse_wheel_template,
    analog_control_wasd_template,
)


class ThresholdEditor(GObject.GObject):
    __gsignals__ = {
        "modified": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(
        self,
        container: Adw.PreferencesGroup,
        *,
        get_thresholds: Callable[[], list[AnalogActionThreshold]],
        set_thresholds: Callable[[list[AnalogActionThreshold]], None],
        get_domain: Callable[[], tuple[float, float]],
        is_axis_control: Callable[[], bool],
        get_current_mode: Callable[[], str],
        ensure_digital_mode: Callable[[], None],
        on_modified: Callable[..., None],
        open_actions_dialog: Callable[[int], None],
    ) -> None:
        super().__init__()
        self._container = container
        self._get_thresholds = get_thresholds
        self._set_thresholds = set_thresholds
        self._get_domain = get_domain
        self._is_axis_control = is_axis_control
        self._get_current_mode = get_current_mode
        self._ensure_digital_mode = ensure_digital_mode
        self._on_modified = on_modified
        self._open_actions_dialog = open_actions_dialog
        self._syncing_threshold = False
        self.threshold_rows: list[Adw.ExpanderRow] = []

    def refresh(self, expanded_indices: set[int] | None = None) -> None:
        for row in self.threshold_rows:
            self._container.remove(row)
        self.threshold_rows.clear()

        for index, threshold in enumerate(self._get_thresholds()):
            row = self._build_threshold_row(index, threshold)
            if expanded_indices and index in expanded_indices:
                row.set_expanded(True)
            self._container.add(row)
            self.threshold_rows.append(row)

    def expanded_indices(self) -> set[int]:
        expanded: set[int] = set()
        for index, row in enumerate(self.threshold_rows):
            if row.get_expanded():
                expanded.add(index)
        return expanded

    def sync_for_input_type(self) -> None:
        minimum, maximum = self._get_domain()
        if self._is_axis_control():
            for threshold in self._get_thresholds():
                threshold.axis = "x"
                threshold.trigger_min = max(minimum, min(maximum, threshold.trigger_min))
                threshold.trigger_max = max(minimum, min(maximum, threshold.trigger_max))
                threshold.release_min = max(minimum, min(maximum, threshold.release_min))
                threshold.release_max = max(minimum, min(maximum, threshold.release_max))
                if threshold.trigger_min > threshold.trigger_max:
                    threshold.trigger_min, threshold.trigger_max = (
                        threshold.trigger_max,
                        threshold.trigger_min,
                    )
                threshold.release_min = min(threshold.release_min, threshold.trigger_min)
                threshold.release_max = max(threshold.release_max, threshold.trigger_max)

    def add_range(self) -> None:
        trigger_min = 0.50 if self._is_axis_control() else 0.65
        release_min = 0.45 if self._is_axis_control() else 0.55
        thresholds = self._get_thresholds()
        thresholds.append(
            AnalogActionThreshold(
                axis="x",
                trigger_min=trigger_min,
                trigger_max=1.0,
                release_min=release_min,
                release_max=1.0,
                actions=[],
            )
        )
        if self._get_current_mode() == "mouse":
            self._ensure_digital_mode()
        self.refresh()
        self._modified()

    def remove_threshold(self, index: int) -> None:
        thresholds = self._get_thresholds()
        if 0 <= index < len(thresholds):
            thresholds.pop(index)
        self.refresh()
        self._modified()

    def edit_threshold_actions(self, index: int) -> None:
        if not 0 <= index < len(self._get_thresholds()):
            return
        self._open_actions_dialog(index)

    def actions_selected(self, actions: list[object], index: int) -> None:
        thresholds = self._get_thresholds()
        if not 0 <= index < len(thresholds):
            return
        expanded = self.expanded_indices()
        expanded.add(index)
        thresholds[index].actions = [
            action for action in actions if isinstance(action, MappingAction)
        ]
        self.refresh(expanded)
        self._modified()

    def apply_wasd_template(self) -> None:
        self.apply_template(analog_control_wasd_template())

    def apply_arrow_template(self) -> None:
        self.apply_template(analog_control_arrow_template())

    def apply_mouse_wheel_template(self) -> None:
        self.apply_template(analog_control_mouse_wheel_template())

    def apply_template(self, thresholds: list[AnalogActionThreshold]) -> None:
        if self._is_axis_control():
            return
        current = self._get_thresholds()
        current.extend(self._copy_threshold(threshold) for threshold in thresholds)
        self._set_thresholds(current)
        if self._get_current_mode() == "mouse":
            self._ensure_digital_mode()
        self.refresh()
        self._modified()

    def _build_threshold_row(
        self,
        index: int,
        threshold: AnalogActionThreshold,
    ) -> Adw.ExpanderRow:
        is_axis = self._is_axis_control()
        row = Adw.ExpanderRow()
        title = f"Range {index + 1}"
        if not is_axis:
            title = f"{title}: {threshold.axis.upper()}"
        row.set_title(title)
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

        bar_row = Adw.ActionRow()
        range_bar = ThresholdRangeBar()
        minimum, maximum = self._get_domain()
        range_bar.set_domain(minimum, maximum)
        range_bar.set_ranges(
            threshold.trigger_min,
            threshold.trigger_max,
            threshold.release_min,
            threshold.release_max,
        )
        bar_row.set_child(range_bar)
        row._range_bar = range_bar
        row.add_row(bar_row)

        if not is_axis:
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

        trigger_min_spin = self._percent_spin_row(
            "Activation Min",
            threshold.trigger_min,
            self._on_primary_threshold_changed,
            index,
            row,
        )
        trigger_max_spin = self._percent_spin_row(
            "Activation Max",
            threshold.trigger_max,
            self._on_primary_threshold_changed,
            index,
            row,
        )
        hysteresis_spin = self._percent_spin_row(
            "Hysteresis",
            _compute_hysteresis(threshold),
            self._on_primary_threshold_changed,
            index,
            row,
            lower=0.0,
            upper=100.0,
            step=1.0,
        )
        row._spin_trigger_min = trigger_min_spin
        row._spin_trigger_max = trigger_max_spin
        row._spin_hysteresis = hysteresis_spin
        row.add_row(trigger_min_spin)
        row.add_row(trigger_max_spin)
        row.add_row(hysteresis_spin)

        advanced_row = Adw.ExpanderRow(title="Advanced")
        advanced_row.set_subtitle("Release range")
        release_min_spin = self._percent_spin_row(
            "Release Min",
            threshold.release_min,
            self._on_advanced_threshold_changed,
            index,
            row,
        )
        release_max_spin = self._percent_spin_row(
            "Release Max",
            threshold.release_max,
            self._on_advanced_threshold_changed,
            index,
            row,
        )
        row._spin_release_min = release_min_spin
        row._spin_release_max = release_max_spin
        advanced_row.add_row(release_min_spin)
        advanced_row.add_row(release_max_spin)
        row.add_row(advanced_row)

        if threshold.actions:
            for action_index, action in enumerate(threshold.actions, start=1):
                child = Adw.ActionRow()
                child.set_use_markup(False)
                child.set_title(f"{action_index}. {describe_mapping_action_verbose(action)}")
                row.add_row(child)
        return row

    def _percent_spin_row(
        self,
        title: str,
        value: float,
        callback: Callable[[Adw.SpinRow, object, int, Adw.ExpanderRow], None],
        index: int,
        row: Adw.ExpanderRow,
        *,
        lower: float = -100.0,
        upper: float = 100.0,
        step: float = 5.0,
    ) -> Adw.SpinRow:
        spin = Adw.SpinRow(
            title=f"{title} (%)",
            adjustment=Gtk.Adjustment(
                value=_to_percent(value),
                lower=lower,
                upper=upper,
                step_increment=step,
            ),
            digits=0,
        )
        spin.connect("notify::value", callback, index, row)
        return spin

    def _threshold_subtitle(self, threshold: AnalogActionThreshold) -> str:
        count = len(threshold.actions)
        noun = "action" if count == 1 else "actions"
        hysteresis = _compute_hysteresis(threshold)
        return (
            f"activate {_to_percent(threshold.trigger_min):.0f}%.."
            f"{_to_percent(threshold.trigger_max):.0f}% · "
            f"hysteresis {_to_percent(hysteresis):.0f}% · "
            f"{count} {noun}"
        )

    def _on_edit_threshold_actions_clicked(self, _button, index: int) -> None:
        self.edit_threshold_actions(index)

    def _on_remove_threshold_clicked(self, _button, index: int) -> None:
        self.remove_threshold(index)

    def _on_threshold_axis_toggled(
        self,
        button: Gtk.ToggleButton,
        index: int,
        axis: str,
        row: Adw.ExpanderRow,
    ) -> None:
        if not button.get_active():
            return
        thresholds = self._get_thresholds()
        if not 0 <= index < len(thresholds):
            return
        thresholds[index].axis = axis
        row.set_title(f"Range {index + 1}: {axis.upper()}")
        row.set_subtitle(self._threshold_subtitle(thresholds[index]))
        self._modified()

    def _on_primary_threshold_changed(
        self,
        _spin: Adw.SpinRow,
        _param: object,
        index: int,
        row: Adw.ExpanderRow,
    ) -> None:
        if self._syncing_threshold:
            return
        thresholds = self._get_thresholds()
        if not 0 <= index < len(thresholds):
            return
        threshold = thresholds[index]
        trigger_min = _from_percent(row._spin_trigger_min.get_value())
        trigger_max = _from_percent(row._spin_trigger_max.get_value())
        if trigger_min > trigger_max:
            trigger_min, trigger_max = trigger_max, trigger_min
            self._set_spin_value(row._spin_trigger_min, trigger_min)
            self._set_spin_value(row._spin_trigger_max, trigger_max)

        hysteresis = max(0.0, min(1.0, _from_percent(row._spin_hysteresis.get_value())))
        minimum, maximum = self._get_domain()
        release_min = max(minimum, trigger_min - hysteresis)
        release_max = min(maximum, trigger_max + hysteresis)
        threshold.trigger_min = trigger_min
        threshold.trigger_max = trigger_max
        threshold.release_min = release_min
        threshold.release_max = release_max

        self._syncing_threshold = True
        try:
            row._spin_release_min.set_value(_to_percent(release_min))
            row._spin_release_max.set_value(_to_percent(release_max))
        finally:
            self._syncing_threshold = False
        self._update_threshold_row_visuals(row, index)
        self._modified()

    def _on_advanced_threshold_changed(
        self,
        _spin: Adw.SpinRow,
        _param: object,
        index: int,
        row: Adw.ExpanderRow,
    ) -> None:
        if self._syncing_threshold:
            return
        thresholds = self._get_thresholds()
        if not 0 <= index < len(thresholds):
            return
        threshold = thresholds[index]
        release_min = min(
            threshold.trigger_min,
            _from_percent(row._spin_release_min.get_value()),
        )
        release_max = max(
            threshold.trigger_max,
            _from_percent(row._spin_release_max.get_value()),
        )
        threshold.release_min = release_min
        threshold.release_max = release_max
        hysteresis = _compute_hysteresis(threshold)

        self._syncing_threshold = True
        try:
            row._spin_release_min.set_value(_to_percent(release_min))
            row._spin_release_max.set_value(_to_percent(release_max))
            row._spin_hysteresis.set_value(_to_percent(hysteresis))
        finally:
            self._syncing_threshold = False
        self._update_threshold_row_visuals(row, index)
        self._modified()

    def _set_spin_value(self, spin: Adw.SpinRow, value: float) -> None:
        self._syncing_threshold = True
        try:
            spin.set_value(_to_percent(value))
        finally:
            self._syncing_threshold = False

    def _update_threshold_row_visuals(self, row: Adw.ExpanderRow, index: int) -> None:
        thresholds = self._get_thresholds()
        if not 0 <= index < len(thresholds):
            return
        threshold = thresholds[index]
        minimum, maximum = self._get_domain()
        row._range_bar.set_domain(minimum, maximum)
        row._range_bar.set_ranges(
            threshold.trigger_min,
            threshold.trigger_max,
            threshold.release_min,
            threshold.release_max,
        )
        row.set_subtitle(self._threshold_subtitle(threshold))

    def _modified(self) -> None:
        self.emit("modified")
        self._on_modified()

    def _copy_threshold(self, threshold: AnalogActionThreshold) -> AnalogActionThreshold:
        return AnalogActionThreshold(
            axis=threshold.axis,
            trigger_min=threshold.trigger_min,
            trigger_max=threshold.trigger_max,
            release_min=threshold.release_min,
            release_max=threshold.release_max,
            actions=list(threshold.actions),
        )
