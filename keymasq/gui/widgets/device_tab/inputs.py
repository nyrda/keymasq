"""Input relabel and delete workflow."""

import logging
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.model.hardware import AnalogInputDefinition, ButtonDefinition
from keymasq.gui.widgets.device_tab import rename_dialogs

log = logging.getLogger(__name__)


class InputInventoryMixin:
    def _on_analog_name_right_clicked(
        self: Any,
        click,
        n_press,
        x,
        y,
        analog: AnalogInputDefinition,
    ) -> None:
        if n_press != 1 or self.demo_mode:
            return
        self._show_analog_relabel_dialog(analog)

    def _on_name_label_right_clicked(
        self: Any,
        click,
        n_press,
        x,
        y,
        button: ButtonDefinition,
    ) -> None:
        if n_press != 1 or self.demo_mode:
            return
        self._show_relabel_dialog(button)

    def _show_relabel_dialog(self: Any, button: ButtonDefinition) -> None:
        if self.hardware_manager is None:
            return
        rename_dialogs.present_button_relabel_dialog(
            parent=self.get_root(),
            button=button,
            on_delete_clicked=self._on_delete_button_clicked,
            on_save=self._rename_button_label,
            on_close_clicked=self._on_close_dialog_clicked,
        )

    def _rename_button_label(
        self: Any,
        button: ButtonDefinition,
        new_label: str,
    ) -> bool:
        new_label = new_label.strip()
        if not new_label:
            return False
        if self.hardware_manager is None:
            log.warning("Cannot rename button %s without a hardware manager", button.id)
            return False
        for item in self.device.buttons:
            if item.id == button.id:
                item.label = new_label
                break
        self.hardware_manager.save_hardware(self.device)
        self._request_session_async({"command": "reload"}, self._ignore_session_response)
        widget = self._button_widgets.get(button.id)
        if widget:
            widget._name_label.set_text(new_label)
        return True

    def _show_analog_relabel_dialog(self: Any, analog: AnalogInputDefinition) -> None:
        if self.hardware_manager is None:
            return
        rename_dialogs.present_analog_relabel_dialog(
            parent=self.get_root(),
            analog=analog,
            on_delete_clicked=self._on_delete_analog_clicked,
            on_save=self._rename_analog_label,
            on_close_clicked=self._on_close_dialog_clicked,
        )

    def _rename_analog_label(
        self: Any,
        analog: AnalogInputDefinition,
        new_label: str,
    ) -> bool:
        new_label = new_label.strip()
        if not new_label:
            return False
        if self.hardware_manager is None:
            log.warning("Cannot rename analog input %s without a hardware manager", analog.id)
            return False
        for item in self.device.analog_inputs:
            if item.id == analog.id:
                item.label = new_label
                break
        self.hardware_manager.save_hardware(self.device)
        self._request_session_async({"command": "reload"}, self._ignore_session_response)
        widget = self._button_widgets.get(analog.id)
        if widget:
            widget._name_label.set_text(new_label)
        return True

    def _on_delete_button_clicked(
        self: Any,
        _button: Gtk.Button,
        dialog: Adw.Dialog,
        button: ButtonDefinition,
    ) -> None:
        self._delete_button(button, dialog)

    def _delete_button(
        self: Any,
        button: ButtonDefinition,
        dialog: Adw.Dialog,
    ) -> None:
        if self.hardware_manager is None:
            log.warning("Cannot delete button %s without a hardware manager", button.id)
            dialog.close()
            return
        original_count = len(self.device.buttons)
        self.device.buttons = [item for item in self.device.buttons if item.id != button.id]
        if len(self.device.buttons) == original_count:
            dialog.close()
            return

        if self.profile_manager is not None:
            self.profile_manager.remove_device_button_mappings(
                self.device.hardware_id,
                button.id,
            )
        self.hardware_manager.save_hardware(self.device)
        self._request_session_async({"command": "reload"}, self._ignore_session_response)
        dialog.close()
        if self.profile_manager is not None:
            self._reload_ui()

    def _on_delete_analog_clicked(
        self: Any,
        _button: Gtk.Button,
        dialog: Adw.Dialog,
        analog: AnalogInputDefinition,
    ) -> None:
        self._delete_analog(analog, dialog)

    def _delete_analog(
        self: Any,
        analog: AnalogInputDefinition,
        dialog: Adw.Dialog,
    ) -> None:
        if self.hardware_manager is None:
            log.warning("Cannot delete analog input %s without a hardware manager", analog.id)
            dialog.close()
            return
        original_count = len(self.device.analog_inputs)
        self.device.analog_inputs = [
            item for item in self.device.analog_inputs if item.id != analog.id
        ]
        if len(self.device.analog_inputs) == original_count:
            dialog.close()
            return

        if self.profile_manager is not None:
            self.profile_manager.remove_device_button_mappings(
                self.device.hardware_id,
                analog.id,
            )
        self.hardware_manager.save_hardware(self.device)
        self._request_session_async({"command": "reload"}, self._ignore_session_response)
        dialog.close()
        if self.profile_manager is not None:
            self._reload_ui()
