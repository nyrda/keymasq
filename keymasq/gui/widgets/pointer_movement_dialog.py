"""Edit one profile's pointer movement factors or controller-axis output."""

import logging
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq import __version__
from keymasq.common.model.actions import MappingAction
from keymasq.common.model.core import ActionType
from keymasq.common.model.pointer import (
    MAX_POINTER_COUNTS,
    MAX_POINTER_FACTOR,
    MAX_POINTER_RECENTER_MS,
    MAX_POINTER_WINDOW_MS,
    MIN_POINTER_WINDOW_MS,
    PointerMovementConfig,
)
from keymasq.common.output_axes import STANDARD_OUTPUT_AXES, OutputAxis, learned_output_axes
from keymasq.common.virtual_device_templates import resolve_virtual_devices, template_output_axes
from keymasq.gui.widgets.docs_links import docs_page_url
from keymasq.gui.widgets.gamepad_output_choices import (
    GamepadOutputChoiceSet,
    gamepad_output_choice_matches,
    load_gamepad_output_choices,
)
from keymasq.gui.widgets.spin_inputs import add_spin_secondary_step_controller, spin_row

log = logging.getLogger(__name__)

_MODES = (
    ("mouse", "Mouse Movement"),
    ("axes", "Controller Axes"),
    ("passthrough", "Unchanged"),
    ("suppress", "Blocked"),
)
_ACTION_MODES = {"passthrough": ActionType.PASSTHROUGH, "suppress": ActionType.SUPPRESS}
_BEHAVIORS = (("velocity", "Velocity"), ("position", "Position"))
_OVERSHOOT = (("drag", "Drag"), ("keep", "Keep"))
_DIRECTIONS = (("both", "Both"), ("max", "Max"), ("min", "Min"))

type OutputChoicesLoader = Callable[[str | None], GamepadOutputChoiceSet]


def _combo_row(title: str, labels: list[str], subtitle: str | None = None) -> Adw.ComboRow:
    row = Adw.ComboRow(title=title, model=Gtk.StringList.new(labels))
    if subtitle:
        row.set_subtitle(subtitle)
    return row


def _select(row: Adw.ComboRow, options: tuple[tuple[str, str], ...], value: str) -> None:
    keys = [key for key, _label in options]
    row.set_selected(keys.index(value) if value in keys else 0)


def _selected(row: Adw.ComboRow, options: tuple[tuple[str, str], ...]) -> str:
    index = row.get_selected()
    return options[index][0] if 0 <= index < len(options) else options[0][0]


def _fraction(percent_row: Adw.SpinRow) -> float:
    return round(percent_row.get_value() / 100.0, 9)


class PointerMovementDialog(Adw.Dialog):
    def __init__(
        self,
        source_label: str,
        current_action: MappingAction | None,
        *,
        on_save: Callable[[MappingAction | None], None],
        on_apply: Callable[[MappingAction], None] | None = None,
        output_choices_loader: OutputChoicesLoader = load_gamepad_output_choices,
    ) -> None:
        super().__init__(title=f"Pointer Movement · {source_label}", content_width=560)
        self._on_save = on_save
        self._on_apply = on_apply or on_save
        self._output_choices_loader = output_choices_loader
        self._output_ids: list[str | None] = []
        self._axes_by_output: dict[str, tuple[OutputAxis, ...]] = {}
        self._axis_choices: list[str | None] = []
        self._refreshing_outputs = False
        self._outputs_loaded = False
        config = (
            current_action.pointer_movement
            if current_action is not None
            and current_action.action_type == ActionType.POINTER_MOVEMENT
            and current_action.pointer_movement is not None
            else PointerMovementConfig()
        )
        self._saved_axes = (config.x_axis, config.y_axis)
        mode = next(
            (
                key
                for key, action_type in _ACTION_MODES.items()
                if current_action is not None and current_action.action_type == action_type
            ),
            config.mode,
        )

        page = Adw.PreferencesPage()

        mode_group = Adw.PreferencesGroup(
            description=(
                "Scale this mouse's movement, or turn it into controller axes. "
                "Movement is measured in the mouse's own counts. Unchanged overrides "
                "lower-priority profiles with normal movement, and Blocked stops it."
            )
        )
        self.mode_row = _combo_row("Output", [label for _key, label in _MODES])
        _select(self.mode_row, _MODES, mode)
        self.mode_row.connect("notify::selected", self._sync_visibility)
        mode_group.add(self.mode_row)
        page.add(mode_group)

        self.movement_group = Adw.PreferencesGroup(
            title="Movement",
            description="Swap applies first, then inversion and factors.",
        )
        self.factor_x_row = spin_row(
            "Horizontal Factor", config.factor_x, 0.0, MAX_POINTER_FACTOR, 0.05, 2
        )
        self.factor_x_row.set_subtitle("Multiplies horizontal movement")
        self.factor_y_row = spin_row(
            "Vertical Factor", config.factor_y, 0.0, MAX_POINTER_FACTOR, 0.05, 2
        )
        self.factor_y_row.set_subtitle("Multiplies vertical movement")
        self.invert_x_row = Adw.SwitchRow(title="Invert Horizontal", active=config.invert_x)
        self.invert_y_row = Adw.SwitchRow(title="Invert Vertical", active=config.invert_y)
        self.swap_row = Adw.SwitchRow(title="Swap Axes", active=config.swap_axes)
        self.swap_row.set_subtitle("Horizontal movement drives vertical output and vice versa")
        for row in (
            self.factor_x_row,
            self.factor_y_row,
            self.invert_x_row,
            self.invert_y_row,
            self.swap_row,
        ):
            self.movement_group.add(row)
        page.add(self.movement_group)

        self.output_group = Adw.PreferencesGroup(
            title="Controller Output",
            description=(
                "Velocity follows movement speed and returns to rest when the mouse stops. "
                "Position holds while the mouse stays moved."
            ),
        )
        self.output_row = Adw.ComboRow(title="Output Device")
        self.output_row.connect("notify::selected", self._on_output_selected)
        self.output_group.add(self.output_row)
        self.behavior_row = _combo_row("Behavior", [label for _key, label in _BEHAVIORS])
        _select(self.behavior_row, _BEHAVIORS, config.behavior)
        self.behavior_row.connect("notify::selected", self._sync_visibility)
        self.output_group.add(self.behavior_row)
        self.full_speed_row = spin_row(
            "Full Output Speed", config.full_speed, 1.0, MAX_POINTER_COUNTS, 100.0, 0
        )
        self.full_speed_row.set_subtitle("Counts per second that reach full axis output")
        self.window_row = spin_row(
            "Speed Window", config.window_ms, MIN_POINTER_WINDOW_MS, MAX_POINTER_WINDOW_MS, 1, 0
        )
        self.window_row.set_subtitle(
            "Milliseconds of movement averaged for speed; output returns to rest after this"
        )
        self.radius_row = spin_row(
            "Full Output Distance", config.radius, 1.0, MAX_POINTER_COUNTS, 50.0, 0
        )
        self.radius_row.set_subtitle("Counts from the center that reach full axis output")
        self.overshoot_row = _combo_row(
            "Beyond Full Output",
            [label for _key, label in _OVERSHOOT],
            "Drag moves the center along; Keep must be undone first",
        )
        _select(self.overshoot_row, _OVERSHOOT, config.overshoot)
        self.recenter_row = spin_row(
            "Recenter After Idle", config.recenter_ms, 0, MAX_POINTER_RECENTER_MS, 50, 0
        )
        self.recenter_row.set_subtitle("Milliseconds without movement; 0 turns recentering off")
        for row in (
            self.full_speed_row,
            self.window_row,
            self.radius_row,
            self.overshoot_row,
            self.recenter_row,
        ):
            self.output_group.add(row)
        page.add(self.output_group)

        self.axes_group = Adw.PreferencesGroup(
            title="Axes",
            description=(
                "Max or Min output uses only rightward or downward movement unless inverted."
            ),
        )
        self.x_axis_row = Adw.ComboRow(title="Horizontal Axis")
        self.x_direction_row = _combo_row("Horizontal Direction", [x[1] for x in _DIRECTIONS])
        _select(self.x_direction_row, _DIRECTIONS, config.x_direction)
        self.y_axis_row = Adw.ComboRow(title="Vertical Axis")
        self.y_direction_row = _combo_row("Vertical Direction", [x[1] for x in _DIRECTIONS])
        _select(self.y_direction_row, _DIRECTIONS, config.y_direction)
        for row in (self.x_axis_row, self.x_direction_row, self.y_axis_row, self.y_direction_row):
            self.axes_group.add(row)
        page.add(self.axes_group)

        self.response_group = Adw.PreferencesGroup(title="Response")
        self.deadzone_row = spin_row("Deadzone", config.deadzone * 100.0, 0.0, 95.0, 1.0, 0)
        self.deadzone_row.set_subtitle("Percent of full output ignored near rest")
        self.minimum_output_row = spin_row(
            "Minimum Output", config.minimum_output * 100.0, 0.0, 95.0, 1.0, 0
        )
        self.minimum_output_row.set_subtitle(
            "Percent output for the smallest movement, to skip a game's deadzone"
        )
        self.response_curve_row = spin_row(
            "Response Curve", config.response_curve, 0.25, 4.0, 0.05, 2
        )
        self.response_curve_row.set_subtitle(
            "Below 1 is faster near rest, above 1 is slower near rest"
        )
        for row in (self.deadzone_row, self.minimum_output_row, self.response_curve_row):
            self.response_group.add(row)
        page.add(self.response_group)

        self._selected_output_id = config.output_id

        footer = Gtk.Box(spacing=8)
        for side in ("top", "bottom", "start", "end"):
            getattr(footer, f"set_margin_{side}")(12)
        docs_button = Gtk.Button(label="?")
        docs_button.add_css_class("flat")
        docs_button.add_css_class("actions-docs-button")
        docs_button.set_tooltip_text("Open Pointer Movement documentation")
        docs_button.connect("clicked", self._on_docs_clicked)
        footer.append(docs_button)
        self.remove_button = Gtk.Button(label="Remove")
        self.remove_button.add_css_class("destructive-action")
        self.remove_button.set_sensitive(current_action is not None)
        self.remove_button.connect("clicked", self._on_remove_clicked)
        footer.append(self.remove_button)
        footer.append(Gtk.Box(hexpand=True))
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", self._on_cancel_clicked)
        footer.append(cancel)
        self.apply_button = Gtk.Button(label="Apply")
        self.apply_button.set_tooltip_text("Apply without closing, to fine tune while playing")
        self.apply_button.connect("clicked", self._on_apply_clicked)
        footer.append(self.apply_button)
        self.save_button = Gtk.Button(label="Save")
        self.save_button.add_css_class("suggested-action")
        self.save_button.connect("clicked", self._on_save_clicked)
        footer.append(self.save_button)

        self._spin_rows = (
            self.factor_x_row,
            self.factor_y_row,
            self.full_speed_row,
            self.window_row,
            self.radius_row,
            self.recenter_row,
            self.deadzone_row,
            self.minimum_output_row,
            self.response_curve_row,
        )
        for row, step in (
            (self.factor_x_row, 1.0),
            (self.factor_y_row, 1.0),
            (self.full_speed_row, 500.0),
            (self.window_row, 10.0),
            (self.radius_row, 100.0),
            (self.recenter_row, 100.0),
            (self.deadzone_row, 5.0),
            (self.minimum_output_row, 5.0),
            (self.response_curve_row, 0.25),
        ):
            add_spin_secondary_step_controller(row, page_step=step, snap_to_step=True)
        self._loaded_spin_state = [(row.get_value(), row.get_text()) for row in self._spin_rows]
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        page.set_vexpand(True)
        content.append(page)
        content.append(footer)
        self.set_child(content)
        self._sync_visibility()

    def action(self) -> MappingAction:
        mode = _selected(self.mode_row, _MODES)
        if mode in _ACTION_MODES:
            return MappingAction(action_type=_ACTION_MODES[mode])
        return MappingAction(
            action_type=ActionType.POINTER_MOVEMENT,
            pointer_movement=self.config(mode),
        )

    def config(self, mode: str) -> PointerMovementConfig:
        # update() reparses the rounded display text, so only rows the user touched.
        for row, loaded in zip(self._spin_rows, self._loaded_spin_state, strict=True):
            if (row.get_value(), row.get_text()) != loaded:
                row.update()
        return PointerMovementConfig(
            mode=mode,
            factor_x=self.factor_x_row.get_value(),
            factor_y=self.factor_y_row.get_value(),
            invert_x=self.invert_x_row.get_active(),
            invert_y=self.invert_y_row.get_active(),
            swap_axes=self.swap_row.get_active(),
            output_id=self._selected_output_id,
            behavior=_selected(self.behavior_row, _BEHAVIORS),
            full_speed=self.full_speed_row.get_value(),
            window_ms=int(self.window_row.get_value()),
            radius=self.radius_row.get_value(),
            overshoot=_selected(self.overshoot_row, _OVERSHOOT),
            recenter_ms=int(self.recenter_row.get_value()),
            x_axis=self._selected_axis(self.x_axis_row),
            y_axis=self._selected_axis(self.y_axis_row),
            x_direction=_selected(self.x_direction_row, _DIRECTIONS),
            y_direction=_selected(self.y_direction_row, _DIRECTIONS),
            deadzone=_fraction(self.deadzone_row),
            minimum_output=_fraction(self.minimum_output_row),
            response_curve=self.response_curve_row.get_value(),
        )

    def _sync_visibility(self, *_args: object) -> None:
        mode = _selected(self.mode_row, _MODES)
        axes = mode == "axes"
        velocity = _selected(self.behavior_row, _BEHAVIORS) == "velocity"
        if axes and not self._outputs_loaded:
            self._outputs_loaded = True
            self._refresh_output_choices()
        self.set_content_height(900 if axes else 640 if mode == "mouse" else 320)
        self.movement_group.set_visible(mode not in _ACTION_MODES)
        self.output_group.set_visible(axes)
        self.axes_group.set_visible(axes)
        self.response_group.set_visible(axes)
        self.full_speed_row.set_visible(velocity)
        self.window_row.set_visible(velocity)
        self.radius_row.set_visible(not velocity)
        self.overshoot_row.set_visible(not velocity)
        self.recenter_row.set_visible(not velocity)

    def _refresh_output_choices(self) -> None:
        choice_set = self._output_choices_loader(self._selected_output_id)
        self._axes_by_output = {
            device.output_id: template_output_axes(device.template)
            for device in resolve_virtual_devices(
                choice_set.count, choice_set.virtual_device_config
            )
        }
        for hardware in choice_set.hardware_configs:
            hardware_id = str(getattr(hardware, "hardware_id", "") or "")
            if hardware_id:
                self._axes_by_output[hardware_id] = learned_output_axes(
                    getattr(hardware, "analog_inputs", []) or []
                )
        self._output_ids = [output_id for output_id, _label in choice_set.choices]
        selected = next(
            (
                index
                for index, output_id in enumerate(self._output_ids)
                if gamepad_output_choice_matches(output_id, self._selected_output_id)
            ),
            0,
        )
        self._refreshing_outputs = True
        try:
            self.output_row.set_model(
                Gtk.StringList.new([label for _output_id, label in choice_set.choices])
            )
            self.output_row.set_selected(selected)
        finally:
            self._refreshing_outputs = False
        self._refresh_axis_choices()

    def _on_output_selected(self, row: Adw.ComboRow, _param: object) -> None:
        index = row.get_selected()
        if self._refreshing_outputs or not 0 <= index < len(self._output_ids):
            return
        output_id = self._output_ids[index]
        if output_id == self._selected_output_id:
            return
        self._saved_axes = (
            self._selected_axis(self.x_axis_row),
            self._selected_axis(self.y_axis_row),
        )
        self._selected_output_id = output_id
        self._refresh_axis_choices()

    def _available_axes(self) -> tuple[OutputAxis, ...]:
        axes = self._axes_by_output.get(self._selected_output_id or "virtual-gamepad-1")
        return axes if axes is not None else STANDARD_OUTPUT_AXES

    def _refresh_axis_choices(self) -> None:
        axes = self._available_axes()
        choices: list[str | None] = [None, *(axis.evdev.lower() for axis in axes)]
        labels = ["None", *(f"{axis.label} · {axis.evdev}" for axis in axes)]
        for saved in self._saved_axes:
            if saved is not None and saved not in choices:
                choices.append(saved)
                labels.append(f"{saved.upper()} (unavailable)")
        self._axis_choices = choices
        for row, saved in zip((self.x_axis_row, self.y_axis_row), self._saved_axes, strict=True):
            row.set_model(Gtk.StringList.new(labels))
            row.set_selected(choices.index(saved))

    def _selected_axis(self, row: Adw.ComboRow) -> str | None:
        if not self._outputs_loaded:
            return self._saved_axes[0 if row is self.x_axis_row else 1]
        index = row.get_selected()
        return self._axis_choices[index] if 0 <= index < len(self._axis_choices) else None

    def _on_save_clicked(self, _button: Gtk.Button) -> None:
        self._on_save(self.action())
        self.close()

    def _on_apply_clicked(self, _button: Gtk.Button) -> None:
        self._on_apply(self.action())
        self.remove_button.set_sensitive(True)

    def _on_docs_clicked(self, _button: Gtk.Button) -> None:
        url = docs_page_url("pointer-movement", version=__version__)
        try:
            Gtk.UriLauncher.new(url).launch(None, None, None)
        except Exception:
            log.exception("Could not open Pointer Movement documentation %s", url)

    def _on_cancel_clicked(self, _button: Gtk.Button) -> None:
        self.close()

    def _on_remove_clicked(self, _button: Gtk.Button) -> None:
        self._on_save(None)
        self.close()
