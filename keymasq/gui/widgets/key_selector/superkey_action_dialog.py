from __future__ import annotations

from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, GObject, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.model.core import ActionType
from keymasq.common.model.superkeys import SuperkeyAction

from .gamepad_axis import GamepadAxisControlsMixin
from .macro_tab import SuperkeyMacroTabMixin
from .options_panel import SuperkeyOptionsPanelMixin
from .tabs import SharedInputTabsMixin, _create_actions_docs_button
from .targets import MEDIA_KEY_TARGETS


class SuperkeyActionDialog(
    Adw.Dialog,
    SharedInputTabsMixin,
    GamepadAxisControlsMixin,
    SuperkeyOptionsPanelMixin,
    SuperkeyMacroTabMixin,
):
    __gsignals__ = {
        "action-selected": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    _gamepad_output_selector_mode = "inline"
    _include_tap_options = False
    _rapidfire_warning_context = "superkey action dialog"

    def _build_selected_action(
        self,
        action_type: ActionType,
        **kwargs: Any,
    ) -> SuperkeyAction:
        return SuperkeyAction(action_type=action_type, **kwargs)

    def _emit_selected_action(self, action: SuperkeyAction | None) -> None:
        self.emit("action-selected", action)

    def __init__(
        self,
        parent: Gtk.Widget,
        action_type: str,
        current_action: SuperkeyAction | None = None,
    ):
        self._action_type = action_type
        self._current_action = current_action
        self._parent = parent

        title = f"Configure {action_type.replace('_', ' ').title()} Action"
        super().__init__(title=title, content_width=540, content_height=520)

        self._rapidfire_enabled = False
        self._rapidfire_hold = 20
        self._rapidfire_wait = 20
        self._superkey_macro_list: list[dict] = []
        self._superkey_selected_macro: str | None = None
        self._selected_gamepad_output_id: str | None = (
            current_action.output_id
            if current_action
            and current_action.action_type in (ActionType.GAMEPAD, ActionType.GAMEPAD_AXIS)
            else None
        )
        self._gamepad_output_ids: list[str | None] = []
        self._gamepad_output_warning_label: Gtk.Label | None = None

        if current_action:
            self._rapidfire_enabled = current_action.rapidfire_enabled
            self._rapidfire_hold = current_action.rapidfire_hold_ms
            self._rapidfire_wait = current_action.rapidfire_wait_ms
            if current_action.action_type == ActionType.MACRO:
                self._superkey_selected_macro = current_action.macro_name

        self._build_ui()

    def _build_ui(self):
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        main_box.set_margin_top(12)
        main_box.set_margin_bottom(12)
        main_box.set_margin_start(12)
        main_box.set_margin_end(12)

        self.options_box = self._build_options_box()

        self.stack = Gtk.Stack()
        self.stack.set_vexpand(True)
        self.stack.connect("notify::visible-child", self._on_tab_changed)
        self.stack.add_titled(self._build_keyboard_tab(), "keyboard", "Keyboard")
        self.stack.add_titled(self._build_navigation_tab(), "navigation", "Navigation")
        self.stack.add_titled(self._build_media_tab(), "media", "Media")
        self.stack.add_titled(self._build_mouse_tab(), "mouse", "Mouse")
        self.stack.add_titled(self._build_gamepad_tab(), "gamepad", "Gamepad")
        self.stack.add_titled(self._build_macro_tab(), "macro", "Macro")
        self.stack.add_titled(self._build_exec_tab(), "exec", "Command")

        self._set_initial_tab()

        frame = Gtk.Frame()
        frame.set_vexpand(True)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        title_label = Gtk.Label(label=self.get_title())
        title_label.add_css_class("title-3")
        title_label.set_halign(Gtk.Align.CENTER)
        title_label.set_margin_bottom(12)
        inner.append(title_label)

        inner.append(Gtk.Separator())

        switcher = Gtk.StackSwitcher()
        switcher.set_stack(self.stack)
        switcher.set_halign(Gtk.Align.CENTER)
        switcher.set_margin_top(8)
        switcher.set_margin_bottom(8)
        inner.append(switcher)

        inner.append(Gtk.Separator())
        inner.append(self.stack)
        inner.append(Gtk.Separator())

        options_pad = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        options_pad.set_margin_top(8)
        options_pad.set_margin_bottom(8)
        options_pad.set_margin_start(12)
        options_pad.set_margin_end(12)
        options_pad.append(self.options_box)
        inner.append(options_pad)

        inner.append(Gtk.Separator())

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_box.set_hexpand(True)
        btn_box.set_margin_top(8)
        btn_box.set_margin_bottom(8)
        btn_box.set_margin_start(12)
        btn_box.set_margin_end(12)

        self.actions_docs_btn = _create_actions_docs_button()
        self.actions_docs_btn.connect("clicked", self._on_actions_docs_clicked)
        btn_box.append(self.actions_docs_btn)

        footer_spacer = Gtk.Box()
        footer_spacer.set_hexpand(True)
        btn_box.append(footer_spacer)

        clear_btn = Gtk.Button(label="Clear")
        clear_btn.connect("clicked", self._on_clear_clicked)
        btn_box.append(clear_btn)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", self._on_cancel_clicked)
        btn_box.append(cancel_btn)

        inner.append(btn_box)

        frame.set_child(inner)
        main_box.append(frame)
        self.set_child(main_box)

        GLib.idle_add(self._load_superkey_macro_list)

    def _on_cancel_clicked(self, _button: Gtk.Button) -> None:
        self.close()

    def _on_tab_changed(self, stack, param):
        child_name = self.stack.get_visible_child_name()
        is_exec = child_name == "exec"
        is_media = child_name == "media"
        if self.rapidfire_check:
            self.options_box.set_visible(not (is_exec or is_media))
        self._update_actions_docs_button()

    def _build_exec_tab(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)
        box.set_valign(Gtk.Align.CENTER)

        label = Gtk.Label(label="Execute a shell command:")
        label.set_halign(Gtk.Align.START)
        box.append(label)

        self.cmd_entry = Gtk.Entry()
        self.cmd_entry.set_placeholder_text("e.g., notify-send 'Hello'")
        self.cmd_entry.set_hexpand(True)
        if self._current_action and self._current_action.action_type == ActionType.EXEC:
            self.cmd_entry.set_text(self._current_action.cmd or "")
        box.append(self.cmd_entry)

        exec_btn = Gtk.Button(label="Execute Command")
        exec_btn.add_css_class("suggested-action")
        exec_btn.connect("clicked", self._on_exec_clicked)
        box.append(exec_btn)

        return box

    def _on_exec_clicked(self, btn):
        cmd = self.cmd_entry.get_text().strip()
        if cmd:
            self._warn_and_clear_unsupported_rapidfire(ActionType.EXEC)
            action = SuperkeyAction(
                action_type=ActionType.EXEC,
                cmd=cmd,
            )
            self.emit("action-selected", action)
            self.close()

    def _on_clear_clicked(self, btn):
        self.emit("action-selected", None)
        self.close()

    def _set_initial_tab(self):
        if not self._current_action:
            return
        tab_map = {
            ActionType.KEYBOARD: (
                "media" if self._current_action.target in MEDIA_KEY_TARGETS else "keyboard"
            ),
            ActionType.MOUSE: "mouse",
            ActionType.GAMEPAD: "gamepad",
            ActionType.GAMEPAD_AXIS: "gamepad",
            ActionType.MACRO: "macro",
            ActionType.EXEC: "exec",
            ActionType.MPRIS: "media",
        }
        name = tab_map.get(self._current_action.action_type)
        if name:
            self.stack.set_visible_child_name(name)
