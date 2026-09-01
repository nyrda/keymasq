"""GTK form for editing one Motion Control draft."""

from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.model.analog import SAME_DEVICE_OUTPUT_ID
from keymasq.common.virtual_devices import (
    is_virtual_gamepad_output_id,
    virtual_gamepad_output_id,
)
from keymasq.gui.widgets.analog_control.gamepad import (
    GamepadOutputRoutingHandle,
    add_gamepad_output_routing,
    gamepad_output_target_key,
    populate_gamepad_output_target_buttons,
)
from keymasq.gui.widgets.gamepad_output_choices import (
    GamepadOutputChoiceSet,
    gamepad_output_choice_matches,
    load_gamepad_output_choices,
    selected_gamepad_output_id,
    update_gamepad_output_warning_label,
    virtual_gamepad_count,
)
from keymasq.gui.widgets.managed_editor.shell import LabeledForm
from keymasq.gui.widgets.motion_control.draft import (
    MotionAxisRoutingDraft,
    MotionControlDraft,
    MotionGamepadDraft,
    MotionMouseDraft,
)
from keymasq.session.hardware import HardwareManager

_AXIS_OUTPUTS = ("none", "horizontal", "vertical")
_MODES = ("mouse", "gamepad")

OutputChoicesLoader = Callable[[str | None], GamepadOutputChoiceSet]


def _default_output_choices(selected_id: str | None) -> GamepadOutputChoiceSet:
    return load_gamepad_output_choices(
        selected_id,
        count_loader=virtual_gamepad_count,
        hardware_manager_factory=HardwareManager,
    )


class MotionControlEditorView(Gtk.Box):
    """Owns the widgets and transient state for one Motion Control draft."""

    def __init__(
        self,
        *,
        on_modified: Callable[[], None],
        output_choices_loader: OutputChoicesLoader = _default_output_choices,
        output_count_loader: Callable[[], int] = virtual_gamepad_count,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self._notify_modified = on_modified
        self._output_choices_loader = output_choices_loader
        self._output_count_loader = output_count_loader
        self._loading = True
        self._selected_output_id: str | None = None
        self._output_ids: list[str | None] = []
        self._output_target_items: list[tuple[str, str | None]] = []
        self._output_target_buttons: dict[str, Gtk.ToggleButton] = {}
        self._hardware_output_configs: dict[str, object] = {}
        self._refreshing_outputs = False
        initial = MotionControlDraft.new()
        self._mouse_draft = initial.mouse
        self._gamepad_draft = initial.gamepad
        self._active_mode = initial.mode
        self._build_fields()
        self._connect_signals()
        self._loading = False
        self.load(initial)

    def _build_fields(self) -> None:
        identity = LabeledForm(label_width=128)
        self.name_entry = Gtk.Entry(hexpand=True)
        self.description_entry = Gtk.Entry(hexpand=True)
        self.mode_dropdown = Gtk.DropDown.new_from_strings(["Mouse", "Controller Stick"])
        identity.append("Name:", self.name_entry)
        identity.append("Description:", self.description_entry)
        identity.append("Output:", self.mode_dropdown)
        self.append(identity.grid)
        self.append(Gtk.Separator())

        axes = LabeledForm(label_width=184)
        output_labels = ["Unused", "Horizontal mouse movement", "Vertical mouse movement"]
        self.yaw_output = Gtk.DropDown.new_from_strings(output_labels)
        self.pitch_output = Gtk.DropDown.new_from_strings(output_labels)
        self.roll_output = Gtk.DropDown.new_from_strings(output_labels)
        self.deadzone = self._spin(0.0, 90.0, 0.1)
        self.smoothing = self._spin(0.0, 0.99, 0.01)
        self.response_curve = self._spin(0.1, 4.0, 0.1)
        axes.append("Yaw (turn left/right):", self.yaw_output)
        axes.append("Pitch (tilt forward/back):", self.pitch_output)
        axes.append("Roll (tilt side to side):", self.roll_output)
        axes.append("Deadzone (°/s):", self.deadzone)
        axes.append("Smoothing:", self.smoothing)
        axes.append("Response curve:", self.response_curve)
        self.append(axes.grid)

        invert_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.invert_x = Gtk.CheckButton(label="Invert horizontal")
        self.invert_y = Gtk.CheckButton(label="Invert vertical")
        invert_box.append(self.invert_x)
        invert_box.append(self.invert_y)
        self.append(invert_box)

        self.mouse_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        mouse_form = LabeledForm(label_width=164)
        self.sensitivity_x = self._spin(0.0, 100.0, 0.1)
        self.sensitivity_y = self._spin(0.0, 100.0, 0.1)
        mouse_form.append("Horizontal sensitivity:", self.sensitivity_x)
        mouse_form.append("Vertical sensitivity:", self.sensitivity_y)
        self.mouse_box.append(mouse_form.grid)
        self.append(self.mouse_box)

        self.gamepad_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        output_group = Adw.PreferencesGroup(
            title="Analog Output Settings",
            description="Route motion to a gamepad output device.",
        )
        self.gamepad_output: GamepadOutputRoutingHandle = add_gamepad_output_routing(
            output_group,
            output_selected=self._on_gamepad_output_selected,
        )
        self.gamepad_output_warning_row = Adw.ActionRow()
        self.gamepad_output_warning_label = Gtk.Label(xalign=0, wrap=True)
        self.gamepad_output_warning_label.add_css_class("warning")
        self.gamepad_output_warning_label.add_css_class("caption")
        self.gamepad_output_warning_row.set_child(self.gamepad_output_warning_label)
        self.gamepad_output_warning_row.set_visible(False)
        output_group.add(self.gamepad_output_warning_row)
        self.gamepad_box.append(output_group)

        gamepad_form = LabeledForm(label_width=164)
        self.max_rate = self._spin(1.0, 4000.0, 10.0)
        gamepad_form.append("Full stick rate (°/s):", self.max_rate)
        self.gamepad_box.append(gamepad_form.grid)
        self.append(self.gamepad_box)

        note = Gtk.Label(
            label=(
                "Hardware configuration owns calibration and raw-unit conversion. "
                "This Motion Control only tunes normalized gyro input."
            )
        )
        note.set_wrap(True)
        note.set_xalign(0.0)
        note.add_css_class("dim-label")
        self.append(note)

    def _connect_signals(self) -> None:
        self.name_entry.connect("changed", self._on_modified)
        self.description_entry.connect("changed", self._on_modified)
        self.mode_dropdown.connect("notify::selected", self._on_mode_changed)
        for dropdown in (self.yaw_output, self.pitch_output, self.roll_output):
            dropdown.connect("notify::selected", self._on_modified)
        for spin in (
            self.deadzone,
            self.smoothing,
            self.response_curve,
            self.sensitivity_x,
            self.sensitivity_y,
            self.max_rate,
        ):
            spin.connect("value-changed", self._on_modified)
        self.invert_x.connect("toggled", self._on_modified)
        self.invert_y.connect("toggled", self._on_modified)

    @staticmethod
    def _spin(minimum: float, maximum: float, step: float) -> Gtk.SpinButton:
        spin = Gtk.SpinButton.new_with_range(minimum, maximum, step)
        spin.set_digits(2)
        return spin

    def load(self, draft: MotionControlDraft) -> None:
        self._loading = True
        try:
            self._mouse_draft = draft.mouse
            self._gamepad_draft = draft.gamepad
            self._active_mode = draft.mode
            self.name_entry.set_text(draft.name)
            self.description_entry.set_text(draft.description)
            self.mode_dropdown.set_selected(_MODES.index(draft.mode))
            self._load_axis_routing(draft.axis_routing)
            self._load_active_settings()
            self._update_mode_visibility()
        finally:
            self._loading = False

    def draft(self) -> MotionControlDraft:
        self._store_active_settings()
        return MotionControlDraft(
            name=self.name_entry.get_text(),
            description=self.description_entry.get_text(),
            mode=self._active_mode,
            axis_routing=self._axis_routing_draft(),
            mouse=self._mouse_draft,
            gamepad=self._gamepad_draft,
        )

    def focus_name(self) -> None:
        self.name_entry.grab_focus()

    def _on_mode_changed(self, _dropdown: Gtk.DropDown, _param: object) -> None:
        if self._loading:
            return
        self._store_active_settings()
        self._active_mode = _MODES[min(1, int(self.mode_dropdown.get_selected()))]
        self._loading = True
        try:
            self._load_active_settings()
            self._update_mode_visibility()
        finally:
            self._loading = False
        self._notify_modified()

    def _on_modified(self, *_args: object) -> None:
        if not self._loading:
            self._notify_modified()

    def _load_active_settings(self) -> None:
        settings = self._mouse_draft if self._active_mode == "mouse" else self._gamepad_draft
        self.deadzone.set_value(settings.deadzone_dps)
        self.smoothing.set_value(settings.smoothing)
        self.response_curve.set_value(settings.response_curve)
        self.invert_x.set_active(settings.invert_x)
        self.invert_y.set_active(settings.invert_y)
        if self._active_mode == "mouse":
            self.sensitivity_x.set_value(self._mouse_draft.sensitivity_x)
            self.sensitivity_y.set_value(self._mouse_draft.sensitivity_y)
        else:
            self._selected_output_id = self._gamepad_draft.output_id
            self._refresh_output_choices()
            self._set_output_target_options(
                self._gamepad_draft.target,
                self._gamepad_draft.target_analog_id,
            )
            self.max_rate.set_value(self._gamepad_draft.max_rate_dps)
            self._update_output_warning()

    def _store_active_settings(self) -> None:
        if self._active_mode == "mouse":
            self._mouse_draft = MotionMouseDraft(
                sensitivity_x=self.sensitivity_x.get_value(),
                sensitivity_y=self.sensitivity_y.get_value(),
                deadzone_dps=self.deadzone.get_value(),
                smoothing=self.smoothing.get_value(),
                response_curve=self.response_curve.get_value(),
                invert_x=self.invert_x.get_active(),
                invert_y=self.invert_y.get_active(),
            )
            return
        self._gamepad_draft = MotionGamepadDraft(
            output_id=self._selected_output_id,
            target=self.current_output_target(),
            target_analog_id=self.current_output_target_analog_id(),
            max_rate_dps=self.max_rate.get_value(),
            deadzone_dps=self.deadzone.get_value(),
            smoothing=self.smoothing.get_value(),
            response_curve=self.response_curve.get_value(),
            invert_x=self.invert_x.get_active(),
            invert_y=self.invert_y.get_active(),
        )

    def _load_axis_routing(self, routing: MotionAxisRoutingDraft) -> None:
        for dropdown, output in (
            (self.yaw_output, routing.yaw),
            (self.pitch_output, routing.pitch),
            (self.roll_output, routing.roll),
        ):
            dropdown.set_selected(_AXIS_OUTPUTS.index(output))

    def _axis_routing_draft(self) -> MotionAxisRoutingDraft:
        return MotionAxisRoutingDraft(
            yaw=self._selected_axis_output(self.yaw_output),
            pitch=self._selected_axis_output(self.pitch_output),
            roll=self._selected_axis_output(self.roll_output),
        )

    @staticmethod
    def _selected_axis_output(dropdown: Gtk.DropDown) -> str:
        selected = int(dropdown.get_selected())
        if 0 <= selected < len(_AXIS_OUTPUTS):
            return _AXIS_OUTPUTS[selected]
        return "none"

    def _update_mode_visibility(self) -> None:
        is_gamepad = self._active_mode == "gamepad"
        labels = (
            ["Unused", "Stick X", "Stick Y"]
            if is_gamepad
            else ["Unused", "Horizontal mouse movement", "Vertical mouse movement"]
        )
        for dropdown in (self.yaw_output, self.pitch_output, self.roll_output):
            selected = dropdown.get_selected()
            dropdown.set_model(Gtk.StringList.new(labels))
            dropdown.set_selected(selected)
        self.mouse_box.set_visible(not is_gamepad)
        self.gamepad_box.set_visible(is_gamepad)
        self._update_output_warning()

    @property
    def output_target_buttons(self) -> dict[str, Gtk.ToggleButton]:
        return self._output_target_buttons

    @property
    def selected_output_id(self) -> str | None:
        return self._selected_output_id

    def _refresh_output_choices(self) -> None:
        selected = self._selected_output_id
        helper_selected = None if selected == SAME_DEVICE_OUTPUT_ID else selected
        choice_set = self._output_choices_loader(helper_selected)
        self._hardware_output_configs = {
            str(getattr(config, "hardware_id", "") or ""): config
            for config in choice_set.hardware_configs
        }
        choices = [
            (SAME_DEVICE_OUTPUT_ID, "Default (same device)"),
            *[
                (
                    virtual_gamepad_output_id(1) if output_id is None else output_id,
                    label,
                )
                for output_id, label in choice_set.choices
            ],
        ]
        self._output_ids = [output_id for output_id, _label in choices]
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
            self.gamepad_output.gamepad_output_dropdown.set_model(
                Gtk.StringList.new([label for _output_id, label in choices])
            )
            self.gamepad_output.gamepad_output_dropdown.set_selected(selected_index)
        finally:
            self._refreshing_outputs = False
        self._selected_output_id = self._output_ids[selected_index]

    def _on_gamepad_output_selected(self, dropdown: Gtk.DropDown) -> None:
        if self._refreshing_outputs:
            return
        target = self.current_output_target()
        analog_id = self.current_output_target_analog_id()
        self._selected_output_id = selected_gamepad_output_id(
            int(dropdown.get_selected()),
            self._output_ids,
            self._selected_output_id,
        )
        self._set_output_target_options(target, analog_id)
        self._update_output_warning()
        self._on_modified()

    def _set_output_target_options(
        self,
        selected_target: str = "right",
        selected_analog_id: str | None = None,
    ) -> None:
        choices = self._output_target_choices()
        self._output_target_items = [(target, analog_id) for target, analog_id, _ in choices]
        self._output_target_buttons = populate_gamepad_output_target_buttons(
            self.gamepad_output.gamepad_output_target_box,
            choices,
            selected_target=selected_target,
            selected_analog_id=selected_analog_id,
            on_toggled=self._on_output_target_toggled,
        )

    def _output_target_choices(self) -> list[tuple[str, str | None, str]]:
        selected = self._selected_output_id
        hardware = (
            self._hardware_output_configs.get(selected or "")
            if selected and not is_virtual_gamepad_output_id(selected)
            else None
        )
        if hardware is not None:
            choices = []
            for analog in getattr(hardware, "analog_inputs", []) or []:
                if getattr(analog, "type", None) != "stick":
                    continue
                analog_id = str(getattr(analog, "id", "") or "")
                if analog_id:
                    choices.append(
                        ("analog", analog_id, str(getattr(analog, "label", "") or analog_id))
                    )
            if choices:
                return choices
        return [("right", None, "Right Stick"), ("left", None, "Left Stick")]

    def current_output_target(self) -> str:
        for target, analog_id in self._output_target_items:
            button = self._output_target_buttons.get(
                gamepad_output_target_key(target, analog_id)
            )
            if button is not None and button.get_active():
                return target
        return "right"

    def current_output_target_analog_id(self) -> str | None:
        for target, analog_id in self._output_target_items:
            button = self._output_target_buttons.get(
                gamepad_output_target_key(target, analog_id)
            )
            if button is not None and button.get_active() and target == "analog":
                return analog_id
        return None

    def _on_output_target_toggled(self, button: Gtk.ToggleButton) -> None:
        if button.get_active():
            self._on_modified()

    def _update_output_warning(self) -> None:
        if self._active_mode != "gamepad":
            self.gamepad_output_warning_label.set_label("")
            self.gamepad_output_warning_row.set_visible(False)
            return
        message = update_gamepad_output_warning_label(
            self.gamepad_output_warning_label,
            self._selected_output_id,
            count_loader=self._output_count_loader,
        )
        self.gamepad_output_warning_row.set_visible(bool(message))
