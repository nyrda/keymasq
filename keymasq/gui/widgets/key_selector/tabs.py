# pyright: reportAttributeAccessIssue=false, reportUnusedFunction=false
from __future__ import annotations

import logging
from typing import Any, Protocol

import evdev
import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gdk, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.models import ActionType, MappingAction, SuperkeyAction
from keymasq.gui.widgets.gamepad_output_choices import (
    gamepad_output_choice_matches,
    gamepad_output_choices,
    gamepad_output_unavailable_message,
)
from keymasq.gui.widgets.input_picker_shared import (
    build_gamepad_tab as build_shared_gamepad_tab,
)
from keymasq.gui.widgets.input_picker_shared import (
    build_keyboard_tab as build_shared_keyboard_tab,
)
from keymasq.gui.widgets.input_picker_shared import (
    build_media_tab as build_shared_media_tab,
)
from keymasq.gui.widgets.input_picker_shared import (
    build_mouse_tab as build_shared_mouse_tab,
)
from keymasq.gui.widgets.input_picker_shared import (
    build_navigation_tab as build_shared_navigation_tab,
)

from . import compat
from .targets import (
    ACTION_DOC_LINKS,
    F_EXTRA,
    KEY_TO_EVDEV,
    KEY_WIDTHS,
    KEYBOARD_LAYOUT,
    MEDIA_KEY_GROUPS,
    MPRIS_MEDIA_GROUPS,
    SYSTEM_KEY_GROUPS,
    _actions_docs_url,
    _keyboard_target_allows_rapidfire,
    _keyboard_target_allows_tap,
    _resolve_gamepad_button_target,
)

log = logging.getLogger("keymasq.gui.widgets.key_selector_dialog")

_compact_tabs_css_installed = False


def _unit_label(text: str) -> Gtk.Label:
    label = Gtk.Label(label=text)
    label.add_css_class("dim-label")
    label.set_halign(Gtk.Align.START)
    return label


class InputTabsHost(Protocol):
    def _build_selected_action(
        self,
        action_type: ActionType,
        **kwargs: Any,
    ) -> MappingAction | SuperkeyAction: ...

    def _emit_selected_action(self, action: MappingAction | SuperkeyAction | None) -> None: ...


def _create_actions_docs_button() -> Gtk.Button:
    btn = Gtk.Button(label="?")
    btn.add_css_class("flat")
    btn.add_css_class("actions-docs-button")
    btn.set_tooltip_text("Open documentation for this tab")
    return btn


def _ensure_compact_tabs_css() -> None:
    global _compact_tabs_css_installed
    if _compact_tabs_css_installed:
        return

    display = Gdk.Display.get_default()
    if display is None:
        return

    provider = Gtk.CssProvider()
    provider.load_from_string(
        """
        .compact-map-tabs button {
            padding-left: 7px;
            padding-right: 7px;
            min-height: 28px;
            font-size: 0.85em;
        }
        """
    )
    Gtk.StyleContext.add_provider_for_display(
        display,
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )
    _compact_tabs_css_installed = True


class SharedInputTabsMixin:
    _include_keyboard_capture_controls = False
    _include_mouse_move_controls = False
    _include_tap_options = False
    _gamepad_output_selector_mode = "inline"

    def _create_key_button(
        self, label: str, evdev: str, width: float = 1, large: bool = False, protected: bool = False
    ) -> Gtk.Button:
        btn = Gtk.Button(label=label)
        btn.add_css_class("key-button")

        if large:
            btn.set_size_request(200, 50)
        else:
            base_width = 36
            btn.set_size_request(int(base_width * width), 34)

        if protected:
            btn.add_css_class("protected-key")
            btn.set_tooltip_text("Protected - cannot remap")

        btn._evdev_name = evdev
        btn._protected = protected
        return btn

    def _build_keyboard_tab(self) -> Gtk.Widget:
        scrolled = build_shared_keyboard_tab(
            self,
            keyboard_layout=KEYBOARD_LAYOUT,
            key_to_evdev=KEY_TO_EVDEV,
            key_widths=KEY_WIDTHS,
            system_key_groups=SYSTEM_KEY_GROUPS,
        )
        if not self._include_keyboard_capture_controls:
            return scrolled

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        scrolled.set_vexpand(True)
        outer.append(scrolled)

        toolbar_sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        toolbar_sep.set_margin_start(12)
        toolbar_sep.set_margin_end(12)
        outer.append(toolbar_sep)

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        toolbar.set_halign(Gtk.Align.CENTER)
        toolbar.set_margin_top(12)
        toolbar.set_margin_bottom(12)
        toolbar.set_margin_start(12)
        toolbar.set_margin_end(12)

        self.kb_capture_btn = Gtk.Button(label="Capture Key")
        self.kb_capture_btn.connect("clicked", self._on_keyboard_capture_clicked)
        toolbar.append(self.kb_capture_btn)

        self.kb_capture_status = Gtk.Label(label="")
        self.kb_capture_status.add_css_class("dim-label")
        toolbar.append(self.kb_capture_status)

        toolbar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        toolbar.append(Gtk.Label(label="Key code:"))

        self.kb_code_entry = Gtk.Entry()
        self.kb_code_entry.set_placeholder_text("e.g. 125 or key_leftmeta")
        self.kb_code_entry.set_width_chars(18)
        toolbar.append(self.kb_code_entry)

        code_btn = Gtk.Button(label="Map Code")
        code_btn.connect("clicked", self._on_map_code_clicked)
        toolbar.append(code_btn)

        outer.append(toolbar)

        if not hasattr(self, "_kb_capture_controller"):
            self._kb_capture_pending = False
            self._kb_capture_controller = Gtk.EventControllerKey()
            self._kb_capture_controller.connect(
                "key-pressed", self._on_keyboard_capture_key_pressed
            )
            self.add_controller(self._kb_capture_controller)

        return outer

    def _on_keyboard_capture_clicked(self, btn) -> None:
        self._kb_capture_pending = True
        self.kb_capture_status.set_text("Press a key...")

    def _on_keyboard_capture_key_pressed(self, controller, keyval, keycode, state) -> bool:
        if not getattr(self, "_kb_capture_pending", False):
            return False
        evdev_name = self._keyval_to_evdev(keyval)
        if not evdev_name:
            self.kb_capture_status.set_text("Unrecognized key")
            self._kb_capture_pending = False
            return True
        self.kb_capture_status.set_text(f"Captured: {evdev_name}")
        self._kb_capture_pending = False
        self._emit_keyboard_mapping(evdev_name)
        return True

    def _on_map_code_clicked(self, btn) -> None:
        raw = self.kb_code_entry.get_text().strip().lower()
        if not raw:
            return
        evdev_name = None
        if raw.startswith("key_"):
            evdev_name = raw
        else:
            try:
                code = int(raw)
                key_name = evdev.ecodes.KEY.get(code)
                if isinstance(key_name, str) and key_name.startswith("KEY_"):
                    evdev_name = key_name.lower()
                elif isinstance(key_name, (list, tuple)):
                    for candidate in key_name:
                        if candidate.startswith("KEY_"):
                            evdev_name = candidate.lower()
                            break
            except (TypeError, ValueError):
                evdev_name = None
        if not evdev_name:
            self.kb_code_entry.set_text("")
            self.kb_code_entry.set_placeholder_text("Unknown key code")
            return
        self._emit_keyboard_mapping(evdev_name)

    def _emit_keyboard_mapping(self, evdev_name: str) -> None:
        self._on_keyboard_clicked(None, evdev_name)

    def _keyval_to_evdev(self, keyval: int) -> str | None:
        name = (Gdk.keyval_name(keyval) or "").lower()
        if not name:
            return None
        special = {
            "escape": "key_esc",
            "tab": "key_tab",
            "return": "key_enter",
            "backspace": "key_backspace",
            "space": "key_space",
            "shift_l": "key_leftshift",
            "shift_r": "key_rightshift",
            "control_l": "key_leftctrl",
            "control_r": "key_rightctrl",
            "alt_l": "key_leftalt",
            "alt_r": "key_rightalt",
            "super_l": "key_leftmeta",
            "super_r": "key_rightmeta",
            "menu": "key_menu",
            "left": "key_left",
            "right": "key_right",
            "up": "key_up",
            "down": "key_down",
            "minus": "key_minus",
            "equal": "key_equal",
            "bracketleft": "key_leftbrace",
            "bracketright": "key_rightbrace",
            "backslash": "key_backslash",
            "semicolon": "key_semicolon",
            "apostrophe": "key_apostrophe",
            "comma": "key_comma",
            "period": "key_dot",
            "slash": "key_slash",
        }
        if name in special:
            return special[name]
        if len(name) == 1 and name.isalpha():
            return f"key_{name}"
        if name.isdigit():
            return f"key_{name}"
        if name.startswith("f") and name[1:].isdigit():
            return f"key_{name}"
        return None

    def _build_navigation_tab(self) -> Gtk.Widget:
        return build_shared_navigation_tab(self, f_extra=F_EXTRA)

    def _build_media_tab(self) -> Gtk.Widget:
        return build_shared_media_tab(
            self,
            media_groups=MEDIA_KEY_GROUPS,
            mpris_groups=MPRIS_MEDIA_GROUPS,
        )

    def _build_mouse_tab(self) -> Gtk.Widget:
        box = build_shared_mouse_tab(self)
        if not self._include_mouse_move_controls:
            return box

        box.append(Gtk.Separator())

        move_label = Gtk.Label(label="Move Cursor")
        move_label.add_css_class("heading")
        box.append(move_label)

        # Mode selector. Natural is the default and leads the group.
        mode_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        mode_row.set_halign(Gtk.Align.CENTER)

        self.mouse_move_natural_check = Gtk.CheckButton(label="Natural")
        self.mouse_move_natural_check.set_active(self._mouse_move_mode == "natural")
        self.mouse_move_natural_check.set_tooltip_text(
            "Glide the cursor to a screen position along a human-like path"
        )
        self.mouse_move_natural_check.connect("toggled", self._on_mouse_move_mode_changed)
        mode_row.append(self.mouse_move_natural_check)

        self.mouse_move_rel_check = Gtk.CheckButton(label="Relative")
        self.mouse_move_rel_check.set_group(self.mouse_move_natural_check)
        self.mouse_move_rel_check.set_active(self._mouse_move_mode == "rel")
        self.mouse_move_rel_check.set_tooltip_text(
            "Nudge the cursor by an X/Y offset from where it is now"
        )
        self.mouse_move_rel_check.connect("toggled", self._on_mouse_move_mode_changed)
        mode_row.append(self.mouse_move_rel_check)

        self.mouse_move_abs_check = Gtk.CheckButton(label="Absolute")
        self.mouse_move_abs_check.set_group(self.mouse_move_natural_check)
        self.mouse_move_abs_check.set_active(self._mouse_move_mode == "abs")
        self.mouse_move_abs_check.set_tooltip_text(
            "Warp the cursor instantly to a screen position"
        )
        self.mouse_move_abs_check.connect("toggled", self._on_mouse_move_mode_changed)
        mode_row.append(self.mouse_move_abs_check)

        box.append(mode_row)

        # Target coordinates and the commit button, kept together so Capture
        # sits right beside the X/Y fields it fills in.
        coords_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        coords_row.set_halign(Gtk.Align.CENTER)

        coords_row.append(Gtk.Label(label="X"))
        self.mouse_move_x_spin = Gtk.SpinButton()
        self.mouse_move_x_spin.set_adjustment(
            Gtk.Adjustment(value=self._mouse_move_x, lower=-10000, upper=10000, step_increment=1)
        )
        self.mouse_move_x_spin.set_width_chars(6)
        coords_row.append(self.mouse_move_x_spin)

        coords_row.append(Gtk.Label(label="Y"))
        self.mouse_move_y_spin = Gtk.SpinButton()
        self.mouse_move_y_spin.set_adjustment(
            Gtk.Adjustment(value=self._mouse_move_y, lower=-10000, upper=10000, step_increment=1)
        )
        self.mouse_move_y_spin.set_width_chars(6)
        coords_row.append(self.mouse_move_y_spin)

        move_map_btn = Gtk.Button(label="Map Move")
        move_map_btn.add_css_class("suggested-action")
        move_map_btn.connect("clicked", self._on_mouse_move_map_clicked)
        move_map_btn.set_margin_start(4)
        coords_row.append(move_map_btn)

        box.append(coords_row)

        capture_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        capture_row.set_halign(Gtk.Align.CENTER)

        if not self._slurp_available:
            delay_label = Gtk.Label(label="Capture in:")
            capture_row.append(delay_label)

        self.mouse_move_capture_delay_spin = Gtk.SpinButton()
        self.mouse_move_capture_delay_spin.set_adjustment(
            Gtk.Adjustment(
                value=self._capture_delay_seconds,
                lower=0.2,
                upper=15.0,
                step_increment=0.2,
            )
        )
        self.mouse_move_capture_delay_spin.set_digits(1)
        self.mouse_move_capture_delay_spin.set_width_chars(4)
        self.mouse_move_capture_delay_spin.set_visible(not self._slurp_available)
        capture_row.append(self.mouse_move_capture_delay_spin)

        if not self._slurp_available:
            delay_suffix = Gtk.Label(label="s")
            capture_row.append(delay_suffix)

        btn_label = "Capture" if self._slurp_available else "Capture Position"
        self.mouse_move_capture_btn = Gtk.Button(label=btn_label)
        self.mouse_move_capture_btn.set_tooltip_text("Read the current cursor position into X/Y")
        self.mouse_move_capture_btn.connect("clicked", self._on_capture_position_clicked)
        capture_row.append(self.mouse_move_capture_btn)

        self.mouse_move_capture_status = Gtk.Label(label="")
        self.mouse_move_capture_status.add_css_class("dim-label")
        self.mouse_move_capture_status.set_halign(Gtk.Align.START)
        capture_row.append(self.mouse_move_capture_status)

        self.mouse_move_capture_row = capture_row
        box.append(self.mouse_move_capture_row)

        # Natural-only tuning, laid out as an aligned label/value grid.
        natural_row = Gtk.Grid(column_spacing=10, row_spacing=8)
        natural_row.set_halign(Gtk.Align.CENTER)

        speed_label = Gtk.Label(label="Speed:")
        speed_label.set_halign(Gtk.Align.END)
        natural_row.attach(speed_label, 0, 0, 1, 1)
        self.mouse_move_speed_spin = Gtk.SpinButton()
        self.mouse_move_speed_spin.set_adjustment(
            Gtk.Adjustment(
                value=self._mouse_move_speed,
                lower=1.0,
                upper=12000.0,
                step_increment=50.0,
                page_increment=250.0,
            )
        )
        self.mouse_move_speed_spin.set_digits(0)
        self.mouse_move_speed_spin.set_width_chars(6)
        self.mouse_move_speed_spin.set_tooltip_text("Travel speed in pixels per second")
        natural_row.attach(self.mouse_move_speed_spin, 1, 0, 1, 1)
        natural_row.attach(_unit_label("px/s"), 2, 0, 1, 1)

        curve_label = Gtk.Label(label="Curve:")
        curve_label.set_halign(Gtk.Align.END)
        natural_row.attach(curve_label, 3, 0, 1, 1)
        curve_labels = ["Linear", "Ease", "Minimum Jerk"]
        self.mouse_move_curve_dropdown = Gtk.DropDown.new_from_strings(curve_labels)
        self.mouse_move_curve_dropdown.set_tooltip_text(
            "Velocity profile: constant, ease in/out, or smoothest (minimum jerk)"
        )
        curve_values = ["linear", "ease_in_out", "minimum_jerk"]
        try:
            self.mouse_move_curve_dropdown.set_selected(curve_values.index(self._mouse_move_curve))
        except ValueError:
            self.mouse_move_curve_dropdown.set_selected(1)
        natural_row.attach(self.mouse_move_curve_dropdown, 4, 0, 2, 1)

        jitter_label = Gtk.Label(label="Jitter:")
        jitter_label.set_halign(Gtk.Align.END)
        natural_row.attach(jitter_label, 0, 1, 1, 1)
        self.mouse_move_jitter_spin = Gtk.SpinButton()
        self.mouse_move_jitter_spin.set_adjustment(
            Gtk.Adjustment(
                value=self._mouse_move_jitter,
                lower=0.0,
                upper=20.0,
                step_increment=0.1,
                page_increment=1.0,
            )
        )
        self.mouse_move_jitter_spin.set_digits(1)
        self.mouse_move_jitter_spin.set_width_chars(4)
        self.mouse_move_jitter_spin.set_tooltip_text(
            "Random sideways wobble added to the path, in pixels"
        )
        natural_row.attach(self.mouse_move_jitter_spin, 1, 1, 1, 1)
        natural_row.attach(_unit_label("px"), 2, 1, 1, 1)

        tolerance_label = Gtk.Label(label="Tolerance:")
        tolerance_label.set_halign(Gtk.Align.END)
        natural_row.attach(tolerance_label, 3, 1, 1, 1)
        self.mouse_move_tolerance_spin = Gtk.SpinButton()
        self.mouse_move_tolerance_spin.set_adjustment(
            Gtk.Adjustment(
                value=self._mouse_move_tolerance,
                lower=0,
                upper=50,
                step_increment=1,
                page_increment=5,
            )
        )
        self.mouse_move_tolerance_spin.set_width_chars(4)
        self.mouse_move_tolerance_spin.set_tooltip_text(
            "Stop once the cursor is within this many pixels of the target"
        )
        natural_row.attach(self.mouse_move_tolerance_spin, 4, 1, 1, 1)
        natural_row.attach(_unit_label("px"), 5, 1, 1, 1)

        self.mouse_move_natural_options_row = natural_row
        box.append(self.mouse_move_natural_options_row)

        natural_row_2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        natural_row_2.set_halign(Gtk.Align.CENTER)

        duration_label = Gtk.Label(label="Give up after:")
        natural_row_2.append(duration_label)
        self.mouse_move_duration_spin = Gtk.SpinButton()
        self.mouse_move_duration_spin.set_adjustment(
            Gtk.Adjustment(
                value=self._mouse_move_max_duration_ms,
                lower=1,
                upper=30000,
                step_increment=100,
                page_increment=500,
            )
        )
        self.mouse_move_duration_spin.set_width_chars(6)
        self.mouse_move_duration_spin.set_tooltip_text(
            "Abort the move if the target has not been reached within this time"
        )
        natural_row_2.append(self.mouse_move_duration_spin)

        natural_row_2.append(Gtk.Label(label="ms"))

        self.mouse_move_natural_options_row_2 = natural_row_2
        box.append(self.mouse_move_natural_options_row_2)

        self._update_mouse_move_mode_visibility()

        return box

    def _build_gamepad_tab(self) -> Gtk.Widget:
        if self._gamepad_output_selector_mode == "title":
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            warning = Gtk.Label(label="")
            warning.set_margin_top(8)
            warning.set_margin_start(12)
            warning.set_margin_end(12)
            warning.set_wrap(True)
            warning.set_xalign(0)
            warning.add_css_class("dim-label")
            warning.add_css_class("warning")
            self._gamepad_output_warning_label = warning
            box.append(warning)
            box.append(build_shared_gamepad_tab(self))
            self._update_gamepad_output_warning()
            self._prefill_gamepad_inputs()
            return box

        choices = self._gamepad_output_choices()
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        outer.set_margin_top(8)
        if len(choices) > 1 or self._selected_gamepad_output_id:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row.set_margin_start(12)
            row.set_margin_end(12)
            label = Gtk.Label(label="Output")
            label.set_xalign(0)
            label.set_hexpand(True)
            self._gamepad_output_ids = [output_id for output_id, _label in choices]
            dropdown = Gtk.DropDown.new_from_strings([label for _output_id, label in choices])
            selected = 0
            for index, output_id in enumerate(self._gamepad_output_ids):
                if gamepad_output_choice_matches(output_id, self._selected_gamepad_output_id):
                    selected = index
                    break
            dropdown.set_selected(selected)
            dropdown.connect("notify::selected", self._on_gamepad_output_selected)
            self._gamepad_output_dropdown = dropdown
            row.append(label)
            row.append(dropdown)
            outer.append(row)
        warning = Gtk.Label(label="")
        warning.set_margin_start(12)
        warning.set_margin_end(12)
        warning.set_wrap(True)
        warning.set_xalign(0)
        warning.add_css_class("dim-label")
        warning.add_css_class("warning")
        self._gamepad_output_warning_label = warning
        outer.append(warning)
        outer.append(build_shared_gamepad_tab(self))
        self._update_gamepad_output_warning()
        self._prefill_gamepad_inputs()
        return outer

    def _build_gamepad_output_header(self) -> Gtk.Widget | None:
        choices = self._gamepad_output_choices()
        if len(choices) <= 1 and not self._selected_gamepad_output_id:
            return None

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_valign(Gtk.Align.CENTER)
        arrow = Gtk.Label(label="→")
        arrow.add_css_class("dim-label")
        self._gamepad_output_ids = [output_id for output_id, _label in choices]
        dropdown = Gtk.DropDown.new_from_strings([label for _output_id, label in choices])
        dropdown.set_valign(Gtk.Align.CENTER)
        selected = 0
        for index, output_id in enumerate(self._gamepad_output_ids):
            if gamepad_output_choice_matches(output_id, self._selected_gamepad_output_id):
                selected = index
                break
        dropdown.set_selected(selected)
        dropdown.connect("notify::selected", self._on_gamepad_output_selected)
        self._gamepad_output_dropdown = dropdown
        box.append(arrow)
        box.append(dropdown)
        box.set_visible(False)
        return box

    def _gamepad_output_choices(self) -> list[tuple[str | None, str]]:
        return gamepad_output_choices(
            self._selected_gamepad_output_id,
            count=compat.virtual_gamepad_count(),
            hardware_manager_factory=compat.hardware_manager,
        )

    def _on_gamepad_output_selected(self, dropdown: Gtk.DropDown, _param) -> None:
        selected = int(dropdown.get_selected())
        if 0 <= selected < len(self._gamepad_output_ids):
            self._selected_gamepad_output_id = self._gamepad_output_ids[selected]
        self._update_gamepad_output_warning()

    def _update_gamepad_output_warning(self) -> None:
        label = self._gamepad_output_warning_label
        if label is None:
            return
        message = gamepad_output_unavailable_message(
            self._selected_gamepad_output_id,
            compat.virtual_gamepad_count(),
        )
        label.set_label(message or "")
        label.set_visible(bool(message))

    def _rapidfire_fields(self, *, supported: bool = True) -> dict[str, object]:
        check = getattr(self, "rapidfire_check", None)
        if not supported or check is None:
            return {
                "rapidfire_enabled": False,
                "rapidfire_hold_ms": 20,
                "rapidfire_wait_ms": 20,
            }
        return {
            "rapidfire_enabled": bool(self._rapidfire_enabled),
            "rapidfire_hold_ms": int(self.hold_spin.get_value()),
            "rapidfire_wait_ms": int(self.wait_spin.get_value()),
        }

    def _tap_fields(self, *, supported: bool = True) -> dict[str, object]:
        if not self._include_tap_options:
            return {}
        if not supported:
            return {"tap_enabled": False, "tap_hold_ms": 150}
        return {
            "tap_enabled": bool(self._tap_enabled),
            "tap_hold_ms": int(self.tap_spin.get_value()),
        }

    def _input_option_fields(
        self,
        *,
        rapidfire_supported: bool = True,
        tap_supported: bool = True,
    ) -> dict[str, object]:
        fields = self._rapidfire_fields(supported=rapidfire_supported)
        fields.update(self._tap_fields(supported=tap_supported))
        return fields

    def _on_mpris_clicked(self, btn, command: str) -> None:
        self._warn_and_clear_unsupported_rapidfire(ActionType.MPRIS)
        action = self._build_selected_action(
            ActionType.MPRIS,
            mpris_command=command,
        )
        self._emit_selected_action(action)
        self.close()

    def _on_keyboard_clicked(self, btn, evdev_name: str):
        use_rapidfire = _keyboard_target_allows_rapidfire(evdev_name)
        use_tap = _keyboard_target_allows_tap(evdev_name)
        action = self._build_selected_action(
            ActionType.KEYBOARD,
            target=evdev_name,
            **self._input_option_fields(
                rapidfire_supported=use_rapidfire,
                tap_supported=use_tap,
            ),
        )
        self._emit_selected_action(action)
        self.close()

    def _on_f_key_selected(self, btn):
        idx = self.f_dropdown.get_selected()
        f_key = F_EXTRA[idx]
        evdev_name = KEY_TO_EVDEV.get(f_key)
        if evdev_name:
            self._on_keyboard_clicked(btn, evdev_name)

    def _on_mouse_clicked(self, btn, evdev_name: str):
        action = self._build_selected_action(
            ActionType.MOUSE,
            target=evdev_name,
            **self._input_option_fields(),
        )
        self._emit_selected_action(action)
        self.close()

    def _on_gamepad_clicked(self, btn, evdev_name: str):
        action = self._build_selected_action(
            ActionType.GAMEPAD,
            target=evdev_name,
            output_id=self._selected_gamepad_output_id,
            **self._input_option_fields(),
        )
        self._emit_selected_action(action)
        self.close()

    def _on_gamepad_axis_clicked(self, btn, axis_target: str, axis_value: int):
        action = self._build_selected_action(
            ActionType.GAMEPAD_AXIS,
            target=axis_target,
            axis_value=int(axis_value),
            output_id=self._selected_gamepad_output_id,
            **self._input_option_fields(),
        )
        self._emit_selected_action(action)
        self.close()

    def _on_gamepad_code_clicked(self, widget) -> None:
        evdev_name = _resolve_gamepad_button_target(self.gamepad_code_entry.get_text())
        if not evdev_name:
            self.gamepad_code_entry.set_text("")
            self.gamepad_code_entry.set_placeholder_text("Unknown button code")
            return
        self._on_gamepad_clicked(widget, evdev_name)

    def _active_actions_docs_link(self) -> tuple[str, str] | None:
        child_name = self.stack.get_visible_child_name()
        if not child_name:
            return None
        return ACTION_DOC_LINKS.get(child_name)

    def _update_actions_docs_button(self) -> None:
        if not hasattr(self, "actions_docs_btn"):
            return
        link = self._active_actions_docs_link()
        self.actions_docs_btn.set_visible(link is not None)
        if link is None:
            return
        _anchor, title = link
        self.actions_docs_btn.set_tooltip_text(f"Open {title} documentation")

    def _on_actions_docs_clicked(self, _button: Gtk.Button) -> None:
        link = self._active_actions_docs_link()
        if link is None:
            return
        anchor, _title = link
        url = _actions_docs_url(anchor)
        try:
            launcher = Gtk.UriLauncher.new(url)
            launcher.launch(None, None, None)
        except Exception:
            log.exception("Could not open action documentation %s", url)

    def _on_f_dropdown_changed(self, dropdown, pspec, btn: Gtk.Button):
        idx = dropdown.get_selected()
        f_key = F_EXTRA[idx]
        btn.set_label(f"Map {f_key}")
