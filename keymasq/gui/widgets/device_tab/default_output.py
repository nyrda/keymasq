"""Describe the hardware controller route on input cards."""

from typing import Any

from keymasq.common.controller_routing import is_controller_interface
from keymasq.common.devices import resolve_evdev_code
from keymasq.common.model.hardware import AnalogInputDefinition, ButtonDefinition
from keymasq.common.virtual_device_templates import ResolvedVirtualDevice, resolve_virtual_devices
from keymasq.gui.session_client import GuiTaskResult, run_gui_task
from keymasq.gui.widgets.gamepad_output_choices import virtual_device_config, virtual_gamepad_count


class DefaultOutputMixin:
    def _refresh_default_output_templates(self: Any) -> None:
        if not any(is_controller_interface(device) for device in self.device.evdev_devices):
            return

        def load() -> list[ResolvedVirtualDevice]:
            return list(resolve_virtual_devices(virtual_gamepad_count(), virtual_device_config()))

        def loaded(result: GuiTaskResult[list[ResolvedVirtualDevice]]) -> None:
            if result.error is not None:
                return
            self._default_output_templates = {
                device.output_id: device.template for device in result.value or []
            }
            for input_id in self._button_widgets:
                self._update_button_display(input_id)

        run_gui_task(load, loaded)

    def _default_output_description(
        self: Any,
        control: ButtonDefinition | AnalogInputDefinition,
    ) -> str | None:
        presentation = self._default_output_presentation(control)
        return presentation[1] if presentation is not None else None

    def _default_output_presentation(
        self: Any,
        control: ButtonDefinition | AnalogInputDefinition,
    ) -> tuple[str, str] | None:
        sources = {
            device.id for device in self.device.evdev_devices if is_controller_interface(device)
        }
        if not sources or control.source and control.source not in sources:
            return None
        output_id = self.device.default_output
        if output_id in {None, "passthrough"}:
            return None
        template = getattr(self, "_default_output_templates", {}).get(output_id)
        if template is None:
            return "Unavailable", f"Default → {output_id} (unavailable)"
        if isinstance(control, ButtonDefinition):
            names = [control.evdev]
            supported = {resolve_evdev_code(button.evdev) for button in template.buttons}
        else:
            names = [axis.evdev for axis in control.axes]
            supported = {resolve_evdev_code(axis.evdev) for axis in template.axes}
        matched = [name.upper() for name in names if resolve_evdev_code(name) in supported]
        if not matched:
            return "No output", f"No matching output on {output_id}"
        suffix = " · some axes unmatched" if len(matched) != len(names) else ""
        if isinstance(control, ButtonDefinition):
            target = next(
                button for button in template.buttons
                if resolve_evdev_code(button.evdev) == resolve_evdev_code(control.evdev)
            )
            summary = f"→ {target.label}"
        else:
            summary = f"→ {', '.join(name.removeprefix('ABS_') for name in matched)}"
        return (
            "Partial output" if suffix else summary,
            f"Default → {output_id} · {', '.join(matched)}{suffix}",
        )
