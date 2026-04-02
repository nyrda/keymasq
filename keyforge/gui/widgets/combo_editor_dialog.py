from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GObject, Gtk

from keyforge.common.combos import normalize_combo_evdev
from keyforge.common.models import (
    ActionType,
    ComboConfig,
    ComboEvent,
    ComboStep,
    MappingAction,
)
from keyforge.gui.session_client import session_request_async
from keyforge.gui.widgets.action_labels import describe_mapping_action_compact
from keyforge.gui.widgets.key_selector_dialog import (
    EVDEV_TO_GAMEPAD,
    EVDEV_TO_KEY,
    KeySelectorDialog,
)

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


def combo_action_label(action: MappingAction | None) -> str:
    if action is None:
        return "Action"
    if action.action_type == ActionType.KEYBOARD:
        return combo_key_label(action.target or "?")
    if action.action_type == ActionType.MOUSE:
        return combo_key_label(action.target or "?")
    if action.action_type == ActionType.GAMEPAD:
        return combo_key_label(action.target or "?")
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
    ) -> None:
        title = "Edit Combo" if combo else "Add Combo"
        super().__init__(title=title, content_width=560, content_height=520)
        self._parent = parent
        self._draft = deepcopy(combo) if combo else new_combo_draft()
        self._profile_name = profile_name
        self._sibling_combos = deepcopy(sibling_combos or [])
        self._recording_unlocked = False
        self._capture_inflight = False
        self._validation_message = ""

        self._normalize_step_timeouts()
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
        cancel_button.connect("clicked", lambda _btn: self.close())
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

        main_group = Adw.PreferencesGroup(title="Trigger & Action")
        main_inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)

        top_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        self.add_step_button = Gtk.Button(label="Capture Step")
        self.add_step_button.add_css_class("suggested-action")
        self.add_step_button.connect("clicked", self._on_add_step_clicked)
        top_row.append(self.add_step_button)

        self.unlock_button = Gtk.Button(label="Unlock Capture")
        self.unlock_button.connect("clicked", self._on_unlock_clicked)
        top_row.append(self.unlock_button)

        clear_button = Gtk.Button(label="Clear")
        clear_button.add_css_class("flat")
        clear_button.connect("clicked", self._on_clear_clicked)
        top_row.append(clear_button)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        top_row.append(spacer)

        self.action_summary = Gtk.Label()
        self.action_summary.set_xalign(1)
        top_row.append(self.action_summary)

        select_action_button = Gtk.Button(label="Select Action")
        select_action_button.add_css_class("suggested-action")
        select_action_button.connect("clicked", self._on_select_action_clicked)
        top_row.append(select_action_button)

        main_inner.append(top_row)

        self.steps_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        main_inner.append(self.steps_box)

        self.validation_label = Gtk.Label()
        self.validation_label.add_css_class("combo-error-label")
        self.validation_label.add_css_class("caption")
        self.validation_label.set_halign(Gtk.Align.START)
        self.validation_label.set_wrap(True)
        self.validation_label.set_visible(False)
        main_inner.append(self.validation_label)

        self.capture_status = Gtk.Label(label="Add a step, then press the keys for that step.")
        self.capture_status.add_css_class("dim-label")
        self.capture_status.add_css_class("caption")
        self.capture_status.set_halign(Gtk.Align.START)
        self.capture_status.set_wrap(True)
        main_inner.append(self.capture_status)

        self.capture_privilege_status = Gtk.Label(
            label="Original-input capture uses privileged raw events from keyforged."
        )
        self.capture_privilege_status.add_css_class("dim-label")
        self.capture_privilege_status.add_css_class("caption")
        self.capture_privilege_status.set_halign(Gtk.Align.START)
        self.capture_privilege_status.set_wrap(True)
        main_inner.append(self.capture_privilege_status)

        main_group.add(main_inner)
        content.append(main_group)

        scrolled.set_child(content)
        toolbar_view.set_content(scrolled)
        self.set_child(toolbar_view)
        self._update_capture_controls()

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
        self.capture_status.set_text("Trigger cleared.")
        self._refresh_trigger_display()
        self._update_save_button()

    def _on_select_action_clicked(self, _button: Gtk.Button) -> None:
        dialog = KeySelectorDialog(self, "Combo Action", self._draft.action)
        dialog.connect("key-selected", self._on_action_selected)
        dialog.present(self.get_root())

    def _on_action_selected(self, _dialog: KeySelectorDialog, action: MappingAction | None) -> None:
        self._draft.action = deepcopy(action) if action is not None else None
        self._update_action_summary()
        self._update_save_button()

    def _on_save_clicked(self, _button: Gtk.Button) -> None:
        if not self.save_button.get_sensitive():
            return
        name = self.name_entry.get_text().strip()
        self._draft.name = name or combo_default_name(self._draft)
        self.emit("combo-saved", deepcopy(self._draft))
        self.close()

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

    def _on_remove_step_clicked(self, _button: Gtk.Button, index: int) -> None:
        if 0 <= index < len(self._draft.steps):
            self._draft.steps.pop(index)
            self._normalize_step_timeouts()
            self._refresh_trigger_display()
            self._update_save_button()

    def _on_timeout_changed(self, spin: Gtk.SpinButton, index: int) -> None:
        if index <= 0 or index >= len(self._draft.steps):
            return
        self._draft.steps[index].timeout_ms = int(spin.get_value_as_int())
        self._update_save_button()

    def _update_action_summary(self) -> None:
        self.action_summary.set_text(describe_mapping_action(self._draft.action))
        if self._draft.action is None:
            self.action_summary.add_css_class("dim-label")
        else:
            self.action_summary.remove_css_class("dim-label")

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

    def _validate_draft(self) -> str:
        if not self._draft.steps:
            return "Add at least one combo step."
        if self._draft.action is None:
            return "Select an action."
        if self._draft.action.action_type == ActionType.SUPERKEY:
            return "Super Key actions are not supported for combos yet."
        for index, step in enumerate(self._draft.steps[1:], start=1):
            timeout_ms = step.timeout_ms
            if timeout_ms is None:
                return f"Step {index + 1} timeout is required."
            if timeout_ms < MIN_STEP_TIMEOUT_MS or timeout_ms > MAX_STEP_TIMEOUT_MS:
                return (
                    f"Step {index + 1} timeout must be between "
                    f"{MIN_STEP_TIMEOUT_MS} and {MAX_STEP_TIMEOUT_MS} ms."
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
        self._recording_unlocked = bool(
            result.get("recording_unlocked", False)
        ) or not unlock_required
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
        if self._recording_unlocked:
            self.capture_privilege_status.set_text(
                "Original-input capture is unlocked. Capture reads raw key events before remapping."
            )
        else:
            self.capture_privilege_status.set_text(
                "Original-input capture uses privileged raw events. "
                "Unlock to capture original keys."
            )

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

        events.sort(key=lambda event: sort_combo_keys([event.evdev])[0])
        step = ComboStep(
            events=events,
            timeout_ms=None if not self._draft.steps else DEFAULT_STEP_TIMEOUT_MS,
        )
        self._draft.steps.append(step)
        self._normalize_step_timeouts()
        warnings = result.get("warnings") or []
        if warnings:
            warning_text = ", ".join(str(warning) for warning in warnings)
            self.capture_status.set_text(
                f"Added step: {combo_step_label(step)} ({warning_text})"
            )
        else:
            self.capture_status.set_text(f"Added step: {combo_step_label(step)}")
        self._refresh_trigger_display()
        self._update_save_button()
        self._update_capture_controls()
        return False

    def _on_closed(self, _dialog: Adw.Dialog) -> None:
        self._capture_inflight = False

    def _is_recording_locked(self, result: dict) -> bool:
        if result.get("error_code") == "recording_locked":
            return True
        message = str(result.get("message", "") or "").lower()
        return "recording_locked" in message
