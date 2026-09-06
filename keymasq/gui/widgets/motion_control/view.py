"""GTK form for editing one Motion Control draft."""

from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.model.analog import SAME_DEVICE_OUTPUT_ID, AnalogControlConfig
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
from keymasq.gui.widgets.fuzzy_search import install_listbox_fuzzy_filter
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
    MotionAnalogDraft,
    MotionAxisRoutingDraft,
    MotionControlDraft,
    MotionGamepadDraft,
    MotionMouseDraft,
    MotionTiltDraft,
)
from keymasq.session.analog_controls import AnalogControlManager
from keymasq.session.hardware import HardwareManager

_AXIS_OUTPUTS = ("none", "horizontal", "vertical")
_MODES = ("mouse", "gamepad", "tilt_mouse", "tilt_gamepad", "analog")
_TILT_MODES = frozenset({"tilt_mouse", "tilt_gamepad"})
_GAMEPAD_MODES = frozenset({"gamepad", "tilt_gamepad"})
_TILT_REFERENCES = ("activation", "gravity")
_MOTION_ANALOG_SMOOTHING_MODES = ("fixed", "adaptive")
_MOTION_ANALOG_SOURCES = ("gyro", "tilt")
_GYRO_SIGNAL_AXES = ("none", "yaw", "pitch", "roll")
_TILT_SIGNAL_AXES = ("none", "pitch", "roll")

OutputChoicesLoader = Callable[[str | None], GamepadOutputChoiceSet]
AnalogControlsLoader = Callable[[], dict[str, AnalogControlConfig]]
EditAnalogControl = Callable[[str], None]


def _default_output_choices(selected_id: str | None) -> GamepadOutputChoiceSet:
    return load_gamepad_output_choices(
        selected_id,
        count_loader=virtual_gamepad_count,
        hardware_manager_factory=HardwareManager,
    )


def _default_analog_controls() -> dict[str, AnalogControlConfig]:
    return AnalogControlManager().get_all_analog_controls()


class MotionControlEditorView(Gtk.Box):
    """Owns the widgets and transient state for one Motion Control draft."""

    def __init__(
        self,
        *,
        on_modified: Callable[[], None],
        output_choices_loader: OutputChoicesLoader = _default_output_choices,
        output_count_loader: Callable[[], int] = virtual_gamepad_count,
        analog_controls_loader: AnalogControlsLoader = _default_analog_controls,
        edit_analog_control: EditAnalogControl | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self._notify_modified = on_modified
        self._output_choices_loader = output_choices_loader
        self._output_count_loader = output_count_loader
        self._analog_controls_loader = analog_controls_loader
        self._edit_analog_control = edit_analog_control
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
        self._tilt_draft = initial.tilt
        self._motion_analog_draft = initial.analog
        self._gyro_axis_routing_draft = initial.axis_routing
        self._active_mode = initial.mode
        self._build_fields()
        self._connect_signals()
        self._loading = False
        self.load(initial)

    def _build_fields(self) -> None:
        identity = LabeledForm(label_width=128)
        self.name_entry = Gtk.Entry(hexpand=True)
        self.description_entry = Gtk.Entry(hexpand=True)
        self.mode_dropdown = Gtk.DropDown.new_from_strings(
            [
                "Gyro Mouse",
                "Gyro Stick",
                "Tilt Mouse",
                "Tilt Stick",
                "Motion to Analog",
            ]
        )
        identity.append("Name:", self.name_entry)
        identity.append("Description:", self.description_entry)
        identity.append("Control type:", self.mode_dropdown)
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
        self.yaw_label = axes.append("Yaw (turn left/right):", self.yaw_output)
        axes.append("Pitch (tilt forward/back):", self.pitch_output)
        axes.append("Roll (tilt side to side):", self.roll_output)
        self.reference_dropdown = Gtk.DropDown.new_from_strings(
            ["Profile activation pose", "Absolute gravity"]
        )
        self.reference_label = axes.append("Neutral reference:", self.reference_dropdown)
        self.full_scale = self._spin(0.1, 90.0, 0.5)
        self.full_scale_label = axes.append("Full output angle (°):", self.full_scale)
        self.deadzone_label = axes.append("Deadzone (°/s):", self.deadzone)
        axes.append("Smoothing:", self.smoothing)
        axes.append("Response curve:", self.response_curve)
        self.axes_grid = axes.grid
        self.append(self.axes_grid)

        invert_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.invert_x = Gtk.CheckButton(label="Invert horizontal")
        self.invert_y = Gtk.CheckButton(label="Invert vertical")
        invert_box.append(self.invert_x)
        invert_box.append(self.invert_y)
        self.invert_box = invert_box
        self.append(self.invert_box)

        self.motion_analog_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        analog_form = LabeledForm(label_width=184)
        self.analog_source_dropdown = Gtk.DropDown.new_from_strings(
            ["Gyroscope rate", "Controller tilt"]
        )
        self.analog_x_axis = Gtk.DropDown.new_from_strings([])
        self.analog_y_axis = Gtk.DropDown.new_from_strings([])
        self.analog_reference = Gtk.DropDown.new_from_strings(
            ["Profile activation pose", "Absolute gravity"]
        )
        self.analog_full_scale_rate = self._spin(1.0, 4000.0, 10.0)
        self.analog_full_scale_angle = self._spin(0.1, 90.0, 0.5)
        self.analog_smoothing = self._spin(0.0, 0.99, 0.01)
        self.analog_smoothing_mode = Gtk.DropDown.new_from_strings(["Fixed", "Adaptive"])
        self.analog_min_cutoff = self._spin(0.1, 30.0, 0.1)
        self.analog_min_cutoff.set_digits(1)
        self.analog_min_cutoff.set_tooltip_text(
            "Lower values reduce resting jitter but add delay to slow movement."
        )
        self.analog_beta = self._spin(0.0, 100.0, 1.0)
        self.analog_beta.set_digits(0)
        self.analog_beta.set_tooltip_text(
            "Higher values reduce delay while moving, but can let more jitter through."
        )
        analog_control_picker = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        analog_control_hint = Gtk.Label(label="Select one · right-click to edit")
        analog_control_hint.add_css_class("dim-label")
        analog_control_hint.add_css_class("caption")
        analog_control_hint.set_halign(Gtk.Align.START)
        analog_control_picker.append(analog_control_hint)
        self.analog_control_search_entry = Gtk.SearchEntry()
        self.analog_control_search_entry.set_placeholder_text("Search Analog Controls")
        self.analog_control_search_entry.set_tooltip_text(
            "Filter Analog Controls by name, description, input type, or behavior"
        )
        analog_control_picker.append(self.analog_control_search_entry)
        self.analog_control_scrolled = Gtk.ScrolledWindow()
        self.analog_control_scrolled.set_policy(
            Gtk.PolicyType.NEVER,
            Gtk.PolicyType.AUTOMATIC,
        )
        self.analog_control_scrolled.set_min_content_height(176)
        self.analog_control_scrolled.set_max_content_height(220)
        self.analog_control_scrolled.set_propagate_natural_height(True)
        self.analog_control_listbox = Gtk.ListBox()
        self.analog_control_listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.analog_control_listbox.set_valign(Gtk.Align.START)
        self.analog_control_listbox.add_css_class("boxed-list")
        install_listbox_fuzzy_filter(
            self.analog_control_listbox,
            self.analog_control_search_entry,
        )
        self.analog_control_scrolled.set_child(self.analog_control_listbox)
        analog_control_picker.append(self.analog_control_scrolled)
        analog_form.append("Analog Control:", analog_control_picker)
        analog_form.append("Motion source:", self.analog_source_dropdown)
        analog_form.append("Analog X:", self.analog_x_axis)
        self.analog_y_label = analog_form.append("Analog Y:", self.analog_y_axis)
        self.analog_reference_label = analog_form.append(
            "Neutral reference:", self.analog_reference
        )
        self.analog_rate_label = analog_form.append(
            "Full output rate (°/s):", self.analog_full_scale_rate
        )
        self.analog_angle_label = analog_form.append(
            "Full output angle (°):", self.analog_full_scale_angle
        )
        analog_form.append("Smoothing filter:", self.analog_smoothing_mode)
        self.analog_smoothing_label = analog_form.append("Smoothing:", self.analog_smoothing)
        self.analog_min_cutoff_label = analog_form.append(
            "Rest cutoff (Hz):", self.analog_min_cutoff
        )
        self.analog_beta_label = analog_form.append("Motion response:", self.analog_beta)
        self.motion_analog_box.append(analog_form.grid)
        analog_invert_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.analog_invert_x = Gtk.CheckButton(label="Invert X")
        self.analog_invert_y = Gtk.CheckButton(label="Invert Y")
        analog_invert_box.append(self.analog_invert_x)
        analog_invert_box.append(self.analog_invert_y)
        self.motion_analog_box.append(analog_invert_box)
        self.append(self.motion_analog_box)

        self.mouse_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        mouse_form = LabeledForm(label_width=164)
        self.sensitivity_x = self._spin(0.0, 100.0, 0.1)
        self.sensitivity_y = self._spin(0.0, 100.0, 0.1)
        mouse_form.append("Horizontal sensitivity:", self.sensitivity_x)
        mouse_form.append("Vertical sensitivity:", self.sensitivity_y)
        self.mouse_box.append(mouse_form.grid)
        self.append(self.mouse_box)

        self.tilt_mouse_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        tilt_mouse_form = LabeledForm(label_width=164)
        self.tilt_speed_x = self._spin(0.0, 5000.0, 25.0)
        self.tilt_speed_y = self._spin(0.0, 5000.0, 25.0)
        tilt_mouse_form.append("Horizontal speed:", self.tilt_speed_x)
        tilt_mouse_form.append("Vertical speed:", self.tilt_speed_y)
        self.tilt_mouse_box.append(tilt_mouse_form.grid)
        self.append(self.tilt_mouse_box)

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
        self.max_rate.set_digits(0)
        self.max_rate.set_tooltip_text(
            "Controller rotation speed needed for full stick output. "
            "Lower values make aiming faster; higher values make it slower. "
            "This is degrees per second, not a tilt angle."
        )
        self.max_rate_label = gamepad_form.append("Full stick rate (°/s):", self.max_rate)
        self.minimum_output = self._spin(0.0, 100.0, 1.0)
        self.minimum_output.set_digits(0)
        self.minimum_output.set_tooltip_text(
            "Compensates for Steam or game stick deadzones while gyro contributes. "
            "Increase until slow rotation registers. Too much can make aiming jump."
        )
        self.minimum_output_label = gamepad_form.append(
            "Minimum stick output (%):", self.minimum_output
        )
        self.gamepad_box.append(gamepad_form.grid)
        self.append(self.gamepad_box)

        self.normalization_note = Gtk.Label(
            label=(
                "Hardware configuration owns calibration and raw-unit conversion. "
                "This Motion Control only tunes normalized gyro input."
            )
        )
        self.normalization_note.set_wrap(True)
        self.normalization_note.set_xalign(0.0)
        self.normalization_note.add_css_class("dim-label")
        self.append(self.normalization_note)

    def _connect_signals(self) -> None:
        self.name_entry.connect("changed", self._on_modified)
        self.description_entry.connect("changed", self._on_modified)
        self.mode_dropdown.connect("notify::selected", self._on_mode_changed)
        self.reference_dropdown.connect("notify::selected", self._on_modified)
        for dropdown in (self.yaw_output, self.pitch_output, self.roll_output):
            dropdown.connect("notify::selected", self._on_modified)
        for spin in (
            self.deadzone,
            self.smoothing,
            self.response_curve,
            self.sensitivity_x,
            self.sensitivity_y,
            self.max_rate,
            self.minimum_output,
            self.full_scale,
            self.tilt_speed_x,
            self.tilt_speed_y,
        ):
            spin.connect("value-changed", self._on_modified)
        self.invert_x.connect("toggled", self._on_modified)
        self.invert_y.connect("toggled", self._on_modified)
        self.analog_control_listbox.connect("row-selected", self._on_analog_control_selected)
        self.analog_source_dropdown.connect("notify::selected", self._on_analog_source_changed)
        self.analog_x_axis.connect("notify::selected", self._on_modified)
        self.analog_y_axis.connect("notify::selected", self._on_modified)
        self.analog_reference.connect("notify::selected", self._on_modified)
        self.analog_full_scale_rate.connect("value-changed", self._on_modified)
        self.analog_full_scale_angle.connect("value-changed", self._on_modified)
        self.analog_smoothing.connect("value-changed", self._on_modified)
        self.analog_smoothing_mode.connect("notify::selected", self._on_analog_smoothing_changed)
        self.analog_min_cutoff.connect("value-changed", self._on_modified)
        self.analog_beta.connect("value-changed", self._on_modified)
        self.analog_invert_x.connect("toggled", self._on_modified)
        self.analog_invert_y.connect("toggled", self._on_modified)

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
            self._tilt_draft = draft.tilt
            self._motion_analog_draft = draft.analog
            self._gyro_axis_routing_draft = draft.axis_routing
            self._active_mode = draft.mode
            self.name_entry.set_text(draft.name)
            self.description_entry.set_text(draft.description)
            self.mode_dropdown.set_selected(_MODES.index(draft.mode))
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
            axis_routing=self._gyro_axis_routing_draft,
            mouse=self._mouse_draft,
            gamepad=self._gamepad_draft,
            tilt=self._tilt_draft,
            analog=self._motion_analog_draft,
        )

    def focus_name(self) -> None:
        self.name_entry.grab_focus()

    def _on_mode_changed(self, _dropdown: Gtk.DropDown, _param: object) -> None:
        if self._loading:
            return
        self._store_active_settings()
        self._active_mode = _MODES[min(len(_MODES) - 1, int(self.mode_dropdown.get_selected()))]
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
        if self._active_mode == "analog":
            self._load_motion_analog_settings()
            return
        if self._active_mode in _TILT_MODES:
            settings = self._tilt_draft
            self._load_axis_routing(
                MotionAxisRoutingDraft(
                    yaw="none",
                    pitch=self._tilt_draft.pitch,
                    roll=self._tilt_draft.roll,
                )
            )
            self.reference_dropdown.set_selected(_TILT_REFERENCES.index(self._tilt_draft.reference))
            self.full_scale.set_value(self._tilt_draft.full_scale_deg)
            self.tilt_speed_x.set_value(self._tilt_draft.speed_x)
            self.tilt_speed_y.set_value(self._tilt_draft.speed_y)
            deadzone = self._tilt_draft.deadzone_deg
        else:
            settings = self._mouse_draft if self._active_mode == "mouse" else self._gamepad_draft
            self._load_axis_routing(self._gyro_axis_routing_draft)
            deadzone = settings.deadzone_dps
        self.deadzone.set_value(deadzone)
        self.smoothing.set_value(settings.smoothing)
        self.response_curve.set_value(settings.response_curve)
        self.invert_x.set_active(settings.invert_x)
        self.invert_y.set_active(settings.invert_y)
        if self._active_mode == "mouse":
            self.sensitivity_x.set_value(self._mouse_draft.sensitivity_x)
            self.sensitivity_y.set_value(self._mouse_draft.sensitivity_y)
        elif self._active_mode in _GAMEPAD_MODES:
            self._selected_output_id = self._gamepad_draft.output_id
            self._refresh_output_choices()
            self._set_output_target_options(
                self._gamepad_draft.target,
                self._gamepad_draft.target_analog_id,
            )
            self.max_rate.set_value(self._gamepad_draft.max_rate_dps)
            self.minimum_output.set_value(self._gamepad_draft.minimum_output * 100.0)
            self._update_output_warning()

    def _store_active_settings(self) -> None:
        if self._active_mode == "analog":
            self._store_motion_analog_settings()
            return
        if self._active_mode in _TILT_MODES:
            reference_index = int(self.reference_dropdown.get_selected())
            reference = (
                _TILT_REFERENCES[reference_index]
                if 0 <= reference_index < len(_TILT_REFERENCES)
                else "activation"
            )
            self._tilt_draft = MotionTiltDraft(
                reference=reference,
                pitch=self._selected_axis_output(self.pitch_output),
                roll=self._selected_axis_output(self.roll_output),
                deadzone_deg=self.deadzone.get_value(),
                full_scale_deg=self.full_scale.get_value(),
                smoothing=self.smoothing.get_value(),
                response_curve=self.response_curve.get_value(),
                invert_x=self.invert_x.get_active(),
                invert_y=self.invert_y.get_active(),
                speed_x=self.tilt_speed_x.get_value(),
                speed_y=self.tilt_speed_y.get_value(),
            )
            if self._active_mode == "tilt_gamepad":
                self._store_gamepad_output()
            return
        self._gyro_axis_routing_draft = self._axis_routing_draft()
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
        self._store_gamepad_output(
            max_rate_dps=self.max_rate.get_value(),
            minimum_output=self.minimum_output.get_value() / 100.0,
            deadzone_dps=self.deadzone.get_value(),
            smoothing=self.smoothing.get_value(),
            response_curve=self.response_curve.get_value(),
            invert_x=self.invert_x.get_active(),
            invert_y=self.invert_y.get_active(),
        )

    def _store_gamepad_output(
        self,
        *,
        max_rate_dps: float | None = None,
        minimum_output: float | None = None,
        deadzone_dps: float | None = None,
        smoothing: float | None = None,
        response_curve: float | None = None,
        invert_x: bool | None = None,
        invert_y: bool | None = None,
    ) -> None:
        self._gamepad_draft = MotionGamepadDraft(
            output_id=self._selected_output_id,
            target=self.current_output_target(),
            target_analog_id=self.current_output_target_analog_id(),
            max_rate_dps=(
                self._gamepad_draft.max_rate_dps if max_rate_dps is None else max_rate_dps
            ),
            minimum_output=(
                self._gamepad_draft.minimum_output if minimum_output is None else minimum_output
            ),
            deadzone_dps=(
                self._gamepad_draft.deadzone_dps if deadzone_dps is None else deadzone_dps
            ),
            smoothing=self._gamepad_draft.smoothing if smoothing is None else smoothing,
            response_curve=(
                self._gamepad_draft.response_curve if response_curve is None else response_curve
            ),
            invert_x=self._gamepad_draft.invert_x if invert_x is None else invert_x,
            invert_y=self._gamepad_draft.invert_y if invert_y is None else invert_y,
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
        is_tilt = self._active_mode in _TILT_MODES
        is_gamepad = self._active_mode in _GAMEPAD_MODES
        is_analog = self._active_mode == "analog"
        labels = (
            ["Unused", "Stick X", "Stick Y"]
            if is_gamepad
            else ["Unused", "Horizontal mouse movement", "Vertical mouse movement"]
        )
        for dropdown in (self.yaw_output, self.pitch_output, self.roll_output):
            selected = dropdown.get_selected()
            dropdown.set_model(Gtk.StringList.new(labels))
            dropdown.set_selected(selected)
        self.yaw_label.set_visible(not is_tilt)
        self.yaw_output.set_visible(not is_tilt)
        self.reference_label.set_visible(is_tilt)
        self.reference_dropdown.set_visible(is_tilt)
        self.full_scale_label.set_visible(is_tilt)
        self.full_scale.set_visible(is_tilt)
        self.deadzone_label.set_label("Deadzone (°):" if is_tilt else "Deadzone (°/s):")
        self.mouse_box.set_visible(self._active_mode == "mouse")
        self.tilt_mouse_box.set_visible(self._active_mode == "tilt_mouse")
        self.gamepad_box.set_visible(is_gamepad)
        self.axes_grid.set_visible(not is_analog)
        self.invert_box.set_visible(not is_analog)
        self.motion_analog_box.set_visible(is_analog)
        self.max_rate_label.set_visible(self._active_mode == "gamepad")
        self.max_rate.set_visible(self._active_mode == "gamepad")
        self.minimum_output_label.set_visible(self._active_mode == "gamepad")
        self.minimum_output.set_visible(self._active_mode == "gamepad")
        self.normalization_note.set_label(
            (
                "Hardware configuration normalizes motion signals. The attached Analog "
                "Control owns deadzones, response curves, actions, and outputs."
            )
            if is_analog
            else (
                "Hardware configuration owns calibration and raw-unit conversion. "
                "This Motion Control tunes normalized accelerometer orientation."
            )
            if is_tilt
            else (
                "Hardware configuration owns calibration and raw-unit conversion. "
                "This Motion Control only tunes normalized gyro input."
            )
        )
        self._update_output_warning()

    def _load_motion_analog_settings(self) -> None:
        draft = self._motion_analog_draft
        self._load_analog_control_choices(draft.analog_control_name)
        self.analog_source_dropdown.set_selected(_MOTION_ANALOG_SOURCES.index(draft.source))
        self._set_analog_signal_choices(draft.x_axis, draft.y_axis)
        self.analog_reference.set_selected(_TILT_REFERENCES.index(draft.reference))
        self.analog_full_scale_rate.set_value(draft.full_scale_dps)
        self.analog_full_scale_angle.set_value(draft.full_scale_deg)
        self.analog_smoothing.set_value(draft.smoothing)
        self.analog_smoothing_mode.set_selected(
            _MOTION_ANALOG_SMOOTHING_MODES.index(draft.smoothing_mode)
        )
        self.analog_min_cutoff.set_value(draft.adaptive_min_cutoff_hz)
        self.analog_beta.set_value(draft.adaptive_beta)
        self.analog_invert_x.set_active(draft.invert_x)
        self.analog_invert_y.set_active(draft.invert_y)
        self._update_motion_analog_visibility()

    def _store_motion_analog_settings(self) -> None:
        source = _MOTION_ANALOG_SOURCES[
            min(len(_MOTION_ANALOG_SOURCES) - 1, int(self.analog_source_dropdown.get_selected()))
        ]
        axes = _GYRO_SIGNAL_AXES if source == "gyro" else _TILT_SIGNAL_AXES
        self._motion_analog_draft = MotionAnalogDraft(
            analog_control_name=self._selected_analog_control_name(),
            source=source,
            x_axis=self._selected_signal_axis(self.analog_x_axis, axes),
            y_axis=self._selected_signal_axis(self.analog_y_axis, axes),
            reference=_TILT_REFERENCES[
                min(len(_TILT_REFERENCES) - 1, int(self.analog_reference.get_selected()))
            ],
            full_scale_dps=self.analog_full_scale_rate.get_value(),
            full_scale_deg=self.analog_full_scale_angle.get_value(),
            smoothing=self.analog_smoothing.get_value(),
            smoothing_mode=_MOTION_ANALOG_SMOOTHING_MODES[
                min(1, int(self.analog_smoothing_mode.get_selected()))
            ],
            adaptive_min_cutoff_hz=self.analog_min_cutoff.get_value(),
            adaptive_beta=self.analog_beta.get_value(),
            invert_x=self.analog_invert_x.get_active(),
            invert_y=self.analog_invert_y.get_active(),
        )

    def _load_analog_control_choices(self, selected_name: str | None) -> None:
        self._available_analog_controls = self._analog_controls_loader()
        while child := self.analog_control_listbox.get_first_child():
            self.analog_control_listbox.remove(child)

        if not self._available_analog_controls:
            row = Gtk.ListBoxRow()
            row.set_selectable(False)
            row._search_text = "No analog controls saved yet"
            label = Gtk.Label(label="No analog controls saved yet")
            label.add_css_class("dim-label")
            label.set_margin_top(12)
            label.set_margin_bottom(12)
            row.set_child(label)
            self.analog_control_listbox.append(row)
            return

        selected_row: Gtk.ListBoxRow | None = None
        for name in sorted(self._available_analog_controls):
            config = self._available_analog_controls[name]
            row = Gtk.ListBoxRow()
            row._analog_control_name = name
            row._search_text = " ".join(
                (
                    name,
                    config.description or "",
                    self._describe_analog_control(config),
                )
            )
            row.set_size_request(-1, 42)

            right_click = Gtk.GestureClick()
            right_click.set_button(3)
            right_click.connect(
                "pressed",
                self._on_analog_control_row_right_pressed,
                name,
            )
            row.add_controller(right_click)

            content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            content.set_margin_top(8)
            content.set_margin_bottom(8)
            content.set_margin_start(12)
            content.set_margin_end(12)
            name_label = Gtk.Label(label=name)
            name_label.set_halign(Gtk.Align.START)
            name_label.set_hexpand(True)
            content.append(name_label)
            detail_label = Gtk.Label(label=self._describe_analog_control(config))
            detail_label.add_css_class("dim-label")
            detail_label.add_css_class("caption")
            content.append(detail_label)
            row.set_child(content)
            self.analog_control_listbox.append(row)
            if name == selected_name:
                selected_row = row

        if selected_row is not None:
            self.analog_control_listbox.select_row(selected_row)

    def _selected_analog_control_name(self) -> str | None:
        row = self.analog_control_listbox.get_selected_row()
        name = getattr(row, "_analog_control_name", None)
        return name if isinstance(name, str) else None

    @staticmethod
    def _describe_analog_control(config: AnalogControlConfig) -> str:
        parts = ["Axis" if config.input_type == "axis" else "Stick"]
        if config.mouse_motion.enabled:
            parts.append("Mouse")
        if config.gamepad_output.enabled:
            parts.append("Gamepad output")
        if config.thresholds:
            count = len(config.thresholds)
            parts.append(f"{count} range{'s' if count != 1 else ''}")
        return " · ".join(parts)

    def _on_analog_control_selected(
        self,
        _listbox: Gtk.ListBox,
        _row: Gtk.ListBoxRow | None,
    ) -> None:
        if self._loading:
            return
        self._update_motion_analog_visibility()
        self._notify_modified()

    def _on_analog_control_row_right_pressed(
        self,
        gesture: Gtk.GestureClick,
        _n_press: int,
        _x: float,
        _y: float,
        name: str,
    ) -> None:
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        if self._edit_analog_control is not None:
            self._edit_analog_control(name)

    def refresh_analog_controls(self, fallback_name: str | None = None) -> None:
        if self._active_mode != "analog":
            return
        selected_name = self._selected_analog_control_name()
        self._loading = True
        try:
            self._load_analog_control_choices(selected_name)
            if self._selected_analog_control_name() is None and fallback_name != selected_name:
                self._load_analog_control_choices(fallback_name)
            self._update_motion_analog_visibility()
        finally:
            self._loading = False

    def _on_analog_source_changed(self, _dropdown: Gtk.DropDown, _param: object) -> None:
        if self._loading:
            return
        self._set_analog_signal_choices("roll", "pitch")
        self._update_motion_analog_visibility()
        self._notify_modified()

    def _set_analog_signal_choices(self, x_axis: str, y_axis: str) -> None:
        source_index = int(self.analog_source_dropdown.get_selected())
        source = _MOTION_ANALOG_SOURCES[min(len(_MOTION_ANALOG_SOURCES) - 1, source_index)]
        axes = _GYRO_SIGNAL_AXES if source == "gyro" else _TILT_SIGNAL_AXES
        labels = [axis.title() if axis != "none" else "Unused" for axis in axes]
        for dropdown, selected, fallback in (
            (self.analog_x_axis, x_axis, "roll"),
            (self.analog_y_axis, y_axis, "pitch"),
        ):
            dropdown.set_model(Gtk.StringList.new(labels))
            dropdown.set_selected(axes.index(selected if selected in axes else fallback))

    @staticmethod
    def _selected_signal_axis(dropdown: Gtk.DropDown, axes: tuple[str, ...]) -> str:
        selected = int(dropdown.get_selected())
        return axes[selected] if 0 <= selected < len(axes) else "none"

    def _on_analog_smoothing_changed(self, *_args: object) -> None:
        self._update_motion_analog_visibility()
        self._on_modified()

    def _update_motion_analog_visibility(self) -> None:
        adaptive = self.analog_smoothing_mode.get_selected() == 1
        self.analog_smoothing_label.set_visible(not adaptive)
        self.analog_smoothing.set_visible(not adaptive)
        for widget in (
            self.analog_min_cutoff_label,
            self.analog_min_cutoff,
            self.analog_beta_label,
            self.analog_beta,
        ):
            widget.set_visible(adaptive)
        source = _MOTION_ANALOG_SOURCES[
            min(len(_MOTION_ANALOG_SOURCES) - 1, int(self.analog_source_dropdown.get_selected()))
        ]
        is_tilt = source == "tilt"
        self.analog_reference_label.set_visible(is_tilt)
        self.analog_reference.set_visible(is_tilt)
        self.analog_rate_label.set_visible(not is_tilt)
        self.analog_full_scale_rate.set_visible(not is_tilt)
        self.analog_angle_label.set_visible(is_tilt)
        self.analog_full_scale_angle.set_visible(is_tilt)
        name = self._selected_analog_control_name()
        config = self._available_analog_controls.get(name or "")
        is_stick = config is None or config.input_type == "stick"
        self.analog_y_label.set_visible(is_stick)
        self.analog_y_axis.set_visible(is_stick)
        self.analog_invert_y.set_visible(is_stick)

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
            button = self._output_target_buttons.get(gamepad_output_target_key(target, analog_id))
            if button is not None and button.get_active():
                return target
        return "right"

    def current_output_target_analog_id(self) -> str | None:
        for target, analog_id in self._output_target_items:
            button = self._output_target_buttons.get(gamepad_output_target_key(target, analog_id))
            if button is not None and button.get_active() and target == "analog":
                return analog_id
        return None

    def _on_output_target_toggled(self, button: Gtk.ToggleButton) -> None:
        if button.get_active():
            self._on_modified()

    def _update_output_warning(self) -> None:
        if self._active_mode not in _GAMEPAD_MODES:
            self.gamepad_output_warning_label.set_label("")
            self.gamepad_output_warning_row.set_visible(False)
            return
        message = update_gamepad_output_warning_label(
            self.gamepad_output_warning_label,
            self._selected_output_id,
            count_loader=self._output_count_loader,
        )
        self.gamepad_output_warning_row.set_visible(bool(message))
