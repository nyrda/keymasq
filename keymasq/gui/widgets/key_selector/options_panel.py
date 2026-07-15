# pyright: reportAttributeAccessIssue=false
from __future__ import annotations

import logging

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.model.actions import (
    MIN_RAPIDFIRE_HOLD_MS,
    MIN_RAPIDFIRE_WAIT_MS,
    MappingAction,
    action_type_supports_rapidfire,
)
from keymasq.common.model.core import ActionType

from .targets import (
    DEFAULT_RAPIDFIRE_TOOLTIP,
    REPEAT_CATEGORY_OPTIONS,
    REPEAT_RAPIDFIRE_TOOLTIP,
)

log = logging.getLogger(__name__)


class RapidfireWarningMixin:
    _rapidfire_warning_context = "key selector"

    def _warn_and_clear_unsupported_rapidfire(self, action_type: ActionType) -> None:
        if not self._rapidfire_enabled or action_type_supports_rapidfire(action_type):
            return
        log.warning(
            "Ignoring rapidfire for unsupported %s action in %s",
            action_type.value,
            self._rapidfire_warning_context,
        )
        check = getattr(self, "rapidfire_check", None)
        if check is not None and check.get_active():
            check.set_active(False)
        else:
            self._rapidfire_enabled = False
            self._update_options_visibility()


class MappingOptionsPanelMixin(RapidfireWarningMixin):
    def _build_options_box(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)

        row1 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row1.set_halign(Gtk.Align.START)

        self.rapidfire_check = Gtk.CheckButton(label="Rapidfire")
        self.rapidfire_check.set_active(self._rapidfire_enabled)
        self.rapidfire_check.set_tooltip_text(DEFAULT_RAPIDFIRE_TOOLTIP)
        self.rapidfire_check.connect("toggled", self._on_rapidfire_toggled)
        row1.append(self.rapidfire_check)

        self.hold_label = Gtk.Label(label="Hold:")
        row1.append(self.hold_label)

        self.hold_spin = Gtk.SpinButton()
        hold_adj = Gtk.Adjustment(
            value=self._rapidfire_hold,
            lower=MIN_RAPIDFIRE_HOLD_MS,
            upper=1000,
            step_increment=1,
        )
        self.hold_spin.set_adjustment(hold_adj)
        row1.append(self.hold_spin)

        self.hold_ms_label = Gtk.Label(label="ms")
        row1.append(self.hold_ms_label)

        self.wait_label = Gtk.Label(label="Wait:")
        row1.append(self.wait_label)

        self.wait_spin = Gtk.SpinButton()
        wait_adj = Gtk.Adjustment(
            value=self._rapidfire_wait,
            lower=MIN_RAPIDFIRE_WAIT_MS,
            upper=1000,
            step_increment=1,
        )
        self.wait_spin.set_adjustment(wait_adj)
        row1.append(self.wait_spin)

        self.wait_ms_label = Gtk.Label(label="ms")
        row1.append(self.wait_ms_label)

        box.append(row1)

        row2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row2.set_halign(Gtk.Align.START)

        self.tap_check = Gtk.CheckButton(label="Tap")
        self.tap_check.set_active(self._tap_enabled)
        self.tap_check.set_tooltip_text(
            "Send the action as a quick tap when the button is released within the hold window"
        )
        self.tap_check.connect("toggled", self._on_tap_toggled)
        row2.append(self.tap_check)

        self.tap_hold_label = Gtk.Label(label="Hold:")
        row2.append(self.tap_hold_label)

        self.tap_spin = Gtk.SpinButton()
        tap_adj = Gtk.Adjustment(value=self._tap_hold, lower=10, upper=500, step_increment=10)
        self.tap_spin.set_adjustment(tap_adj)
        row2.append(self.tap_spin)

        self.tap_ms_label = Gtk.Label(label="ms")
        row2.append(self.tap_ms_label)

        box.append(row2)

        self._update_options_visibility()

        return box

    def _update_options_visibility(self):
        rf_active = self._allow_rapidfire and self.rapidfire_check.get_active()
        tap_visible = self._allow_tap
        tap_active = tap_visible and self.tap_check.get_active()

        self.rapidfire_check.set_visible(self._allow_rapidfire)
        self.tap_check.set_visible(tap_visible)

        self.hold_label.set_visible(rf_active)
        self.hold_spin.set_visible(rf_active)
        self.hold_ms_label.set_visible(rf_active)
        self.wait_label.set_visible(rf_active)
        self.wait_spin.set_visible(rf_active)
        self.wait_ms_label.set_visible(rf_active)

        self.tap_hold_label.set_visible(tap_active)
        self.tap_spin.set_visible(tap_active)
        self.tap_ms_label.set_visible(tap_active)

    def _on_rapidfire_toggled(self, check):
        if not self._allow_rapidfire:
            return
        self._rapidfire_enabled = check.get_active()
        if self._rapidfire_enabled:
            self.tap_check.set_active(False)
            self._tap_enabled = False
        self._update_options_visibility()

    def _on_tap_toggled(self, check):
        if not self._allow_tap:
            return
        self._tap_enabled = check.get_active()
        if self._tap_enabled:
            self.rapidfire_check.set_active(False)
            self._rapidfire_enabled = False
        self._update_options_visibility()

    def _build_repeat_section(self) -> Gtk.Widget:
        section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        section.set_halign(Gtk.Align.CENTER)

        self._repeat_options_box = section
        section.set_visible(bool(self._repeat_button and self._repeat_button.get_active()))

        section.append(self._build_repeat_rapidfire_row())

        self._repeat_toggle_buttons = {}
        categories_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        categories_box.add_css_class("linked")
        categories_box.set_halign(Gtk.Align.CENTER)
        categories_box.set_hexpand(True)
        categories_box.set_homogeneous(True)
        categories_box.set_size_request(355, -1)
        categories_box.set_margin_top(4)
        for category, label, tooltip in REPEAT_CATEGORY_OPTIONS:
            toggle = Gtk.ToggleButton(label=label)
            toggle.set_active(category in self._repeat_categories)
            toggle.set_tooltip_text(tooltip)
            toggle.set_hexpand(True)
            toggle.connect("toggled", self._on_repeat_category_toggled, category)
            categories_box.append(toggle)
            self._repeat_toggle_buttons[category] = toggle

        section.append(categories_box)

        action_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        action_row.set_halign(Gtk.Align.CENTER)
        action_row.set_margin_top(4)
        repeat_map_btn = Gtk.Button(label="Map Repeat")
        repeat_map_btn.add_css_class("suggested-action")
        repeat_map_btn.connect("clicked", self._on_repeat_map_clicked)
        self._repeat_map_btn = repeat_map_btn
        action_row.append(repeat_map_btn)
        section.append(action_row)
        self._update_repeat_map_button()

        return section

    def _build_repeat_rapidfire_row(self) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.set_halign(Gtk.Align.CENTER)

        self.repeat_rapidfire_check = Gtk.CheckButton(label="Rapidfire")
        self.repeat_rapidfire_check.set_active(self._allow_rapidfire and self._rapidfire_enabled)
        self.repeat_rapidfire_check.set_tooltip_text(REPEAT_RAPIDFIRE_TOOLTIP)
        self.repeat_rapidfire_check.connect("toggled", self._on_repeat_rapidfire_toggled)
        row.append(self.repeat_rapidfire_check)

        self.repeat_hold_label = Gtk.Label(label="Hold:")
        row.append(self.repeat_hold_label)
        self.repeat_hold_spin = Gtk.SpinButton()
        self.repeat_hold_spin.set_adjustment(
            Gtk.Adjustment(
                value=self._rapidfire_hold,
                lower=MIN_RAPIDFIRE_HOLD_MS,
                upper=1000,
                step_increment=1,
            )
        )
        row.append(self.repeat_hold_spin)
        self.repeat_hold_ms_label = Gtk.Label(label="ms")
        row.append(self.repeat_hold_ms_label)

        self.repeat_wait_label = Gtk.Label(label="Wait:")
        row.append(self.repeat_wait_label)
        self.repeat_wait_spin = Gtk.SpinButton()
        self.repeat_wait_spin.set_adjustment(
            Gtk.Adjustment(
                value=self._rapidfire_wait,
                lower=MIN_RAPIDFIRE_WAIT_MS,
                upper=1000,
                step_increment=1,
            )
        )
        row.append(self.repeat_wait_spin)
        self.repeat_wait_ms_label = Gtk.Label(label="ms")
        row.append(self.repeat_wait_ms_label)

        self._update_repeat_rapidfire_visibility()
        return row

    def _update_repeat_rapidfire_visibility(self) -> None:
        self.repeat_rapidfire_check.set_visible(self._allow_rapidfire)
        rf_active = self._allow_rapidfire and self.repeat_rapidfire_check.get_active()
        for widget in (
            self.repeat_hold_label,
            self.repeat_hold_spin,
            self.repeat_hold_ms_label,
            self.repeat_wait_label,
            self.repeat_wait_spin,
            self.repeat_wait_ms_label,
        ):
            widget.set_visible(rf_active)

    def _on_repeat_rapidfire_toggled(self, _check: Gtk.CheckButton) -> None:
        self._update_repeat_rapidfire_visibility()

    def _on_repeat_button_toggled(self, btn: Gtk.ToggleButton) -> None:
        if self._repeat_options_box is not None:
            self._repeat_options_box.set_visible(btn.get_active())

    def _on_repeat_category_toggled(
        self,
        check: Gtk.ToggleButton,
        category: str,
    ) -> None:
        if check.get_active():
            if category not in self._repeat_categories:
                self._repeat_categories.append(category)
        else:
            self._repeat_categories = [
                existing for existing in self._repeat_categories if existing != category
            ]
        self._update_repeat_map_button()

    def _update_repeat_map_button(self) -> None:
        if self._repeat_map_btn is not None:
            self._repeat_map_btn.set_sensitive(bool(self._repeat_categories))

    def _on_repeat_map_clicked(self, _btn: Gtk.Button) -> None:
        if not self._repeat_categories:
            return
        rapidfire_enabled = bool(self._allow_rapidfire and self.repeat_rapidfire_check.get_active())
        action = MappingAction(
            action_type=ActionType.REPEAT,
            repeat_categories=list(self._repeat_categories),
            rapidfire_enabled=rapidfire_enabled,
            rapidfire_hold_ms=int(self.repeat_hold_spin.get_value()),
            rapidfire_wait_ms=int(self.repeat_wait_spin.get_value()),
        )
        self._emit_selected_action(action)


class SuperkeyOptionsPanelMixin(RapidfireWarningMixin):
    _rapidfire_warning_context = "superkey action dialog"

    def _build_options_box(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)

        is_hold = self._action_type in ("hold", "tap_hold")

        if is_hold:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row.set_halign(Gtk.Align.START)

            self.rapidfire_check = Gtk.CheckButton(label="Rapidfire")
            self.rapidfire_check.set_active(self._rapidfire_enabled)
            self.rapidfire_check.set_tooltip_text("Repeatedly send while held")
            self.rapidfire_check.connect("toggled", self._on_rapidfire_toggled)
            row.append(self.rapidfire_check)

            self.hold_label = Gtk.Label(label="Hold:")
            row.append(self.hold_label)

            self.hold_spin = Gtk.SpinButton()
            hold_adj = Gtk.Adjustment(
                value=self._rapidfire_hold,
                lower=MIN_RAPIDFIRE_HOLD_MS,
                upper=1000,
                step_increment=1,
            )
            self.hold_spin.set_adjustment(hold_adj)
            row.append(self.hold_spin)

            self.hold_ms_label = Gtk.Label(label="ms")
            row.append(self.hold_ms_label)

            self.wait_label = Gtk.Label(label="Wait:")
            row.append(self.wait_label)

            self.wait_spin = Gtk.SpinButton()
            wait_adj = Gtk.Adjustment(
                value=self._rapidfire_wait,
                lower=MIN_RAPIDFIRE_WAIT_MS,
                upper=1000,
                step_increment=1,
            )
            self.wait_spin.set_adjustment(wait_adj)
            row.append(self.wait_spin)

            self.wait_ms_label = Gtk.Label(label="ms")
            row.append(self.wait_ms_label)

            box.append(row)
            self._update_options_visibility()
        else:
            self.rapidfire_check = None

        return box

    def _update_options_visibility(self):
        if self.rapidfire_check:
            rf_active = self.rapidfire_check.get_active()
            self.hold_label.set_visible(rf_active)
            self.hold_spin.set_visible(rf_active)
            self.hold_ms_label.set_visible(rf_active)
            self.wait_label.set_visible(rf_active)
            self.wait_spin.set_visible(rf_active)
            self.wait_ms_label.set_visible(rf_active)

    def _on_rapidfire_toggled(self, check):
        self._rapidfire_enabled = check.get_active()
        self._update_options_visibility()
