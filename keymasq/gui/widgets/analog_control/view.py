"""Concrete GTK owner for the analog-control editing form."""

from __future__ import annotations

from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.model.analog import SAME_DEVICE_OUTPUT_ID
from keymasq.common.output_axes import (
    STANDARD_OUTPUT_AXES,
    OutputAxis,
    find_output_axis,
    learned_output_axes,
)
from keymasq.common.virtual_devices import is_virtual_gamepad_output_id
from keymasq.gui.widgets.analog_control.draft import ControlDraft, GamepadDraft, MouseDraft
from keymasq.gui.widgets.analog_control.gamepad import (
    GamepadOutputGroupHandle,
    GamepadPanelCallbacks,
    build_gamepad_output_group,
    gamepad_output_target_key,
    populate_gamepad_output_target_buttons,
)
from keymasq.gui.widgets.analog_control.layout import (
    DigitalGroupHandle,
    DigitalPanelCallbacks,
    TemplateGroupHandle,
    TemplatePanelCallbacks,
    build_digital_group,
    build_template_group,
)
from keymasq.gui.widgets.analog_control.mouse import (
    MouseGroupHandle,
    MousePanelConfig,
    build_mouse_group,
)
from keymasq.gui.widgets.analog_control.options import (
    INPUT_TYPE_OPTIONS,
    gamepad_output_target_label_for_input_type,
    gamepad_output_target_options_for_input_type,
    input_type_index,
    mode_index_for_input_type,
    mode_items_for_input_type,
    mode_labels_for_input_type,
    option_labels,
)
from keymasq.gui.widgets.analog_control.thresholds import ThresholdEditor
from keymasq.gui.widgets.gamepad_output_choices import (
    GamepadOutputChoiceSet,
    gamepad_output_choice_matches,
    load_gamepad_output_choices,
    selected_gamepad_output_id,
    update_gamepad_output_warning_label,
    virtual_gamepad_count,
)
from keymasq.gui.widgets.managed_editor.shell import LabeledForm
from keymasq.gui.widgets.position_capture import PositionCaptureController
from keymasq.gui.widgets.spin_inputs import (
    CompactIntEntryController,
    SplitAxisDesyncController,
    entry_int_value,
    set_entry_int,
)
from keymasq.session.hardware import HardwareManager

OutputChoicesLoader = Callable[[str | None], GamepadOutputChoiceSet]


def _default_output_choices(selected_id: str | None) -> GamepadOutputChoiceSet:
    return load_gamepad_output_choices(
        selected_id,
        count_loader=virtual_gamepad_count,
        hardware_manager_factory=HardwareManager,
    )


class AnalogControlEditorView(Gtk.Box):
    """Own all widgets and transient UI state for one analog-control draft."""

    def __init__(
        self,
        *,
        position_capture: PositionCaptureController,
        on_modified: Callable[[], None],
        open_threshold_actions: Callable[[int], None],
        output_choices_loader: OutputChoicesLoader = _default_output_choices,
        output_count_loader: Callable[[], int] = virtual_gamepad_count,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.capture = position_capture
        self._notify_modified = on_modified
        self._output_choices_loader = output_choices_loader
        self._output_count_loader = output_count_loader
        self._loading = True
        self._syncing_mouse_speed = False
        self._syncing_area_radius = False
        self._syncing_invert_axes = False
        self._last_speed_x = 900.0
        self._last_speed_y = 900.0
        self._last_area_radius_x = 400.0
        self._last_area_radius_y = 400.0
        self._split_desync = SplitAxisDesyncController()
        self._int_entries = CompactIntEntryController(self._on_modified)
        self._mode_items = mode_items_for_input_type("stick")
        self._selected_output_id: str | None = SAME_DEVICE_OUTPUT_ID
        self._output_ids: list[str | None] = []
        self._axis_target_dropdown: Gtk.DropDown | None = None
        self._output_target_items: list[tuple[str, str | None]] = []
        self._output_target_buttons: dict[str, Gtk.ToggleButton] = {}
        self._hardware_output_configs: dict[str, object] = {}
        self._refreshing_outputs = False
        self._tick_ms = 8

        self._build_fields()
        self.append(Gtk.Separator())
        self.mouse: MouseGroupHandle = build_mouse_group(
            MousePanelConfig(
                capture=self.capture,
                int_entries=self._int_entries,
                split_desync=self._split_desync,
                modified=self._on_modified,
                split_speed_changed=self._on_split_mouse_speed_changed,
                area_radius_changed=self._on_area_radius_changed,
                curve_changed=self._on_mouse_curve_changed,
                invert_axis_toggled=self._on_invert_axis_toggled,
                area_start_enabled_changed=self._on_area_start_enabled_changed,
                begin_capture=self.begin_area_capture,
            )
        )
        self.gamepad: GamepadOutputGroupHandle = build_gamepad_output_group(
            GamepadPanelCallbacks(
                modified=self._on_modified,
                output_selected=self._on_gamepad_output_selected,
                direction_toggled=self._on_gamepad_output_direction_toggled,
                curve_changed=self._on_gamepad_output_curve_changed,
            )
        )

        def add_range() -> None:
            self.thresholds.add_range()

        self.digital: DigitalGroupHandle = build_digital_group(
            DigitalPanelCallbacks(add_range=add_range)
        )
        self.templates: TemplateGroupHandle = build_template_group(
            TemplatePanelCallbacks(
                apply_wasd=self.apply_wasd_template,
                apply_arrows=self.apply_arrow_template,
                apply_mouse_wheel=self.apply_mouse_wheel_template,
            )
        )
        self.thresholds = ThresholdEditor(
            self.digital.group,
            get_domain=self.threshold_domain,
            get_current_mode=self.current_mode,
            ensure_digital_mode=self.ensure_digital_mode,
            on_modified=self._on_modified,
            open_actions_dialog=open_threshold_actions,
        )
        self.append(self.mouse.group)
        self.append(self.gamepad.group)
        self.append(self.digital.group)
        self.append(self.templates.group)

        self.input_type_dropdown.connect("notify::selected", self._on_input_type_changed)
        self.mode_dropdown.connect("notify::selected", self._on_mode_changed)
        self.gamepad.gamepad_output_auto_rest_row.connect(
            "notify::active", self._on_auto_rest_changed
        )
        self._loading = False
        self.load(ControlDraft.new())

    def _build_fields(self) -> None:
        form = LabeledForm(label_width=104)
        self.name_entry = Gtk.Entry(hexpand=True)
        self.description_entry = Gtk.Entry(hexpand=True)
        self.input_type_dropdown = Gtk.DropDown.new_from_strings(
            list(option_labels(INPUT_TYPE_OPTIONS))
        )
        self.mode_dropdown = Gtk.DropDown.new_from_strings(
            list(mode_labels_for_input_type("stick"))
        )
        self.name_entry.connect("changed", self._on_modified)
        self.description_entry.connect("changed", self._on_modified)
        for label, widget in (
            ("Name:", self.name_entry),
            ("Description:", self.description_entry),
            ("Input Type:", self.input_type_dropdown),
            ("Mode:", self.mode_dropdown),
        ):
            form.append(label, widget)
        self.append(form.grid)

    def load(self, draft: ControlDraft) -> None:
        self._loading = True
        try:
            self._tick_ms = draft.mouse.tick_ms
            self.name_entry.set_text(draft.name)
            self.description_entry.set_text(draft.description)
            self.input_type_dropdown.set_selected(input_type_index(draft.input_type))
            self._set_mode_options(draft.input_type, draft.mode)
            self._load_mouse(draft.mouse)
            self._load_gamepad(draft.gamepad, draft.input_type)
            self.thresholds.load(draft.thresholds, axis_control=draft.input_type == "axis")
            self._update_curve_graphs()
            self.update_mode_visibility()
        finally:
            self._loading = False

    def draft(self) -> ControlDraft:
        input_type = self.current_input_type()
        return ControlDraft(
            name=self.name_entry.get_text(),
            description=self.description_entry.get_text(),
            input_type=input_type,
            mode=self.current_mode(),
            mouse=MouseDraft(
                speed=self.mouse.speed_row.get_value(),
                speed_x=self.mouse.speed_x_row.get_value(),
                speed_y=self.mouse.speed_y_row.get_value(),
                area_radius_x=self.mouse.area_radius_x_row.get_value(),
                area_radius_y=self.mouse.area_radius_y_row.get_value(),
                area_start_enabled=self.mouse.area_start_enabled_row.get_active(),
                area_start_x=entry_int_value(self.mouse.area_start_x_entry),
                area_start_y=entry_int_value(self.mouse.area_start_y_entry),
                deadzone=self.mouse.deadzone_row.get_value(),
                sensitivity=self.mouse.mouse_sensitivity_row.get_value(),
                response_curve=self.mouse.mouse_response_curve_row.get_value(),
                direction=self.current_mouse_direction(),
                invert_x=self.mouse.invert_x_btn.get_active(),
                invert_y=self.mouse.invert_y_btn.get_active(),
                tick_ms=self._tick_ms,
            ),
            gamepad=GamepadDraft(
                output_id=self._selected_output_id,
                deadzone=self.gamepad.gamepad_output_deadzone_row.get_value() / 100.0,
                target=self.current_output_target(),
                target_analog_id=self.current_output_target_analog_id(),
                target_axis=self.current_output_axis(),
                output_rest=(
                    None
                    if self.gamepad.gamepad_output_auto_rest_row.get_active()
                    else int(self.gamepad.gamepad_output_rest_row.get_value())
                ),
                output_direction=self.current_output_direction(),
                invert_x=self.gamepad.gamepad_output_invert_x_btn.get_active(),
                invert_y=self.gamepad.gamepad_output_invert_y_btn.get_active(),
                sensitivity=self.gamepad.gamepad_output_sensitivity_row.get_value(),
                response_curve=self.gamepad.gamepad_output_response_curve_row.get_value(),
            ),
            thresholds=self.thresholds.snapshot(),
        )

    def current_input_type(self) -> str:
        selected = int(self.input_type_dropdown.get_selected())
        if selected < 0 or selected >= len(INPUT_TYPE_OPTIONS):
            return "stick"
        return INPUT_TYPE_OPTIONS[selected].item_id

    def current_mode(self) -> str:
        selected = int(self.mode_dropdown.get_selected())
        if selected < 0 or selected >= len(self._mode_items):
            return self._mode_items[0]
        return self._mode_items[selected]

    def threshold_domain(self) -> tuple[float, float]:
        return (-1.0, 1.0)

    def ensure_digital_mode(self) -> None:
        self.mode_dropdown.set_selected(self._mode_items.index("digital"))

    def cancel_capture(self) -> None:
        self.capture.cancel("")

    def capture_active(self) -> bool:
        return bool(
            self.capture.pending or self.capture.timeout_id or self.capture.apply is not None
        )

    def set_modifier_key(self, keyval: int, pressed: bool) -> None:
        self._split_desync.set_modifier_key(keyval, pressed)

    def request_axis_desync(self, axis: str) -> None:
        self._split_desync.request(axis)

    @property
    def output_target_buttons(self) -> dict[str, Gtk.ToggleButton]:
        return self._output_target_buttons

    @property
    def selected_output_id(self) -> str | None:
        return self._selected_output_id

    def begin_area_capture(self) -> None:
        self.capture.begin(
            button=self.mouse.area_start_capture_btn,
            status_label=self.mouse.area_start_capture_status,
            delay_seconds=float(self.mouse.area_start_capture_delay_spin.get_value()),
            apply_position=self._apply_area_start_position,
        )

    def apply_wasd_template(self) -> None:
        self.thresholds.apply_wasd_template()

    def apply_arrow_template(self) -> None:
        self.thresholds.apply_arrow_template()

    def apply_mouse_wheel_template(self) -> None:
        self.thresholds.apply_mouse_wheel_template()

    def sync_thresholds_for_input_type(self) -> None:
        self.thresholds.sync_for_input_type(axis_control=self.current_input_type() == "axis")

    def _load_mouse(self, draft: MouseDraft) -> None:
        self._syncing_mouse_speed = True
        self._syncing_area_radius = True
        try:
            self.mouse.speed_row.set_value(draft.speed)
            self.mouse.speed_x_row.set_value(draft.speed_x)
            self.mouse.speed_y_row.set_value(draft.speed_y)
            self.mouse.area_radius_x_row.set_value(draft.area_radius_x)
            self.mouse.area_radius_y_row.set_value(draft.area_radius_y)
        finally:
            self._syncing_mouse_speed = False
            self._syncing_area_radius = False
        self._remember_split_values()
        self.mouse.deadzone_row.set_value(draft.deadzone)
        self.mouse.mouse_sensitivity_row.set_value(draft.sensitivity)
        self.mouse.mouse_response_curve_row.set_value(draft.response_curve)
        button = self.mouse.mouse_direction_buttons.get(draft.direction)
        if button is not None:
            button.set_active(True)
        self._syncing_invert_axes = True
        try:
            self.mouse.invert_x_btn.set_active(draft.invert_x)
            self.mouse.invert_y_btn.set_active(draft.invert_y)
        finally:
            self._syncing_invert_axes = False
        self.mouse.area_start_enabled_row.set_active(draft.area_start_enabled)
        set_entry_int(self.mouse.area_start_x_entry, draft.area_start_x)
        set_entry_int(self.mouse.area_start_y_entry, draft.area_start_y)

    def _load_gamepad(self, draft: GamepadDraft, input_type: str) -> None:
        self._selected_output_id = draft.output_id
        self._refresh_output_choices()
        self._set_output_target_options(
            input_type, draft.target, draft.target_analog_id, draft.target_axis
        )
        self.gamepad.gamepad_output_deadzone_row.set_value(round(draft.deadzone * 100.0))
        self.gamepad.gamepad_output_rest_row.set_value(draft.output_rest or 0)
        self.gamepad.gamepad_output_auto_rest_row.set_active(draft.output_rest is None)
        direction = {
            "both": self.gamepad.gamepad_output_direction_both_btn,
            "min": self.gamepad.gamepad_output_direction_min_btn,
        }.get(draft.output_direction, self.gamepad.gamepad_output_direction_max_btn)
        direction.set_active(True)
        self.gamepad.gamepad_output_invert_x_btn.set_active(draft.invert_x)
        self.gamepad.gamepad_output_invert_y_btn.set_active(draft.invert_y)
        self.gamepad.gamepad_output_sensitivity_row.set_value(draft.sensitivity)
        self.gamepad.gamepad_output_response_curve_row.set_value(draft.response_curve)

    def _set_mode_options(self, input_type: str, selected_mode: str | None = None) -> None:
        items = mode_items_for_input_type(input_type)
        labels = mode_labels_for_input_type(input_type)
        mode = selected_mode or items[0]
        selected = mode_index_for_input_type(input_type, mode) if mode in items else 0
        self._mode_items = items
        self.mode_dropdown.set_model(Gtk.StringList.new(list(labels)))
        self.mode_dropdown.set_selected(selected)

    def _set_output_target_options(
        self,
        input_type: str,
        selected_target: str | None = None,
        selected_analog_id: str | None = None,
        selected_axis: str | None = None,
    ) -> None:
        choices: list[tuple[str, str | None, str]] = self._output_target_choices(input_type)
        if not choices:
            choices = [
                (
                    "same",
                    None,
                    gamepad_output_target_label_for_input_type(input_type, "same"),
                )
            ]
        self.gamepad.gamepad_output_target_side_row.set_title(
            "Output Axis" if input_type == "axis" else "Output Control"
        )
        if input_type == "axis":
            self._set_axis_target_choices(
                choices,
                selected_target or "same",
                selected_axis if selected_target == "axis" else selected_analog_id,
            )
            return
        self._axis_target_dropdown = None
        self._output_target_items = [(target, analog_id) for target, analog_id, _ in choices]
        self._output_target_buttons = populate_gamepad_output_target_buttons(
            self.gamepad.gamepad_output_target_box,
            choices,
            selected_target=selected_target or "same",
            selected_analog_id=selected_analog_id,
            on_toggled=self._on_output_target_toggled,
        )

    def _available_output_axes(self) -> tuple[OutputAxis, ...]:
        selected = self._selected_output_id
        if selected == SAME_DEVICE_OUTPUT_ID:
            # Reusable controls have no source device until they are bound.
            return STANDARD_OUTPUT_AXES
        hardware = self._hardware_output_configs.get(selected or "")
        if hardware is not None:
            return learned_output_axes(getattr(hardware, "analog_inputs", []) or [])
        if selected is None or is_virtual_gamepad_output_id(selected):
            return STANDARD_OUTPUT_AXES
        return ()

    def _set_axis_target_choices(
        self,
        choices: list[tuple[str, str | None, str]],
        target: str,
        detail: str | None,
    ) -> None:
        selected = (target, detail)
        if selected not in [(kind, value) for kind, value, _ in choices]:
            saved_label = detail or gamepad_output_target_label_for_input_type("axis", target)
            label = (
                f"{saved_label} (unavailable)"
                if target == "axis"
                else f"{saved_label} (saved target)"
            )
            choices.append((target, detail, label))
        box = self.gamepad.gamepad_output_target_box
        while child := box.get_first_child():
            box.remove(child)
        self._output_target_buttons.clear()
        self._output_target_items = [(kind, value) for kind, value, _ in choices]
        dropdown = Gtk.DropDown.new_from_strings([label for _, _, label in choices])
        dropdown.set_enable_search(True)
        dropdown.set_selected(self._output_target_items.index(selected))
        dropdown.connect("notify::selected", self._on_axis_target_selected)
        self._axis_target_dropdown = dropdown
        box.append(dropdown)
        self._sync_axis_rest()

    def _selected_axis_target(self) -> tuple[str, str | None]:
        dropdown = self._axis_target_dropdown
        if dropdown is not None and 0 <= dropdown.get_selected() < len(self._output_target_items):
            return self._output_target_items[dropdown.get_selected()]
        return "same", None

    def current_output_axis(self) -> str | None:
        target, detail = self._selected_axis_target()
        return detail if target == "axis" else None

    def _on_axis_target_selected(self, _dropdown: Gtk.DropDown, _param: object) -> None:
        self._sync_axis_rest()
        self._update_output_warning()
        self._on_modified()

    def _sync_axis_rest(self) -> None:
        automatic = self.gamepad.gamepad_output_auto_rest_row.get_active()
        row = self.gamepad.gamepad_output_rest_row
        row.set_sensitive(not automatic)
        axis = find_output_axis(self._available_output_axes(), self.current_output_axis() or "")
        if self._selected_output_id == SAME_DEVICE_OUTPUT_ID:
            axis = None
        if automatic and axis is not None:
            row.set_value(axis.neutral)
        elif automatic:
            row.set_value(0)
        row.set_visible(
            self.current_mode() == "gamepad"
            and self.current_input_type() == "axis"
            and (not automatic or axis is not None)
        )
        self.gamepad.gamepad_output_auto_rest_row.set_subtitle(
            f"Neutral: {axis.neutral}. Range: {axis.minimum} to {axis.maximum}."
            if axis is not None
            else "Use the destination axis neutral value when released"
        )

    def _on_auto_rest_changed(self, _row: Adw.SwitchRow, _param: object) -> None:
        self._sync_axis_rest()

    def _output_target_choices(self, input_type: str) -> list[tuple[str, str | None, str]]:
        if input_type == "axis":
            axis_choices: list[tuple[str, str | None, str]] = [("same", None, "Same Axis")]
            axis_choices.extend(
                [
                    ("axis", axis.evdev.lower(), f"{axis.label} · {axis.evdev}")
                    for axis in self._available_output_axes()
                ]
            )
            return axis_choices
        selected = self._selected_output_id
        hardware = (
            self._hardware_output_configs.get(selected or "")
            if selected and not is_virtual_gamepad_output_id(selected)
            else None
        )
        if hardware is not None:
            choices: list[tuple[str, str | None, str]] = [
                (
                    "same",
                    None,
                    gamepad_output_target_label_for_input_type(input_type, "same"),
                )
            ]
            for analog in getattr(hardware, "analog_inputs", []) or []:
                if getattr(analog, "type", None) != input_type:
                    continue
                analog_id = str(getattr(analog, "id", "") or "")
                if analog_id:
                    choices.append(
                        ("analog", analog_id, str(getattr(analog, "label", "") or analog_id))
                    )
            return choices
        return [
            (option.item_id, None, option.label)
            for option in gamepad_output_target_options_for_input_type(input_type)
        ]

    def _output_target_key(self, target: str, analog_id: str | None) -> str:
        return gamepad_output_target_key(target, analog_id)

    def current_output_target(self) -> str:
        if self._axis_target_dropdown is not None:
            return self._selected_axis_target()[0]
        for target, analog_id in self._output_target_items:
            button = self._output_target_buttons.get(self._output_target_key(target, analog_id))
            if button is not None and button.get_active():
                return target
        return "same"

    def current_output_target_analog_id(self) -> str | None:
        if self._axis_target_dropdown is not None:
            target, detail = self._selected_axis_target()
            return detail if target == "analog" else None
        for target, analog_id in self._output_target_items:
            button = self._output_target_buttons.get(self._output_target_key(target, analog_id))
            if button is not None and button.get_active() and target == "analog":
                return analog_id
        return None

    def current_output_direction(self) -> str:
        if self.gamepad.gamepad_output_direction_both_btn.get_active():
            return "both"
        if self.gamepad.gamepad_output_direction_min_btn.get_active():
            return "min"
        return "max"

    def current_mouse_direction(self) -> str:
        for direction, button in self.mouse.mouse_direction_buttons.items():
            if button.get_active():
                return direction
        return "right"

    def update_mode_visibility(self) -> None:
        input_type = self.current_input_type()
        mode = self.current_mode()
        is_axis = input_type == "axis"
        mouse_visible = mode in {"mouse", "mouse_area"}
        area_visible = mode == "mouse_area" and not is_axis
        velocity_visible = mouse_visible and not area_visible
        self.mouse.group.set_visible(mouse_visible)
        self.mouse.speed_row.set_visible(velocity_visible and is_axis)
        self.mouse.speed_x_row.set_visible(velocity_visible and not is_axis)
        self.mouse.speed_y_row.set_visible(velocity_visible and not is_axis)
        self.mouse.area_radius_x_row.set_visible(area_visible)
        self.mouse.area_radius_y_row.set_visible(area_visible)
        self.mouse.mouse_direction_row.set_visible(velocity_visible and is_axis)
        self.mouse.invert_axes_row.set_title("Invert Axis" if is_axis else "Invert Axes")
        self.mouse.invert_axes_row.set_visible(mouse_visible)
        self.mouse.invert_y_btn.set_visible(not is_axis)
        self.mouse.area_start_enabled_row.set_visible(area_visible)
        show_start = area_visible and self.mouse.area_start_enabled_row.get_active()
        self.mouse.area_start_position_row.set_visible(show_start)
        self.mouse.area_start_capture_row.set_visible(show_start)
        if not show_start and self.capture_active():
            self.cancel_capture()

        gamepad_visible = mode == "gamepad"
        self.gamepad.group.set_visible(gamepad_visible)
        self.gamepad.gamepad_output_rest_row.set_visible(gamepad_visible and is_axis)
        self.gamepad.gamepad_output_auto_rest_row.set_visible(gamepad_visible and is_axis)
        self._sync_axis_rest()
        self.gamepad.gamepad_output_direction_row.set_visible(gamepad_visible and is_axis)
        show_invert = gamepad_visible and (not is_axis or self.current_output_direction() == "both")
        self.gamepad.gamepad_output_invert_row.set_title(
            "Invert Output Axis" if is_axis else "Invert Output Axes"
        )
        self.gamepad.gamepad_output_invert_row.set_visible(show_invert)
        self.gamepad.gamepad_output_invert_y_btn.set_visible(not is_axis)
        self.gamepad.gamepad_output_sensitivity_row.set_visible(gamepad_visible)
        self.gamepad.gamepad_output_response_curve_row.set_visible(gamepad_visible)
        self.gamepad.gamepad_output_curve_row.set_visible(gamepad_visible)
        self.digital.group.set_visible(mode == "digital")
        self.templates.group.set_visible(mode == "digital" and not is_axis)
        self._update_output_warning()

    def _refresh_output_choices(self) -> None:
        selected = self._selected_output_id
        helper_selected = None if selected == SAME_DEVICE_OUTPUT_ID else selected
        choice_set = self._output_choices_loader(helper_selected)
        self._hardware_output_configs = {
            str(getattr(config, "hardware_id", "") or ""): config
            for config in choice_set.hardware_configs
        }
        choices = [(SAME_DEVICE_OUTPUT_ID, "Default (same device)"), *choice_set.choices]
        self._output_ids = [output_id for output_id, _ in choices]
        selected_index = next(
            (
                index
                for index, output_id in enumerate(self._output_ids)
                if gamepad_output_choice_matches(output_id, selected)
            ),
            0,
        )
        self._refreshing_outputs = True
        try:
            self.gamepad.gamepad_output_dropdown.set_model(
                Gtk.StringList.new([label for _, label in choices])
            )
            self.gamepad.gamepad_output_dropdown.set_selected(selected_index)
        finally:
            self._refreshing_outputs = False
        self._selected_output_id = self._output_ids[selected_index]
        self._update_output_warning()

    def _on_gamepad_output_selected(self, dropdown: Gtk.DropDown) -> None:
        if self._refreshing_outputs:
            return
        target = self.current_output_target()
        analog_id = self.current_output_target_analog_id()
        selected_axis = self.current_output_axis()
        self._selected_output_id = selected_gamepad_output_id(
            int(dropdown.get_selected()),
            self._output_ids,
            self._selected_output_id,
        )
        self._set_output_target_options(self.current_input_type(), target, analog_id, selected_axis)
        self._update_output_warning()
        self._on_modified()

    def _update_output_warning(self) -> None:
        if self.current_mode() != "gamepad":
            self.gamepad.gamepad_output_warning_label.set_label("")
            self.gamepad.gamepad_output_warning_row.set_visible(False)
            return
        message = update_gamepad_output_warning_label(
            self.gamepad.gamepad_output_warning_label,
            self._selected_output_id,
            count_loader=self._output_count_loader,
        )
        axis_name = self.current_output_axis()
        if (
            axis_name
            and self._selected_output_id != SAME_DEVICE_OUTPUT_ID
            and find_output_axis(self._available_output_axes(), axis_name) is None
        ):
            message = (
                f"{axis_name.upper()} is unavailable on this output. No axis output will be sent."
            )
            self.gamepad.gamepad_output_warning_label.set_label(message)
        self.gamepad.gamepad_output_warning_row.set_visible(bool(message))

    def _on_input_type_changed(self, _dropdown: Gtk.DropDown, _param: object) -> None:
        mode = self.current_mode()
        input_type = self.current_input_type()
        items = mode_items_for_input_type(input_type)
        self._set_mode_options(input_type, mode if mode in items else items[0])
        self._set_output_target_options(
            input_type,
            self.current_output_target(),
            self.current_output_target_analog_id(),
            self.current_output_axis(),
        )
        expanded = self.thresholds.expanded_indices()
        self.thresholds.sync_for_input_type(axis_control=input_type == "axis")
        self.thresholds.refresh(expanded)
        self.update_mode_visibility()
        self._on_modified()

    def _on_mode_changed(self, _dropdown: Gtk.DropDown, _param: object) -> None:
        self.update_mode_visibility()
        self._on_modified()

    def _on_split_mouse_speed_changed(
        self,
        _row: Adw.SpinRow,
        axis: str,
    ) -> None:
        if self._syncing_mouse_speed:
            return
        was_synced = abs(self._last_speed_x - self._last_speed_y) < 0.000001
        if was_synced and not self._split_desync.requested(axis):
            self._syncing_mouse_speed = True
            try:
                if axis == "x":
                    self.mouse.speed_y_row.set_value(self.mouse.speed_x_row.get_value())
                else:
                    self.mouse.speed_x_row.set_value(self.mouse.speed_y_row.get_value())
            finally:
                self._syncing_mouse_speed = False
        self._clear_desync(axis)
        self._last_speed_x = self.mouse.speed_x_row.get_value()
        self._last_speed_y = self.mouse.speed_y_row.get_value()

    def _on_area_radius_changed(
        self,
        _row: Adw.SpinRow,
        axis: str,
    ) -> None:
        if self._syncing_area_radius:
            return
        was_synced = abs(self._last_area_radius_x - self._last_area_radius_y) < 0.000001
        if was_synced and not self._split_desync.requested(axis):
            self._syncing_area_radius = True
            try:
                if axis == "x":
                    self.mouse.area_radius_y_row.set_value(self.mouse.area_radius_x_row.get_value())
                else:
                    self.mouse.area_radius_x_row.set_value(self.mouse.area_radius_y_row.get_value())
            finally:
                self._syncing_area_radius = False
        self._clear_desync(axis)
        self._last_area_radius_x = self.mouse.area_radius_x_row.get_value()
        self._last_area_radius_y = self.mouse.area_radius_y_row.get_value()

    def _remember_split_values(self) -> None:
        self._last_speed_x = self.mouse.speed_x_row.get_value()
        self._last_speed_y = self.mouse.speed_y_row.get_value()
        self._last_area_radius_x = self.mouse.area_radius_x_row.get_value()
        self._last_area_radius_y = self.mouse.area_radius_y_row.get_value()

    def _clear_desync(self, axis: str) -> None:
        if self._split_desync.axis == axis:
            self._split_desync.clear(axis)

    def _on_invert_axis_toggled(self, _button: Gtk.ToggleButton, _axis: str) -> None:
        if not self._syncing_invert_axes:
            self._on_modified()

    def _on_area_start_enabled_changed(self) -> None:
        self.update_mode_visibility()
        self._on_modified()

    def _apply_area_start_position(self, x: int, y: int) -> None:
        set_entry_int(self.mouse.area_start_x_entry, x)
        set_entry_int(self.mouse.area_start_y_entry, y)
        self._on_modified()

    def _on_output_target_toggled(self, button: Gtk.ToggleButton) -> None:
        if button.get_active():
            self._on_modified()

    def _on_gamepad_output_direction_toggled(self, button: Gtk.ToggleButton) -> None:
        if button.get_active():
            self.update_mode_visibility()
            self._on_modified()

    def _on_gamepad_output_curve_changed(self) -> None:
        self._update_gamepad_curve()
        self._on_modified()

    def _on_mouse_curve_changed(self) -> None:
        self._update_mouse_curve()
        self._on_modified()

    def _update_curve_graphs(self) -> None:
        self._update_mouse_curve()
        self._update_gamepad_curve()

    def _update_mouse_curve(self) -> None:
        self.mouse.mouse_curve_graph.set_curve(
            deadzone=self.mouse.deadzone_row.get_value(),
            sensitivity=self.mouse.mouse_sensitivity_row.get_value(),
            response_curve=self.mouse.mouse_response_curve_row.get_value(),
        )

    def _update_gamepad_curve(self) -> None:
        self.gamepad.gamepad_output_curve_graph.set_curve(
            deadzone=self.gamepad.gamepad_output_deadzone_row.get_value() / 100.0,
            sensitivity=self.gamepad.gamepad_output_sensitivity_row.get_value(),
            response_curve=self.gamepad.gamepad_output_response_curve_row.get_value(),
        )

    def _on_modified(self, *_args: object) -> None:
        if not self._loading:
            self._notify_modified()
