from typing import cast

import evdev
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, GLib, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keyforge.common.devices import (
    canonical_gamepad_button_name,
    gamepad_button_label,
    is_gamepad_button_name,
    resolve_evdev_code,
)
from keyforge.common.models import (
    ActionType,
    ButtonDefinition,
    DeviceType,
    EvdevDevice,
    HardwareConfig,
    MappingAction,
    is_protected_button,
)
from keyforge.gui.icons import device_icon_names, image_from_icon_names
from keyforge.gui.session_client import (
    JsonDict,
    session_request_async,
)
from keyforge.gui.widgets.action_labels import describe_mapping_action_compact
from keyforge.gui.widgets.key_selector_dialog import KeySelectorDialog
from keyforge.gui.widgets.profile_managed_tab import ProfileManagedTab
from keyforge.session.hardware import HardwareManager
from keyforge.session.profiles import ProfileInfo, ProfileManager


class DeviceTab(ProfileManagedTab):
    def __init__(
        self,
        device: HardwareConfig,
        profile_manager: ProfileManager | None,
        hardware_manager: "HardwareManager | None" = None,
        main_window=None,
        demo_mode: bool = False,
        compositor_capabilities: list[str] | None = None,
    ) -> None:
        self.device = device
        self.hardware_manager = hardware_manager
        super().__init__(
            profile_manager=profile_manager,
            main_window=main_window,
            demo_mode=demo_mode,
            compositor_capabilities=compositor_capabilities,
        )
        self._button_widgets: dict[str, Gtk.Box] = {}
        self._user_interacting = False
        self._keyboard_layout_mode = False
        self._highlight_timeout_ids: list[int] = []
        self._listening_keys = False
        self._add_keys_poll_id = None
        self._add_keys_poll_inflight = False
        self._add_keys_capturing = False
        self._add_keys_pending_ids: list[str] = []
        self._capture_active_hardware_id: str | None = None
        self._listen_controller: Gtk.EventControllerKey | None = None
        self._setup_header()
        self._setup_profile_selector()
        self._setup_button_grid()
        self.refresh_profiles()

        if not self.demo_mode:
            self._check_active_profile()
            GLib.timeout_add(500, self._check_active_profile)

    def _selected_layer(self, create: bool = False):
        if not self._selected_profile:
            return None
        if create:
            return self._selected_profile.config.ensure_layer(self.device.hardware_id)
        return self._selected_profile.config.get_layer(self.device.hardware_id)

    def _append_profile_settings_rows(self, settings_grid: Gtk.Grid, row: int) -> int:
        grab_label = Gtk.Label(label="Grab Mode")
        grab_label.set_halign(Gtk.Align.START)
        grab_label.set_valign(Gtk.Align.CENTER)
        settings_grid.attach(grab_label, 0, row, 1, 1)

        self.always_grab_check = Gtk.CheckButton(label="Always grab all interfaces")
        self.always_grab_check.set_active(False)
        self.always_grab_check.set_tooltip_text(
            "Grab all device interfaces even if not all are used. "
            "Prevents lag when switching between profiles that need different interfaces."
        )
        self.always_grab_check.connect("toggled", self._on_always_grab_toggled)
        settings_grid.attach(self.always_grab_check, 1, row, 1, 1)
        return row + 1

    def _update_extra_profile_settings(self) -> None:
        layer = self._selected_layer()
        self.always_grab_check.handler_block_by_func(self._on_always_grab_toggled)
        self.always_grab_check.set_active(layer.always_grab_all if layer else False)
        self.always_grab_check.handler_unblock_by_func(self._on_always_grab_toggled)

    def _active_profile_names_from_response(self, data: dict) -> list[str]:
        devices = data.get("devices", {})
        if not isinstance(devices, dict):
            return []
        return list(devices.get(self.device.hardware_id, {}).get("profiles", []))

    def _after_profile_selection_applied(self) -> None:
        for button_id in self._button_widgets:
            self._update_button_display(button_id)

    def _on_always_grab_toggled(self, check: Gtk.CheckButton) -> None:
        layer = self._selected_layer(create=True)
        if layer:
            layer.always_grab_all = check.get_active()
            self._save_profile()

    def _setup_header(self) -> None:
        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)

        device_icon = image_from_icon_names(
            *device_icon_names(device_kind=self.device_layout_kind()), pixel_size=32
        )
        device_icon.set_valign(Gtk.Align.CENTER)
        header_box.append(device_icon)

        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)

        name_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        name_label = Gtk.Label(label=self.device.name)
        name_label.add_css_class("title-2")
        name_label.set_halign(Gtk.Align.START)
        name_row.append(name_label)

        if not self.demo_mode:
            delete_btn = Gtk.Button(icon_name="user-trash-symbolic")
            delete_btn.set_tooltip_text("Delete device")
            delete_btn.add_css_class("destructive-action")
            delete_btn.add_css_class("flat")
            delete_btn.set_valign(Gtk.Align.CENTER)
            delete_btn.connect("clicked", self._on_delete_device)
            name_row.append(delete_btn)

        info_box.append(name_row)

        caption = (
            f"{self.device.hardware_id} | {len(self.device.evdev_devices)} evdev, "
            f"{len(self.device.buttons)} buttons"
        )
        caption_label = Gtk.Label(label=caption)
        caption_label.add_css_class("dim-label")
        caption_label.add_css_class("caption")
        caption_label.set_halign(Gtk.Align.START)
        info_box.append(caption_label)

        header_box.append(info_box)
        header_box.set_hexpand(True)

        if not self.demo_mode:
            self.add_keys_btn = Gtk.Button(label=self._add_input_button_label())
            self.add_keys_btn.add_css_class("flat")
            self.add_keys_btn.connect("clicked", self._on_add_keys_clicked)
            header_box.append(self.add_keys_btn)

            if self.is_keyboard_hardware():
                self.listen_btn = Gtk.ToggleButton(label="Listen Keys")
                self.listen_btn.add_css_class("flat")
                self.listen_btn.connect("toggled", self._on_listen_toggled)
                header_box.append(self.listen_btn)

        self.append(header_box)

        self.set_focusable(True)

    def _on_delete_device(self, button: Gtk.Button) -> None:
        dialog = Adw.Dialog(title="Delete Device", content_width=360, content_height=-1)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        content.set_margin_top(20)
        content.set_margin_bottom(20)
        content.set_margin_start(20)
        content.set_margin_end(20)

        message = Gtk.Label(label=f"Delete device '{self.device.name}'?\nThis cannot be undone.")
        message.set_halign(Gtk.Align.START)
        content.append(message)

        delete_profiles_check = Gtk.CheckButton(label="Remove device mappings from profiles")
        delete_profiles_check.set_active(True)
        content.append(delete_profiles_check)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_box.set_halign(Gtk.Align.END)
        btn_box.set_margin_top(8)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", self._on_close_dialog_clicked, dialog)
        btn_box.append(cancel_btn)

        delete_btn = Gtk.Button(label="Delete")
        delete_btn.add_css_class("destructive-action")
        delete_btn.connect(
            "clicked",
            self._on_confirm_delete_device,
            dialog,
            delete_profiles_check,
        )
        btn_box.append(delete_btn)

        content.append(btn_box)

        dialog.set_child(content)
        dialog.present(self.get_root())

    def _on_confirm_delete_device(
        self, button: Gtk.Button, dialog: Adw.Dialog, delete_profiles_check: Gtk.CheckButton
    ) -> None:
        delete_profiles = delete_profiles_check.get_active()
        hardware_id = self.device.hardware_id

        if delete_profiles:
            assert self.profile_manager is not None
            self.profile_manager.remove_device_layers(hardware_id)

        assert self.hardware_manager is not None
        self.hardware_manager.delete_hardware(hardware_id)
        session_request_async({"command": "reload"}, lambda _result: False)

        dialog.close()

        root = self.get_root()
        if root and hasattr(root, "stack"):
            root.stack.remove(self)
            root._check_empty_state()

    def _setup_button_grid(self) -> None:
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_margin_top(12)

        self._keyboard_layout_mode = self.is_keyboard_hardware()

        if self._keyboard_layout_mode:
            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)

            buttons_by_id = {b.id: b for b in self.device.buttons}
            used_ids: set[str] = set()

            self._append_keyboard_section(
                content,
                "Number Keys",
                [
                    [
                        "key_1",
                        "key_2",
                        "key_3",
                        "key_4",
                        "key_5",
                        "key_minus",
                    ],
                    [
                        "key_6",
                        "key_7",
                        "key_8",
                        "key_9",
                        "key_0",
                        "key_equal",
                    ],
                ],
                buttons_by_id,
                used_ids,
                max_cols=6,
                expanded=True,
            )

            self._append_keyboard_section(
                content,
                "Keyboard (Left)",
                [
                    ["key_esc"],
                    ["key_tab", "key_q", "key_w", "key_e", "key_r", "key_t"],
                    ["key_capslock", "key_a", "key_s", "key_d", "key_f", "key_g"],
                    ["key_leftshift", "key_z", "key_x", "key_c", "key_v", "key_b"],
                    [
                        "key_leftctrl",
                        "key_leftmeta",
                        "key_leftalt",
                        "key_space",
                        "key_rightalt",
                        "key_rightctrl",
                    ],
                ],
                buttons_by_id,
                used_ids,
                max_cols=7,
                expanded=True,
            )

            self._append_keyboard_section(
                content,
                "Keyboard (Right)",
                [
                    ["key_backspace", "key_y", "key_u", "key_i", "key_o", "key_p"],
                    ["key_enter", "key_h", "key_j", "key_k", "key_l"],
                    ["key_rightshift", "key_n", "key_m"],
                    ["key_rightmeta"],
                ],
                buttons_by_id,
                used_ids,
                max_cols=6,
            )

            self._append_keyboard_section(
                content,
                "Symbols",
                [
                    [
                        "key_leftbrace",
                        "key_rightbrace",
                        "key_backslash",
                        "key_semicolon",
                        "key_apostrophe",
                    ],
                    ["key_comma", "key_dot", "key_slash"],
                ],
                buttons_by_id,
                used_ids,
                max_cols=5,
            )

            self._append_keyboard_section(
                content,
                "F Row",
                [
                    [
                        "key_f1",
                        "key_f2",
                        "key_f3",
                        "key_f4",
                        "key_f5",
                        "key_f6",
                    ],
                    [
                        "key_f7",
                        "key_f8",
                        "key_f9",
                        "key_f10",
                        "key_f11",
                        "key_f12",
                    ],
                ],
                buttons_by_id,
                used_ids,
                max_cols=6,
            )

            self._append_keyboard_section(
                content,
                "Navigation",
                [
                    ["key_up", "key_down"],
                    ["key_left", "key_right"],
                    ["key_sysrq", "key_scrolllock", "key_pause"],
                    ["key_insert", "key_home", "key_pageup"],
                    ["key_delete", "key_end", "key_pagedown"],
                ],
                buttons_by_id,
                used_ids,
                max_cols=4,
            )

            self._append_keyboard_section(
                content,
                "Special",
                [
                    ["key_numlock", "key_kpslash", "key_kpasterisk", "key_kpminus"],
                    ["key_kp7", "key_kp8", "key_kp9", "key_kpplus"],
                    ["key_kp4", "key_kp5", "key_kp6"],
                    ["key_kp1", "key_kp2", "key_kp3", "key_kpenter"],
                    ["key_kp0", "key_kpdot"],
                ],
                buttons_by_id,
                used_ids,
                max_cols=4,
            )

            extras = [b for b in self.device.buttons if b.id not in used_ids]
            if extras:
                self._append_other_buttons_section(content, extras)

            scrolled.set_child(content)
            self.append(scrolled)
            return

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)

        main_ids = {"btn_left", "btn_right", "btn_middle"}
        scroll_keywords = {"scroll", "wheel"}

        main_buttons = []
        scroll_buttons = []
        other_buttons = []
        extra_buttons = []

        for button in self.device.buttons:
            bid = button.id.lower()
            if bid in main_ids:
                main_buttons.append(button)
            elif any(kw in bid for kw in scroll_keywords):
                scroll_buttons.append(button)
            elif bid in ("btn_side", "btn_extra", "btn_4", "btn_forward", "btn_back"):
                other_buttons.append(button)
            else:
                extra_buttons.append(button)

        self.button_grid = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        def _add_section(title: str, buttons: list, parent: Gtk.Box, max_cols: int = 4) -> None:
            if not buttons:
                return
            if title:
                label = Gtk.Label(label=title)
                label.add_css_class("button-section-title")
                label.set_halign(Gtk.Align.START)
                parent.append(label)
            grid = Gtk.Grid()
            grid.set_column_spacing(12)
            grid.set_row_spacing(12)
            col = 0
            r = 0
            for btn in buttons:
                w = self._create_button_widget(btn)
                grid.attach(w, col, r, 1, 1)
                self._button_widgets[btn.id] = w
                col += 1
                if col >= max_cols:
                    col = 0
                    r += 1
                parent.append(grid)

        if self.is_gamepad_hardware():
            buttons_by_id = {b.id: b for b in self.device.buttons}
            for title, button_ids, max_cols in [
                ("Shoulders", ["btn_tl2", "btn_tl", "btn_tr2", "btn_tr"], 4),
                ("Menu Buttons", ["btn_select", "btn_mode", "btn_start"], 3),
                ("Face Buttons", ["btn_north", "btn_west", "btn_east", "btn_south"], 4),
                ("Stick Clicks", ["btn_thumbl", "btn_thumbr"], 2),
                (
                    "D-Pad",
                    ["btn_dpad_up", "btn_dpad_left", "btn_dpad_right", "btn_dpad_down"],
                    4,
                ),
            ]:
                section_buttons = []
                for button_id in button_ids:
                    button = buttons_by_id.pop(button_id, None)
                    if button is not None:
                        section_buttons.append(button)
                _add_section(title, section_buttons, content, max_cols=max_cols)

            extras = sorted(buttons_by_id.values(), key=lambda button: button.label.lower())
            if extras:
                self._append_other_buttons_section(
                    content,
                    extras,
                    title="Additional Controls",
                    expanded=True,
                    prepend=True,
                )

            scrolled.set_child(content)
            self.append(scrolled)
            return

        if extra_buttons:
            extra_expander = Gtk.Expander(label=f"Extra Buttons ({len(extra_buttons)})")
            extra_expander.set_expanded(True)
            extra_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            _add_section("", extra_buttons, extra_box, max_cols=3)
            extra_expander.set_child(extra_box)
            content.append(extra_expander)

        _add_section("Main Buttons", main_buttons, content)
        _add_section("Scroll", scroll_buttons, content)
        _add_section("Side Buttons", other_buttons, content)

        scrolled.set_child(content)
        self.append(scrolled)

    def is_keyboard_hardware(self) -> bool:
        key_count = sum(1 for b in self.device.buttons if b.id.startswith("key_"))
        return key_count >= 40

    def is_gamepad_hardware(self) -> bool:
        if any(dev.device_type == DeviceType.GAMEPAD for dev in self.device.evdev_devices):
            return True
        return any(is_gamepad_button_name(button.evdev) for button in self.device.buttons)

    def device_layout_kind(self) -> str:
        if self.is_keyboard_hardware():
            return "keyboard"
        if self.is_gamepad_hardware():
            return "gamepad"
        return "mouse"

    def _add_input_button_label(self) -> str:
        return "Add Keys..." if self.is_keyboard_hardware() else "Add Buttons..."

    def _append_keyboard_section(
        self,
        parent: Gtk.Box,
        title: str,
        layout_rows: list[list[str]],
        buttons_by_id: dict,
        used_ids: set[str],
        max_cols: int,
        expanded: bool = False,
    ) -> None:
        grid = self._build_keyboard_grid(layout_rows, buttons_by_id, used_ids, max_cols)

        expander = Gtk.Expander(label=title)
        expander.set_expanded(expanded)
        expander.set_child(grid)
        parent.append(expander)

    def _build_keyboard_grid(
        self,
        layout_rows: list[list[str]],
        buttons_by_id: dict,
        used_ids: set[str],
        max_cols: int,
    ) -> Gtk.Grid:
        grid = Gtk.Grid()
        grid.set_column_spacing(8)
        grid.set_row_spacing(8)

        for row_i, row_items in enumerate(layout_rows):
            col_i = 0
            for button_id in row_items:
                button = buttons_by_id.get(button_id)
                if button is None:
                    spacer = Gtk.Box()
                    spacer.set_size_request(92, -1)
                    grid.attach(spacer, col_i, row_i, 1, 1)
                else:
                    widget = self._create_button_widget(button)
                    grid.attach(widget, col_i, row_i, 1, 1)
                    self._button_widgets[button.id] = widget
                    used_ids.add(button.id)
                col_i += 1

            while col_i < max_cols:
                spacer = Gtk.Box()
                spacer.set_size_request(92, -1)
                grid.attach(spacer, col_i, row_i, 1, 1)
                col_i += 1

        return grid

    def _append_other_buttons_section(
        self,
        parent: Gtk.Box,
        extras: list[ButtonDefinition],
        *,
        title: str = "Extra Keys",
        expanded: bool = False,
        prepend: bool = False,
    ) -> None:
        grid = Gtk.Grid()
        grid.set_column_spacing(8)
        grid.set_row_spacing(8)

        col = 0
        row = 0
        max_cols = 6
        for button in sorted(extras, key=lambda b: b.label.lower()):
            widget = self._create_button_widget(button)
            grid.attach(widget, col, row, 1, 1)
            self._button_widgets[button.id] = widget
            col += 1
            if col >= max_cols:
                col = 0
                row += 1

        expander = Gtk.Expander(label=f"{title} ({len(extras)})")
        expander.set_expanded(expanded)
        expander.set_child(grid)
        if prepend:
            parent.prepend(expander)
        else:
            parent.append(expander)

    def _create_button_widget(self, button) -> Gtk.Widget:
        protected = is_protected_button(button.id)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.add_css_class("card")
        box.add_css_class("button-card-passthrough")
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)

        name_label = Gtk.Label(label=button.label)
        name_label.add_css_class("heading")
        header.append(name_label)

        name_right_click = Gtk.GestureClick()
        name_right_click.set_button(Gdk.BUTTON_SECONDARY)
        name_right_click.connect("pressed", self._on_name_label_right_clicked, button)
        name_label.add_controller(name_right_click)

        if protected:
            lock = Gtk.Image(icon_name="system-lock-screen-symbolic")
            lock.set_pixel_size(12)
            header.append(lock)

        box.append(header)

        action_label = Gtk.Label(label=self._describe_passthrough_output(button))
        action_label.add_css_class("caption")
        action_label.set_halign(Gtk.Align.START)
        box.append(action_label)

        action_right_click = Gtk.GestureClick()
        action_right_click.set_button(Gdk.BUTTON_SECONDARY)
        action_right_click.connect("pressed", self._on_action_label_right_clicked, button)
        action_label.add_controller(action_right_click)

        box._action_label = action_label
        box._name_label = name_label
        box._button_id = button.id
        box._protected = protected

        box.set_size_request(96 if self._keyboard_layout_mode else 140, -1)

        click = Gtk.GestureClick()
        click.connect("pressed", self._on_button_clicked, button, protected)
        box.add_controller(click)

        return box

    def _on_button_clicked(
        self,
        click,
        n_press,
        x,
        y,
        button: ButtonDefinition,
        protected: bool,
    ) -> None:
        if click.get_current_button() != Gdk.BUTTON_PRIMARY:
            return

        if protected and not self._left_right_click_remap_allowed():
            self._show_protected_dialog(button)
            return

        if self._selected_profile is None:
            self._show_no_profile_dialog()
            return

        if protected:
            self._show_protected_remap_warning_dialog(button)
            return

        self._show_function_editor(button)

    def _left_right_click_remap_allowed(self) -> bool:
        if self.main_window is not None:
            getter = getattr(self.main_window, "left_right_click_remap_allowed", None)
            if callable(getter):
                return bool(getter())

        root = self.get_root()
        getter = getattr(root, "left_right_click_remap_allowed", None)
        if callable(getter):
            return bool(getter())

        return False

    def _on_action_label_right_clicked(
        self, click, n_press, x, y, button: ButtonDefinition
    ) -> None:
        if n_press != 1:
            return
        layer = self._selected_layer()
        if self._selected_profile is None:
            return

        mapping = layer.mappings.get(button.id) if layer else None
        if not mapping or mapping.action_type != ActionType.MACRO or not mapping.macro_name:
            return
        macro_name = mapping.macro_name

        def on_macro_loaded(result: JsonDict | None) -> bool:
            return self._on_macro_lookup(result, macro_name, button)

        session_request_async(
            {"command": "get_macro", "name": macro_name},
            on_macro_loaded,
        )

    def _on_macro_lookup(
        self,
        result: JsonDict | None,
        macro_name: str,
        button: ButtonDefinition,
    ) -> bool:
        macro = (result or {}).get("macro")
        if (result or {}).get("status") != "ok" or not isinstance(macro, dict):
            self._show_function_editor(button)
            return False

        from keyforge.gui.widgets.macro_editor_dialog import MacroEditorDialog

        dialog = MacroEditorDialog(self.get_root(), macro_name)
        dialog.present(self.get_root())
        return False

    def _on_name_label_right_clicked(self, click, n_press, x, y, button: ButtonDefinition) -> None:
        if n_press != 1 or self.demo_mode:
            return
        self._show_relabel_dialog(button)

    def _show_relabel_dialog(self, button: ButtonDefinition) -> None:
        dialog = Adw.Dialog(title="Rename Key", content_width=420, content_height=-1)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)

        label = Gtk.Label(label=f"Rename '{button.label}'")
        label.set_halign(Gtk.Align.START)
        box.append(label)

        entry = Gtk.Entry()
        entry.set_text(button.label)
        box.append(entry)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_row.set_halign(Gtk.Align.END)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", self._on_close_dialog_clicked, dialog)
        btn_row.append(cancel_btn)

        save_btn = Gtk.Button(label="Save")
        save_btn.add_css_class("suggested-action")

        def on_save(_btn) -> None:
            new_label = entry.get_text().strip()
            if not new_label:
                return
            for b in self.device.buttons:
                if b.id == button.id:
                    b.label = new_label
                    break
            assert self.hardware_manager is not None
            self.hardware_manager.save_hardware(self.device)
            session_request_async({"command": "reload"}, lambda _result: False)
            widget = self._button_widgets.get(button.id)
            if widget:
                widget._name_label.set_text(new_label)
            dialog.close()

        save_btn.connect("clicked", on_save)
        btn_row.append(save_btn)

        box.append(btn_row)
        dialog.set_child(box)
        dialog.present(self.get_root())

    def _show_protected_dialog(self, button) -> None:
        dialog = Adw.AlertDialog(
            heading="Protected Button",
            body=(
                f"{button.label} cannot be remapped in the GUI unless you explicitly "
                "opt in via /etc/keyforge/security.toml.\n\n"
                "Set [gui] allow_left_right_click_remap = true only if you understand "
                "that remapping left or right click can leave you without a usable "
                "pointer button."
            ),
        )
        dialog.add_response("ok", "OK")
        dialog.present(self.get_root())

    def _show_protected_remap_warning_dialog(self, button: ButtonDefinition) -> None:
        dialog = Adw.AlertDialog(
            heading="Remap Critical Mouse Button?",
            body=(
                f"{button.label} is a critical pointer button. Remapping it can leave "
                "you without a working primary or secondary click in the GUI.\n\n"
                "Continue only if you have another way to recover, such as a second "
                "mouse, keyboard navigation, or direct access to the profile files."
            ),
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("continue", "Continue")
        dialog.set_response_appearance("continue", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.connect("response", self._on_protected_remap_response, button)
        dialog.present(self.get_root())

    def _on_protected_remap_response(
        self,
        _dialog: Adw.AlertDialog,
        response: str,
        button: ButtonDefinition,
    ) -> None:
        if response == "continue":
            self._show_function_editor(button)

    def _show_no_profile_dialog(self) -> None:
        dialog = Adw.AlertDialog(
            heading="No Profile Selected",
            body="Select or create a profile first to edit button mappings.",
        )
        dialog.add_response("ok", "OK")
        dialog.present(self.get_root())

    def _show_profile_error_dialog(self, message: str) -> None:
        dialog = Adw.AlertDialog(
            heading="Invalid Profile Configuration",
            body=message,
        )
        dialog.add_response("ok", "OK")
        dialog.present(self.get_root())

    def _show_function_editor(self, button: ButtonDefinition) -> None:
        current_action = None
        layer = self._selected_layer()
        if layer:
            current_action = layer.mappings.get(button.id)

        def on_key_selected(dialog, action):
            layer = self._selected_layer(create=True)
            if layer is None:
                return
            if action is None:
                if button.id in layer.mappings:
                    del layer.mappings[button.id]
            else:
                layer.mappings[button.id] = action
            self._save_profile()
            self._update_button_display(button.id)

        dialog = KeySelectorDialog(self, button.label, current_action)
        dialog.connect("key-selected", on_key_selected)
        dialog.present(self.get_root())

    def _profile_info_by_name(self, profile_name: str) -> ProfileInfo | None:
        if self.profile_manager:
            return self.profile_manager.get_profile(profile_name)
        for profile in self.profiles:
            if profile and profile.config.name == profile_name:
                return profile
        return None

    def _get_effective_mapping_for_button(
        self, button_id: str
    ) -> tuple[str | None, MappingAction | None]:
        winner_profile_name: str | None = None
        winner_mapping: MappingAction | None = None

        for profile_name in self._active_profile_names:
            profile = self._profile_info_by_name(profile_name)
            if profile is None:
                continue
            layer = profile.config.get_layer(self.device.hardware_id)
            if layer is None:
                continue
            mapping = layer.mappings.get(button_id)
            if mapping is None:
                continue
            if mapping.action_type == ActionType.PASSTHROUGH:
                winner_profile_name = profile_name
                winner_mapping = mapping
            else:
                winner_profile_name = profile_name
                winner_mapping = mapping

        return winner_profile_name, winner_mapping

    def _describe_mapping(
        self,
        mapping: MappingAction,
        button: ButtonDefinition | None = None,
    ) -> str:
        if mapping.action_type == ActionType.PASSTHROUGH and button is not None:
            return self._describe_passthrough_output(button)
        return describe_mapping_action_compact(mapping, include_state=True)

    def _describe_passthrough_output(self, button: ButtonDefinition) -> str:
        if button.evdev in {"rel_wheel", "rel_hwheel"} and button.evdev_value is not None:
            if button.evdev == "rel_wheel":
                return "↑ Scroll Up" if button.evdev_value > 0 else "↓ Scroll Down"
            return "→ Scroll Right" if button.evdev_value > 0 else "← Scroll Left"
        return f"→ {self._label_from_evdev(button.evdev)}"

    def _update_button_display(self, button_id: str) -> None:
        widget = self._button_widgets.get(button_id)
        if not widget:
            return

        action_label = widget._action_label
        name_label = widget._name_label
        mapping = None

        layer = self._selected_layer()
        if layer:
            mapping = layer.mappings.get(button_id)

        button = next(
            (candidate for candidate in self.device.buttons if candidate.id == button_id),
            None,
        )
        if button is None:
            return

        winner_profile_name, winner_mapping = self._get_effective_mapping_for_button(button_id)

        for cls in (
            "button-card-mapped",
            "button-card-mapped-active",
            "button-card-mapped-inactive",
            "button-card-passthrough",
        ):
            widget.remove_css_class(cls)
        for cls in ("success", "dim-label"):
            action_label.remove_css_class(cls)
            name_label.remove_css_class(cls)

        if mapping:
            action_label.set_text(self._describe_mapping(mapping, button))
            if self._selected_profile and winner_profile_name == self._selected_profile.config.name:
                action_label.add_css_class("success")
                widget.add_css_class("button-card-mapped-active")
                if winner_mapping is not None:
                    widget.set_tooltip_text("Currently active binding")
                else:
                    widget.set_tooltip_text(None)
            else:
                action_label.add_css_class("dim-label")
                name_label.add_css_class("dim-label")
                widget.add_css_class("button-card-mapped-inactive")
                if winner_profile_name and winner_mapping is not None:
                    widget.set_tooltip_text(
                        f"Active binding: {self._describe_mapping(winner_mapping, button)} "
                        f"from {winner_profile_name}"
                    )
                else:
                    widget.set_tooltip_text("This binding is not currently active")
        else:
            action_label.set_text(self._describe_passthrough_output(button))
            action_label.add_css_class("dim-label")
            widget.add_css_class("button-card-passthrough")
            if winner_profile_name and winner_mapping is not None:
                widget.set_tooltip_text(
                    f"Active binding: {self._describe_mapping(winner_mapping, button)} "
                    f"from {winner_profile_name}"
                )
            else:
                widget.set_tooltip_text(None)

    def _on_listen_toggled(self, btn: Gtk.ToggleButton) -> None:
        self._listening_keys = btn.get_active()
        root = self.get_root()
        if self._listening_keys:
            btn.set_label("Listening...")
            if root and self._listen_controller is None:
                listen_controller = Gtk.EventControllerKey()
                listen_controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
                listen_controller.connect("key-pressed", self._on_key_pressed)
                root.add_controller(listen_controller)
                self._listen_controller = listen_controller
        else:
            btn.set_label("Listen Keys")
            if root and self._listen_controller is not None:
                root.remove_controller(self._listen_controller)
                self._listen_controller = None

    def _on_key_pressed(self, controller, keyval, keycode, state) -> bool:
        if not self._listening_keys or not self._keyboard_layout_mode:
            return False

        button_id = self._keycode_to_button_id(keycode) or self._keyval_to_button_id(keyval)
        if button_id:
            self._highlight_button(button_id)
            if hasattr(self, "listen_btn"):
                self.listen_btn.set_label(f"Listening: {button_id}")
        return False

    def _keycode_to_button_id(self, keycode: int) -> str | None:
        if keycode <= 0:
            return None

        evdev_code = keycode - 8
        if evdev_code <= 0:
            return None

        key_name = evdev.ecodes.KEY.get(evdev_code)
        if isinstance(key_name, str) and key_name.startswith("KEY_"):
            return key_name.lower()
        return None

    def _keyval_to_button_id(self, keyval: int) -> str | None:
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
            "insert": "key_insert",
            "delete": "key_delete",
            "home": "key_home",
            "end": "key_end",
            "page_up": "key_pageup",
            "page_down": "key_pagedown",
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

    def _highlight_button(self, button_id: str) -> None:
        widget = self._button_widgets.get(button_id)
        if not widget:
            return

        widget._name_label.add_css_class("success")

        def clear() -> bool:
            widget._name_label.remove_css_class("success")
            return False

        tid = GLib.timeout_add(220, clear)
        self._highlight_timeout_ids.append(tid)

    def _on_add_keys_clicked(self, btn: Gtk.Button) -> None:
        dialog = Adw.Dialog(
            title=self._add_input_dialog_title(),
            content_width=420,
            content_height=-1,
        )
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)

        info = Gtk.Label(label=self._add_input_summary_text())
        info.set_halign(Gtk.Align.START)
        info.set_wrap(True)
        box.append(info)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.append(Gtk.Label(label=self._add_input_count_label()))
        spin = Gtk.SpinButton()
        spin.set_adjustment(Gtk.Adjustment(value=4, lower=1, upper=64, step_increment=1))
        spin.set_digits(0)
        row.append(spin)
        box.append(row)

        status = Gtk.Label(label="")
        status.add_css_class("dim-label")
        status.set_halign(Gtk.Align.START)
        box.append(status)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_row.set_halign(Gtk.Align.END)
        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", self._on_close_dialog_clicked, dialog)
        btn_row.append(cancel_btn)

        start_btn = Gtk.Button(label="Start Capture")
        start_btn.add_css_class("suggested-action")

        def on_start(_b) -> None:
            if self._add_keys_capturing:
                return
            count = int(spin.get_value())
            status.set_text(self._capture_waiting_label())
            start_btn.set_sensitive(False)
            cancel_btn.set_sensitive(False)
            self._start_add_keys_capture(count, status, dialog)

        start_btn.connect("clicked", on_start)
        btn_row.append(start_btn)
        box.append(btn_row)

        dialog.set_child(box)
        dialog.present(self.get_root())

    def _start_add_keys_capture(
        self, count: int, status_label: Gtk.Label, parent_dialog: Adw.Dialog
    ) -> None:
        vid = self.device.vendor_id
        pid = self.device.product_id
        self._capture_active_hardware_id = f"{vid}:{pid}"
        self._add_keys_pending_ids = [f"key_added_{i + 1}" for i in range(count)]
        def on_capture_begun(result: JsonDict | None) -> bool:
            return self._on_add_keys_capture_begun(result, status_label, parent_dialog)

        session_request_async(
            {
                "command": "begin_capture",
                "hardware_id": self._capture_active_hardware_id,
            },
            on_capture_begun,
        )

    def _on_add_keys_capture_begun(
        self, result: dict | None, status_label: Gtk.Label, parent_dialog: Adw.Dialog
    ) -> bool:
        if not result or result.get("status") != "ok":
            status_label.set_text((result or {}).get("message", "Capture failed"))
            self._stop_add_keys_capture()
            return False

        self._add_keys_capturing = True
        self._add_keys_poll_id = GLib.timeout_add(
            16, self._poll_add_keys_capture, status_label, parent_dialog
        )
        return False

    def _poll_add_keys_capture(self, status_label: Gtk.Label, parent_dialog: Adw.Dialog) -> bool:
        if not self._add_keys_capturing:
            return False

        if self._add_keys_poll_inflight:
            return True

        self._add_keys_poll_inflight = True
        def on_capture_read(result: JsonDict | None) -> bool:
            return self._on_add_keys_capture_read(result, status_label, parent_dialog)

        session_request_async(
            {
                "command": "capture_read",
                "hardware_id": self._capture_active_hardware_id,
            },
            on_capture_read,
        )
        return True

    def _on_add_keys_capture_read(
        self, result: dict | None, status_label: Gtk.Label, parent_dialog: Adw.Dialog
    ) -> bool:
        self._add_keys_poll_inflight = False
        if not self._add_keys_capturing:
            return False

        if not result:
            return False

        if result.get("status") != "ok":
            status_label.set_text(result.get("message", "Capture failed"))
            self._stop_add_keys_capture()
            return False

        captured = result.get("captured")
        if not isinstance(captured, dict):
            return True

        evdev_name = str(captured.get("evdev", ""))
        captured_code = captured.get("code")
        if not self._is_supported_added_input(evdev_name):
            status_label.set_text(f"Unsupported input '{evdev_name}', press another input")
            return False

        if self._button_already_exists(evdev_name, captured_code):
            status_label.set_text(f"{evdev_name} already exists, press another input")
            return False

        source = captured.get("source")
        stable_path = captured.get("stable_path")
        button_type = self._added_input_button_type(evdev_name, source)
        self.device.buttons.append(
            ButtonDefinition(
                id=evdev_name,
                evdev=evdev_name,
                label=self._label_from_evdev(evdev_name),
                evdev_code=int(captured_code) if captured_code is not None else None,
                type=button_type,
                source=source,
            )
        )
        self._ensure_evdev_interface_for_capture(evdev_name, source, stable_path)

        if self._add_keys_pending_ids:
            self._add_keys_pending_ids.pop(0)
        remaining = len(self._add_keys_pending_ids)
        status_label.set_text(f"Captured {evdev_name} ({remaining} remaining)")

        if remaining == 0:
            self._finish_add_keys(parent_dialog)
            return False

        return False

    def _finish_add_keys(self, parent_dialog: Adw.Dialog) -> None:
        self._stop_add_keys_capture()
        assert self.hardware_manager is not None
        self.hardware_manager.save_hardware(self.device)
        parent_dialog.close()
        self._reload_ui()

    def _stop_add_keys_capture(self) -> None:
        self._add_keys_capturing = False
        self._add_keys_poll_inflight = False
        if self._add_keys_poll_id:
            GLib.source_remove(self._add_keys_poll_id)
            self._add_keys_poll_id = None
        self._add_keys_pending_ids = []
        if self._capture_active_hardware_id:
            session_request_async(
                {
                    "command": "end_capture",
                    "hardware_id": self._capture_active_hardware_id,
                },
                self._ignore_session_response,
            )
            self._capture_active_hardware_id = None

    def _on_close_dialog_clicked(self, _button: Gtk.Button, dialog: Adw.Dialog) -> None:
        dialog.close()

    def _ignore_session_response(self, _response: JsonDict | None) -> bool:
        return False

    def _label_from_evdev(self, evdev_name: str) -> str:
        gamepad_label = gamepad_button_label(evdev_name)
        if gamepad_label:
            return gamepad_label
        if evdev_name == "btn_left":
            return "Left Click"
        if evdev_name == "btn_right":
            return "Right Click"
        if evdev_name == "btn_middle":
            return "Middle Click"
        if evdev_name == "btn_side":
            return "Back"
        if evdev_name == "btn_extra":
            return "Forward"
        if evdev_name == "rel_wheel":
            return "Scroll Wheel"
        if evdev_name == "rel_hwheel":
            return "Scroll Horizontal"

        token = evdev_name.upper()
        if token.startswith("KEY_"):
            token = token[4:]
        if token.startswith("BTN_"):
            token = token[4:]
        token = token.replace("LEFT", "Left ").replace("RIGHT", "Right ")
        token = token.replace("CTRL", "Ctrl").replace("ALT", "Alt")
        token = token.replace("META", "Meta").replace("SHIFT", "Shift")
        token = token.replace("PAGEUP", "Page Up").replace("PAGEDOWN", "Page Down")
        return token.replace("_", " ").strip().title()

    def _is_supported_added_input(self, evdev_name: str) -> bool:
        if self.is_gamepad_hardware():
            return evdev_name.startswith("btn_")
        if self.is_keyboard_hardware():
            return (
                evdev_name.startswith("key_")
                or evdev_name.startswith("btn_")
                or evdev_name in {"rel_wheel", "rel_hwheel"}
            )
        return evdev_name.startswith("btn_") or evdev_name in {"rel_wheel", "rel_hwheel"}

    def _button_already_exists(self, evdev_name: str, evdev_code: object | None) -> bool:
        try:
            captured_code = int(cast(int, evdev_code)) if evdev_code is not None else None
        except Exception:
            captured_code = None

        captured_name = canonical_gamepad_button_name(evdev_name)
        for button in self.device.buttons:
            existing_code = button.evdev_code
            if existing_code is None:
                existing_code = resolve_evdev_code(button.evdev)

            if (
                captured_code is not None
                and existing_code is not None
                and existing_code == captured_code
            ):
                return True

            if canonical_gamepad_button_name(button.evdev) == captured_name:
                return True

        return False

    def _ensure_evdev_interface_for_capture(
        self,
        evdev_name: str,
        source: str | None,
        stable_path: str | None,
    ) -> None:
        if not source or not stable_path:
            return

        for dev in self.device.evdev_devices:
            if dev.id == source or dev.path == stable_path:
                return

        source_l = source.lower()
        dtype = self._added_input_device_type(evdev_name, source_l)

        self.device.evdev_devices.append(
            EvdevDevice(path=stable_path, device_type=dtype, id=source)
        )

    def _add_input_dialog_title(self) -> str:
        return "Add Keys" if self.is_keyboard_hardware() else "Add Buttons"

    def _add_input_summary_text(self) -> str:
        if self.is_gamepad_hardware():
            return (
                "Add additional digital gamepad buttons to this config.\n"
                "Press each requested button when prompted."
            )
        if self.is_keyboard_hardware():
            return (
                "Add additional keyboard keys or extra buttons to this config.\n"
                "Press each requested input when prompted."
            )
        return (
            "Add additional mouse buttons or wheel inputs to this config.\n"
            "Press each requested input when prompted."
        )

    def _add_input_count_label(self) -> str:
        if self.is_keyboard_hardware():
            return "Number of inputs:"
        return "Number of buttons:"

    def _capture_waiting_label(self) -> str:
        if self.is_gamepad_hardware():
            return "Waiting for button presses..."
        if self.is_keyboard_hardware():
            return "Waiting for key presses..."
        return "Waiting for button presses..."

    def _added_input_button_type(self, evdev_name: str, source: str | None) -> str:
        if evdev_name.startswith("key_"):
            return "key"
        if self._added_input_device_type(evdev_name, source) == DeviceType.GAMEPAD:
            return "gamepad"
        return "mouse"

    def _added_input_device_type(
        self,
        evdev_name: str,
        source: str | None,
    ) -> DeviceType:
        source_l = str(source or "").lower()
        if source_l in {"kbd", "keyboard"} or "kbd" in source_l:
            return DeviceType.KEYBOARD
        if source_l == "mouse" or "mouse" in source_l:
            return DeviceType.MOUSE
        if source_l == "joystick" or "joystick" in source_l:
            return DeviceType.GAMEPAD

        for dev in self.device.evdev_devices:
            if dev.id == source:
                return dev.device_type

        if evdev_name.startswith("key_"):
            return DeviceType.KEYBOARD
        if self.is_gamepad_hardware():
            return DeviceType.GAMEPAD
        if evdev_name.startswith("btn_") or evdev_name.startswith("rel_"):
            return DeviceType.MOUSE
        return DeviceType.OTHER

    def _reload_ui(self) -> None:
        selected_name = self._selected_profile.config.name if self._selected_profile else None
        selected_name = self._window_selected_profile_name() or selected_name
        if self._listening_keys:
            self._listening_keys = False
            root = self.get_root()
            if root and self._listen_controller is not None:
                root.remove_controller(self._listen_controller)
            self._listen_controller = None
        assert self.profile_manager is not None
        self.profiles = self.profile_manager.list_profiles()
        while child := self.get_first_child():
            self.remove(child)
        self._button_widgets = {}
        self._setup_header()
        self._setup_profile_selector()
        self._setup_button_grid()
        if selected_name:
            for i, name in enumerate(self._profile_names):
                if name == selected_name:
                    self.profile_dropdown.set_selected(i)
                    break
        self._apply_profile_selection()
