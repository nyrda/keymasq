import gi

# pyright: reportAttributeAccessIssue=false, reportUnknownLambdaType=false

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

import evdev
from gi.repository import Adw, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.model.actions import MappingAction
from keymasq.common.model.core import ActionType
from keymasq.gui.widgets.compositor_actions import build_compositor_action_pages
from keymasq.gui.widgets.compositor_actions.compositors import COMPOSITOR_ACTION_DEFINITIONS
from keymasq.gui.widgets.macro_editor.model import (
    EditableControl,
    EditableEvent,
    EditableMove,
    _apply_mapping_action_to_move,
    _control_to_compositor_action,
    _move_to_mapping_action,
)
from keymasq.gui.widgets.macro_editor.panel.rows import (
    field_row,
    rows_list,
    unit_label,
)


class MacroEditorAddPopoversMixin:
    # ------------------------------------------------------------------
    # Add event popovers
    # ------------------------------------------------------------------

    def _default_insert_time_us(self, default_t_us: int | None = None) -> int:
        if default_t_us is not None:
            return max(0, int(default_t_us))
        return int(self._duration_us / 2) if self._duration_us else 500_000

    def _present_mouse_move_dialog(
        self,
        default_t_us: int | None = None,
        move: EditableMove | None = None,
        mode: str = "natural",
    ) -> None:
        from keymasq.gui.widgets.key_selector.dialog import KeySelectorDialog

        if move is not None:
            current_action = _move_to_mapping_action(move)
        else:
            action_type = {
                "abs": ActionType.MOUSE_MOVE_ABS,
                "rel": ActionType.MOUSE_MOVE_REL,
            }.get(mode, ActionType.MOUSE_MOVE_NATURAL_ABS)
            current_action = MappingAction(action_type=action_type)

        dialog = KeySelectorDialog(
            self._parent,
            "Mouse Move",
            current_action,
            allow_passthrough=False,
            allow_clear_mapping=False,
            allow_suppress=False,
            allow_superkey=False,
            allow_repeat=False,
            allow_rapidfire=False,
            allow_tap=False,
            allowed_tabs={"mouse"},
            initial_tab="mouse",
            include_mouse_button_controls=False,
            include_mouse_scroll_controls=False,
            include_mouse_move_controls=True,
            include_mouse_move_failure_controls=True,
            mouse_move_commit_label="Apply Move" if move is not None else "Insert Move",
        )
        if move is not None:
            dialog.connect("key-selected", self._on_mouse_move_selected_for_edit, move)
        else:
            dialog.connect(
                "key-selected",
                self._on_mouse_move_selected_for_insert,
                self._default_insert_time_us(default_t_us),
            )
        dialog.present(self._parent)

    def _on_mouse_move_selected_for_insert(
        self,
        _dialog: Gtk.Widget,
        action: MappingAction | None,
        default_t_us: int,
    ) -> None:
        if action is None:
            return
        move = EditableMove(mode="rel", t_us=max(0, int(default_t_us)), x=0, y=0)
        if not _apply_mapping_action_to_move(move, action):
            return
        self._synthetic_moves.append(move)
        self._synthetic_moves.sort(key=lambda m: m.t_us)
        self._timeline._selected = move
        self._refresh_after_timing_edit()
        self._on_selection_changed(move)

    def _on_mouse_move_selected_for_edit(
        self,
        _dialog: Gtk.Widget,
        action: MappingAction | None,
        move: EditableMove,
    ) -> None:
        if action is None or move not in self._synthetic_moves:
            return
        if not _apply_mapping_action_to_move(move, action):
            return
        self._synthetic_moves.sort(key=lambda m: m.t_us)
        self._refresh_after_timing_edit()
        self._on_selection_changed(move)

    def _insert_control_event(self, control: EditableControl) -> None:
        self._control_events.append(control)
        self._control_events.sort(key=lambda c: c.t_us)
        self._timeline._selected = control
        self._refresh_after_timing_edit()
        self._on_selection_changed(control)

    def _present_macro_call_dialog(
        self,
        *,
        mode: str = "macro_sync",
        default_t_us: int | None = None,
        control: EditableControl | None = None,
    ) -> None:
        """Select a called macro through the shared macro library dialog."""

        from keymasq.gui.widgets.key_selector.dialog import KeySelectorDialog

        current_action = None
        if control is not None and control.macro_name:
            current_action = MappingAction(
                action_type=ActionType.MACRO,
                macro_name=control.macro_name,
                macro_replay_mouse_movement=control.macro_replay_mouse_movement,
                macro_replay_mouse_clicks=control.macro_replay_mouse_clicks,
                macro_speed=control.macro_speed,
            )
        dialog = KeySelectorDialog(
            self._parent,
            "Macro Call",
            current_action,
            allow_passthrough=False,
            allow_clear_mapping=False,
            allow_suppress=False,
            allow_superkey=False,
            allow_repeat=False,
            allow_rapidfire=False,
            allow_tap=False,
            allowed_tabs={"macro"},
            initial_tab="macro",
            macro_library_only=True,
            dialog_title="Choose Macro",
        )
        dialog.connect(
            "key-selected",
            self._on_macro_call_selected,
            mode,
            self._default_insert_time_us(default_t_us),
            control,
        )
        dialog.present(self._parent)

    def _on_macro_call_selected(
        self,
        _dialog: Gtk.Widget,
        action: MappingAction | None,
        mode: str,
        default_t_us: int,
        control: EditableControl | None,
    ) -> None:
        if action is None or action.action_type != ActionType.MACRO or not action.macro_name:
            return
        target = control or EditableControl(mode=mode, t_us=max(0, int(default_t_us)))
        target.macro_name = action.macro_name
        target.macro_replay_mouse_movement = action.macro_replay_mouse_movement
        target.macro_replay_mouse_clicks = action.macro_replay_mouse_clicks
        target.macro_speed = max(0.01, action.macro_speed)
        if control is None:
            self._insert_control_event(target)
        else:
            self._refresh_after_control_change(target)

    def _insert_compositor_action(self, default_t_us: int | None = None) -> None:
        """Insert a compositor action at ``default_t_us`` and edit it in the inspector."""
        status = self._resolve_compositor_action_status()
        if not status.get("compositor_dispatch_available"):
            status = self._resolve_compositor_action_status(self._compositor_action_status)
        definition = next(
            (d for d in COMPOSITOR_ACTION_DEFINITIONS if d.is_available(None, dict(status))),
            None,
        )
        if definition is None:
            # No compositor listener: the picker dialog explains why.
            self._present_compositor_action_dialog(default_t_us=default_t_us)
            return
        first = definition.presets[0] if definition.presets else None
        control = EditableControl(
            mode="compositor_dispatch",
            t_us=max(0, int(default_t_us or 0)),
            compositor_id=definition.compositor_id,
            compositor_dispatcher=first.dispatcher if first is not None else "",
            compositor_args=first.args if first is not None else "",
        )
        self._insert_control_event(control)

    def _present_compositor_action_dialog(
        self,
        default_t_us: int | None = None,
        control: EditableControl | None = None,
    ) -> None:
        title = "Edit Compositor Action" if control is not None else "Insert Compositor Action"
        dialog = Adw.Dialog(title=title, content_width=560, content_height=440)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)

        at_spin = Gtk.SpinButton()
        at_spin.set_adjustment(
            Gtk.Adjustment(
                value=(default_t_us or 0) / 1000, lower=0, upper=3600000, step_increment=1
            )
        )
        at_spin.set_digits(0)
        at_spin.set_width_chars(8)
        if control is None:
            # When editing, the inspector's own At row already owns the time.
            box.append(rows_list(field_row("At", at_spin, unit_label("ms"))))

        current_action = _control_to_compositor_action(control) if control is not None else None

        def on_selected(action: MappingAction) -> None:
            dispatcher = str(action.compositor_dispatcher or "").strip()
            if not dispatcher:
                return
            target = control or EditableControl(mode="compositor_dispatch", t_us=0)
            if control is None:
                target.t_us = max(0, int(at_spin.get_value() * 1000))
            target.compositor_id = str(action.compositor_id or "")
            target.compositor_dispatcher = dispatcher
            target.compositor_args = str(action.compositor_args or "")
            if control is None:
                self._insert_control_event(target)
            else:
                self._refresh_after_control_change(target)
            dialog.close()

        status = self._resolve_compositor_action_status()
        if not status.get("compositor_dispatch_available"):
            status = self._resolve_compositor_action_status(self._compositor_action_status)
        pages = build_compositor_action_pages(
            current_action,
            on_selected,
            status,
            "Apply" if control is not None else "Insert",
        )

        if pages:
            stack = Gtk.Stack()
            stack.set_vexpand(True)
            for page in pages:
                stack.add_titled(page.widget, page.page_id, page.title)
            if len(pages) > 1:
                switcher = Gtk.StackSwitcher()
                switcher.set_stack(stack)
                switcher.set_halign(Gtk.Align.CENTER)
                box.append(switcher)
            box.append(stack)
        else:
            message = Gtk.Label(label="Compositor actions are unavailable for this session.")
            message.add_css_class("dim-label")
            message.set_wrap(True)
            message.set_halign(Gtk.Align.START)
            box.append(message)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer.set_halign(Gtk.Align.END)
        close_btn = Gtk.Button(label="Close")
        close_btn.connect("clicked", self._on_close_dialog_clicked, dialog)
        footer.append(close_btn)
        box.append(footer)

        dialog.set_child(box)
        dialog.present(self._parent)

    def _insert_wait_at(self, t_us: int | None = None) -> None:
        """Insert a fixed wait at ``t_us``. The inspector can switch it to random."""
        control = EditableControl(mode="wait", t_us=max(0, int(t_us or 0)), duration_us=100_000)
        self._insert_control_event(control)

    def _insert_exec_at(self, t_us: int | None = None) -> None:
        """Insert an empty Run Command at ``t_us`` for inline editing."""
        control = EditableControl(
            mode="exec_sync",
            t_us=max(0, int(t_us or 0)),
            command="",
            timeout_ms=min(30000, self._macro_exec_timeout_max_ms),
            inhibit_mouse=False,
        )
        self._insert_control_event(control)

    def _present_add_key_dialog(
        self,
        default_t_us: int | None = None,
        device_type: str = "keyboard",
    ) -> None:
        from keymasq.gui.widgets.key_selector.dialog import KeySelectorDialog

        if default_t_us is None:
            default_t_us = int((self._duration_us / 2) if self._duration_us else 500000)
        if device_type == "gamepad":
            action_type = ActionType.GAMEPAD
            allowed_tabs = {"gamepad"}
            label = "Add Gamepad Button"
        elif device_type == "mouse":
            action_type = ActionType.MOUSE
            allowed_tabs = {"mouse"}
            label = "Add Mouse Click"
        else:
            action_type = ActionType.KEYBOARD
            allowed_tabs = {"keyboard", "navigation", "media"}
            label = "Add Keystroke"

        dialog = KeySelectorDialog(
            self._parent,
            label,
            MappingAction(action_type=action_type),
            allow_passthrough=False,
            allow_clear_mapping=False,
            allow_suppress=False,
            allow_superkey=False,
            allow_repeat=False,
            allow_rapidfire=True,
            allow_gamepad_axis_rapidfire=False,
            allow_tap=False,
            allowed_tabs=allowed_tabs,
            initial_tab=device_type if device_type in {"mouse", "gamepad"} else "keyboard",
            include_mpris_controls=False,
            include_mouse_move_controls=False,
            include_mouse_scroll_controls=False if device_type == "mouse" else True,
        )
        dialog.rapidfire_check.set_tooltip_text(
            "Pulse this input within its macro duration. The gaps adjust so the last release "
            "lands at the configured end."
        )
        dialog.wait_spin.set_tooltip_text(
            "Preferred gap between pulses; adjusted to fit the duration."
        )
        dialog.connect(
            "key-selected",
            self._on_insert_key_selected,
            default_t_us,
        )
        dialog.present(self._parent)

    def _on_insert_key_selected(
        self,
        picker: Gtk.Widget,
        action,
        default_t_us: int,
    ) -> None:
        self._on_key_selected_for_insert(picker, action, default_t_us)

    def _on_key_selected_for_insert(self, dialog: Gtk.Widget, action, default_t_us: int) -> None:
        from keymasq.common.model.core import ActionType

        if action is None or action.action_type not in {
            ActionType.KEYBOARD,
            ActionType.MOUSE,
            ActionType.GAMEPAD,
            ActionType.GAMEPAD_AXIS,
        }:
            return

        target = getattr(action, "target", None)
        if not target:
            return

        code = getattr(evdev.ecodes, str(target).upper(), None)
        if code is None:
            return

        t_us = max(0, int(default_t_us))
        if action.action_type == ActionType.GAMEPAD_AXIS:
            ev = EditableEvent(
                device_type="gamepad",
                ev_type=evdev.ecodes.EV_ABS,
                code=code,
                press_t_us=t_us,
                release_t_us=t_us + 1,
                value=int(getattr(action, "axis_value", 0) or 0),
                output_id=getattr(action, "output_id", None),
            )
        else:
            is_mouse = action.action_type == ActionType.MOUSE
            ev = EditableEvent(
                device_type="mouse"
                if is_mouse
                else "gamepad"
                if action.action_type == ActionType.GAMEPAD
                else "keyboard",
                ev_type=evdev.ecodes.EV_KEY,
                code=code,
                press_t_us=t_us,
                release_t_us=t_us + (80000 if is_mouse else 50000),
                output_id=getattr(action, "output_id", None)
                if action.action_type == ActionType.GAMEPAD
                else None,
            )
        ev.apply_rapidfire(action)
        self._events.append(ev)
        self._events.sort(key=lambda item: item.press_t_us)
        self._timeline._selected = ev
        self._revealer.set_reveal_child(True)
        self._refresh_after_key_timing_change(ev)
