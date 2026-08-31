"""Motion-control selection and first-use presets."""

from typing import Any

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.model.actions import MappingAction
from keymasq.common.model.core import ActionType
from keymasq.common.model.motion import MotionControlConfig, MotionGamepadConfig
from keymasq.gui.session_reload import notify_session_reload_async
from keymasq.session.motion_controls import MotionControlManager


class MotionTabMixin:
    def _build_motion_presets_tab(self: Any) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(20)
        box.set_margin_bottom(20)
        box.set_margin_start(20)
        box.set_margin_end(20)
        intro = Gtk.Label(label="Create a tuned motion control and attach it to this profile.")
        intro.set_wrap(True)
        intro.add_css_class("dim-label")
        box.append(intro)
        mouse = Gtk.Button(label="Mouse")
        mouse.add_css_class("card")
        mouse.set_tooltip_text("Aim the pointer from angular velocity")
        mouse.connect("clicked", self._on_motion_preset_clicked, "mouse")
        box.append(mouse)
        gamepad = Gtk.Button(label="Right Stick")
        gamepad.add_css_class("card")
        gamepad.set_tooltip_text("Drive the virtual controller's right stick")
        gamepad.connect("clicked", self._on_motion_preset_clicked, "gamepad")
        box.append(gamepad)
        manage = Gtk.Button(label="Open Motion Controls…")
        manage.add_css_class("flat")
        manage.connect("clicked", self._on_open_motion_manager_clicked)
        box.append(manage)
        return box

    def _build_motion_control_tab(self: Any) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        toolbar_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        toolbar_row.set_halign(Gtk.Align.START)
        toolbar_row.set_margin_top(8)
        toolbar_row.set_margin_start(12)
        toolbar = Gtk.Button(label="Open Motion Controls…")
        toolbar.add_css_class("flat")
        toolbar.connect("clicked", self._on_open_motion_manager_clicked)
        toolbar_row.append(toolbar)
        hint = Gtk.Label(label="Select one · right-click to edit")
        hint.add_css_class("dim-label")
        hint.add_css_class("caption")
        toolbar_row.append(hint)
        outer.append(toolbar_row)
        self._motion_control_listbox = Gtk.ListBox()
        self._motion_control_listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._motion_control_listbox.connect("row-selected", self._on_motion_row_selected)
        self._motion_control_listbox.add_css_class("boxed-list")
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_child(self._motion_control_listbox)
        scrolled.set_vexpand(True)
        scrolled.set_margin_start(12)
        scrolled.set_margin_end(12)
        scrolled.set_margin_bottom(12)
        outer.append(scrolled)
        self._load_motion_control_list()
        return outer

    def _load_motion_control_list(self: Any) -> None:
        manager = MotionControlManager()
        controls = manager.get_all_motion_controls()
        while child := self._motion_control_listbox.get_first_child():
            self._motion_control_listbox.remove(child)
        selected = self._selected_motion_control
        for name in manager.list_motion_controls():
            config = controls[name]
            row = Gtk.ListBoxRow()
            row._motion_control_name = name
            right_click = Gtk.GestureClick()
            right_click.set_button(3)
            right_click.connect(
                "pressed",
                self._on_motion_control_row_right_pressed,
                name,
            )
            row.add_controller(right_click)
            content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            content.set_margin_top(10)
            content.set_margin_bottom(10)
            content.set_margin_start(12)
            content.set_margin_end(12)
            label = Gtk.Label(label=name)
            label.set_xalign(0.0)
            label.set_hexpand(True)
            content.append(label)
            detail = Gtk.Label(label="Gyro mouse" if config.mode == "mouse" else "Controller stick")
            detail.add_css_class("dim-label")
            content.append(detail)
            row.set_child(content)
            self._motion_control_listbox.append(row)
            if name == selected:
                self._motion_control_listbox.select_row(row)
        self._sync_selected_motion_control()

    def _on_motion_row_selected(self: Any, _listbox: Gtk.ListBox, _row: object) -> None:
        self._sync_selected_motion_control()
        if self.stack.get_visible_child_name() == "motion_control":
            self.map_btn.set_sensitive(self._selected_motion_control is not None)

    def _sync_selected_motion_control(self: Any) -> None:
        row = self._motion_control_listbox.get_selected_row()
        name = getattr(row, "_motion_control_name", None) if row is not None else None
        self._selected_motion_control = name if isinstance(name, str) else None

    def _on_motion_control_map_clicked(self: Any, _button: Gtk.Button) -> None:
        if self._selected_motion_control:
            self._emit_selected_action(
                MappingAction(
                    action_type=ActionType.MOTION_CONTROL,
                    motion_control_name=self._selected_motion_control,
                )
            )

    def _on_motion_preset_clicked(self: Any, _button: Gtk.Button, mode: str) -> None:
        manager = MotionControlManager()
        base = "Mouse" if mode == "mouse" else "Right Stick"
        name = manager.unique_motion_control_name(base)
        config = MotionControlConfig(
            name=name,
            mode=mode,
            gamepad=MotionGamepadConfig(target="right"),
        )
        manager.save_motion_control(config)
        notify_session_reload_async()
        self._emit_selected_action(
            MappingAction(
                action_type=ActionType.MOTION_CONTROL,
                motion_control_name=name,
            )
        )

    def _on_open_motion_manager_clicked(self: Any, _button: Gtk.Button) -> None:
        self._open_motion_control_manager()

    def _on_motion_control_row_right_pressed(
        self: Any,
        gesture: Gtk.GestureClick,
        _n_press: int,
        _x: float,
        _y: float,
        name: str,
    ) -> None:
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self._open_motion_control_manager(name)

    def _open_motion_control_manager(self: Any, select_name: str | None = None) -> None:
        from keymasq.gui.widgets.motion_control.dialog import MotionControlDialog

        root = self.get_root()
        dialog = MotionControlDialog(root, self._profile_manager_for_child_dialog())
        dialog.connect("motion-control-saved", self._on_motion_manager_changed)
        dialog.connect("motion-control-deleted", self._on_motion_manager_changed)
        dialog.present(root)
        if select_name:
            dialog.select_control_by_name(select_name)

    def _on_motion_manager_changed(self: Any, _dialog: object, _name: str) -> None:
        notify_session_reload_async()
        self._load_motion_control_list()
        if self.stack.get_visible_child_name() == "motion_presets":
            self.stack.set_visible_child_name("motion_control")
