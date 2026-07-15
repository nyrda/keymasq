"""Digital-threshold state transitions and GTK editor view."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.model.actions import MappingAction
from keymasq.common.model.analog import AnalogActionThreshold
from keymasq.gui.widgets.action_labels import describe_mapping_action_verbose
from keymasq.gui.widgets.analog_control.draft import clone_threshold
from keymasq.gui.widgets.analog_control.options import (
    AXIS_ITEMS,
    compute_hysteresis,
    from_percent,
    to_percent,
)
from keymasq.gui.widgets.threshold_range_bar import ThresholdRangeBar
from keymasq.session.analog_controls import (
    analog_control_arrow_template,
    analog_control_mouse_wheel_template,
    analog_control_wasd_template,
)


@dataclass(slots=True)
class ThresholdState:
    """UI-independent mutable threshold collection and range transitions."""

    items: list[AnalogActionThreshold] = field(default_factory=list)
    axis_control: bool = False

    def load(
        self,
        thresholds: list[AnalogActionThreshold] | tuple[AnalogActionThreshold, ...],
        *,
        axis_control: bool,
    ) -> None:
        self.items = [clone_threshold(threshold) for threshold in thresholds]
        self.axis_control = axis_control

    def snapshot(self) -> tuple[AnalogActionThreshold, ...]:
        return tuple(clone_threshold(threshold) for threshold in self.items)

    def sync_input_type(self, *, axis_control: bool, domain: tuple[float, float]) -> None:
        self.axis_control = axis_control
        if not axis_control:
            return
        minimum, maximum = domain
        for threshold in self.items:
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
        trigger_min = 0.50 if self.axis_control else 0.65
        release_min = 0.45 if self.axis_control else 0.55
        self.items.append(
            AnalogActionThreshold(
                axis="x",
                trigger_min=trigger_min,
                trigger_max=1.0,
                release_min=release_min,
                release_max=1.0,
                actions=[],
            )
        )

    def remove(self, index: int) -> bool:
        if not 0 <= index < len(self.items):
            return False
        self.items.pop(index)
        return True

    def set_actions(self, index: int, actions: list[object]) -> bool:
        if not 0 <= index < len(self.items):
            return False
        self.items[index].actions = [
            action for action in actions if isinstance(action, MappingAction)
        ]
        return True

    def append_template(self, thresholds: list[AnalogActionThreshold]) -> bool:
        if self.axis_control:
            return False
        self.items.extend(clone_threshold(threshold) for threshold in thresholds)
        return True

    def set_axis(self, index: int, axis: str) -> bool:
        if not 0 <= index < len(self.items):
            return False
        self.items[index].axis = axis
        return True

    def update_primary(
        self,
        index: int,
        *,
        trigger_min: float,
        trigger_max: float,
        hysteresis: float,
        domain: tuple[float, float],
    ) -> AnalogActionThreshold | None:
        if not 0 <= index < len(self.items):
            return None
        if trigger_min > trigger_max:
            trigger_min, trigger_max = trigger_max, trigger_min
        hysteresis = max(0.0, min(1.0, hysteresis))
        minimum, maximum = domain
        threshold = self.items[index]
        threshold.trigger_min = trigger_min
        threshold.trigger_max = trigger_max
        threshold.release_min = max(minimum, trigger_min - hysteresis)
        threshold.release_max = min(maximum, trigger_max + hysteresis)
        return threshold

    def update_advanced(
        self,
        index: int,
        *,
        release_min: float,
        release_max: float,
    ) -> AnalogActionThreshold | None:
        if not 0 <= index < len(self.items):
            return None
        threshold = self.items[index]
        threshold.release_min = min(threshold.trigger_min, release_min)
        threshold.release_max = max(threshold.trigger_max, release_max)
        return threshold


@dataclass(slots=True)
class ThresholdRowView:
    row: Adw.ExpanderRow
    range_bar: ThresholdRangeBar
    trigger_min: Adw.SpinRow
    trigger_max: Adw.SpinRow
    hysteresis: Adw.SpinRow
    release_min: Adw.SpinRow
    release_max: Adw.SpinRow


class ThresholdEditor:
    """Own the threshold GTK rows while delegating transitions to ``ThresholdState``."""

    def __init__(
        self,
        container: Adw.PreferencesGroup,
        *,
        get_domain: Callable[[], tuple[float, float]],
        get_current_mode: Callable[[], str],
        ensure_digital_mode: Callable[[], None],
        on_modified: Callable[[], None],
        open_actions_dialog: Callable[[int], None],
    ) -> None:
        self._container = container
        self._get_domain = get_domain
        self._get_current_mode = get_current_mode
        self._ensure_digital_mode = ensure_digital_mode
        self._on_modified = on_modified
        self._open_actions_dialog = open_actions_dialog
        self._syncing = False
        self.state = ThresholdState()
        self.rows: list[ThresholdRowView] = []

    @property
    def thresholds(self) -> list[AnalogActionThreshold]:
        return self.state.items

    def load(
        self,
        thresholds: list[AnalogActionThreshold] | tuple[AnalogActionThreshold, ...],
        *,
        axis_control: bool,
    ) -> None:
        self.state.load(thresholds, axis_control=axis_control)
        self.refresh()

    def snapshot(self) -> tuple[AnalogActionThreshold, ...]:
        return self.state.snapshot()

    def refresh(self, expanded_indices: set[int] | None = None) -> None:
        for view in self.rows:
            self._container.remove(view.row)
        self.rows.clear()
        for index, threshold in enumerate(self.state.items):
            view = self._build_row(index, threshold)
            if expanded_indices and index in expanded_indices:
                view.row.set_expanded(True)
            self._container.add(view.row)
            self.rows.append(view)

    def expanded_indices(self) -> set[int]:
        return {index for index, view in enumerate(self.rows) if view.row.get_expanded()}

    def sync_for_input_type(self, *, axis_control: bool) -> None:
        self.state.sync_input_type(axis_control=axis_control, domain=self._get_domain())

    def add_range(self) -> None:
        self.state.add_range()
        if self._get_current_mode() == "mouse":
            self._ensure_digital_mode()
        self.refresh()
        self._modified()

    def remove_threshold(self, index: int) -> None:
        if self.state.remove(index):
            self.refresh()
            self._modified()

    def edit_threshold_actions(self, index: int) -> None:
        if 0 <= index < len(self.state.items):
            self._open_actions_dialog(index)

    def actions_selected(self, actions: list[object], index: int) -> None:
        expanded = self.expanded_indices()
        if not self.state.set_actions(index, actions):
            return
        expanded.add(index)
        self.refresh(expanded)
        self._modified()

    def apply_wasd_template(self) -> None:
        self.apply_template(analog_control_wasd_template())

    def apply_arrow_template(self) -> None:
        self.apply_template(analog_control_arrow_template())

    def apply_mouse_wheel_template(self) -> None:
        self.apply_template(analog_control_mouse_wheel_template())

    def apply_template(self, thresholds: list[AnalogActionThreshold]) -> None:
        if not self.state.append_template(thresholds):
            return
        if self._get_current_mode() == "mouse":
            self._ensure_digital_mode()
        self.refresh()
        self._modified()

    def _build_row(self, index: int, threshold: AnalogActionThreshold) -> ThresholdRowView:
        is_axis = self.state.axis_control
        row = Adw.ExpanderRow()
        row.set_title(self._row_title(index, threshold))
        row.set_subtitle(self._subtitle(threshold))
        row.set_enable_expansion(True)

        edit = Gtk.Button(label="Edit Actions")
        edit.add_css_class("flat")
        edit.connect("clicked", self._on_edit_clicked, index)
        row.add_suffix(edit)
        remove = Gtk.Button(icon_name="edit-delete-symbolic")
        remove.add_css_class("flat")
        remove.add_css_class("destructive-action")
        remove.connect("clicked", self._on_remove_clicked, index)
        row.add_suffix(remove)

        bar_row = Adw.ActionRow()
        range_bar = ThresholdRangeBar()
        range_bar.set_domain(*self._get_domain())
        range_bar.set_ranges(
            threshold.trigger_min,
            threshold.trigger_max,
            threshold.release_min,
            threshold.release_max,
        )
        bar_row.set_child(range_bar)
        row.add_row(bar_row)

        if not is_axis:
            axis_row = Adw.ActionRow(title="Axis")
            axis_buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
            axis_buttons.add_css_class("linked")
            axis_group: Gtk.ToggleButton | None = None
            for axis in AXIS_ITEMS:
                button = Gtk.ToggleButton(label=axis.upper())
                if axis_group is None:
                    axis_group = button
                else:
                    button.set_group(axis_group)
                button.set_active(threshold.axis == axis)
                button.connect(
                    "toggled",
                    self._on_axis_toggled,
                    index,
                    axis,
                    row,
                )
                axis_buttons.append(button)
            axis_row.add_suffix(axis_buttons)
            row.add_row(axis_row)

        trigger_min = self._percent_row("Activation Min", threshold.trigger_min)
        trigger_max = self._percent_row("Activation Max", threshold.trigger_max)
        hysteresis = self._percent_row(
            "Hysteresis",
            compute_hysteresis(threshold),
            lower=0.0,
            upper=100.0,
            step=1.0,
        )
        release_min = self._percent_row("Release Min", threshold.release_min)
        release_max = self._percent_row("Release Max", threshold.release_max)
        view = ThresholdRowView(
            row=row,
            range_bar=range_bar,
            trigger_min=trigger_min,
            trigger_max=trigger_max,
            hysteresis=hysteresis,
            release_min=release_min,
            release_max=release_max,
        )
        for spin in (trigger_min, trigger_max, hysteresis):
            spin.connect("notify::value", self._on_primary_changed, index, view)
        for spin in (release_min, release_max):
            spin.connect("notify::value", self._on_advanced_changed, index, view)
        row.add_row(trigger_min)
        row.add_row(trigger_max)
        row.add_row(hysteresis)
        advanced = Adw.ExpanderRow(title="Advanced", subtitle="Release range")
        advanced.add_row(release_min)
        advanced.add_row(release_max)
        row.add_row(advanced)

        for action_index, action in enumerate(threshold.actions, start=1):
            child = Adw.ActionRow()
            child.set_use_markup(False)
            child.set_title(f"{action_index}. {describe_mapping_action_verbose(action)}")
            row.add_row(child)
        return view

    def _on_edit_clicked(self, _button: Gtk.Button, index: int) -> None:
        self.edit_threshold_actions(index)

    def _on_remove_clicked(self, _button: Gtk.Button, index: int) -> None:
        self.remove_threshold(index)

    def _percent_row(
        self,
        title: str,
        value: float,
        *,
        lower: float = -100.0,
        upper: float = 100.0,
        step: float = 5.0,
    ) -> Adw.SpinRow:
        return Adw.SpinRow(
            title=f"{title} (%)",
            adjustment=Gtk.Adjustment(
                value=to_percent(value),
                lower=lower,
                upper=upper,
                step_increment=step,
            ),
            digits=0,
        )

    def _on_axis_toggled(
        self,
        button: Gtk.ToggleButton,
        index: int,
        axis: str,
        row: Adw.ExpanderRow,
    ) -> None:
        if not button.get_active() or not self.state.set_axis(index, axis):
            return
        row.set_title(self._row_title(index, self.state.items[index]))
        row.set_subtitle(self._subtitle(self.state.items[index]))
        self._modified()

    def _on_primary_changed(
        self,
        _spin: Adw.SpinRow,
        _param: object,
        index: int,
        view: ThresholdRowView,
    ) -> None:
        if self._syncing:
            return
        trigger_min = from_percent(view.trigger_min.get_value())
        trigger_max = from_percent(view.trigger_max.get_value())
        threshold = self.state.update_primary(
            index,
            trigger_min=trigger_min,
            trigger_max=trigger_max,
            hysteresis=from_percent(view.hysteresis.get_value()),
            domain=self._get_domain(),
        )
        if threshold is None:
            return
        self._syncing = True
        try:
            view.trigger_min.set_value(to_percent(threshold.trigger_min))
            view.trigger_max.set_value(to_percent(threshold.trigger_max))
            view.release_min.set_value(to_percent(threshold.release_min))
            view.release_max.set_value(to_percent(threshold.release_max))
        finally:
            self._syncing = False
        self._update_row(view, threshold)
        self._modified()

    def _on_advanced_changed(
        self,
        _spin: Adw.SpinRow,
        _param: object,
        index: int,
        view: ThresholdRowView,
    ) -> None:
        if self._syncing:
            return
        threshold = self.state.update_advanced(
            index,
            release_min=from_percent(view.release_min.get_value()),
            release_max=from_percent(view.release_max.get_value()),
        )
        if threshold is None:
            return
        self._syncing = True
        try:
            view.release_min.set_value(to_percent(threshold.release_min))
            view.release_max.set_value(to_percent(threshold.release_max))
            view.hysteresis.set_value(to_percent(compute_hysteresis(threshold)))
        finally:
            self._syncing = False
        self._update_row(view, threshold)
        self._modified()

    def _update_row(self, view: ThresholdRowView, threshold: AnalogActionThreshold) -> None:
        view.range_bar.set_domain(*self._get_domain())
        view.range_bar.set_ranges(
            threshold.trigger_min,
            threshold.trigger_max,
            threshold.release_min,
            threshold.release_max,
        )
        view.row.set_subtitle(self._subtitle(threshold))

    def _row_title(self, index: int, threshold: AnalogActionThreshold) -> str:
        title = f"Range {index + 1}"
        return title if self.state.axis_control else f"{title}: {threshold.axis.upper()}"

    def _subtitle(self, threshold: AnalogActionThreshold) -> str:
        count = len(threshold.actions)
        noun = "action" if count == 1 else "actions"
        return (
            f"activate {to_percent(threshold.trigger_min):.0f}%.."
            f"{to_percent(threshold.trigger_max):.0f}% · "
            f"hysteresis {to_percent(compute_hysteresis(threshold)):.0f}% · "
            f"{count} {noun}"
        )

    def _modified(self) -> None:
        self._on_modified()
