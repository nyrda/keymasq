"""Mapping interaction, editor launch, and grid presentation."""

from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.model.actions import MappingAction
from keymasq.common.model.core import ActionType
from keymasq.common.model.hardware import AnalogInputDefinition, ButtonDefinition
from keymasq.common.model.motion import MotionSensorDefinition
from keymasq.gui.session_client import JsonDict
from keymasq.gui.widgets.device_control_layout import resolve_device_layout_kind
from keymasq.gui.widgets.device_tab import mapping_display
from keymasq.gui.widgets.device_tab.grid import (
    DeviceGridBuilder,
    DeviceGridCallbacks,
    mapping_action_summary_chars,
    supports_analog_learning,
)
from keymasq.session.profile.types import ProfileInfo


class MappingMixin:
    def _grid_callbacks(self: Any) -> DeviceGridCallbacks:
        return DeviceGridCallbacks(
            on_add_inputs_clicked=self._on_add_keys_clicked,
            on_learn_analog_clicked=self._on_learn_analog_clicked,
            on_mapping_button_clicked=self._on_mapping_button_clicked,
            on_analog_mapping_clicked=self._on_analog_mapping_clicked,
            on_motion_mapping_clicked=self._on_motion_mapping_clicked,
            on_motion_action_right_clicked=self._on_motion_action_right_clicked,
            on_name_label_right_clicked=self._on_name_label_right_clicked,
            on_action_label_right_clicked=self._on_action_label_right_clicked,
            on_analog_name_right_clicked=self._on_analog_name_right_clicked,
        )

    def _grid_builder(self: Any) -> DeviceGridBuilder:
        return DeviceGridBuilder(
            device=self.device,
            demo_mode=self.demo_mode,
            callbacks=self._grid_callbacks(),
            describe_passthrough_output=self._describe_passthrough_output,
        )

    def _setup_button_grid(self: Any) -> None:
        result = self._grid_builder().build()
        self._keyboard_layout_mode = result.keyboard_layout_mode
        self._button_widgets.update(result.button_widgets)
        self.append(result.widget)

    def _create_learn_tile(self: Any) -> Gtk.Button:
        return self._grid_builder().create_learn_tile()

    def _supports_analog_learning(self: Any) -> bool:
        return supports_analog_learning(self.device)

    def device_layout_kind(self: Any) -> str:
        return resolve_device_layout_kind(self.device)

    def _mapping_action_summary_chars(self: Any) -> int:
        return mapping_action_summary_chars(self.device_layout_kind())

    def _on_analog_mapping_clicked(
        self: Any,
        _button_widget: Gtk.Button,
        analog: AnalogInputDefinition,
    ) -> None:
        self._activate_analog_mapping(analog)

    def _on_motion_mapping_clicked(
        self: Any,
        _button_widget: Gtk.Button,
        sensor: MotionSensorDefinition,
    ) -> None:
        if self._selected_profile is None:
            self._show_no_profile_dialog()
            return
        self._show_motion_editor(sensor)

    def _on_motion_action_right_clicked(
        self: Any,
        gesture: Gtk.GestureClick,
        n_press: int,
        _x: float,
        _y: float,
        sensor: MotionSensorDefinition,
    ) -> None:
        if n_press != 1 or self._selected_profile is None:
            return
        layer = self._selected_layer()
        mapping = layer.mappings.get(sensor.id) if layer else None
        if mapping is None or mapping.action_type != ActionType.MOTION_CONTROL:
            return
        if not mapping.motion_control_name:
            return
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self._open_motion_control_manager(mapping.motion_control_name)

    def _open_motion_control_manager(self: Any, select_name: str) -> None:
        from keymasq.gui.widgets.motion_control.dialog import MotionControlDialog

        root = self.get_root()
        dialog = MotionControlDialog(root, self.profile_manager)
        dialog.connect("motion-control-saved", self._on_motion_control_manager_changed)
        dialog.connect("motion-control-deleted", self._on_motion_control_manager_changed)
        dialog.present(root)
        dialog.select_control_by_name(select_name)

    def _on_motion_control_manager_changed(self: Any, _dialog: object, _name: str) -> None:
        self._notify_session_reload_async()
        self._reload_ui()

    def _on_mapping_button_clicked(
        self: Any,
        _button_widget: Gtk.Button,
        button: ButtonDefinition,
        protected: bool,
    ) -> None:
        self._activate_mapping_button(button, protected)

    def _on_button_clicked(
        self: Any,
        click,
        n_press,
        x,
        y,
        button: ButtonDefinition,
        protected: bool,
    ) -> None:
        if click.get_current_button() == Gdk.BUTTON_PRIMARY:
            self._activate_mapping_button(button, protected)

    def _activate_mapping_button(
        self: Any,
        button: ButtonDefinition,
        protected: bool,
    ) -> None:
        if self._selected_profile is None:
            self._show_no_profile_dialog()
            return
        if protected:
            self._show_protected_remap_warning_dialog(button)
            return
        self._show_function_editor(button)

    def _activate_analog_mapping(self: Any, analog: AnalogInputDefinition) -> None:
        if self._selected_profile is None:
            self._show_no_profile_dialog()
            return
        self._show_analog_editor(analog)

    def _on_action_label_right_clicked(
        self: Any,
        click,
        n_press,
        x,
        y,
        button: ButtonDefinition,
    ) -> None:
        if n_press != 1 or self._selected_profile is None:
            return
        layer = self._selected_layer()
        mapping = layer.mappings.get(button.id) if layer else None
        if not mapping or mapping.action_type != ActionType.MACRO or not mapping.macro_name:
            return
        macro_name = mapping.macro_name

        def on_macro_loaded(result: JsonDict | None) -> bool:
            return self._on_macro_lookup(result, macro_name, button)

        self._request_session_async(
            {"command": "get_macro", "name": macro_name},
            on_macro_loaded,
        )

    def _on_macro_lookup(
        self: Any,
        result: JsonDict | None,
        macro_name: str,
        button: ButtonDefinition,
    ) -> bool:
        macro = (result or {}).get("macro")
        if (result or {}).get("status") != "ok" or not isinstance(macro, dict):
            self._show_function_editor(button)
            return False

        from keymasq.gui.widgets.macro_editor.dialog import MacroEditorDialog

        dialog = MacroEditorDialog(self.get_root(), macro_name)
        dialog.present(self.get_root())
        return False

    def _show_protected_remap_warning_dialog(
        self: Any,
        button: ButtonDefinition,
    ) -> None:
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
        self: Any,
        _dialog: Adw.AlertDialog,
        response: str,
        button: ButtonDefinition,
    ) -> None:
        if response == "continue":
            self._show_function_editor(button)

    def _show_no_profile_dialog(self: Any) -> None:
        dialog = Adw.AlertDialog(
            heading="No Profile Selected",
            body="Select or create a profile first to edit button mappings.",
        )
        dialog.add_response("ok", "OK")
        dialog.present(self.get_root())

    def _show_profile_error_dialog(self: Any, message: str) -> None:
        dialog = Adw.AlertDialog(
            heading="Invalid Profile Configuration",
            body=message,
        )
        dialog.add_response("ok", "OK")
        dialog.present(self.get_root())

    def _show_function_editor(self: Any, button: ButtonDefinition) -> None:
        target_profile = self._selected_profile
        layer = self._device_layer_for_profile(target_profile)
        current_action = layer.mappings.get(button.id) if layer else None
        dialog = self._create_key_selector_dialog(self, button.label, current_action)
        defer_commit = self._defer_selector_commit_until_dialog_closed(dialog)

        def on_key_selected(_dialog, action: MappingAction | None) -> None:
            def commit_selection() -> None:
                current_profile = self._resolve_mapping_target_profile(target_profile)
                current_layer = self._device_layer_for_profile(current_profile, create=True)
                if current_layer is None:
                    return
                if action is None:
                    current_layer.mappings.pop(button.id, None)
                else:
                    current_layer.mappings[button.id] = action
                if self._profile_is_selected(current_profile):
                    self._selected_profile = current_profile
                    self._update_button_display(button.id)
                    self._update_header_caption()
                self._save_specific_profile(current_profile)

            defer_commit(commit_selection)

        dialog.connect("key-selected", on_key_selected)
        dialog.present(self.get_root())

    def _show_analog_editor(self: Any, analog: AnalogInputDefinition) -> None:
        target_profile = self._selected_profile
        layer = self._device_layer_for_profile(target_profile)
        current_action = layer.mappings.get(analog.id) if layer else None
        dialog = self._create_key_selector_dialog(
            self,
            analog.label,
            current_action,
            allow_rapidfire=False,
            allow_tap=False,
            allow_macro_options=False,
            source_type="analog",
            analog_input_type=analog.type,
        )
        defer_commit = self._defer_selector_commit_until_dialog_closed(dialog)

        def on_key_selected(_dialog, action: MappingAction | None) -> None:
            def commit_selection() -> None:
                current_profile = self._resolve_mapping_target_profile(target_profile)
                current_layer = self._device_layer_for_profile(current_profile, create=True)
                if current_layer is None:
                    return
                if action is None:
                    current_layer.mappings.pop(analog.id, None)
                else:
                    current_layer.mappings[analog.id] = action
                if self._profile_is_selected(current_profile):
                    self._selected_profile = current_profile
                    self._update_button_display(analog.id)
                    self._update_header_caption()
                self._save_specific_profile(current_profile)

            defer_commit(commit_selection)

        dialog.connect("key-selected", on_key_selected)
        dialog.present(self.get_root())

    def _show_motion_editor(self: Any, sensor: MotionSensorDefinition) -> None:
        target_profile = self._selected_profile
        layer = self._device_layer_for_profile(target_profile)
        current_action = layer.mappings.get(sensor.id) if layer else None
        dialog = self._create_key_selector_dialog(
            self,
            sensor.label,
            current_action,
            allow_rapidfire=False,
            allow_tap=False,
            allow_macro_options=False,
            allow_superkey=False,
            allow_repeat=False,
            source_type="motion",
        )
        defer_commit = self._defer_selector_commit_until_dialog_closed(dialog)

        def on_selected(_dialog: object, action: MappingAction | None) -> None:
            def commit_selection() -> None:
                current_profile = self._resolve_mapping_target_profile(target_profile)
                current_layer = self._device_layer_for_profile(current_profile, create=True)
                if current_layer is None:
                    return
                if action is None:
                    current_layer.mappings.pop(sensor.id, None)
                else:
                    current_layer.mappings[sensor.id] = action
                if self._profile_is_selected(current_profile):
                    self._selected_profile = current_profile
                    self._update_button_display(sensor.id)
                    self._update_header_caption()
                self._save_specific_profile(current_profile)

            defer_commit(commit_selection)

        dialog.connect("key-selected", on_selected)
        dialog.present(self.get_root())

    def _profile_info_by_name(self: Any, profile_name: str) -> ProfileInfo | None:
        return mapping_display.profile_info_by_name(
            self.profile_manager,
            self.profiles,
            profile_name,
        )

    def _get_effective_mapping_for_button(
        self: Any,
        button_id: str,
    ) -> tuple[str | None, MappingAction | None]:
        return mapping_display.get_effective_mapping_for_button(
            active_profile_names=self._active_profile_names,
            profile_lookup=self._profile_info_by_name,
            hardware_id=self.device.hardware_id,
            button_id=button_id,
        )

    def _describe_mapping(
        self: Any,
        mapping: MappingAction,
        button: ButtonDefinition | None = None,
    ) -> str:
        return mapping_display.describe_mapping(
            mapping,
            describe_passthrough=self._describe_passthrough_output,
            button=button,
        )

    def _describe_passthrough_output(self: Any, button: ButtonDefinition) -> str:
        return mapping_display.describe_passthrough_output(
            button,
            label_from_evdev=self._label_from_evdev,
        )

    def _update_button_display(self: Any, button_id: str) -> None:
        mapping_display.update_button_display(
            button_widgets=self._button_widgets,
            button_id=button_id,
            device=self.device,
            selected_layer=self._selected_layer(),
            selected_profile=self._selected_profile,
            effective_mapping=self._get_effective_mapping_for_button(button_id),
            describe_mapping_for_button=self._describe_mapping,
            describe_passthrough=self._describe_passthrough_output,
            action_summary_chars=self._mapping_action_summary_chars(),
        )
