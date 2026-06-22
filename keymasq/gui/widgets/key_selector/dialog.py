from __future__ import annotations

from collections.abc import Callable
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GObject, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.models import (
    DEFAULT_NATURAL_MOUSE_MOVE_CURVE,
    DEFAULT_NATURAL_MOUSE_MOVE_JITTER,
    DEFAULT_NATURAL_MOUSE_MOVE_MAX_DURATION_MS,
    DEFAULT_NATURAL_MOUSE_MOVE_SPEED,
    DEFAULT_NATURAL_MOUSE_MOVE_TOLERANCE,
    DEFAULT_REPEAT_CATEGORIES,
    ActionType,
    AnalogControlConfig,
    MappingAction,
    SuperkeyConfig,
)
from keymasq.gui.widgets.action_labels import describe_mapping_action_verbose
from keymasq.gui.widgets.compositor_actions import (
    build_compositor_action_pages,
    compositor_action_tab_name,
)
from keymasq.gui.widgets.mouse_move_units import speed_kpx_s_to_px_s
from keymasq.gui.widgets.position_capture import PositionCallback, PositionCaptureController

from .analog_tab import AnalogTabMixin
from .compat import detect_compositor_sync, get_slurp_capture, session_request_async
from .gamepad_axis import GamepadAxisControlsMixin
from .macro_tab import MacroTabMixin
from .options_panel import MappingOptionsPanelMixin
from .profile_tab import ProfileTabMixin
from .superkey_tab import SuperkeyTabMixin
from .tabs import SharedInputTabsMixin, _create_actions_docs_button, _ensure_compact_tabs_css
from .targets import EVDEV_TO_GAMEPAD, EVDEV_TO_KEY, MEDIA_KEY_TARGETS
from .type_tab import TypeTabMixin


class KeySelectorDialog(
    Adw.Dialog,
    SharedInputTabsMixin,
    GamepadAxisControlsMixin,
    MappingOptionsPanelMixin,
    MacroTabMixin,
    TypeTabMixin,
    ProfileTabMixin,
    SuperkeyTabMixin,
    AnalogTabMixin,
):
    __gsignals__ = {
        "key-selected": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    _include_keyboard_capture_controls = True
    _include_mouse_move_controls = True
    _include_tap_options = True
    _gamepad_output_selector_mode = "title"
    _rapidfire_warning_context = "key selector"
    _selection_emit_closes_dialog = True

    def _build_selected_action(
        self,
        action_type: ActionType,
        **kwargs: Any,
    ) -> MappingAction:
        return MappingAction(action_type=action_type, **kwargs)

    def _emit_selected_action(self, action: MappingAction | None) -> None:
        self.emit("key-selected", action)
        self.close()

    def __init__(
        self,
        parent: Gtk.Widget,
        button_label: str,
        current_action: MappingAction | None = None,
        compositor_action_status: dict[str, object] | None = None,
        *,
        allow_passthrough: bool = True,
        allow_clear_mapping: bool = True,
        allow_suppress: bool = True,
        allow_superkey: bool = True,
        allow_repeat: bool = True,
        allow_rapidfire: bool = True,
        allow_tap: bool = True,
        allow_macro_options: bool = True,
        source_type: str = "button",
        analog_input_type: str | None = None,
        allowed_tabs: set[str] | list[str] | tuple[str, ...] | None = None,
        initial_tab: str | None = None,
        include_mpris_controls: bool = True,
        include_mouse_button_controls: bool = True,
        include_mouse_scroll_controls: bool = True,
        include_mouse_move_controls: bool | None = None,
        include_mouse_move_failure_controls: bool = False,
        mouse_move_commit_label: str = "Map Move",
    ):
        super().__init__(title=f"Map: {button_label}", content_width=570, content_height=580)
        self._parent = parent
        self._button_label = button_label
        self._current_action = current_action
        self._allow_passthrough = allow_passthrough
        self._allow_clear_mapping = allow_clear_mapping
        self._allow_suppress = allow_suppress
        self._allow_superkey = allow_superkey
        self._allow_repeat = allow_repeat
        self._allow_rapidfire = allow_rapidfire
        self._allow_tap = allow_tap
        self._allow_macro_options = allow_macro_options
        self._source_type = str(source_type or "button")
        self._allowed_tabs = set(allowed_tabs) if allowed_tabs is not None else None
        self._initial_tab = initial_tab
        self._include_mpris_controls = include_mpris_controls
        self._include_mouse_button_controls = include_mouse_button_controls
        self._include_mouse_scroll_controls = include_mouse_scroll_controls
        if include_mouse_move_controls is not None:
            self._include_mouse_move_controls = include_mouse_move_controls
        self._include_mouse_move_failure_controls = include_mouse_move_failure_controls
        self._mouse_move_commit_label = mouse_move_commit_label
        self._analog_input_type = (
            str(analog_input_type or "").lower()
            if self._source_type == "analog" and analog_input_type
            else None
        )
        self._compositor_action_status = self._resolve_compositor_action_status(
            compositor_action_status
        )
        self._rapidfire_enabled = False
        self._rapidfire_hold = 20
        self._rapidfire_wait = 20
        self._tap_enabled = False
        self._tap_hold = 50
        self._repeat_categories: list[str] = list(DEFAULT_REPEAT_CATEGORIES)
        self._repeat_toggle_buttons: dict[str, Gtk.ToggleButton] = {}
        self._repeat_button: Gtk.ToggleButton | None = None
        self._repeat_map_btn: Gtk.Button | None = None
        self._repeat_options_box: Gtk.Widget | None = None
        self._macro_list: list[dict] = []
        self._selected_macro: str | None = None
        self._selected_type_macro: str | None = None
        self._type_macro_details_loaded = False
        self._type_macro_details_loading = False
        self._type_details_applying = False
        self._type_controls_modified = False
        self._type_create_pending = False
        self._cancel_macro_playback_btn: Gtk.Button | None = None
        self._macro_recording_enabled = self._resolve_macro_recording_enabled(default=False)
        self._macro_slot_console: Gtk.Box | None = None
        self._superkey_list: list[SuperkeyConfig] = []
        self._superkey_names: list[str] = []
        self._selected_superkey: str | None = None
        self._analog_control_list: list[AnalogControlConfig] = []
        self._analog_control_names: list[str] = []
        self._selected_analog_control: str | None = None
        self._selected_analog_controls: list[str] = []
        self._macro_replay_movement: bool = True
        self._macro_replay_clicks: bool = True
        self._macro_speed: float = 1.0
        self._profile_entries: list[dict] = []
        self._selected_profile_action: str = "enable"
        self._selected_profile_name: str = ""
        self._profile_lifetime_preset: str = "until_changed"
        self._profile_lifetime_action_count: int = 2
        self._profile_lifetime_timeout_ms: int = 1500
        self._profile_custom_trigger_end: bool = False
        self._profile_custom_action_count: bool = False
        self._profile_custom_timeout: bool = False
        self._profile_lifetime_selection_updating: bool = False
        self._profile_lifetime_model_keys: list[str] = []
        self._selected_gamepad_output_id: str | None = (
            current_action.output_id
            if current_action
            and current_action.action_type in (ActionType.GAMEPAD, ActionType.GAMEPAD_AXIS)
            else None
        )
        self._gamepad_output_ids: list[str | None] = []
        self._gamepad_output_dropdown: Gtk.DropDown | None = None
        self._gamepad_output_header: Gtk.Widget | None = None
        self._gamepad_output_warning_label: Gtk.Label | None = None
        self._profile_name_items: list[str] = []
        self._profile_name_populating: bool = False
        self._exec_cmd: str = ""
        self._mouse_move_x: int = 0
        self._mouse_move_y: int = 0
        self._mouse_move_mode: str = "natural"
        self._mouse_move_speed: float = DEFAULT_NATURAL_MOUSE_MOVE_SPEED
        self._mouse_move_jitter: float = DEFAULT_NATURAL_MOUSE_MOVE_JITTER
        self._mouse_move_curve: str = DEFAULT_NATURAL_MOUSE_MOVE_CURVE
        self._mouse_move_tolerance: int = DEFAULT_NATURAL_MOUSE_MOVE_TOLERANCE
        self._mouse_move_max_duration_ms: int = DEFAULT_NATURAL_MOUSE_MOVE_MAX_DURATION_MS
        self._mouse_move_stop_on_failure: bool = False
        self._capture_delay_seconds: float = 2.0
        self._capture_timeout_id: int = 0
        self._capture_pending: bool = False
        self._capture_request_id: int = 0
        self._capture_apply: PositionCallback | None = None
        self._capture_status_label: Gtk.Label | None = None
        self._capture_button: Gtk.Button | None = None
        self._slurp_capture = get_slurp_capture()
        self._slurp_capture.set_compositor(detect_compositor_sync())
        self._slurp_available = self._slurp_capture.available
        self._position_capture = PositionCaptureController(
            slurp_capture=self._slurp_capture,
            slurp_available=self._slurp_available,
            request_async=session_request_async,
            on_state_changed=self._sync_position_capture_legacy_state,
        )

        if current_action:
            self._rapidfire_enabled = current_action.rapidfire_enabled
            self._rapidfire_hold = current_action.rapidfire_hold_ms
            self._rapidfire_wait = current_action.rapidfire_wait_ms
            self._tap_enabled = current_action.tap_enabled
            self._tap_hold = current_action.tap_hold_ms
            if current_action.action_type == ActionType.MACRO:
                self._selected_macro = current_action.macro_name
                self._selected_type_macro = current_action.macro_name
                self._macro_replay_movement = current_action.macro_replay_mouse_movement
                self._macro_replay_clicks = current_action.macro_replay_mouse_clicks
                self._macro_speed = current_action.macro_speed
            elif current_action.action_type == ActionType.SUPERKEY:
                self._selected_superkey = current_action.superkey_name
            elif current_action.action_type == ActionType.ANALOG_CONTROL:
                self._selected_analog_controls = list(current_action.analog_control_names)
                if not self._selected_analog_controls and current_action.analog_control_name:
                    self._selected_analog_controls = [current_action.analog_control_name]
                self._selected_analog_control = (
                    self._selected_analog_controls[0]
                    if self._selected_analog_controls
                    else None
                )
            elif current_action.action_type == ActionType.EXEC:
                self._exec_cmd = current_action.cmd or ""
            elif current_action.action_type == ActionType.REPEAT:
                self._repeat_categories = list(
                    current_action.repeat_categories
                    if current_action.repeat_categories is not None
                    else DEFAULT_REPEAT_CATEGORIES
                )
            elif current_action.action_type in (
                ActionType.PROFILE_ENABLE,
                ActionType.PROFILE_DISABLE,
                ActionType.PROFILE_TOGGLE,
            ):
                self._selected_profile_name = str(
                    current_action.profile_name or current_action.target or ""
                )
                self._restore_profile_lifetime(current_action.profile_deactivation)
                if current_action.action_type == ActionType.PROFILE_ENABLE:
                    self._selected_profile_action = "enable"
                elif current_action.action_type == ActionType.PROFILE_DISABLE:
                    self._selected_profile_action = "disable"
                else:
                    self._selected_profile_action = "toggle"
            elif current_action.action_type in (
                ActionType.MOUSE_MOVE_REL,
                ActionType.MOUSE_MOVE_ABS,
                ActionType.MOUSE_MOVE_NATURAL_ABS,
            ):
                self._mouse_move_x = int(current_action.move_x)
                self._mouse_move_y = int(current_action.move_y)
                if current_action.action_type == ActionType.MOUSE_MOVE_REL:
                    self._mouse_move_mode = "rel"
                elif current_action.action_type == ActionType.MOUSE_MOVE_ABS:
                    self._mouse_move_mode = "abs"
                elif current_action.action_type == ActionType.MOUSE_MOVE_NATURAL_ABS:
                    self._mouse_move_mode = "natural"
                    self._mouse_move_speed = float(current_action.move_speed)
                    self._mouse_move_jitter = float(current_action.move_jitter)
                    self._mouse_move_curve = str(current_action.move_curve)
                    self._mouse_move_tolerance = int(current_action.move_tolerance)
                    self._mouse_move_max_duration_ms = int(
                        current_action.move_max_duration_ms
                    )
                    self._mouse_move_stop_on_failure = bool(
                        current_action.move_stop_on_failure
                    )
        if not self._allow_rapidfire:
            self._rapidfire_enabled = False
        if not self._allow_tap:
            self._tap_enabled = False

        self._build_ui()

    def _tab_allowed(self, name: str) -> bool:
        return self._allowed_tabs is None or name in self._allowed_tabs

    def _build_ui(self):
        _ensure_compact_tabs_css()

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        main_box.set_margin_top(12)
        main_box.set_margin_bottom(12)
        main_box.set_margin_start(12)
        main_box.set_margin_end(12)

        self.options_box = self._build_options_box()
        self._compositor_action_pages = build_compositor_action_pages(
            self._current_action,
            self._on_compositor_action_selected,
            self._compositor_action_status,
            capture_position=self._capture_compositor_position,
        )
        self._compositor_action_page_ids = {page.page_id for page in self._compositor_action_pages}

        self.stack = Gtk.Stack()
        self.stack.set_vexpand(True)
        if self._source_type == "analog":
            if self._tab_allowed("analog_presets"):
                self.stack.add_titled(
                    self._build_analog_presets_tab(),
                    "analog_presets",
                    "Presets",
                )
            if self._tab_allowed("analog_control"):
                self.stack.add_titled(
                    self._build_analog_control_tab(),
                    "analog_control",
                    "Analog Controls",
                )
            if self._tab_allowed("special"):
                self.stack.add_titled(self._build_special_tab(), "special", "Special")
        else:
            if self._tab_allowed("special"):
                self.stack.add_titled(self._build_special_tab(), "special", "Special")
            if self._tab_allowed("keyboard"):
                self.stack.add_titled(self._build_keyboard_tab(), "keyboard", "Keyboard")
            if self._tab_allowed("type"):
                self.stack.add_titled(self._build_type_tab(), "type", "Type")
            if self._tab_allowed("navigation"):
                self.stack.add_titled(self._build_navigation_tab(), "navigation", "Navigation")
            if self._tab_allowed("media"):
                self.stack.add_titled(self._build_media_tab(), "media", "Media")
            if self._tab_allowed("mouse"):
                self.stack.add_titled(self._build_mouse_tab(), "mouse", "Mouse")
            for page in self._compositor_action_pages:
                if self._tab_allowed(page.page_id):
                    self.stack.add_titled(page.widget, page.page_id, page.title)
            if self._tab_allowed("gamepad"):
                self.stack.add_titled(self._build_gamepad_tab(), "gamepad", "Gamepad")
            if self._allow_superkey and self._tab_allowed("superkey"):
                self.stack.add_titled(self._build_superkey_tab(), "superkey", "Super Keys")
            if self._tab_allowed("macro"):
                self.stack.add_titled(self._build_macro_tab(), "macro", "Macro")
            if self._tab_allowed("profile"):
                self.stack.add_titled(self._build_profile_tab(), "profile", "Profile")

        self._set_initial_tab()

        frame = Gtk.Frame()
        frame.set_vexpand(True)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title_row.set_halign(Gtk.Align.CENTER)
        title_row.set_margin_top(12)
        title_row.set_margin_bottom(6)

        title_label = Gtk.Label(label=f"Map: {self._button_label}")
        title_label.add_css_class("title-3")
        title_label.set_halign(Gtk.Align.CENTER)
        title_row.append(title_label)

        self._gamepad_output_header = self._build_gamepad_output_header()
        if self._gamepad_output_header is not None:
            title_row.append(self._gamepad_output_header)

        inner.append(title_row)

        if self._current_action:
            current_label = Gtk.Label(label=self._describe_current_action())
            current_label.add_css_class("dim-label")
            current_label.set_halign(Gtk.Align.CENTER)
            current_label.set_margin_bottom(10)
            inner.append(current_label)
        else:
            title_row.set_margin_bottom(12)

        inner.append(Gtk.Separator())

        sidebar = Gtk.StackSidebar()
        sidebar.set_stack(self.stack)
        sidebar.set_size_request(120, -1)

        paned = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        paned.set_vexpand(True)
        paned.append(sidebar)
        paned.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        right_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        right_box.set_hexpand(True)
        right_box.append(self.stack)
        right_box.append(Gtk.Separator())

        options_pad = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        options_pad.set_margin_top(8)
        options_pad.set_margin_bottom(8)
        options_pad.set_margin_start(12)
        options_pad.set_margin_end(12)
        options_pad.append(self.options_box)
        right_box.append(options_pad)

        paned.append(right_box)
        inner.append(paned)

        inner.append(Gtk.Separator())

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer.set_hexpand(True)
        footer.set_margin_top(8)
        footer.set_margin_bottom(8)
        footer.set_margin_start(12)
        footer.set_margin_end(12)

        self.actions_docs_btn = _create_actions_docs_button()
        self.actions_docs_btn.connect("clicked", self._on_actions_docs_clicked)
        footer.append(self.actions_docs_btn)

        footer_spacer = Gtk.Box()
        footer_spacer.set_hexpand(True)
        footer.append(footer_spacer)

        cancel_macro_playback_btn = self._create_cancel_macro_playback_button()
        cancel_macro_playback_btn.set_visible(False)
        self._cancel_macro_playback_btn = cancel_macro_playback_btn
        footer.append(cancel_macro_playback_btn)

        self.map_btn = Gtk.Button(label="Map")
        self.map_btn.add_css_class("suggested-action")
        self.map_btn.set_sensitive(False)
        self.map_btn.set_visible(False)
        self.map_btn.connect("clicked", self._on_map_clicked)
        footer.append(self.map_btn)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", self._on_cancel_clicked)
        footer.append(cancel_btn)
        inner.append(footer)

        frame.set_child(inner)
        main_box.append(frame)
        self.set_child(main_box)

        self.stack.connect("notify::visible-child", self._on_tab_changed)
        self._on_tab_changed(self.stack, None)

    def _on_cancel_clicked(self, _button: Gtk.Button) -> None:
        self.close()

    def _build_special_tab(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)
        box.set_valign(Gtk.Align.CENTER)

        special_buttons_added = False

        if self._allow_clear_mapping:
            clear_label = "Clear Mapping" if self._source_type == "analog" else "Passthrough"
            clear_btn = self._create_key_button(clear_label, "clear_mapping", large=True)
            clear_btn.connect("clicked", self._on_special_clicked, "clear_mapping")
            clear_btn.set_tooltip_text(
                "Do not store a mapping here, so lower-priority profiles can still apply"
            )
            box.append(clear_btn)
            special_buttons_added = True

        if self._allow_suppress:
            suppress_btn = self._create_key_button("Suppress", "suppress", large=True)
            suppress_btn.connect("clicked", self._on_special_clicked, "suppress")
            suppress_btn.set_tooltip_text("Block the button press entirely — nothing is sent")
            box.append(suppress_btn)
            special_buttons_added = True

        if self._source_type == "analog":
            return box

        if self._allow_repeat:
            repeat_btn = Gtk.ToggleButton(label="Repeat Last Action")
            repeat_btn.add_css_class("key-button")
            repeat_btn.set_size_request(200, 50)
            repeat_btn.set_active(
                bool(
                    self._current_action
                    and self._current_action.action_type == ActionType.REPEAT
                )
            )
            repeat_btn.connect("toggled", self._on_repeat_button_toggled)
            repeat_btn.set_tooltip_text("Replay the last remembered mapped action")
            self._repeat_button = repeat_btn
            box.append(repeat_btn)
            box.append(self._build_repeat_section())
            special_buttons_added = True

        exec_label = Gtk.Label(label="Execute Shell Command")
        exec_label.add_css_class("dim-label")
        if special_buttons_added:
            box.append(Gtk.Separator())
        box.append(exec_label)

        exec_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        exec_box.set_halign(Gtk.Align.CENTER)

        self.exec_entry = Gtk.Entry()
        self.exec_entry.set_placeholder_text("e.g., notify-send 'hello'")
        self.exec_entry.set_size_request(300, -1)
        self.exec_entry.set_text(self._exec_cmd)
        self.exec_entry.connect("changed", self._on_exec_text_changed)
        exec_box.append(self.exec_entry)

        self.exec_map_btn = Gtk.Button(label="Map Command")
        self.exec_map_btn.add_css_class("suggested-action")
        self.exec_map_btn.set_sensitive(bool(self._exec_cmd.strip()))
        self.exec_map_btn.connect("clicked", self._on_exec_map_clicked)
        exec_box.append(self.exec_map_btn)

        box.append(exec_box)

        return box

    def _on_special_clicked(self, btn, action_type: str):
        if action_type == "clear_mapping":
            self._emit_selected_action(None)
        elif action_type == "explicit_passthrough":
            self._warn_and_clear_unsupported_rapidfire(ActionType.PASSTHROUGH)
            action = MappingAction(action_type=ActionType.PASSTHROUGH)
            self._emit_selected_action(action)
        elif action_type == "suppress":
            self._warn_and_clear_unsupported_rapidfire(ActionType.SUPPRESS)
            action = MappingAction(action_type=ActionType.SUPPRESS)
            self._emit_selected_action(action)
        elif action_type == "cancel_macro_playback":
            self._warn_and_clear_unsupported_rapidfire(ActionType.CANCEL_MACRO_PLAYBACK)
            action = MappingAction(action_type=ActionType.CANCEL_MACRO_PLAYBACK)
            self._emit_selected_action(action)
        else:
            self.close()

    def _on_tab_changed(self, stack, param):
        child_name = self.stack.get_visible_child_name()
        is_special = child_name == "special"
        is_superkey = child_name == "superkey"
        is_analog_control = child_name == "analog_control"
        is_analog_presets = child_name == "analog_presets"
        is_analog_only = is_analog_control or is_analog_presets
        is_type = child_name == "type"
        is_macro = child_name == "macro"
        is_profile = child_name == "profile"
        is_mouse_move = child_name == "mouse" and self._include_mouse_move_controls
        is_exec = child_name == "exec"
        is_gamepad = child_name == "gamepad"
        is_media = child_name == "media"
        is_compositor_action = child_name in self._compositor_action_page_ids
        has_options = self._allow_rapidfire or self._allow_tap
        # Repeat carries its own rapidfire controls in the Special tab, so the
        # shared footer options only apply to the concrete-input tabs.
        show_options = has_options and not (
            is_special
            or is_superkey
            or is_analog_only
            or is_type
            or is_macro
            or is_profile
            or is_exec
            or is_compositor_action
            or is_media
        )
        self.options_box.set_sensitive(show_options)
        self.options_box.set_visible(show_options)
        self._update_options_visibility()
        self.map_btn.set_visible(
            is_superkey
            or is_analog_control
            or is_type
            or is_macro
            or is_profile
            or is_mouse_move
        )
        self.map_btn.set_label(self._mouse_move_commit_label if is_mouse_move else "Map")
        if self._cancel_macro_playback_btn is not None:
            self._cancel_macro_playback_btn.set_visible(is_macro)
        if is_superkey:
            self.map_btn.set_sensitive(self._selected_superkey is not None)
        elif is_analog_control:
            self.map_btn.set_sensitive(bool(self._selected_analog_controls))
        elif is_type:
            self._maybe_load_type_macro_details()
            self._sync_type_map_button()
        elif is_macro:
            self.map_btn.set_sensitive(self._selected_macro is not None)
        elif is_profile:
            self.map_btn.set_sensitive(bool(self._selected_profile_name))
        elif is_mouse_move:
            self.map_btn.set_sensitive(True)
        else:
            self.map_btn.set_sensitive(False)
        if self._gamepad_output_header is not None:
            self._gamepad_output_header.set_visible(is_gamepad)
        self._update_actions_docs_button()

    def _on_exec_text_changed(self, entry: Gtk.Entry) -> None:
        self.exec_map_btn.set_sensitive(bool(entry.get_text().strip()))

    def _on_exec_map_clicked(self, btn: Gtk.Button) -> None:
        cmd = self.exec_entry.get_text().strip()
        if not cmd:
            return
        self._warn_and_clear_unsupported_rapidfire(ActionType.EXEC)
        action = MappingAction(action_type=ActionType.EXEC, cmd=cmd)
        self._emit_selected_action(action)

    def _on_compositor_action_selected(self, action: MappingAction) -> None:
        self._warn_and_clear_unsupported_rapidfire(ActionType.COMPOSITOR_DISPATCH)
        self._emit_selected_action(action)

    def _on_mouse_move_map_clicked(self, btn) -> None:
        x = int(self.mouse_move_x_spin.get_value())
        y = int(self.mouse_move_y_spin.get_value())
        if self.mouse_move_natural_check.get_active():
            self._warn_and_clear_unsupported_rapidfire(ActionType.MOUSE_MOVE_NATURAL_ABS)
            curve_index = int(self.mouse_move_curve_dropdown.get_selected())
            curve_values = ["linear", "natural"]
            curve = (
                curve_values[curve_index]
                if 0 <= curve_index < len(curve_values)
                else DEFAULT_NATURAL_MOUSE_MOVE_CURVE
            )
            action = MappingAction(
                action_type=ActionType.MOUSE_MOVE_NATURAL_ABS,
                move_x=x,
                move_y=y,
                move_speed=speed_kpx_s_to_px_s(self.mouse_move_speed_spin.get_value()),
                move_jitter=float(self.mouse_move_jitter_spin.get_value()),
                move_curve=curve,
                move_tolerance=int(self.mouse_move_tolerance_spin.get_value()),
                move_max_duration_ms=int(self.mouse_move_duration_spin.get_value()),
                move_stop_on_failure=bool(
                    self.mouse_move_stop_on_failure_check.get_active()
                )
                if hasattr(self, "mouse_move_stop_on_failure_check")
                else False,
            )
            self._emit_selected_action(action)
            return
        if self.mouse_move_abs_check.get_active():
            action_type = ActionType.MOUSE_MOVE_ABS
        else:
            action_type = ActionType.MOUSE_MOVE_REL
        action = MappingAction(
            action_type=action_type,
            move_x=x,
            move_y=y,
            rapidfire_enabled=self._rapidfire_enabled,
            rapidfire_hold_ms=int(self.hold_spin.get_value()),
            rapidfire_wait_ms=int(self.wait_spin.get_value()),
            tap_enabled=self._tap_enabled,
            tap_hold_ms=int(self.tap_spin.get_value()),
        )
        self._emit_selected_action(action)

    def _on_mouse_move_mode_changed(self, check: Gtk.CheckButton) -> None:
        self._update_mouse_move_mode_visibility()

    def _update_mouse_move_mode_visibility(self) -> None:
        is_abs = self.mouse_move_abs_check.get_active()
        is_natural = self.mouse_move_natural_check.get_active()
        uses_absolute_target = is_abs or is_natural
        self.mouse_move_capture_row.set_visible(uses_absolute_target)
        self.mouse_move_natural_options_row.set_visible(is_natural)
        self.mouse_move_natural_options_row_2.set_visible(is_natural)
        if hasattr(self, "mouse_move_stop_on_failure_check"):
            self.mouse_move_stop_on_failure_check.set_visible(
                self._include_mouse_move_failure_controls and is_natural
            )
        if not uses_absolute_target:
            self._cancel_capture_position("")

    def _on_capture_position_clicked(self, btn: Gtk.Button) -> None:
        self._begin_position_capture(
            self.mouse_move_capture_btn,
            self.mouse_move_capture_status,
            self._apply_mouse_move_capture_position,
        )

    def _apply_mouse_move_capture_position(self, x: int, y: int) -> None:
        self.mouse_move_x_spin.set_value(int(x))
        self.mouse_move_y_spin.set_value(int(y))

    def _capture_compositor_position(
        self,
        button: Gtk.Button,
        status_label: Gtk.Label,
        callback: Callable[[int, int], None],
    ) -> None:
        self._begin_position_capture(
            button,
            status_label,
            callback,
        )

    def _sync_position_capture_legacy_state(self) -> None:
        self._capture_timeout_id = self._position_capture.timeout_id
        self._capture_pending = self._position_capture.pending
        self._capture_request_id = self._position_capture.request_id
        self._capture_apply = self._position_capture.apply
        self._capture_status_label = self._position_capture.status_label
        self._capture_button = self._position_capture.button
        self._capture_delay_seconds = self._position_capture.delay_seconds

    def _begin_position_capture(
        self,
        button: Gtk.Button | None,
        status_label: Gtk.Label | None,
        apply_position: PositionCallback,
    ) -> None:
        delay_seconds = (
            float(self.mouse_move_capture_delay_spin.get_value())
            if hasattr(self, "mouse_move_capture_delay_spin")
            else self._capture_delay_seconds
        )
        self._position_capture.begin(
            button=button,
            status_label=status_label,
            delay_seconds=delay_seconds,
            apply_position=apply_position,
        )
        self._sync_position_capture_legacy_state()

    def _on_slurp_capture_result(self, request_id: int, result) -> None:
        self._position_capture.on_slurp_result(request_id, result)
        self._sync_position_capture_legacy_state()

    def _capture_position_after_delay(self, request_id: int) -> bool:
        result = self._position_capture.capture_after_delay(request_id)
        self._sync_position_capture_legacy_state()
        return result

    def _on_capture_position_response(self, request_id: int, response: dict | None) -> bool:
        result = self._position_capture.on_response(request_id, response)
        self._sync_position_capture_legacy_state()
        return result

    def _cancel_capture_position(self, status_text: str) -> None:
        self._position_capture.cancel(status_text)
        self._sync_position_capture_legacy_state()

    def _on_map_clicked(self, btn) -> None:
        child_name = self.stack.get_visible_child_name()
        if child_name == "superkey":
            self._on_superkey_map_clicked(btn)
        elif child_name == "analog_control":
            self._on_analog_control_map_clicked(btn)
        elif child_name == "type":
            self._on_type_map_clicked(btn)
        elif child_name == "macro":
            self._on_macro_map_clicked(btn)
        elif child_name == "profile":
            self._on_profile_map_clicked(btn)
        elif child_name == "mouse":
            self._on_mouse_move_map_clicked(btn)

    def _set_initial_tab(self):
        if self._initial_tab and self._tab_allowed(self._initial_tab):
            self.stack.set_visible_child_name(self._initial_tab)
            return
        if self._source_type == "analog":
            self._set_initial_analog_tab()
            return
        if not self._current_action:
            return
        compositor_tab = compositor_action_tab_name(
            self._current_action,
            self._compositor_action_status,
        )
        if compositor_tab not in self._compositor_action_page_ids:
            compositor_tab = None
        macro_tab = "macro"
        tab_map = {
            ActionType.PASSTHROUGH: "special",
            ActionType.SUPPRESS: "special",
            ActionType.REPEAT: "special",
            ActionType.SUPERKEY: "superkey",
            ActionType.ANALOG_CONTROL: "analog_control",
            ActionType.START_MACRO_RECORDING: "macro",
            ActionType.STOP_MACRO_RECORDING: "macro",
            ActionType.PLAY_MACRO_SLOT: "macro",
            ActionType.CANCEL_MACRO_PLAYBACK: "macro",
            ActionType.EMERGENCY_RESET: "macro",
            ActionType.EXEC: "special",
            ActionType.MPRIS: "media",
            ActionType.KEYBOARD: (
                "media" if self._current_action.target in MEDIA_KEY_TARGETS else "keyboard"
            ),
            ActionType.MOUSE: "mouse",
            ActionType.MOUSE_MOVE_REL: "mouse",
            ActionType.MOUSE_MOVE_ABS: "mouse",
            ActionType.MOUSE_MOVE_NATURAL_ABS: "mouse",
            ActionType.GAMEPAD: "gamepad",
            ActionType.GAMEPAD_AXIS: "gamepad",
            ActionType.MACRO: macro_tab,
            ActionType.PROFILE_ENABLE: "profile",
            ActionType.PROFILE_DISABLE: "profile",
            ActionType.PROFILE_TOGGLE: "profile",
        }
        name = compositor_tab or tab_map.get(self._current_action.action_type)
        if name == "superkey" and not self._allow_superkey:
            return
        if name:
            self.stack.set_visible_child_name(name)

    def _resolve_compositor_action_status(
        self,
        compositor_action_status: dict[str, object] | None,
    ) -> dict[str, bool | str | None]:
        resolved: dict[str, bool | str | None] = {
            "compositor_id": None,
            "listener_name": None,
            "compositor_dispatch_available": False,
        }
        if isinstance(compositor_action_status, dict):
            for key in resolved:
                value = compositor_action_status.get(key)
                if isinstance(value, (bool, str)) or value is None:
                    resolved[key] = value
            return resolved

        root = self._parent.get_root() if hasattr(self._parent, "get_root") else None
        get_status = getattr(root, "get_compositor_action_status", None)
        if callable(get_status):
            status = get_status()
            if isinstance(status, dict):
                for key in resolved:
                    value = status.get(key)
                    if isinstance(value, (bool, str)) or value is None:
                        resolved[key] = value
        return resolved

    def _describe_current_action(self) -> str:
        return describe_mapping_action_verbose(
            self._current_action,
            keyboard_label=lambda value: EVDEV_TO_KEY.get(value, value),
            gamepad_label=lambda value: EVDEV_TO_GAMEPAD.get(value, value),
        )

    def close(self) -> None:
        self._cancel_capture_position("")
        super().close()
