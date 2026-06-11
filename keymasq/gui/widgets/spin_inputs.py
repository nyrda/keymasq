from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import (  # pyright: ignore[reportAttributeAccessIssue]
    Adw,  # pyright: ignore[reportAttributeAccessIssue]
    Gdk,  # pyright: ignore[reportAttributeAccessIssue]
    GLib,  # pyright: ignore[reportAttributeAccessIssue]
    Gtk,  # pyright: ignore[reportAttributeAccessIssue]
)

SPLIT_DESYNC_MODIFIERS = Gdk.ModifierType.SHIFT_MASK | Gdk.ModifierType.CONTROL_MASK
SPLIT_DESYNC_KEYS = {
    Gdk.KEY_Shift_L,
    Gdk.KEY_Shift_R,
    Gdk.KEY_Control_L,
    Gdk.KEY_Control_R,
}


def spin_row(
    title: str,
    value: float,
    lower: float,
    upper: float,
    step: float,
    digits: int,
    *,
    page_step: float | None = None,
    on_changed: Callable[..., None] | None = None,
) -> Adw.SpinRow:
    row = Adw.SpinRow(
        title=title,
        adjustment=Gtk.Adjustment(
            value=value,
            lower=lower,
            upper=upper,
            step_increment=step,
            page_increment=page_step if page_step is not None else step,
        ),
        digits=digits,
    )
    if on_changed is not None:
        row.connect("notify::value", on_changed)
    return row


def compact_int_entry(
    value: int,
    controller: "CompactIntEntryController",
) -> Gtk.Entry:
    entry = Gtk.Entry()
    entry.set_text(str(int(value)))
    entry.set_width_chars(6)
    entry.set_max_width_chars(6)
    entry.set_alignment(0.5)
    entry.set_input_purpose(Gtk.InputPurpose.NUMBER)
    key_controller = Gtk.EventControllerKey()
    key_controller.connect("key-pressed", controller.on_key_pressed, entry)
    entry.add_controller(key_controller)
    entry.connect("changed", controller.on_changed)
    return entry


def int_entry_key_pressed(
    _controller: Gtk.EventControllerKey,
    keyval: int,
    _keycode: int,
    state: Gdk.ModifierType,
    entry: Gtk.Entry,
) -> bool:
    if state & (
        Gdk.ModifierType.CONTROL_MASK
        | Gdk.ModifierType.ALT_MASK
        | Gdk.ModifierType.META_MASK
    ):
        return False
    codepoint = Gdk.keyval_to_unicode(keyval)
    if codepoint == 0:
        return False
    char = chr(codepoint)
    if char.isdigit():
        return False
    if char == "-":
        return entry.get_position() != 0 or entry.get_text().startswith("-")
    return char.isprintable()


def sanitize_int_entry_text(text: str) -> str:
    stripped = str(text)
    prefix = "-" if stripped.startswith("-") else ""
    digits = "".join(ch for ch in stripped[len(prefix) :] if ch.isdigit())
    return f"{prefix}{digits}"


def entry_int_value(entry: Gtk.Entry) -> int:
    try:
        return int(entry.get_text().strip())
    except ValueError:
        return 0


def set_entry_int(entry: Gtk.Entry, value: int) -> None:
    entry.set_text(str(int(value)))


class CompactIntEntryController:
    def __init__(self, on_modified: Callable[..., None]) -> None:
        self._on_modified = on_modified
        self._syncing = False

    @property
    def syncing(self) -> bool:
        return self._syncing

    def on_key_pressed(
        self,
        controller: Gtk.EventControllerKey,
        keyval: int,
        keycode: int,
        state: Gdk.ModifierType,
        entry: Gtk.Entry,
    ) -> bool:
        return int_entry_key_pressed(controller, keyval, keycode, state, entry)

    def on_changed(self, entry: Gtk.Entry) -> None:
        if self._syncing:
            return
        text = entry.get_text()
        sanitized = sanitize_int_entry_text(text)
        if sanitized != text:
            GLib.idle_add(self.apply_sanitized_text, entry)
        self._on_modified()

    def apply_sanitized_text(self, entry: Gtk.Entry) -> bool:
        sanitized = sanitize_int_entry_text(entry.get_text())
        if sanitized != entry.get_text():
            self._syncing = True
            try:
                entry.set_text(sanitized)
                entry.set_position(len(sanitized))
            finally:
                self._syncing = False
        return False


class SplitAxisDesyncController:
    def __init__(self) -> None:
        self.axis: str | None = None
        self.clear_id = 0
        self._active_modifier_keys: set[int] = set()

    @property
    def modifier_active(self) -> bool:
        return bool(self._active_modifier_keys)

    def set_modifier_key(self, keyval: int, active: bool) -> bool:
        if keyval not in SPLIT_DESYNC_KEYS:
            return False
        if active:
            self._active_modifier_keys.add(keyval)
        else:
            self._active_modifier_keys.discard(keyval)
        return True

    def add_click_controller(self, row: Adw.SpinRow, axis: str) -> None:
        click = Gtk.GestureClick()
        click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        click.connect("pressed", self._on_click, axis)
        click.connect("released", self._on_click, axis)
        row.add_controller(click)

    def _on_click(
        self,
        gesture: Gtk.GestureClick,
        _n_press: int,
        _x: float,
        _y: float,
        axis: str,
    ) -> None:
        try:
            state = gesture.get_current_event_state()
        except (RuntimeError, TypeError, ValueError):
            state = Gdk.ModifierType(0)
        if state & SPLIT_DESYNC_MODIFIERS:
            self.request(axis)

    def request(self, axis: str) -> None:
        self.axis = axis
        if self.clear_id:
            GLib.source_remove(self.clear_id)
        self.clear_id = GLib.idle_add(self.clear, axis)

    def clear(self, axis: str | None = None) -> bool:
        if axis is None or self.axis == axis:
            self.axis = None
        self.clear_id = 0
        return False

    def requested(self, axis: str) -> bool:
        return self.modifier_active or self.axis == axis


def add_spin_secondary_step_controller(
    row: Adw.SpinRow,
    *,
    page_step: float | None = None,
    reset_value: float | None = None,
    split_desync_axis: str | None = None,
    request_split_desync: Callable[[str], None] | None = None,
) -> None:
    click = Gtk.GestureClick()
    click.set_button(3)
    click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
    click.connect(
        "pressed",
        _on_spin_secondary_step_pressed,
        row,
        page_step,
        reset_value,
        split_desync_axis,
        request_split_desync,
    )
    row.add_controller(click)


def _on_spin_secondary_step_pressed(
    gesture: Gtk.GestureClick,
    _n_press: int,
    x: float,
    _y: float,
    row: Adw.SpinRow,
    page_step: float | None,
    reset_value: float | None,
    split_desync_axis: str | None,
    request_split_desync: Callable[[str], None] | None,
) -> None:
    direction = spin_secondary_step_direction(row, x)
    if direction is None:
        return
    gesture.set_state(Gtk.EventSequenceState.CLAIMED)
    try:
        state = gesture.get_current_event_state()
    except (RuntimeError, TypeError, ValueError):
        state = Gdk.ModifierType(0)
    if (
        split_desync_axis
        and request_split_desync is not None
        and state & SPLIT_DESYNC_MODIFIERS
    ):
        request_split_desync(split_desync_axis)
    apply_spin_secondary_step(row, direction, page_step, reset_value)


def spin_secondary_step_direction(row: Adw.SpinRow, x: float) -> int | None:
    width = row.get_width()
    if width <= 0:
        return None
    button_area_width = min(96.0, max(64.0, width * 0.35))
    button_area_start = width - button_area_width
    if x < button_area_start:
        return None
    return 1 if x >= width - (button_area_width / 2.0) else -1


def apply_spin_secondary_step(
    row: Adw.SpinRow,
    direction: int,
    page_step: float | None,
    reset_value: float | None = None,
) -> None:
    if reset_value is not None:
        row.set_value(reset_value)
        return
    if page_step is None:
        return
    adjustment = row.get_adjustment()
    next_value = row.get_value() + (page_step if direction > 0 else -page_step)
    row.set_value(
        min(
            adjustment.get_upper(),
            max(adjustment.get_lower(), next_value),
        )
    )
