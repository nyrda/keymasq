from typing import cast

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, GLib, Gtk, Pango  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.devices import (
    canonical_gamepad_button_name,
    gamepad_button_label,
    is_gamepad_button_name,
    is_low_res_wheel_evdev,
    normalize_wheel_value,
    resolve_evdev_code,
    resolve_evdev_event_type,
    wheel_button_id,
    wheel_duplicate_key,
    wheel_label,
)
from keymasq.common.models import (
    ActionType,
    ButtonDefinition,
    DeviceType,
    EvdevDevice,
    HardwareConfig,
    MappingAction,
    is_protected_button,
)
from keymasq.gui.icons import device_icon_names, image_from_icon_names
from keymasq.gui.session_client import (
    JsonDict,
    session_request_async,
)
from keymasq.gui.widgets.action_labels import describe_mapping_action_compact
from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog
from keymasq.gui.widgets.profile_managed_tab import ProfileManagedTab
from keymasq.session.hardware import HardwareManager
from keymasq.session.profiles import ProfileInfo, ProfileManager

_ADD_INPUTS_TOOLTIP = "Capture additional physical buttons or keys for this device"
_KEYBOARD_BUTTON_CARD_WIDTH = 112
_KEYBOARD_LABEL_CHARS = 12
_KEYBOARD_ACTION_SUMMARY_CHARS = 16
_POINTER_BUTTON_CARD_WIDTH = 187
_POINTER_NAME_LABEL_CHARS = 20
_POINTER_ACTION_SUMMARY_CHARS = 31
_ACTION_SUMMARY_MARKER = " [...] "
_COMPACT_ACTION_WORDS = {
    "recording": "rec",
    "playback": "play",
    "profile": "prof",
}


def _char_middle_shorten_text(text: str, max_chars: int, marker: str) -> str:
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    if max_chars <= 3:
        return text[:max_chars]
    if max_chars <= len(marker) + 2:
        return text[: max_chars - 3] + "..."

    budget = max_chars - len(marker)
    head_len = max(1, (budget + 1) // 2)
    tail_len = max(1, budget - head_len)
    return f"{text[:head_len]}{marker}{text[-tail_len:]}"


def _compact_action_words(text: str, max_chars: int) -> str | None:
    words = text.split()
    if len(words) < 2:
        return None

    if words[0] == "→" and any(word in {"⚡", "↓"} for word in words[2:]):
        compact_text = " ".join(words[1:])
        if len(compact_text) <= max_chars:
            return compact_text

    compact_words = [
        _COMPACT_ACTION_WORDS.get(word.lower(), word)
        for word in words
    ]
    compact_text = " ".join(compact_words)
    if compact_text != text and len(compact_text) <= max_chars:
        return compact_text
    return None


def _middle_shorten_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text

    compact = _compact_action_words(text, max_chars)
    if compact is not None:
        return compact

    marker = _ACTION_SUMMARY_MARKER if max_chars >= 20 else "..."

    words = text.split()
    if len(words) < 3:
        return _char_middle_shorten_text(text, max_chars, marker)

    budget = max_chars - len(marker)
    if budget <= 2:
        return _char_middle_shorten_text(text, max_chars, marker)

    suffix_words: list[str] = []
    suffix_len = 0
    suffix_target = max(9, min(12, budget // 3))
    for word in reversed(words):
        next_len = len(word) + (1 if suffix_words else 0)
        if suffix_words and suffix_len + next_len > suffix_target:
            break
        if suffix_len + next_len > budget - 2:
            break
        suffix_words.insert(0, word)
        suffix_len += next_len

    if not suffix_words:
        return _char_middle_shorten_text(text, max_chars, marker)

    prefix_budget = budget - suffix_len
    if prefix_budget <= 0:
        return _char_middle_shorten_text(text, max_chars, marker)

    prefix_words: list[str] = []
    prefix_len = 0
    prefix_limit = len(words) - len(suffix_words)
    for word in words[:prefix_limit]:
        next_len = len(word) + (1 if prefix_words else 0)
        if prefix_words and prefix_len + next_len > prefix_budget:
            break
        if not prefix_words and next_len > prefix_budget:
            break
        prefix_words.append(word)
        prefix_len += next_len

    if not prefix_words:
        return _char_middle_shorten_text(text, max_chars, marker)

    shortened = f"{' '.join(prefix_words)}{marker}{' '.join(suffix_words)}"
    if len(shortened) <= max_chars:
        return shortened
    return _char_middle_shorten_text(text, max_chars, marker)


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
        self._button_widgets: dict[str, Gtk.Button] = {}
        self._user_interacting = False
        self._keyboard_layout_mode = False
        self._highlight_timeout_ids: list[int] = []
        self._add_keys_poll_id = None
        self._add_keys_poll_inflight = False
        self._add_keys_capturing = False
        self._add_keys_pending_ids: list[str] = []
        self._capture_active_hardware_id: str | None = None
        self._add_inputs_dialog: Adw.Dialog | None = None
        self._add_inputs_escape_controller: Gtk.EventControllerKey | None = None
        self._add_inputs_escape_root: Gtk.Widget | None = None
        self._setup_header()
        self._setup_profile_selector()
        self._setup_button_grid()
        self.refresh_profiles()

    def _selected_layer(self, create: bool = False):
        if not self._selected_profile:
            return None
        if create:
            return self._selected_profile.config.ensure_layer(self.device.hardware_id)
        return self._selected_profile.config.get_layer(self.device.hardware_id)

    def _append_profile_settings_groups(self, container: Gtk.Box) -> None:
        self.always_grab_checks: dict[str, Adw.SwitchRow] = {}

        grab_group = Adw.PreferencesGroup()
        self.always_grab_group = grab_group
        self._sync_always_grab_device_list()

        if not hasattr(self, "always_grab_check"):
            self.always_grab_check = Adw.SwitchRow(title=self._device_grab_label_text())

        container.append(grab_group)

    def _update_extra_profile_settings(self) -> None:
        self._sync_always_grab_device_list()
        for hardware_id, switch_row in self.always_grab_checks.items():
            layer = self._profile_layer_for_hardware(hardware_id)
            switch_row.handler_block_by_func(self._on_always_grab_toggled)
            switch_row.set_active(layer.always_grab_all if layer else False)
            switch_row.handler_unblock_by_func(self._on_always_grab_toggled)

    def _active_profile_names_from_response(self, data: dict) -> list[str]:
        devices = data.get("devices", {})
        if not isinstance(devices, dict):
            return []
        return list(devices.get(self.device.hardware_id, {}).get("profiles", []))

    def _active_profiles_summary_title(self) -> str:
        return "Applied profiles:"

    def _active_profiles_summary_tooltip(self) -> str:
        return (
            "Profiles currently applied to this device. "
            "Enabled profiles without mappings are not listed."
        )

    def _active_profiles_empty_tooltip(self) -> str:
        return "No profiles are currently applied to this device."

    def _active_profiles_layer_tooltip(self) -> str:
        return (
            "Applied profiles. Layer order: "
            + " -> ".join(self._active_profile_names)
        )

    def _after_profile_selection_applied(self) -> None:
        for button_id in self._button_widgets:
            self._update_button_display(button_id)
        self._update_header_caption()

    def _after_active_profiles_changed(self) -> None:
        for button_id in self._button_widgets:
            self._update_button_display(button_id)
        self._update_header_caption()

    def _count_mapped_buttons(self) -> int:
        layer = self._selected_layer()
        if not layer:
            return 0
        return sum(
            1
            for mapping in layer.mappings.values()
            if mapping.action_type != ActionType.PASSTHROUGH
        )

    def _update_header_caption(self) -> None:
        mapped = self._count_mapped_buttons()
        total = len(self.device.buttons)
        base = (
            f"{self.device.hardware_id} | {len(self.device.evdev_devices)} evdev, "
            f"{total} buttons"
        )
        if mapped > 0:
            caption = f"{base} · {mapped} mapped"
        else:
            caption = base
        self._header_caption_label.set_text(caption)

    def _profile_layer_for_hardware(self, hardware_id: str, create: bool = False):
        if not self._selected_profile:
            return None
        if create:
            return self._selected_profile.config.ensure_layer(hardware_id)
        return self._selected_profile.config.get_layer(hardware_id)

    def _profile_settings_devices(self) -> list[HardwareConfig]:
        devices: list[HardwareConfig] = []
        seen: set[str] = set()

        root = self.main_window or self.get_root()
        stack = getattr(root, "stack", None)
        if stack is not None:
            child = stack.get_first_child()
            while child is not None:
                device = getattr(child, "device", None)
                hardware_id = getattr(device, "hardware_id", None)
                if (
                    isinstance(device, HardwareConfig)
                    and isinstance(hardware_id, str)
                    and hardware_id not in seen
                ):
                    devices.append(device)
                    seen.add(hardware_id)
                child = child.get_next_sibling()

        if self.device.hardware_id not in seen:
            devices.append(self.device)

        return devices

    def _sync_always_grab_device_list(self) -> None:
        if not hasattr(self, "always_grab_checks"):
            return

        devices = self._profile_settings_devices()
        current_ids = {device.hardware_id for device in devices}
        for hardware_id, switch_row in list(self.always_grab_checks.items()):
            if hardware_id in current_ids:
                continue
            self.always_grab_group.remove(switch_row)
            del self.always_grab_checks[hardware_id]

        for device in self._profile_settings_devices():
            switch_row = self.always_grab_checks.get(device.hardware_id)
            if switch_row is None:
                switch_row = Adw.SwitchRow()
                switch_row.set_tooltip_text(
                    "Grab all device interfaces even if not all are used. "
                    "Prevents lag when switching between profiles that need different interfaces."
                )
                switch_row.connect(
                    "notify::active", self._on_always_grab_toggled, device.hardware_id
                )
                self.always_grab_group.add(switch_row)
                self.always_grab_checks[device.hardware_id] = switch_row
            switch_row.set_title(self._device_grab_label_text(device))
            if device.hardware_id == self.device.hardware_id:
                self.always_grab_check = switch_row

    def _on_always_grab_toggled(
        self,
        switch_row: Adw.SwitchRow,
        _param,
        hardware_id: str | None = None,
    ) -> None:
        layer = self._profile_layer_for_hardware(
            hardware_id or self.device.hardware_id,
            create=True,
        )
        if layer:
            layer.always_grab_all = switch_row.get_active()
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

        self.device_name_label = Gtk.Label(label=self.device.name)
        self.device_name_label.add_css_class("title-2")
        self.device_name_label.set_halign(Gtk.Align.START)
        self.device_name_label.set_ellipsize(Pango.EllipsizeMode.END)
        if not self.demo_mode:
            self.device_name_label.set_tooltip_text("Right-click to rename device")
            name_right_click = Gtk.GestureClick()
            name_right_click.set_button(Gdk.BUTTON_SECONDARY)
            name_right_click.connect("pressed", self._on_device_name_right_clicked)
            self.device_name_label.add_controller(name_right_click)
        name_row.append(self.device_name_label)

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
        self._header_caption_label = Gtk.Label(label=caption)
        self._header_caption_label.add_css_class("dim-label")
        self._header_caption_label.add_css_class("caption")
        self._header_caption_label.set_halign(Gtk.Align.START)
        info_box.append(self._header_caption_label)

        header_box.append(info_box)
        header_box.set_hexpand(True)

        self.append(header_box)

        self.set_focusable(True)

    def _device_grab_label_text(self, device: HardwareConfig | None = None) -> str:
        device = device or self.device
        iface_count = len(device.evdev_devices)
        if iface_count > 1:
            return f"Always grab all {iface_count} interfaces of {device.name}"
        return f"Always grab {device.name}"

    def _on_device_name_right_clicked(self, click, n_press, x, y) -> None:
        if n_press != 1 or self.demo_mode:
            return
        self._show_device_rename_dialog()

    def _show_device_rename_dialog(self) -> None:
        dialog = Adw.Dialog(title="Rename Device", content_width=420, content_height=-1)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)

        label = Gtk.Label(label=f"Rename '{self.device.name}'")
        label.set_halign(Gtk.Align.START)
        box.append(label)

        entry = Gtk.Entry()
        entry.set_text(self.device.name)
        entry.set_activates_default(True)
        box.append(entry)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_row.set_halign(Gtk.Align.END)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", self._on_close_dialog_clicked, dialog)
        btn_row.append(cancel_btn)

        save_btn = Gtk.Button(label="Save")
        save_btn.add_css_class("suggested-action")
        save_btn.set_receives_default(True)

        def on_save(_btn) -> None:
            if self._rename_device(entry.get_text()):
                dialog.close()

        save_btn.connect("clicked", on_save)
        btn_row.append(save_btn)

        box.append(btn_row)
        dialog.set_child(box)
        dialog.present(self.get_root())

    def _rename_device(self, new_name: str) -> bool:
        new_name = new_name.strip()
        if not new_name:
            return False
        if new_name == self.device.name:
            return True

        self.device.name = new_name
        assert self.hardware_manager is not None
        self.hardware_manager.save_hardware(self.device)
        session_request_async({"command": "reload"}, lambda _result: False)
        self._update_device_name_display()
        self._notify_device_renamed()
        return True

    def _update_device_name_display(self) -> None:
        self.device_name_label.set_text(self.device.name)
        if hasattr(self, "always_grab_check"):
            self.always_grab_check.set_title(self._device_grab_label_text())
        self._sync_always_grab_device_list()

    def _notify_device_renamed(self) -> None:
        target = self.main_window or self.get_root()
        updater = getattr(target, "update_device_display_name", None)
        if callable(updater):
            updater(self.device.hardware_id, self.device.name)

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
                    ["key_esc", "key_grave"],
                    ["key_tab", "key_q", "key_w", "key_e", "key_r", "key_t"],
                    ["key_capslock", "key_a", "key_s", "key_d", "key_f", "key_g"],
                    ["key_leftshift", "key_z", "key_x", "key_c", "key_v", "key_b"],
                    [
                        "key_leftctrl",
                        "key_leftmeta",
                        "key_leftalt",
                        "key_space",
                    ],
                ],
                buttons_by_id,
                used_ids,
                max_cols=6,
                expanded=True,
            )

            self._append_keyboard_section(
                content,
                "Keyboard (Right)",
                [
                    ["key_backspace", "key_y", "key_u", "key_i", "key_o", "key_p"],
                    ["key_enter", "key_h", "key_j", "key_k", "key_l"],
                    ["key_n", "key_m", "key_rightshift"],
                    ["key_rightmeta", "key_rightalt", "key_rightctrl"],
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
                self._append_other_buttons_section(
                    content,
                    extras,
                    title="Extra Buttons",
                    expanded=True,
                    prepend=True,
                )

            self._append_learn_tile(content)
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

            self._append_learn_tile(content)
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

        self._append_learn_tile(content)
        scrolled.set_child(content)
        self.append(scrolled)

    def _learn_label_noun(self) -> str:
        if self.is_keyboard_hardware():
            return "Keys"
        return "Buttons"

    def _learn_label_text(self) -> str:
        return f"Learn {self._learn_label_noun()}"

    def _make_icon_label_box(self, icon_name: str, label_text: str) -> Gtk.Box:
        inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        inner.set_halign(Gtk.Align.CENTER)
        inner.set_valign(Gtk.Align.CENTER)

        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.set_pixel_size(16)
        inner.append(icon)

        label = Gtk.Label(label=label_text)
        inner.append(label)
        return inner

    def _create_learn_tile(self) -> Gtk.Button:
        btn = Gtk.Button()
        btn.add_css_class("button-card-learn")
        btn.set_halign(Gtk.Align.START)
        btn.set_tooltip_text(_ADD_INPUTS_TOOLTIP)
        btn.connect("clicked", self._on_add_keys_clicked)
        inner = self._make_icon_label_box("list-add-symbolic", self._learn_label_text())
        btn.set_child(inner)
        return btn

    def _append_learn_tile(self, parent: Gtk.Box) -> None:
        if self.demo_mode:
            return
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        row.set_margin_top(8)
        row.append(self._create_learn_tile())
        parent.append(row)

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
        expander.add_css_class("device-section-expander")
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
        grid.set_column_spacing(4)
        grid.set_row_spacing(4)

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
        grid.set_column_spacing(4)
        grid.set_row_spacing(4)

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
        expander.add_css_class("device-section-expander")
        expander.set_expanded(expanded)
        expander.set_child(grid)
        if prepend:
            parent.prepend(expander)
        else:
            parent.append(expander)

    def _create_button_widget(self, button) -> Gtk.Button:
        protected = is_protected_button(button.id)

        btn = Gtk.Button()
        btn.add_css_class("card")
        btn.add_css_class("button-card-passthrough")
        btn.set_margin_top(2)
        btn.set_margin_bottom(2)
        btn.set_margin_start(2)
        btn.set_margin_end(2)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_halign(Gtk.Align.FILL)
        box.set_valign(Gtk.Align.CENTER)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)

        name_label = Gtk.Label(label=button.label)
        name_label.add_css_class("heading")
        name_label.set_xalign(0.0)
        name_label.set_ellipsize(Pango.EllipsizeMode.END)
        name_label.set_width_chars(
            _KEYBOARD_LABEL_CHARS
            if self._keyboard_layout_mode
            else _POINTER_NAME_LABEL_CHARS
        )
        name_label.set_max_width_chars(
            _KEYBOARD_LABEL_CHARS
            if self._keyboard_layout_mode
            else _POINTER_NAME_LABEL_CHARS
        )
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
        action_label.add_css_class("button-card-action-label")
        action_label.set_halign(Gtk.Align.START)
        action_label.set_xalign(0.0)
        action_label.set_single_line_mode(True)
        action_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        action_label.set_width_chars(
            _KEYBOARD_ACTION_SUMMARY_CHARS
            if self._keyboard_layout_mode
            else _POINTER_ACTION_SUMMARY_CHARS
        )
        action_label.set_max_width_chars(
            _KEYBOARD_ACTION_SUMMARY_CHARS
            if self._keyboard_layout_mode
            else _POINTER_ACTION_SUMMARY_CHARS
        )
        box.append(action_label)

        action_right_click = Gtk.GestureClick()
        action_right_click.set_button(Gdk.BUTTON_SECONDARY)
        action_right_click.connect("pressed", self._on_action_label_right_clicked, button)
        action_label.add_controller(action_right_click)

        btn._action_label = action_label
        btn._name_label = name_label
        btn._button_id = button.id
        btn._protected = protected

        btn.set_size_request(
            _KEYBOARD_BUTTON_CARD_WIDTH
            if self._keyboard_layout_mode
            else _POINTER_BUTTON_CARD_WIDTH,
            -1,
        )
        btn.set_child(box)
        btn.connect("clicked", self._on_mapping_button_clicked, button, protected)

        return btn

    def _on_mapping_button_clicked(
        self,
        _button_widget: Gtk.Button,
        button: ButtonDefinition,
        protected: bool,
    ) -> None:
        self._activate_mapping_button(button, protected)

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

        self._activate_mapping_button(button, protected)

    def _activate_mapping_button(self, button: ButtonDefinition, protected: bool) -> None:
        if self._selected_profile is None:
            self._show_no_profile_dialog()
            return

        if protected:
            self._show_protected_remap_warning_dialog(button)
            return

        self._show_function_editor(button)

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

        from keymasq.gui.widgets.macro_editor_dialog import MacroEditorDialog

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
        btn_row.set_halign(Gtk.Align.FILL)

        delete_btn = Gtk.Button(label="Delete")
        delete_btn.add_css_class("destructive-action")
        delete_btn.connect("clicked", self._on_delete_button_clicked, dialog, button)
        btn_row.append(delete_btn)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        btn_row.append(spacer)

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

    def _on_delete_button_clicked(
        self,
        _button: Gtk.Button,
        dialog: Adw.Dialog,
        button: ButtonDefinition,
    ) -> None:
        self._delete_button(button, dialog)

    def _delete_button(self, button: ButtonDefinition, dialog: Adw.Dialog) -> None:
        original_count = len(self.device.buttons)
        self.device.buttons = [
            existing for existing in self.device.buttons if existing.id != button.id
        ]
        if len(self.device.buttons) == original_count:
            dialog.close()
            return

        if self.profile_manager is not None:
            self.profile_manager.remove_device_button_mappings(self.device.hardware_id, button.id)

        assert self.hardware_manager is not None
        self.hardware_manager.save_hardware(self.device)
        session_request_async({"command": "reload"}, lambda _result: False)
        dialog.close()
        if self.profile_manager is not None:
            self._reload_ui()

    def _show_protected_remap_warning_dialog(self, button: ButtonDefinition) -> None:
        dialog = Adw.AlertDialog(
            heading="Remap Critical Mouse Button?",
            body=(
                f"{button.label} is a critical pointer button. Remapping it can remove "
                "your normal left or right click <b>everywhere</b>.\n\n"
                "Continue only if you have a reliable recovery path, such as another "
                "mouse, keyboard navigation, or direct access to the profile files."
            ),
        )
        dialog.set_body_use_markup(True)
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
            self._update_header_caption()

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

    def _set_action_label_text(self, label: Gtk.Label, text: str) -> None:
        max_chars = (
            _KEYBOARD_ACTION_SUMMARY_CHARS
            if self._keyboard_layout_mode
            else _POINTER_ACTION_SUMMARY_CHARS
        )
        shortened = _middle_shorten_text(text, max_chars)
        label.set_text(shortened)
        label.set_tooltip_text(text if shortened != text else None)

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
            description = self._describe_mapping(mapping, button)
            self._set_action_label_text(action_label, description)
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
            self._set_action_label_text(
                action_label,
                self._describe_passthrough_output(button),
            )
            action_label.add_css_class("dim-label")
            widget.add_css_class("button-card-passthrough")
            if winner_profile_name and winner_mapping is not None:
                widget.set_tooltip_text(
                    f"Active binding: {self._describe_mapping(winner_mapping, button)} "
                    f"from {winner_profile_name}"
                )
            else:
                widget.set_tooltip_text(None)

    def _on_add_keys_clicked(self, _button: Gtk.Button | None) -> None:
        dialog = Adw.Dialog(
            title="Add Inputs",
            content_width=420,
            content_height=-1,
        )
        dialog.connect("closed", self._on_add_inputs_dialog_closed)
        self._add_inputs_dialog = dialog
        self._install_add_inputs_escape_controller(dialog)
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
        spin.set_adjustment(Gtk.Adjustment(value=1, lower=1, upper=64, step_increment=1))
        spin.set_digits(0)
        row.append(spin)
        box.append(row)

        privilege_status = Gtk.Label(label="")
        privilege_status.add_css_class("dim-label")
        privilege_status.set_halign(Gtk.Align.START)
        privilege_status.set_wrap(True)
        box.append(privilege_status)

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

        unlock_btn = Gtk.Button()
        unlock_btn.set_child(self._make_unlock_button_content("Unlock"))
        unlock_btn.set_tooltip_text(
            "Authorize raw original-input capture so Keymasq can detect additional "
            "keys and mouse buttons before remapping."
        )
        unlock_btn.connect(
            "clicked",
            self._on_add_inputs_unlock_clicked,
            start_btn,
            privilege_status,
            status,
        )
        btn_row.append(unlock_btn)

        def on_start(_b) -> None:
            if self._add_keys_capturing:
                return
            count = int(spin.get_value())
            status.set_text(self._capture_waiting_label())
            start_btn.set_sensitive(False)
            self._start_add_keys_capture(
                count,
                status,
                dialog,
                start_btn=start_btn,
                unlock_btn=unlock_btn,
                privilege_status=privilege_status,
            )

        start_btn.connect("clicked", on_start)
        btn_row.append(start_btn)
        box.append(btn_row)

        self._update_add_inputs_capture_controls(start_btn, unlock_btn, privilege_status)
        dialog.set_child(box)
        dialog.present(self.get_root())

    def _start_add_keys_capture(
        self,
        count: int,
        status_label: Gtk.Label,
        parent_dialog: Adw.Dialog,
        *,
        start_btn: Gtk.Button | None = None,
        unlock_btn: Gtk.Button | None = None,
        privilege_status: Gtk.Label | None = None,
    ) -> None:
        vid = self.device.vendor_id
        pid = self.device.product_id
        self._capture_active_hardware_id = f"{vid}:{pid}"
        self._add_keys_pending_ids = [f"key_added_{i + 1}" for i in range(count)]
        def on_capture_begun(result: JsonDict | None) -> bool:
            return self._on_add_keys_capture_begun(
                result,
                status_label,
                parent_dialog,
                start_btn=start_btn,
                unlock_btn=unlock_btn,
                privilege_status=privilege_status,
            )

        session_request_async(
            {
                "command": "begin_capture",
                "hardware_id": self._capture_active_hardware_id,
            },
            on_capture_begun,
        )

    def _on_add_keys_capture_begun(
        self,
        result: dict | None,
        status_label: Gtk.Label,
        parent_dialog: Adw.Dialog,
        *,
        start_btn: Gtk.Button | None = None,
        unlock_btn: Gtk.Button | None = None,
        privilege_status: Gtk.Label | None = None,
    ) -> bool:
        if not result or result.get("status") != "ok":
            status_label.set_text((result or {}).get("message", "Capture failed"))
            self._stop_add_keys_capture()
            self._update_add_inputs_capture_controls(start_btn, unlock_btn, privilege_status)
            return False

        self._add_keys_capturing = True
        self._add_keys_poll_id = GLib.timeout_add(
            16,
            self._poll_add_keys_capture,
            status_label,
            parent_dialog,
            start_btn,
            unlock_btn,
            privilege_status,
        )
        return False

    def _poll_add_keys_capture(
        self,
        status_label: Gtk.Label,
        parent_dialog: Adw.Dialog,
        start_btn: Gtk.Button | None = None,
        unlock_btn: Gtk.Button | None = None,
        privilege_status: Gtk.Label | None = None,
    ) -> bool:
        if not self._add_keys_capturing:
            return False

        if self._add_keys_poll_inflight:
            return True

        self._add_keys_poll_inflight = True
        def on_capture_read(result: JsonDict | None) -> bool:
            return self._on_add_keys_capture_read(
                result,
                status_label,
                parent_dialog,
                start_btn=start_btn,
                unlock_btn=unlock_btn,
                privilege_status=privilege_status,
            )

        session_request_async(
            {
                "command": "capture_read",
                "hardware_id": self._capture_active_hardware_id,
            },
            on_capture_read,
        )
        return True

    def _on_add_keys_capture_read(
        self,
        result: dict | None,
        status_label: Gtk.Label,
        parent_dialog: Adw.Dialog,
        *,
        start_btn: Gtk.Button | None = None,
        unlock_btn: Gtk.Button | None = None,
        privilege_status: Gtk.Label | None = None,
    ) -> bool:
        self._add_keys_poll_inflight = False
        if not self._add_keys_capturing:
            return False

        if not result:
            return False

        if result.get("status") != "ok":
            status_label.set_text(result.get("message", "Capture failed"))
            self._stop_add_keys_capture()
            self._update_add_inputs_capture_controls(start_btn, unlock_btn, privilege_status)
            return False

        captured = result.get("captured")
        if not isinstance(captured, dict):
            return True

        evdev_name = str(captured.get("evdev", ""))
        captured_code = captured.get("code")
        captured_value = captured.get("value")
        if not self._is_supported_added_input(evdev_name):
            status_label.set_text(f"Unsupported input '{evdev_name}', press another input")
            return False

        if self._button_already_exists(evdev_name, captured_code, captured_value):
            status_label.set_text(f"{evdev_name} already exists, press another input")
            if evdev_name == "key_esc":
                self._cancel_add_inputs(parent_dialog)
            return False

        source = captured.get("source")
        stable_path = captured.get("stable_path")
        button_type = self._added_input_button_type(evdev_name, source)
        button_id = evdev_name
        button_label = self._label_from_evdev(evdev_name)
        captured_display = evdev_name
        evdev_value: int | None = None
        if is_low_res_wheel_evdev(evdev_name):
            normalized_value = normalize_wheel_value(
                int(cast(int, captured_value)) if captured_value is not None else None
            )
            button_id = wheel_button_id(evdev_name, normalized_value) or evdev_name
            button_label = wheel_label(evdev_name, normalized_value) or button_label
            captured_display = button_label
            evdev_value = normalized_value
            button_type = "wheel"
        self.device.buttons.append(
            ButtonDefinition(
                id=button_id,
                evdev=evdev_name,
                label=button_label,
                evdev_code=int(captured_code) if captured_code is not None else None,
                evdev_value=evdev_value,
                type=button_type,
                source=source,
            )
        )
        self._ensure_evdev_interface_for_capture(evdev_name, source, stable_path)

        if self._add_keys_pending_ids:
            self._add_keys_pending_ids.pop(0)
        remaining = len(self._add_keys_pending_ids)
        status_label.set_text(f"Captured {captured_display} ({remaining} remaining)")

        if remaining == 0:
            self._finish_add_keys(parent_dialog)
            return False

        return False

    def _finish_add_keys(self, parent_dialog: Adw.Dialog) -> None:
        self._stop_add_keys_capture()
        assert self.hardware_manager is not None
        self.hardware_manager.save_hardware(self.device)
        session_request_async({"command": "reload"}, self._ignore_session_response)
        parent_dialog.close()
        self._reload_ui()

    def _add_inputs_unlock_state(self) -> tuple[bool, bool, bool]:
        root = self.main_window or self.get_root()
        unlock_required = bool(getattr(root, "_recording_unlock_required", True))
        recording_unlocked = bool(getattr(root, "_recording_unlocked", False))
        refresh_owner = bool(getattr(root, "_recording_refresh_owner", False))
        return unlock_required, recording_unlocked, refresh_owner

    def _update_add_inputs_capture_controls(
        self,
        start_btn: Gtk.Button | None,
        unlock_btn: Gtk.Button | None,
        privilege_status: Gtk.Label | None,
    ) -> None:
        if start_btn is None or unlock_btn is None or privilege_status is None:
            return

        unlock_required, recording_unlocked, refresh_owner = self._add_inputs_unlock_state()
        can_capture = not unlock_required or (recording_unlocked and refresh_owner)

        start_btn.set_sensitive(can_capture and not self._add_keys_capturing)
        if can_capture:
            start_btn.add_css_class("suggested-action")
        else:
            start_btn.remove_css_class("suggested-action")

        if not unlock_required:
            unlock_btn.set_visible(False)
            privilege_status.set_text(
                "Unlock not required. Add-input capture reads raw key events before remapping."
            )
            return

        if can_capture:
            unlock_btn.set_visible(False)
            privilege_status.set_text(
                "Original-input capture is unlocked. Add inputs reads raw key events before "
                "remapping."
            )
            return

        unlock_btn.set_visible(True)
        label = "Claim" if recording_unlocked else "Unlock"
        unlock_btn.set_child(self._make_unlock_button_content(label))
        if recording_unlocked:
            unlock_btn.set_tooltip_text(
                "Claim this GUI as the active owner before capturing additional inputs."
            )
            privilege_status.set_text(
                "Unlock active in another session. Claim unlock to add additional keys and "
                "mouse buttons."
            )
        else:
            unlock_btn.set_tooltip_text(
                "Authorize raw original-input capture so Keymasq can detect additional "
                "keys and mouse buttons before remapping."
            )
            privilege_status.set_text(
                "Original-input capture uses privileged raw events. Unlock to add additional "
                "keys and mouse buttons."
            )

    def _make_unlock_button_content(self, label: str) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        icon = Gtk.Image.new_from_icon_name("channel-insecure-symbolic")
        box.append(icon)
        lbl = Gtk.Label(label=label)
        box.append(lbl)
        return box

    def _on_add_inputs_unlock_clicked(
        self,
        button: Gtk.Button,
        start_btn: Gtk.Button,
        privilege_status: Gtk.Label,
        status_label: Gtk.Label,
    ) -> None:
        root = self.main_window or self.get_root()
        present_unlock = getattr(root, "present_unlock_dialog", None)
        if callable(present_unlock):
            present_unlock(
                on_success=lambda: self._on_add_inputs_unlock_success(
                    start_btn,
                    button,
                    privilege_status,
                    status_label,
                )
            )
            return
        status_label.set_text("Unlock is only available from the main window.")

    def _on_add_inputs_unlock_success(
        self,
        start_btn: Gtk.Button,
        unlock_btn: Gtk.Button,
        privilege_status: Gtk.Label,
        status_label: Gtk.Label,
    ) -> None:
        status_label.set_text("")
        self._update_add_inputs_capture_controls(start_btn, unlock_btn, privilege_status)

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

    def _on_add_inputs_dialog_closed(self, dialog: Adw.Dialog) -> None:
        self._stop_add_keys_capture()
        self._remove_add_inputs_escape_controller()
        if self._add_inputs_dialog is dialog:
            self._add_inputs_dialog = None

    def _install_add_inputs_escape_controller(self, dialog: Adw.Dialog) -> None:
        self._remove_add_inputs_escape_controller()
        root = self.get_root()
        if not isinstance(root, Gtk.Widget):
            return

        controller = Gtk.EventControllerKey()
        controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        controller.connect("key-pressed", self._on_add_inputs_key_pressed, dialog)
        root.add_controller(controller)
        self._add_inputs_escape_controller = controller
        self._add_inputs_escape_root = root

    def _remove_add_inputs_escape_controller(self) -> None:
        if self._add_inputs_escape_root and self._add_inputs_escape_controller:
            self._add_inputs_escape_root.remove_controller(self._add_inputs_escape_controller)
        self._add_inputs_escape_controller = None
        self._add_inputs_escape_root = None

    def _on_add_inputs_key_pressed(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        _state: Gdk.ModifierType,
        dialog: Adw.Dialog,
    ) -> bool:
        if keyval != Gdk.KEY_Escape:
            return False
        self._cancel_add_inputs(dialog)
        return True

    def _cancel_add_inputs(self, dialog: Adw.Dialog) -> None:
        self._stop_add_keys_capture()
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
        return (
            evdev_name.startswith("key_")
            or evdev_name.startswith("btn_")
            or is_low_res_wheel_evdev(evdev_name)
        )

    def _button_already_exists(
        self,
        evdev_name: str,
        evdev_code: object | None,
        evdev_value: object | None = None,
    ) -> bool:
        try:
            captured_code = int(cast(int, evdev_code)) if evdev_code is not None else None
        except Exception:
            captured_code = None
        try:
            captured_value = int(cast(int, evdev_value)) if evdev_value is not None else None
        except Exception:
            captured_value = None

        captured_name = canonical_gamepad_button_name(evdev_name)
        captured_event_type = resolve_evdev_event_type(evdev_name)
        captured_wheel_key = wheel_duplicate_key(evdev_name, captured_code, captured_value)
        for button in self.device.buttons:
            existing_code = button.evdev_code
            if existing_code is None:
                existing_code = resolve_evdev_code(button.evdev)
            existing_event_type = resolve_evdev_event_type(button.evdev)

            if captured_wheel_key is not None:
                existing_wheel_key = wheel_duplicate_key(
                    button.evdev,
                    existing_code,
                    button.evdev_value,
                )
                if existing_wheel_key == captured_wheel_key:
                    return True
                if (
                    existing_event_type == captured_event_type
                    and existing_code == captured_code
                    and is_low_res_wheel_evdev(button.evdev)
                    and button.evdev_value is None
                ):
                    return True
                continue

            if (
                captured_code is not None
                and existing_code is not None
                and existing_code == captured_code
                and existing_event_type == captured_event_type
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

    def _add_input_summary_text(self) -> str:
        if self.is_gamepad_hardware():
            return (
                "Add additional digital gamepad buttons to this config.\n"
                "Press each requested button when prompted."
            )
        return (
            "Add additional keys and mouse buttons to this config.\n"
            "Press each requested input when prompted."
        )

    def _add_input_count_label(self) -> str:
        return "Number of inputs:"

    def _capture_waiting_label(self) -> str:
        if self.is_gamepad_hardware():
            return "Waiting for button presses..."
        return "Waiting for inputs..."

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
