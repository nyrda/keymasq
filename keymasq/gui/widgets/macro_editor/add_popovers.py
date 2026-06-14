import gi

# pyright: reportAttributeAccessIssue=false, reportUnknownLambdaType=false

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

import evdev
from gi.repository import Adw, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.models import ActionType, MappingAction
from keymasq.gui.widgets.compositor_actions import build_compositor_action_pages
from keymasq.gui.widgets.macro_editor.model import (
    EditableControl,
    EditableEvent,
    EditableMove,
    _apply_mapping_action_to_move,
    _control_to_compositor_action,
    _move_to_mapping_action,
)


class MacroEditorAddPopoversMixin:
    # ------------------------------------------------------------------
    # Add event popovers
    # ------------------------------------------------------------------

    def _on_add_key(self, btn) -> None:
        self._present_add_key_dialog()

    def _on_add_click(self, btn) -> None:
        self._present_add_key_dialog(device_type="mouse")

    def _on_add_move_rel(self, btn) -> None:
        self._present_mouse_move_dialog(mode="rel")

    def _on_add_move_abs(self, btn) -> None:
        self._present_mouse_move_dialog(mode="abs")

    def _default_insert_time_us(self, default_t_us: int | None = None) -> int:
        if default_t_us is not None:
            return max(0, int(default_t_us))
        return int(self._duration_us / 2) if self._duration_us else 500_000

    def _insert_move_event(self, mode: str, default_t_us: int | None = None) -> None:
        move = EditableMove(
            mode=mode,
            t_us=self._default_insert_time_us(default_t_us),
            x=0,
            y=0,
        )
        self._synthetic_moves.append(move)
        self._synthetic_moves.sort(key=lambda m: m.t_us)
        self._timeline._selected = move
        self._refresh_after_timing_edit()

    def _present_mouse_move_dialog(
        self,
        default_t_us: int | None = None,
        move: EditableMove | None = None,
        mode: str = "natural",
    ) -> None:
        from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

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

        at_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        at_row.append(Gtk.Label(label="At (ms):"))
        at_value_us = control.t_us if control is not None else (default_t_us or 0)
        at_spin = Gtk.SpinButton()
        at_spin.set_adjustment(
            Gtk.Adjustment(value=at_value_us / 1000, lower=0, upper=3600000, step_increment=1)
        )
        at_spin.set_digits(0)
        at_spin.set_width_chars(8)
        at_row.append(at_spin)
        box.append(at_row)

        current_action = _control_to_compositor_action(control) if control is not None else None

        def on_selected(action: MappingAction) -> None:
            dispatcher = str(action.compositor_dispatcher or "").strip()
            if not dispatcher:
                return
            target = control or EditableControl(mode="compositor_dispatch", t_us=0)
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

    def _show_add_control_popover(
        self,
        anchor: Gtk.Widget,
        control_mode: str,
        default_t_us: int | None = None,
        pointing_to=None,
    ) -> None:
        popover = Gtk.Popover()
        popover.set_parent(anchor)
        if pointing_to is not None:
            popover.set_pointing_to(pointing_to)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)

        title_text = {
            "wait": "Insert Wait (Fixed)",
            "wait_random": "Insert Wait (Random)",
            "exec_sync": "Insert Exec Sync",
            "exec_async": "Insert Exec Async",
        }.get(control_mode, "Insert Control")

        title = Gtk.Label(label=title_text)
        title.add_css_class("heading")
        title.set_halign(Gtk.Align.START)
        box.append(title)

        at_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        at_row.append(Gtk.Label(label="At (ms):"))
        at_spin = Gtk.SpinButton()
        at_spin.set_adjustment(
            Gtk.Adjustment(
                value=(default_t_us or 0) / 1000, lower=0, upper=3600000, step_increment=1
            )
        )
        at_spin.set_digits(0)
        at_spin.set_width_chars(7)
        at_row.append(at_spin)
        box.append(at_row)

        duration_spin: Gtk.SpinButton | None = None
        min_spin: Gtk.SpinButton | None = None
        max_spin: Gtk.SpinButton | None = None
        timeout_spin: Gtk.SpinButton | None = None
        inhibit_check: Gtk.CheckButton | None = None
        cmd_entry: Gtk.Entry | None = None

        if control_mode == "wait":
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row.append(Gtk.Label(label="Duration (ms):"))
            duration_spin_widget = Gtk.SpinButton()
            duration_spin = duration_spin_widget
            duration_spin_widget.set_adjustment(
                Gtk.Adjustment(value=100, lower=0, upper=600000, step_increment=10)
            )
            duration_spin_widget.set_digits(0)
            duration_spin_widget.set_width_chars(8)
            row.append(duration_spin_widget)
            box.append(row)
        elif control_mode == "wait_random":
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row.append(Gtk.Label(label="Min (ms):"))
            min_spin_widget = Gtk.SpinButton()
            min_spin = min_spin_widget
            min_spin_widget.set_adjustment(
                Gtk.Adjustment(value=50, lower=0, upper=600000, step_increment=10)
            )
            min_spin_widget.set_digits(0)
            min_spin_widget.set_width_chars(7)
            row.append(min_spin_widget)
            row.append(Gtk.Label(label="Max (ms):"))
            max_spin_widget = Gtk.SpinButton()
            max_spin = max_spin_widget
            max_spin_widget.set_adjustment(
                Gtk.Adjustment(value=150, lower=0, upper=600000, step_increment=10)
            )
            max_spin_widget.set_digits(0)
            max_spin_widget.set_width_chars(7)
            row.append(max_spin_widget)
            box.append(row)
        elif control_mode in {"exec_sync", "exec_async"}:
            cmd_row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            cmd_label = Gtk.Label(label="Command:")
            cmd_label.set_halign(Gtk.Align.START)
            cmd_row.append(cmd_label)
            cmd_entry_widget = Gtk.Entry()
            cmd_entry = cmd_entry_widget
            cmd_entry_widget.set_placeholder_text("/absolute/path/to/script.sh")
            cmd_row.append(cmd_entry_widget)
            box.append(cmd_row)

            if control_mode == "exec_sync":
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                row.append(Gtk.Label(label="Timeout (ms):"))
                timeout_spin_widget = Gtk.SpinButton()
                timeout_spin = timeout_spin_widget
                timeout_spin_widget.set_adjustment(
                    Gtk.Adjustment(
                        value=min(30000, self._macro_exec_timeout_max_ms),
                        lower=1,
                        upper=self._macro_exec_timeout_max_ms,
                        step_increment=100,
                    )
                )
                timeout_spin_widget.set_digits(0)
                timeout_spin_widget.set_width_chars(8)
                row.append(timeout_spin_widget)
                box.append(row)

                timeout_hint = Gtk.Label(
                    label=f"Policy max timeout: {self._macro_exec_timeout_max_ms}ms"
                )
                timeout_hint.add_css_class("dim-label")
                timeout_hint.set_halign(Gtk.Align.START)
                box.append(timeout_hint)

                inhibit_check_widget = Gtk.CheckButton(label="Inhibit mouse movement while waiting")
                inhibit_check = inhibit_check_widget
                inhibit_check_widget.set_active(False)
                box.append(inhibit_check_widget)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer.set_halign(Gtk.Align.END)
        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", self._on_popover_cancel_clicked, popover)
        footer.append(cancel_btn)

        add_btn = Gtk.Button(label="Insert")
        add_btn.add_css_class("suggested-action")

        def on_insert(_b) -> None:
            t_us = int(at_spin.get_value() * 1000)
            control = EditableControl(mode=control_mode, t_us=t_us)

            if control_mode == "wait" and duration_spin is not None:
                control.duration_us = max(0, int(duration_spin.get_value() * 1000))
            elif control_mode == "wait_random" and min_spin is not None and max_spin is not None:
                mn = max(0, int(min_spin.get_value() * 1000))
                mx = max(mn, int(max_spin.get_value() * 1000))
                control.min_us = mn
                control.max_us = mx
            elif control_mode in {"exec_sync", "exec_async"} and cmd_entry is not None:
                command = cmd_entry.get_text().strip()
                control.command = command
                if control_mode == "exec_sync":
                    control.timeout_ms = (
                        max(1, int(timeout_spin.get_value())) if timeout_spin is not None else 30000
                    )
                    control.inhibit_mouse = bool(
                        inhibit_check.get_active() if inhibit_check is not None else False
                    )

            self._insert_control_event(control)
            popover.popdown()

        add_btn.connect("clicked", on_insert)
        footer.append(add_btn)
        box.append(footer)

        popover.set_child(box)
        popover.popup()

    def _present_add_key_dialog(
        self,
        default_t_us: int | None = None,
        device_type: str = "keyboard",
    ) -> None:
        from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog

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
            allow_rapidfire=False,
            allow_tap=False,
            allowed_tabs=allowed_tabs,
            initial_tab=device_type if device_type in {"mouse", "gamepad"} else "keyboard",
            include_mpris_controls=False,
            include_mouse_move_controls=False,
            include_mouse_scroll_controls=False if device_type == "mouse" else True,
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
        from keymasq.common.models import ActionType

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
        self._events.append(ev)
        self._events.sort(key=lambda item: item.press_t_us)
        self._timeline._selected = ev
        self._revealer.set_reveal_child(True)
        self._refresh_after_key_timing_change(ev)

    def _show_add_click_popover(
        self,
        anchor: Gtk.Widget,
        default_t_us: int | None = None,
        pointing_to=None,
    ) -> None:
        popover = Gtk.Popover()
        popover.set_parent(anchor)
        if pointing_to is not None:
            popover.set_pointing_to(pointing_to)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)

        title = Gtk.Label(label="Add Mouse Click")
        title.add_css_class("heading")
        box.append(title)

        at_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        at_row.append(Gtk.Label(label="At time:"))
        if default_t_us is not None:
            default_t = default_t_us / 1e6
        else:
            default_t = (self._duration_us / 1e6 / 2) if self._duration_us else 0.5
        at_spin = Gtk.SpinButton()
        at_spin.set_adjustment(
            Gtk.Adjustment(value=default_t, lower=0, upper=3600, step_increment=0.1)
        )
        at_spin.set_digits(3)
        at_row.append(at_spin)
        at_row.append(Gtk.Label(label="s"))
        box.append(at_row)

        mouse_buttons = [
            ("Left Button", evdev.ecodes.BTN_LEFT),
            ("Right Button", evdev.ecodes.BTN_RIGHT),
            ("Middle Button", evdev.ecodes.BTN_MIDDLE),
            ("Side Button", evdev.ecodes.BTN_SIDE),
            ("Extra Button", evdev.ecodes.BTN_EXTRA),
        ]

        btn_ui_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_ui_row.append(Gtk.Label(label="Button:"))
        btn_model = Gtk.StringList()
        for name, _ in mouse_buttons:
            btn_model.append(name)
        btn_dropdown = Gtk.DropDown()
        btn_dropdown.set_model(btn_model)
        btn_dropdown.set_size_request(160, -1)
        btn_ui_row.append(btn_dropdown)
        box.append(btn_ui_row)

        hold_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hold_row.append(Gtk.Label(label="Hold:"))
        hold_spin = Gtk.SpinButton()
        hold_spin.set_adjustment(
            Gtk.Adjustment(value=0.080, lower=0.001, upper=10, step_increment=0.010)
        )
        hold_spin.set_digits(3)
        hold_row.append(hold_spin)
        hold_row.append(Gtk.Label(label="s"))
        box.append(hold_row)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer.set_halign(Gtk.Align.END)

        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", self._on_popover_cancel_clicked, popover)
        footer.append(cancel)

        add = Gtk.Button(label="Add")
        add.add_css_class("suggested-action")

        def on_add(_btn):
            t_us = int(at_spin.get_value() * 1e6)
            hold_us = int(hold_spin.get_value() * 1e6)
            idx = btn_dropdown.get_selected()
            _, code = mouse_buttons[idx]
            ev = EditableEvent(
                device_type="mouse",
                ev_type=evdev.ecodes.EV_KEY,
                code=code,
                press_t_us=t_us,
                release_t_us=t_us + hold_us,
            )
            self._events.append(ev)
            self._events.sort(key=lambda e: e.press_t_us)
            self._update_stats()
            self._timeline.queue_draw()
            self._sync_close_guard()
            popover.popdown()

        add.connect("clicked", on_add)
        footer.append(add)
        box.append(footer)

        popover.set_child(box)
        popover.popup()
