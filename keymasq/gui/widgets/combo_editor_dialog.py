from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GObject, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.combos import (
    EMERGENCY_CANCEL_COMBO_LABEL,
    is_emergency_cancel_combo_evdevs,
    normalize_combo_evdev,
)
from keymasq.common.models import (
    PROTECTED_BUTTONS,
    ActionType,
    ComboConfig,
    ComboEvent,
    ComboStep,
    MappingAction,
)
from keymasq.gui.session_client import session_request_async
from keymasq.gui.widgets.action_labels import describe_mapping_action_compact
from keymasq.gui.widgets.key_selector_dialog import (
    EVDEV_TO_GAMEPAD,
    EVDEV_TO_KEY,
    KeySelectorDialog,
)
from keymasq.session.superkeys import SuperkeyManager

DEFAULT_STEP_TIMEOUT_MS = 600
MIN_STEP_TIMEOUT_MS = 50
MAX_STEP_TIMEOUT_MS = 5000


def new_combo_draft() -> ComboConfig:
    return ComboConfig(id=uuid4().hex[:8])


def sort_combo_keys(keys: list[str]) -> list[str]:
    modifier_order = {
        "ctrl": 0,
        "shift": 1,
        "alt": 2,
        "meta": 3,
        "key_menu": 4,
    }
    return sorted(
        [normalize_combo_evdev(key) for key in keys],
        key=lambda key: (modifier_order.get(key, 100), combo_key_label(key).casefold()),
    )


def combo_key_label(key: str) -> str:
    key = normalize_combo_evdev(key)
    if key in EVDEV_TO_KEY:
        return EVDEV_TO_KEY[key]
    if key in EVDEV_TO_GAMEPAD:
        return EVDEV_TO_GAMEPAD[key]
    if key == "ctrl":
        return "Ctrl"
    if key == "shift":
        return "Shift"
    if key == "alt":
        return "Alt"
    if key == "meta":
        return "Meta"
    if key == "wheel_up":
        return "Scroll Up"
    if key == "wheel_down":
        return "Scroll Down"
    if key == "wheel_left":
        return "Scroll Left"
    if key == "wheel_right":
        return "Scroll Right"

    token = key.upper()
    if token.startswith("KEY_"):
        token = token[4:]
    if token.startswith("BTN_"):
        token = token[4:]
    token = token.replace("LEFTCTRL", "LCtrl").replace("RIGHTCTRL", "RCtrl")
    token = token.replace("LEFTSHIFT", "LShift").replace("RIGHTSHIFT", "RShift")
    token = token.replace("LEFTALT", "LAlt").replace("RIGHTALT", "RAlt")
    token = token.replace("LEFTMETA", "LMeta").replace("RIGHTMETA", "RMeta")
    token = token.replace("PAGEUP", "PgUp").replace("PAGEDOWN", "PgDn")
    return token.replace("_", " ").title()


def combo_step_event_key(event: ComboEvent) -> str:
    return normalize_combo_evdev(event.evdev)


def combo_event_sort_key(event: ComboEvent) -> str:
    return sort_combo_keys([event.evdev])[0]


def combo_event_signature(event: ComboEvent) -> tuple[str, str, str]:
    return (
        str(event.hardware_id or "").lower(),
        str(event.source or "").lower(),
        normalize_combo_evdev(str(event.evdev or "").lower()),
    )


def combo_step_signature(step: ComboStep) -> tuple[tuple[str, str, str], ...]:
    return tuple(sorted(combo_event_signature(event) for event in step.events))


def combo_step_label(step: ComboStep) -> str:
    return "+".join(
        combo_key_label(key)
        for key in sort_combo_keys([combo_step_event_key(event) for event in step.events])
    )


def combo_trigger_label(steps: list[ComboStep]) -> str:
    return " -> ".join(combo_step_label(step) for step in steps if step.events)


def combo_is_emergency_cancel_trigger(steps: list[ComboStep]) -> bool:
    return (
        len(steps) == 1
        and bool(steps[0].events)
        and is_emergency_cancel_combo_evdevs(event.evdev for event in steps[0].events)
    )


def combo_is_single_critical_mouse_trigger(steps: list[ComboStep]) -> bool:
    return (
        len(steps) == 1
        and len(steps[0].events) == 1
        and normalize_combo_evdev(steps[0].events[0].evdev) in PROTECTED_BUTTONS
    )


def combo_restore_key_options(steps: list[ComboStep]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for step in steps:
        for key in sort_combo_keys([combo_step_event_key(event) for event in step.events]):
            if key in seen:
                continue
            seen.add(key)
            ordered.append(key)
    return ordered


def combo_action_label(action: MappingAction | None) -> str:
    if action is None:
        return "Action"
    if action.action_type == ActionType.KEYBOARD:
        return combo_key_label(action.target or "?")
    if action.action_type == ActionType.MOUSE:
        return combo_key_label(action.target or "?")
    if action.action_type == ActionType.GAMEPAD:
        return combo_key_label(action.target or "?")
    if action.action_type == ActionType.GAMEPAD_AXIS:
        return f"{action.target or '?'}={int(action.axis_value)}"
    if action.action_type == ActionType.EXEC:
        return action.cmd or "Exec"
    if action.action_type == ActionType.COMPOSITOR_DISPATCH:
        dispatcher = action.compositor_dispatcher or "Compositor"
        args = str(action.compositor_args or "").strip()
        return f"{dispatcher} {args}".strip()
    if action.action_type == ActionType.SUPPRESS:
        return "Suppress"
    if action.action_type == ActionType.MACRO:
        return action.macro_name or "Macro"
    if action.action_type == ActionType.PROFILE_ENABLE:
        return f"Enable {action.profile_name or '?'}"
    if action.action_type == ActionType.PROFILE_DISABLE:
        return f"Disable {action.profile_name or '?'}"
    if action.action_type == ActionType.PROFILE_TOGGLE:
        return f"Toggle {action.profile_name or '?'}"
    if action.action_type == ActionType.START_MACRO_RECORDING:
        return "Start Recording"
    if action.action_type == ActionType.STOP_MACRO_RECORDING:
        return "Stop Recording"
    if action.action_type == ActionType.CANCEL_MACRO_PLAYBACK:
        return "Cancel Playback"
    if action.action_type == ActionType.EMERGENCY_RESET:
        return "Emergency Reset"
    if action.action_type == ActionType.MOUSE_MOVE_REL:
        return f"Move {action.move_x}, {action.move_y}"
    if action.action_type == ActionType.MOUSE_MOVE_ABS:
        return f"Move Abs {action.move_x}, {action.move_y}"
    if action.action_type == ActionType.SUPERKEY:
        return action.superkey_name or "Super Key"
    return action.action_type.value.replace("_", " ").title()


def combo_default_name(combo: ComboConfig) -> str:
    trigger = combo_trigger_label(combo.steps)
    action = combo_action_label(combo.action)
    if trigger and action:
        return f"{trigger} -> {action}"
    if trigger:
        return trigger
    if action:
        return action
    return "Combo"


def describe_mapping_action(action: MappingAction | None) -> str:
    return describe_mapping_action_compact(action)


class ComboEditorDialog(Adw.Dialog):
    __gsignals__ = {
        "combo-saved": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(
        self,
        parent: Gtk.Widget,
        combo: ComboConfig | None = None,
        profile_name: str | None = None,
        sibling_combos: list[ComboConfig] | None = None,
        emergency_cancel_combo_enabled: bool = True,
    ) -> None:
        title = "Edit Combo" if combo else "Add Combo"
        super().__init__(title=title, content_width=720, content_height=840)
        self._parent = parent
        self._draft = deepcopy(combo) if combo else new_combo_draft()
        self._profile_name = profile_name
        self._sibling_combos = deepcopy(sibling_combos or [])
        self._emergency_cancel_combo_enabled = bool(emergency_cancel_combo_enabled)
        self._recording_unlocked = False
        self._capture_inflight = False
        self._validation_message = ""
        self._restore_trigger_key_rows: list[Gtk.ListBoxRow] = []
        self._restore_trigger_key_labels: dict[str, Gtk.Label] = {}
        self._restore_trigger_key_buttons: dict[str, Gtk.CheckButton] = {}

        self._normalize_step_timeouts()
        self._normalize_restore_trigger_keys()
        self._setup_ui()
        self._refresh_trigger_display()
        self._update_action_summary()
        self._update_save_button()
        self._refresh_authorization_state_async()
        self.connect("closed", self._on_closed)

    def _setup_ui(self) -> None:
        toolbar_view = Adw.ToolbarView()

        header_bar = Adw.HeaderBar()
        header_bar.set_show_end_title_buttons(False)
        header_bar.set_show_start_title_buttons(False)

        cancel_button = Gtk.Button(label="Cancel")
        cancel_button.connect("clicked", self._on_cancel_clicked)
        header_bar.pack_start(cancel_button)

        self.save_button = Gtk.Button(label="Save")
        self.save_button.add_css_class("suggested-action")
        self.save_button.connect("clicked", self._on_save_clicked)
        header_bar.pack_end(self.save_button)

        toolbar_view.add_top_bar(header_bar)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        content.set_margin_top(20)
        content.set_margin_bottom(20)
        content.set_margin_start(24)
        content.set_margin_end(24)

        name_group = Adw.PreferencesGroup(title="Name", description="Auto-generated if left empty")
        self.name_entry = Gtk.Entry()
        self.name_entry.set_placeholder_text("e.g. Quick Save")
        self.name_entry.set_hexpand(True)
        self.name_entry.set_text(self._draft.name)
        self.name_entry.connect("changed", self._on_name_changed)
        name_group.add(self.name_entry)
        content.append(name_group)

        trigger_group = Adw.PreferencesGroup(
            title="Trigger",
            description="Define the input sequence that should activate this combo.",
        )
        trigger_inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)

        top_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        self.add_step_button = Gtk.Button(label="Capture Step")
        self.add_step_button.add_css_class("suggested-action")
        self.add_step_button.connect("clicked", self._on_add_step_clicked)
        top_row.append(self.add_step_button)

        self.unlock_button = Gtk.Button()
        self.unlock_button.set_child(self._make_unlock_button_content())
        self.unlock_button.set_tooltip_text(
            "Authorize raw original-input capture so combo capture can read the actual "
            "keys and buttons before remapping."
        )
        self.unlock_button.add_css_class("flat")
        self.unlock_button.connect("clicked", self._on_unlock_clicked)
        top_row.append(self.unlock_button)

        clear_button = Gtk.Button(label="Clear")
        clear_button.add_css_class("flat")
        clear_button.connect("clicked", self._on_clear_clicked)
        top_row.append(clear_button)

        trigger_inner.append(top_row)

        self.steps_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        trigger_inner.append(self.steps_box)

        self.validation_label = Gtk.Label()
        self.validation_label.add_css_class("combo-error-label")
        self.validation_label.add_css_class("caption")
        self.validation_label.set_halign(Gtk.Align.START)
        self.validation_label.set_wrap(True)
        self.validation_label.set_visible(False)
        trigger_inner.append(self.validation_label)

        self.capture_status = Gtk.Label(label="Add a step, then press the keys for that step.")
        self.capture_status.add_css_class("dim-label")
        self.capture_status.add_css_class("caption")
        self.capture_status.set_halign(Gtk.Align.START)
        self.capture_status.set_wrap(True)
        trigger_inner.append(self.capture_status)

        self.capture_privilege_status = Gtk.Label(
            label="Capture reads original key events before remapping."
        )
        self.capture_privilege_status.add_css_class("dim-label")
        self.capture_privilege_status.add_css_class("caption")
        self.capture_privilege_status.set_halign(Gtk.Align.START)
        self.capture_privilege_status.set_wrap(True)
        trigger_inner.append(self.capture_privilege_status)

        trigger_group.add(trigger_inner)
        content.append(trigger_group)

        action_group = Adw.PreferencesGroup(
            title="Action",
            description="Choose what should run when the combo matches.",
        )
        self.action_list = Gtk.ListBox()
        self.action_list.add_css_class("boxed-list")
        self.action_list.set_selection_mode(Gtk.SelectionMode.NONE)

        self.action_row = Gtk.ListBoxRow()
        self.action_row.set_activatable(False)

        action_row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        action_row_box.add_css_class("combo-compact-row")
        action_row_box.set_margin_top(8)
        action_row_box.set_margin_bottom(8)
        action_row_box.set_margin_start(12)
        action_row_box.set_margin_end(12)

        self.action_summary_label = Gtk.Label(label="No action selected")
        self.action_summary_label.set_xalign(0.0)
        self.action_summary_label.set_hexpand(True)
        self.action_summary_label.set_wrap(True)
        self.action_summary_label.add_css_class("dim-label")
        action_row_box.append(self.action_summary_label)

        self.select_action_button = Gtk.Button(label="Choose...")
        self.select_action_button.connect("clicked", self._on_select_action_clicked)
        action_row_box.append(self.select_action_button)

        self.action_row.set_child(action_row_box)
        self.action_list.append(self.action_row)
        action_group.add(self.action_list)
        content.append(action_group)

        trigger_state_group = Adw.PreferencesGroup(
            title="Trigger State",
            description="Optional handling for trigger keys that would otherwise stay active.",
        )

        self.recall_trigger_keys_row = Adw.SwitchRow(
            title="Recall Trigger Keys",
            subtitle="Release this combo's trigger keys before the action runs.",
        )
        self.recall_trigger_keys_row.set_active(self._draft.recall_trigger_keys)
        self.recall_trigger_keys_row.connect(
            "notify::active",
            self._on_recall_trigger_keys_changed,
        )
        trigger_state_group.add(self.recall_trigger_keys_row)
        content.append(trigger_state_group)

        self.restore_trigger_keys_group = Adw.PreferencesGroup(
            title="Restore Keys",
            description=(
                "Re-press selected trigger keys if they are still held when the combo ends."
            ),
        )
        self.restore_trigger_keys_list = Gtk.ListBox()
        self.restore_trigger_keys_list.add_css_class("boxed-list")
        self.restore_trigger_keys_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.restore_trigger_keys_group.add(self.restore_trigger_keys_list)
        content.append(self.restore_trigger_keys_group)

        scrolled.set_child(content)
        toolbar_view.set_content(scrolled)
        self.set_child(toolbar_view)
        self._update_capture_controls()
        self._refresh_restore_trigger_keys()

    def _on_name_changed(self, entry: Gtk.Entry) -> None:
        self._draft.name = entry.get_text().strip()
        self._update_save_button()

    def _on_add_step_clicked(self, _button: Gtk.Button) -> None:
        if self._capture_inflight:
            return
        if not self._recording_unlocked:
            self.capture_status.set_text("Unlock required before capturing original input.")
            self._update_capture_controls()
            return
        if not self._profile_name:
            self.capture_status.set_text("Select a profile before capturing a combo.")
            return
        self._capture_inflight = True
        self.capture_status.set_text("Waiting for the next combo...")
        self._update_capture_controls()
        session_request_async(
            {
                "command": "capture_combo",
                "profile_name": self._profile_name,
                "timeout_s": 15.0,
            },
            self._on_capture_combo_response,
            timeout=20.0,
        )

    def _on_clear_clicked(self, _button: Gtk.Button) -> None:
        self._draft.steps = []
        self._normalize_step_timeouts()
        self._normalize_restore_trigger_keys()
        self.capture_status.set_text("Trigger cleared.")
        self._refresh_trigger_display()
        self._update_save_button()

    def _on_select_action_clicked(self, _button: Gtk.Button) -> None:
        dialog = KeySelectorDialog(self, "Combo Action", self._draft.action)
        dialog.connect("key-selected", self._on_action_selected)
        dialog.present(self)

    def _on_action_selected(self, _dialog: KeySelectorDialog, action: MappingAction | None) -> None:
        self._draft.action = deepcopy(action) if action is not None else None
        self._update_action_summary()
        self._update_save_button()

    def _on_save_clicked(self, _button: Gtk.Button) -> None:
        if not self.save_button.get_sensitive():
            return
        if combo_is_single_critical_mouse_trigger(self._draft.steps):
            self._show_critical_mouse_combo_warning()
            return
        self._save_draft()

    def _save_draft(self) -> None:
        name = self.name_entry.get_text().strip()
        self._draft.name = name or combo_default_name(self._draft)
        self.emit("combo-saved", deepcopy(self._draft))
        self.close()

    def _show_critical_mouse_combo_warning(self) -> None:
        trigger = combo_trigger_label(self._draft.steps) or "this button"
        dialog = Adw.AlertDialog(
            heading="Remap Critical Mouse Button?",
            body=(
                f"{trigger} is a critical pointer button. Using it as a single-button "
                "combo can remove your normal left or right click <b>everywhere</b>.\n\n"
                "Continue only if you have a reliable recovery path, such as another "
                "mouse, keyboard navigation, or direct access to the profile files."
            ),
        )
        dialog.set_body_use_markup(True)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("continue", "Continue")
        dialog.set_response_appearance("continue", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.connect("response", self._on_critical_mouse_combo_warning_response)
        dialog.present(self)

    def _on_critical_mouse_combo_warning_response(
        self,
        _dialog: Adw.AlertDialog,
        response: str,
    ) -> None:
        if response == "continue":
            self._save_draft()

    def _refresh_trigger_display(self) -> None:
        while child := self.steps_box.get_first_child():
            self.steps_box.remove(child)

        if not self._draft.steps:
            empty = Gtk.Label(label="No trigger defined yet.")
            empty.add_css_class("dim-label")
            empty.set_halign(Gtk.Align.START)
            self.steps_box.append(empty)
            return

        for index, step in enumerate(self._draft.steps):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row.add_css_class("combo-step-row")

            step_num = Gtk.Label(label=str(index + 1))
            step_num.add_css_class("dim-label")
            step_num.add_css_class("caption")
            step_num.set_size_request(16, -1)
            row.append(step_num)

            pill = Gtk.Label(label=combo_step_label(step))
            pill.add_css_class("combo-step-pill")
            row.append(pill)

            spacer = Gtk.Box()
            spacer.set_hexpand(True)
            row.append(spacer)

            if index > 0:
                adjustment = Gtk.Adjustment(
                    value=float(step.timeout_ms or DEFAULT_STEP_TIMEOUT_MS),
                    lower=MIN_STEP_TIMEOUT_MS,
                    upper=MAX_STEP_TIMEOUT_MS,
                    step_increment=50.0,
                    page_increment=100.0,
                    page_size=0.0,
                )
                timeout_spin = Gtk.SpinButton(adjustment=adjustment, climb_rate=0.0, digits=0)
                timeout_spin.set_numeric(True)
                timeout_spin.set_width_chars(5)
                timeout_spin.connect("value-changed", self._on_timeout_changed, index)
                row.append(timeout_spin)

                ms_label = Gtk.Label(label="ms")
                ms_label.add_css_class("dim-label")
                ms_label.add_css_class("caption")
                row.append(ms_label)

            remove_button = Gtk.Button(icon_name="window-close-symbolic")
            remove_button.add_css_class("flat")
            remove_button.set_tooltip_text("Remove step")
            remove_button.connect("clicked", self._on_remove_step_clicked, index)
            row.append(remove_button)

            self.steps_box.append(row)

        self._refresh_restore_trigger_keys()

    def _on_remove_step_clicked(self, _button: Gtk.Button, index: int) -> None:
        if 0 <= index < len(self._draft.steps):
            self._draft.steps.pop(index)
            self._normalize_step_timeouts()
            self._normalize_restore_trigger_keys()
            self._refresh_trigger_display()
            self._update_save_button()

    def _on_timeout_changed(self, spin: Gtk.SpinButton, index: int) -> None:
        if index <= 0 or index >= len(self._draft.steps):
            return
        self._draft.steps[index].timeout_ms = int(spin.get_value_as_int())
        self._update_save_button()

    def _update_action_summary(self) -> None:
        if self._draft.action is None:
            self.action_summary_label.set_text("No action selected")
            self.action_summary_label.add_css_class("dim-label")
            self.select_action_button.set_label("Choose...")
            return

        self.action_summary_label.set_text(describe_mapping_action(self._draft.action))
        self.action_summary_label.remove_css_class("dim-label")
        self.select_action_button.set_label("Change...")

    def _update_save_button(self) -> None:
        self._normalize_step_timeouts()
        self._validation_message = self._validate_draft()
        self.validation_label.set_text(self._validation_message)
        self.validation_label.set_visible(bool(self._validation_message))
        self.save_button.set_sensitive(not self._validation_message)

    def _normalize_step_timeouts(self) -> None:
        for index, step in enumerate(self._draft.steps):
            if index == 0:
                step.timeout_ms = None
            elif step.timeout_ms is None:
                step.timeout_ms = DEFAULT_STEP_TIMEOUT_MS

    def _normalize_restore_trigger_keys(self) -> None:
        available = set(combo_restore_key_options(self._draft.steps))
        self._draft.restore_trigger_keys = [
            key for key in self._draft.restore_trigger_keys if key in available
        ]

    def _refresh_restore_trigger_keys(self) -> None:
        options = combo_restore_key_options(self._draft.steps)
        self.restore_trigger_keys_group.set_visible(
            self._draft.recall_trigger_keys and bool(options)
        )

        for row in self._restore_trigger_key_rows:
            self.restore_trigger_keys_list.remove(row)
        self._restore_trigger_key_rows.clear()
        self._restore_trigger_key_labels.clear()
        self._restore_trigger_key_buttons.clear()

        if not self._draft.recall_trigger_keys or not options:
            return

        for key in options:
            row = Gtk.ListBoxRow()
            row.set_activatable(False)

            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            row_box.add_css_class("combo-compact-row")
            row_box.set_margin_top(6)
            row_box.set_margin_bottom(6)
            row_box.set_margin_start(12)
            row_box.set_margin_end(12)

            label = Gtk.Label(label=combo_key_label(key))
            label.set_xalign(0.0)
            label.set_hexpand(True)
            row_box.append(label)

            check = Gtk.CheckButton()
            check.set_valign(Gtk.Align.CENTER)
            check.set_active(key in self._draft.restore_trigger_keys)
            check.connect("toggled", self._on_restore_trigger_key_toggled, key)
            row_box.append(check)

            row.set_child(row_box)
            self.restore_trigger_keys_list.append(row)
            self._restore_trigger_key_rows.append(row)
            self._restore_trigger_key_labels[key] = label
            self._restore_trigger_key_buttons[key] = check

    def _on_recall_trigger_keys_changed(
        self,
        row: Adw.SwitchRow,
        _pspec: GObject.ParamSpec,
    ) -> None:
        self._draft.recall_trigger_keys = bool(row.get_active())
        self._refresh_restore_trigger_keys()
        self._update_save_button()

    def _on_restore_trigger_key_toggled(
        self,
        button: Gtk.CheckButton,
        key: str,
    ) -> None:
        if button.get_active():
            if key not in self._draft.restore_trigger_keys:
                self._draft.restore_trigger_keys.append(key)
        else:
            self._draft.restore_trigger_keys = [
                existing for existing in self._draft.restore_trigger_keys if existing != key
            ]
        self._update_save_button()

    def _validate_draft(self) -> str:
        if not self._draft.steps:
            return "Add at least one combo step."
        if self._draft.action is None:
            return "Select an action."
        if self._draft.action.action_type == ActionType.SUPERKEY:
            superkey_name = str(self._draft.action.superkey_name or "").strip()
            if not superkey_name:
                return "Select a saved Super Key."
            if SuperkeyManager().get_superkey(superkey_name) is None:
                return "The selected Super Key could not be loaded."
        for index, step in enumerate(self._draft.steps[1:], start=1):
            timeout_ms = step.timeout_ms
            if timeout_ms is None:
                return f"Step {index + 1} timeout is required."
            if timeout_ms < MIN_STEP_TIMEOUT_MS or timeout_ms > MAX_STEP_TIMEOUT_MS:
                return (
                    f"Step {index + 1} timeout must be between "
                    f"{MIN_STEP_TIMEOUT_MS} and {MAX_STEP_TIMEOUT_MS} ms."
                )
        if self._emergency_cancel_combo_enabled and combo_is_emergency_cancel_trigger(
            self._draft.steps
        ):
            return (
                f"{EMERGENCY_CANCEL_COMBO_LABEL} is reserved for emergency "
                "macro playback cancellation."
            )
        if self._is_exact_duplicate_of_sibling():
            return "A combo with the same trigger already exists in this profile."
        return ""

    def _is_exact_duplicate_of_sibling(self) -> bool:
        current_steps = [combo_step_signature(step) for step in self._draft.steps]
        if not current_steps:
            return False
        for combo in self._sibling_combos:
            if combo.id == self._draft.id:
                continue
            sibling_steps = [combo_step_signature(step) for step in combo.steps]
            if sibling_steps == current_steps:
                return True
        return False

    def _refresh_authorization_state_async(self, *_args) -> None:
        session_request_async({"command": "get_status"}, self._on_status_response, timeout=1.0)

    def _on_status_response(self, result: dict | None) -> bool:
        result = result or {}
        unlock_required = bool(result.get("recording_unlock_required", True))
        self._recording_unlocked = (
            bool(result.get("recording_unlocked", False)) or not unlock_required
        )
        self._update_capture_controls()
        return False

    def _update_capture_controls(self) -> None:
        needs_unlock = not self._recording_unlocked and not self._capture_inflight
        if self._capture_inflight:
            self.add_step_button.set_label("Capturing...")
            self.add_step_button.set_sensitive(False)
            self.add_step_button.set_visible(True)
        else:
            self.add_step_button.set_label("Capture Step")
            self.add_step_button.set_sensitive(True)
            self.add_step_button.set_visible(not needs_unlock)

        self.unlock_button.set_visible(needs_unlock)
        self.unlock_button.set_tooltip_text(
            "Authorize raw original-input capture so combo capture can read the actual "
            "keys and buttons before remapping."
        )
        if self._recording_unlocked:
            self.capture_privilege_status.set_text(
                "Original-input capture is unlocked. Capture reads raw key events before remapping."
            )
        else:
            self.capture_privilege_status.set_text(
                "Original-input capture uses privileged raw events. "
                "Unlock to capture original keys."
            )

    def _make_unlock_button_content(self) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        icon = Gtk.Image.new_from_icon_name("channel-insecure-symbolic")
        box.append(icon)
        lbl = Gtk.Label(label="Unlock Capture")
        box.append(lbl)
        return box

    def _on_unlock_clicked(self, _button: Gtk.Button) -> None:
        root = self.get_root()
        present_unlock = getattr(root, "present_unlock_dialog", None)
        if callable(present_unlock):
            present_unlock(on_success=self._refresh_authorization_state_async)
            return
        self.capture_status.set_text("Unlock is only available from the main window.")

    def _on_capture_combo_response(self, result: dict | None) -> bool:
        self._capture_inflight = False
        if not result or result.get("status") != "ok":
            self.capture_status.set_text(
                (result or {}).get("message", "Combo capture failed: session unavailable")
            )
            if result and self._is_recording_locked(result):
                self._recording_unlocked = False
            self._update_capture_controls()
            return False

        events_data = result.get("events")
        if not isinstance(events_data, list) or not events_data:
            self.capture_status.set_text("Combo capture returned no events.")
            self._update_capture_controls()
            return False

        events = []
        for item in events_data:
            if not isinstance(item, dict):
                continue
            evdev = str(item.get("evdev", "") or "")
            hardware_id = str(item.get("hardware_id", "") or "")
            if not evdev or not hardware_id:
                continue
            source_raw = item.get("source")
            source = str(source_raw) if source_raw is not None else None
            events.append(
                ComboEvent(
                    evdev=normalize_combo_evdev(evdev),
                    hardware_id=hardware_id,
                    source=source,
                )
            )
        if not events:
            self.capture_status.set_text("Combo capture returned no valid events.")
            self._update_capture_controls()
            return False

        events.sort(key=combo_event_sort_key)
        step = ComboStep(
            events=events,
            timeout_ms=None if not self._draft.steps else DEFAULT_STEP_TIMEOUT_MS,
        )
        self._draft.steps.append(step)
        self._normalize_step_timeouts()
        self._normalize_restore_trigger_keys()
        warnings = result.get("warnings") or []
        if warnings:
            warning_text = ", ".join(str(warning) for warning in warnings)
            self.capture_status.set_text(f"Added step: {combo_step_label(step)} ({warning_text})")
        else:
            self.capture_status.set_text(f"Added step: {combo_step_label(step)}")
        self._refresh_trigger_display()
        self._update_save_button()
        self._update_capture_controls()
        return False

    def _on_closed(self, _dialog: Adw.Dialog) -> None:
        self._capture_inflight = False

    def _on_cancel_clicked(self, _button: Gtk.Button) -> None:
        self.close()

    def _is_recording_locked(self, result: dict) -> bool:
        if result.get("error_code") == "recording_locked":
            return True
        message = str(result.get("message", "") or "").lower()
        return "recording_locked" in message
