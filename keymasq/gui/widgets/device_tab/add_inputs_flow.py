from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, GLib, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.devices import (
    canonical_gamepad_button_name,
    is_low_res_wheel_evdev,
    normalize_wheel_value,
    resolve_evdev_code,
    resolve_evdev_event_type,
    wheel_button_id,
    wheel_duplicate_key,
    wheel_label,
)
from keymasq.common.models import (
    ButtonDefinition,
    DeviceType,
    EvdevDevice,
    HardwareConfig,
)
from keymasq.gui.session_client import JsonDict
from keymasq.gui.widgets.device_control_layout import device_layout_kind
from keymasq.gui.widgets.device_tab.capture_helpers import (
    _make_capture_status_row,
    _set_capture_status,
    make_unlock_button_content,
)
from keymasq.gui.widgets.device_tab.input_helpers import label_from_evdev

SessionRequestAsync = Callable[[JsonDict, Callable[[JsonDict | None], bool]], object]


@dataclass(frozen=True)
class AddInputsResult:
    buttons: list[ButtonDefinition]
    evdev_devices: list[EvdevDevice]


class AddInputsFlow:
    def __init__(
        self,
        parent_window,
        session_client: SessionRequestAsync,
        hardware_config: HardwareConfig,
        on_complete: Callable[[AddInputsResult], None],
    ) -> None:
        self.parent_window = parent_window
        self._session_request_async = session_client
        self.hardware_config = hardware_config
        self._on_complete = on_complete
        self._poll_id: int | None = None
        self._poll_inflight = False
        self._capturing = False
        self._pending_ids: list[str] = []
        self._capture_active_hardware_id: str | None = None
        self._dialog: Adw.Dialog | None = None
        self._escape_controller: Gtk.EventControllerKey | None = None
        self._escape_root: Gtk.Widget | None = None
        self._captured_buttons: list[ButtonDefinition] = []
        self._captured_evdev_devices: list[EvdevDevice] = []

    def present(self) -> None:
        dialog = Adw.Dialog(
            title="Add Inputs",
            content_width=420,
            content_height=-1,
        )
        dialog.connect("closed", self._on_dialog_closed)
        self._dialog = dialog
        self._install_escape_controller(dialog)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)

        info = Gtk.Label(label=self._summary_text())
        info.set_halign(Gtk.Align.START)
        info.set_wrap(True)
        box.append(info)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.append(Gtk.Label(label=self._count_label()))
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
        box.append(_make_capture_status_row(status))

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_row.set_halign(Gtk.Align.END)
        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", self._on_close_dialog_clicked, dialog)
        btn_row.append(cancel_btn)

        start_btn = Gtk.Button(label="Start Capture")
        start_btn.add_css_class("suggested-action")

        unlock_btn = Gtk.Button()
        unlock_btn.set_child(make_unlock_button_content("Unlock"))
        unlock_btn.set_tooltip_text(
            "Authorize raw original-input capture so Keymasq can detect additional "
            "keys and mouse buttons before remapping."
        )
        unlock_btn.connect(
            "clicked",
            self._on_unlock_clicked,
            start_btn,
            privilege_status,
            status,
        )
        btn_row.append(unlock_btn)

        def on_start(_button) -> None:
            if self._capturing:
                return
            count = int(spin.get_value())
            self._pending_ids = [f"key_added_{i + 1}" for i in range(count)]
            self._captured_buttons = []
            self._captured_evdev_devices = []
            _set_capture_status(status, self._waiting_label(), recording=True)
            start_btn.set_sensitive(False)
            self._start_capture(
                status,
                dialog,
                start_btn=start_btn,
                unlock_btn=unlock_btn,
                privilege_status=privilege_status,
            )

        start_btn.connect("clicked", on_start)
        btn_row.append(start_btn)
        box.append(btn_row)

        self._update_capture_controls(start_btn, unlock_btn, privilege_status)
        dialog.set_child(box)
        dialog.present(self.parent_window)

    def _start_capture(
        self,
        status_label: Gtk.Label,
        parent_dialog: Adw.Dialog,
        *,
        start_btn: Gtk.Button | None = None,
        unlock_btn: Gtk.Button | None = None,
        privilege_status: Gtk.Label | None = None,
    ) -> None:
        self._capture_active_hardware_id = self.hardware_config.hardware_id

        def on_capture_begun(result: JsonDict | None) -> bool:
            return self._on_capture_begun(
                result,
                status_label,
                parent_dialog,
                start_btn=start_btn,
                unlock_btn=unlock_btn,
                privilege_status=privilege_status,
            )

        self._session_request_async(
            {
                "command": "begin_capture",
                "hardware_id": self._capture_active_hardware_id,
                "evdev_paths": [device.path for device in self.hardware_config.evdev_devices],
                "end_on_disconnect": True,
            },
            on_capture_begun,
        )

    def _on_capture_begun(
        self,
        result: JsonDict | None,
        status_label: Gtk.Label,
        parent_dialog: Adw.Dialog,
        *,
        start_btn: Gtk.Button | None = None,
        unlock_btn: Gtk.Button | None = None,
        privilege_status: Gtk.Label | None = None,
    ) -> bool:
        if not result or result.get("status") != "ok":
            _set_capture_status(status_label, (result or {}).get("message", "Capture failed"))
            self.stop_capture()
            self._update_capture_controls(start_btn, unlock_btn, privilege_status)
            return False

        self._capturing = True
        self._poll_id = GLib.timeout_add(
            16,
            self._poll_capture,
            status_label,
            parent_dialog,
            start_btn,
            unlock_btn,
            privilege_status,
        )
        return False

    def _poll_capture(
        self,
        status_label: Gtk.Label,
        parent_dialog: Adw.Dialog,
        start_btn: Gtk.Button | None = None,
        unlock_btn: Gtk.Button | None = None,
        privilege_status: Gtk.Label | None = None,
    ) -> bool:
        if not self._capturing:
            return False

        if self._poll_inflight:
            return True

        self._poll_inflight = True

        def on_capture_read(result: JsonDict | None) -> bool:
            return self._on_capture_read(
                result,
                status_label,
                parent_dialog,
                start_btn=start_btn,
                unlock_btn=unlock_btn,
                privilege_status=privilege_status,
            )

        self._session_request_async(
            {
                "command": "capture_read",
                "hardware_id": self._capture_active_hardware_id,
            },
            on_capture_read,
        )
        return True

    def _on_capture_read(
        self,
        result: JsonDict | None,
        status_label: Gtk.Label,
        parent_dialog: Adw.Dialog,
        *,
        start_btn: Gtk.Button | None = None,
        unlock_btn: Gtk.Button | None = None,
        privilege_status: Gtk.Label | None = None,
    ) -> bool:
        self._poll_inflight = False
        if not self._capturing:
            return False

        if not result:
            return False

        if result.get("status") != "ok":
            _set_capture_status(status_label, result.get("message", "Capture failed"))
            self.stop_capture()
            self._update_capture_controls(start_btn, unlock_btn, privilege_status)
            return False

        captured = result.get("captured")
        if not isinstance(captured, dict):
            return True

        evdev_name = str(captured.get("evdev", ""))
        captured_code = captured.get("code")
        captured_value = captured.get("value")
        if not self._is_supported_added_input(evdev_name):
            _set_capture_status(
                status_label,
                f"Unsupported input '{evdev_name}', press another input",
                recording=True,
            )
            return False

        if self._button_already_exists(evdev_name, captured_code, captured_value):
            _set_capture_status(
                status_label,
                f"{evdev_name} already exists, press another input",
                recording=True,
            )
            if evdev_name == "key_esc":
                self._cancel(parent_dialog)
            return False

        source = captured.get("source")
        stable_path = captured.get("stable_path")
        button_type = self._button_type(evdev_name, source)
        button_id = evdev_name
        button_label = label_from_evdev(evdev_name)
        captured_display = evdev_name
        evdev_value: int | None = None
        if is_low_res_wheel_evdev(evdev_name):
            try:
                raw_value = int(cast(int, captured_value)) if captured_value is not None else None
            except (TypeError, ValueError):
                raw_value = None
            normalized_value = normalize_wheel_value(raw_value)
            button_id = wheel_button_id(evdev_name, normalized_value) or evdev_name
            button_label = wheel_label(evdev_name, normalized_value) or button_label
            captured_display = button_label
            evdev_value = normalized_value
            button_type = "wheel"
        try:
            evdev_code = int(cast(int, captured_code)) if captured_code is not None else None
        except (TypeError, ValueError):
            evdev_code = None
        self._captured_buttons.append(
            ButtonDefinition(
                id=button_id,
                evdev=evdev_name,
                label=button_label,
                evdev_code=evdev_code,
                evdev_value=evdev_value,
                type=button_type,
                source=source,
            )
        )
        self._queue_evdev_interface(evdev_name, source, stable_path)

        if self._pending_ids:
            self._pending_ids.pop(0)
        remaining = len(self._pending_ids)
        _set_capture_status(
            status_label,
            f"Captured {captured_display} ({remaining} remaining)",
            recording=True,
        )

        if remaining == 0:
            self._finish(parent_dialog)
            return False

        return False

    def _finish(self, parent_dialog: Adw.Dialog) -> None:
        result = AddInputsResult(
            buttons=list(self._captured_buttons),
            evdev_devices=list(self._captured_evdev_devices),
        )
        self.stop_capture()
        self._on_complete(result)
        parent_dialog.close()

    def _unlock_state(self) -> tuple[bool, bool, bool]:
        unlock_required = bool(getattr(self.parent_window, "_recording_unlock_required", True))
        recording_unlocked = bool(getattr(self.parent_window, "_recording_unlocked", False))
        refresh_owner = bool(getattr(self.parent_window, "_recording_refresh_owner", False))
        return unlock_required, recording_unlocked, refresh_owner

    def _update_capture_controls(
        self,
        start_btn: Gtk.Button | None,
        unlock_btn: Gtk.Button | None,
        privilege_status: Gtk.Label | None,
    ) -> None:
        if start_btn is None or unlock_btn is None or privilege_status is None:
            return

        unlock_required, recording_unlocked, refresh_owner = self._unlock_state()
        can_capture = not unlock_required or (recording_unlocked and refresh_owner)

        start_btn.set_sensitive(can_capture and not self._capturing)
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
        unlock_btn.set_child(make_unlock_button_content(label))
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

    def _on_unlock_clicked(
        self,
        button: Gtk.Button,
        start_btn: Gtk.Button,
        privilege_status: Gtk.Label,
        status_label: Gtk.Label,
    ) -> None:
        present_unlock = getattr(self.parent_window, "present_unlock_dialog", None)
        if callable(present_unlock):
            present_unlock(
                on_success=lambda: self._on_unlock_success(
                    start_btn,
                    button,
                    privilege_status,
                    status_label,
                )
            )
            return
        _set_capture_status(status_label, "Unlock is only available from the main window.")

    def _on_unlock_success(
        self,
        start_btn: Gtk.Button,
        unlock_btn: Gtk.Button,
        privilege_status: Gtk.Label,
        status_label: Gtk.Label,
    ) -> None:
        _set_capture_status(status_label, "")
        self._update_capture_controls(start_btn, unlock_btn, privilege_status)

    def stop_capture(self) -> None:
        self._capturing = False
        self._poll_inflight = False
        if self._poll_id:
            GLib.source_remove(self._poll_id)
            self._poll_id = None
        self._pending_ids = []
        if self._capture_active_hardware_id:
            self._session_request_async(
                {
                    "command": "end_capture",
                    "hardware_id": self._capture_active_hardware_id,
                },
                self._ignore_session_response,
            )
            self._capture_active_hardware_id = None

    def _on_close_dialog_clicked(self, _button: Gtk.Button, dialog: Adw.Dialog) -> None:
        dialog.close()

    def _on_dialog_closed(self, dialog: Adw.Dialog) -> None:
        self.stop_capture()
        self._remove_escape_controller()
        if self._dialog is dialog:
            self._dialog = None

    def _install_escape_controller(self, dialog: Adw.Dialog) -> None:
        self._remove_escape_controller()
        root = self.parent_window
        if not isinstance(root, Gtk.Widget):
            return

        controller = Gtk.EventControllerKey()
        controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        controller.connect("key-pressed", self._on_key_pressed, dialog)
        root.add_controller(controller)
        self._escape_controller = controller
        self._escape_root = root

    def _remove_escape_controller(self) -> None:
        if self._escape_root and self._escape_controller:
            self._escape_root.remove_controller(self._escape_controller)
        self._escape_controller = None
        self._escape_root = None

    def _on_key_pressed(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        _state: Gdk.ModifierType,
        dialog: Adw.Dialog,
    ) -> bool:
        if keyval != Gdk.KEY_Escape:
            return False
        self._cancel(dialog)
        return True

    def _cancel(self, dialog: Adw.Dialog) -> None:
        self.stop_capture()
        dialog.close()

    def _ignore_session_response(self, _response: JsonDict | None) -> bool:
        return False

    def _queue_evdev_interface(
        self,
        evdev_name: str,
        source: str | None,
        stable_path: str | None,
    ) -> None:
        if not source or not stable_path:
            return

        for dev in [*self.hardware_config.evdev_devices, *self._captured_evdev_devices]:
            if dev.id == source or dev.path == stable_path:
                return

        source_l = source.lower()
        dtype = self._device_type(evdev_name, source_l)

        self._captured_evdev_devices.append(
            EvdevDevice(path=stable_path, device_type=dtype, id=source)
        )

    def _is_supported_added_input(self, evdev_name: str) -> bool:
        if self._layout_kind() == "gamepad":
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
        except (TypeError, ValueError):
            captured_code = None
        try:
            captured_value = int(cast(int, evdev_value)) if evdev_value is not None else None
        except (TypeError, ValueError):
            captured_value = None

        captured_name = canonical_gamepad_button_name(evdev_name)
        captured_event_type = resolve_evdev_event_type(evdev_name)
        captured_wheel_key = wheel_duplicate_key(evdev_name, captured_code, captured_value)
        for button in [*self.hardware_config.buttons, *self._captured_buttons]:
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

    def _summary_text(self) -> str:
        if self._layout_kind() == "gamepad":
            return (
                "Add additional digital gamepad buttons to this config.\n"
                "Press each requested button when prompted."
            )
        return (
            "Add additional keys and mouse buttons to this config.\n"
            "Press each requested input when prompted."
        )

    def _count_label(self) -> str:
        return "Number of inputs:"

    def _waiting_label(self) -> str:
        kind = self._layout_kind()
        if kind == "gamepad":
            return "Recording button presses..."
        if kind == "keyboard":
            return "Recording keys..."
        return "Recording inputs..."

    def _button_type(self, evdev_name: str, source: str | None) -> str:
        if evdev_name.startswith("key_"):
            return "key"
        if self._device_type(evdev_name, source) == DeviceType.GAMEPAD:
            return "gamepad"
        return "mouse"

    def _device_type(
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

        for dev in self.hardware_config.evdev_devices:
            if dev.id == source:
                return dev.device_type

        if evdev_name.startswith("key_"):
            return DeviceType.KEYBOARD
        if self._layout_kind() == "gamepad":
            return DeviceType.GAMEPAD
        if evdev_name.startswith("btn_") or evdev_name.startswith("rel_"):
            return DeviceType.MOUSE
        return DeviceType.OTHER

    def _layout_kind(self) -> str:
        return device_layout_kind(self.hardware_config)
