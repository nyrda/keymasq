# pyright: reportAttributeAccessIssue=false
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.gamepad_axes import (
    GAMEPAD_AXIS_RANGES,
    gamepad_axis_percent_from_value,
    gamepad_axis_range,
    gamepad_axis_value_from_percent,
    normalize_gamepad_axis_target,
)
from keymasq.common.models import ActionType

from .targets import (
    _GAMEPAD_AXIS_CUSTOM_SLOT,
    EVDEV_TO_GAMEPAD,
    _resolve_gamepad_axis_target,
)


class GamepadAxisControlsMixin:
    gamepad_axis_targets: list[str]
    gamepad_axis_dropdown: Gtk.DropDown
    gamepad_axis_custom_entry: Gtk.Entry
    gamepad_axis_value: Gtk.SpinButton
    gamepad_axis_percent: Gtk.SpinButton
    gamepad_axis_percent_label: Gtk.Label
    gamepad_code_entry: Gtk.Entry
    _syncing_gamepad_axis_controls: bool

    def _build_gamepad_axis_controls(self) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.set_halign(Gtk.Align.CENTER)

        self.gamepad_axis_targets = [*GAMEPAD_AXIS_RANGES, _GAMEPAD_AXIS_CUSTOM_SLOT]
        labels = [GAMEPAD_AXIS_RANGES[target].label for target in GAMEPAD_AXIS_RANGES]
        labels.append("Custom")
        self.gamepad_axis_dropdown = Gtk.DropDown.new_from_strings(labels)
        self.gamepad_axis_dropdown.connect("notify::selected", self._on_gamepad_axis_changed)
        row.append(self.gamepad_axis_dropdown)

        self.gamepad_axis_custom_entry = Gtk.Entry()
        self.gamepad_axis_custom_entry.set_placeholder_text("abs_ code")
        self.gamepad_axis_custom_entry.set_width_chars(11)
        self.gamepad_axis_custom_entry.set_tooltip_text(
            "Custom axis evdev name (e.g. abs_hat0x, abs_hat0y, abs_throttle, "
            "abs_rudder) or numeric code."
        )
        row.append(self.gamepad_axis_custom_entry)

        self.gamepad_axis_value = Gtk.SpinButton()
        self.gamepad_axis_value.set_numeric(True)
        self.gamepad_axis_value.set_increments(1, 256)
        self.gamepad_axis_value.set_tooltip_text("Raw axis value")
        self.gamepad_axis_value.connect("value-changed", self._on_gamepad_axis_value_changed)
        row.append(self.gamepad_axis_value)

        self.gamepad_axis_percent = Gtk.SpinButton(
            adjustment=Gtk.Adjustment(value=100, lower=-100, upper=100, step_increment=1)
        )
        self.gamepad_axis_percent.set_numeric(True)
        self.gamepad_axis_percent.set_tooltip_text("Percent")
        self.gamepad_axis_percent.connect("value-changed", self._on_gamepad_axis_percent_changed)
        row.append(self.gamepad_axis_percent)
        self.gamepad_axis_percent_label = Gtk.Label(label="%")
        row.append(self.gamepad_axis_percent_label)

        apply_btn = Gtk.Button(label="Map Analog")
        apply_btn.add_css_class("suggested-action")
        apply_btn.connect("clicked", self._on_gamepad_axis_apply_clicked)
        row.append(apply_btn)

        self._on_gamepad_axis_changed(self.gamepad_axis_dropdown, None)
        return row

    def _selected_gamepad_axis_slot(self) -> str:
        index = int(self.gamepad_axis_dropdown.get_selected())
        if index < 0 or index >= len(self.gamepad_axis_targets):
            return "abs_x"
        return self.gamepad_axis_targets[index]

    def _selected_gamepad_axis_target(self) -> str | None:
        slot = self._selected_gamepad_axis_slot()
        if slot == _GAMEPAD_AXIS_CUSTOM_SLOT:
            return _resolve_gamepad_axis_target(self.gamepad_axis_custom_entry.get_text())
        return slot

    def _on_gamepad_axis_changed(self, dropdown, _param) -> None:
        is_custom = self._selected_gamepad_axis_slot() == _GAMEPAD_AXIS_CUSTOM_SLOT
        self.gamepad_axis_custom_entry.set_visible(is_custom)
        self.gamepad_axis_percent.set_visible(not is_custom)
        self.gamepad_axis_percent_label.set_visible(not is_custom)

        if is_custom:
            current = int(self.gamepad_axis_value.get_value())
            self._syncing_gamepad_axis_controls = True
            self.gamepad_axis_value.set_adjustment(
                Gtk.Adjustment(
                    value=current,
                    lower=-2147483648,
                    upper=2147483647,
                    step_increment=1,
                    page_increment=256,
                )
            )
            self._syncing_gamepad_axis_controls = False
            return

        axis = gamepad_axis_range(self._selected_gamepad_axis_slot())
        if axis is None:
            return
        self._syncing_gamepad_axis_controls = True
        self.gamepad_axis_value.set_adjustment(
            Gtk.Adjustment(
                value=axis.maximum,
                lower=axis.minimum,
                upper=axis.maximum,
                step_increment=1,
                page_increment=256,
            )
        )
        lower = -100 if axis.minimum < 0 else 0
        self.gamepad_axis_percent.set_adjustment(
            Gtk.Adjustment(value=100, lower=lower, upper=100, step_increment=1)
        )
        self._syncing_gamepad_axis_controls = False

    def _on_gamepad_axis_percent_changed(self, spin: Gtk.SpinButton) -> None:
        if getattr(self, "_syncing_gamepad_axis_controls", False):
            return
        target = self._selected_gamepad_axis_target()
        if target is None:
            return
        self._syncing_gamepad_axis_controls = True
        self.gamepad_axis_value.set_value(
            gamepad_axis_value_from_percent(target, spin.get_value())
        )
        self._syncing_gamepad_axis_controls = False

    def _on_gamepad_axis_value_changed(self, spin: Gtk.SpinButton) -> None:
        if getattr(self, "_syncing_gamepad_axis_controls", False):
            return
        if self._selected_gamepad_axis_slot() == _GAMEPAD_AXIS_CUSTOM_SLOT:
            return
        target = self._selected_gamepad_axis_target()
        if target is None:
            return
        self._syncing_gamepad_axis_controls = True
        self.gamepad_axis_percent.set_value(
            gamepad_axis_percent_from_value(target, spin.get_value())
        )
        self._syncing_gamepad_axis_controls = False

    def _on_gamepad_axis_apply_clicked(self, btn) -> None:
        target = self._selected_gamepad_axis_target()
        if not target:
            self.gamepad_axis_custom_entry.set_text("")
            self.gamepad_axis_custom_entry.set_placeholder_text("Unknown axis code")
            return
        self._on_gamepad_axis_clicked(
            btn,
            target,
            int(self.gamepad_axis_value.get_value()),
        )

    def _prefill_gamepad_inputs(self) -> None:
        """Restore the code/axis fields when editing an existing gamepad action."""
        action = getattr(self, "_current_action", None)
        if action is None:
            return
        action_type = getattr(action, "action_type", None)
        target = str(getattr(action, "target", "") or "")

        if action_type == ActionType.GAMEPAD:
            # Pre-fill the free-form code field for buttons outside the template.
            if target and target not in EVDEV_TO_GAMEPAD and hasattr(self, "gamepad_code_entry"):
                self.gamepad_code_entry.set_text(target)
            return

        if action_type == ActionType.GAMEPAD_AXIS:
            self._prefill_gamepad_axis(target, int(getattr(action, "axis_value", 0) or 0))

    def _prefill_gamepad_axis(self, target: str, value: int) -> None:
        normalized = normalize_gamepad_axis_target(target) or target
        if normalized in GAMEPAD_AXIS_RANGES:
            self.gamepad_axis_dropdown.set_selected(
                self.gamepad_axis_targets.index(normalized)
            )
        elif _resolve_gamepad_axis_target(normalized):
            self.gamepad_axis_dropdown.set_selected(
                self.gamepad_axis_targets.index(_GAMEPAD_AXIS_CUSTOM_SLOT)
            )
            self.gamepad_axis_custom_entry.set_text(normalized)
        else:
            return
        self.gamepad_axis_value.set_value(value)

    def _on_gamepad_axis_clicked(self, btn, axis_target: str, axis_value: int) -> None:
        raise NotImplementedError
