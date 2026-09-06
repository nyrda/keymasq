import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from keymasq.common.devices import is_gamepad_button_name
from keymasq.common.virtual_device_templates import (
    VirtualDeviceConfig,
    resolve_virtual_devices,
)
from keymasq.common.virtual_devices import (
    is_virtual_gamepad_output_id,
    virtual_gamepad_output_id,
)
from keymasq.session.hardware import HardwareManager
from keymasq.session.settings import load_virtual_gamepad_count
from keymasq.session.virtual_devices import load_virtual_device_config

log = logging.getLogger("keymasq.gui.widgets.gamepad_output_choices")


@dataclass(frozen=True, slots=True)
class GamepadOutputChoiceSet:
    choices: list[tuple[str | None, str]]
    count: int
    hardware_configs: list[object]
    virtual_device_config: VirtualDeviceConfig = VirtualDeviceConfig()


class HardwareManagerLike(Protocol):
    def list_hardware(self) -> Sequence[object]: ...


class WarningLabelLike(Protocol):
    def set_label(self, label: str) -> None: ...

    def set_visible(self, visible: bool) -> None: ...


def virtual_gamepad_count() -> int:
    try:
        return max(0, int(load_virtual_gamepad_count()))
    except Exception:
        log.exception("Unable to load virtual gamepad count; using default of 1")
        return 1


def virtual_device_config() -> VirtualDeviceConfig:
    try:
        return load_virtual_device_config()
    except Exception:
        log.exception("Unable to load virtual device configuration; using built-ins only")
        return VirtualDeviceConfig()


def _virtual_gamepad_index(output_id: str | None) -> int | None:
    if output_id is None:
        return 1
    if not is_virtual_gamepad_output_id(output_id):
        return None
    try:
        return int(output_id.removeprefix("virtual-gamepad-"))
    except ValueError:
        return None


def gamepad_output_unavailable_message(output_id: str | None, count: int) -> str | None:
    index = _virtual_gamepad_index(output_id)
    if index is None:
        return None
    if index <= count:
        return None
    if output_id is None:
        return "No virtual gamepads are configured."
    return (
        f"{output_id} is not configured. This mapping will be saved, but output will be "
        "dropped until that virtual gamepad is enabled."
    )


def _format_current_virtual_output_choice(output_id: str) -> str:
    if is_virtual_gamepad_output_id(output_id):
        return f"{output_id} (unavailable)"
    return f"{output_id} (unknown)"


def _is_hardware_gamepad(config: object) -> bool:
    evdev_devices = getattr(config, "evdev_devices", []) or []
    for device in evdev_devices:
        device_type = getattr(device, "device_type", None)
        if getattr(device_type, "value", device_type) == "gamepad":
            return True
    return any(
        is_gamepad_button_name(getattr(button, "evdev", None))
        for button in getattr(config, "buttons", []) or []
    )


def _hardware_gamepad_output_label(config: object) -> str:
    hardware_id = str(getattr(config, "hardware_id", "") or "")
    name = str(getattr(config, "name", "") or "").strip()
    if name and hardware_id:
        return f"{name} ({hardware_id})"
    return name or hardware_id


def gamepad_output_choice_matches(choice_id: str | None, selected_id: str | None) -> bool:
    if choice_id == selected_id:
        return True
    return choice_id is None and selected_id == "virtual-gamepad-1"


def gamepad_output_choices_for(
    selected_id: str | None,
    count: int,
    hardware_configs: Sequence[object],
    device_config: VirtualDeviceConfig | None = None,
) -> list[tuple[str | None, str]]:
    if device_config is None:
        device_config = VirtualDeviceConfig()
    default_label = "Virtual Gamepad 1" if count > 0 else "Default output unavailable"
    choices: list[tuple[str | None, str]] = [(None, default_label)]
    for index in range(2, count + 1):
        output_id = virtual_gamepad_output_id(index)
        choices.append((output_id, f"Virtual Gamepad {index}"))

    for device in resolve_virtual_devices(count, device_config)[count:]:
        choices.append(
            (
                device.output_id,
                f"{device.template.label} ({device.output_id})",
            )
        )

    for config in hardware_configs:
        if _is_hardware_gamepad(config):
            hardware_id = str(getattr(config, "hardware_id", "") or "").strip()
            if not hardware_id:
                continue
            choices.append(
                (
                    hardware_id,
                    _hardware_gamepad_output_label(config),
                )
            )

    if selected_id and all(
        not gamepad_output_choice_matches(output_id, selected_id) for output_id, _label in choices
    ):
        choices.append((selected_id, _format_current_virtual_output_choice(selected_id)))
    return choices


def load_gamepad_output_hardware_configs(
    hardware_manager_factory: Callable[[], HardwareManagerLike] = HardwareManager,
) -> list[object]:
    try:
        return list(hardware_manager_factory().list_hardware())
    except (OSError, RuntimeError) as exc:
        log.debug("Unable to load hardware configs for gamepad outputs: %s", exc)
        return []


def gamepad_output_choices(
    selected_id: str | None,
    *,
    count: int | None = None,
    hardware_configs: Sequence[object] | None = None,
    device_config: VirtualDeviceConfig | None = None,
    hardware_manager_factory: Callable[[], HardwareManagerLike] = HardwareManager,
) -> list[tuple[str | None, str]]:
    if count is None:
        count = virtual_gamepad_count()
    if hardware_configs is None:
        hardware_configs = load_gamepad_output_hardware_configs(hardware_manager_factory)
    if device_config is None:
        device_config = virtual_device_config()
    return gamepad_output_choices_for(selected_id, count, hardware_configs, device_config)


def load_gamepad_output_choices(
    selected_id: str | None,
    *,
    count_loader: Callable[[], int] = virtual_gamepad_count,
    config_loader: Callable[[], VirtualDeviceConfig] = virtual_device_config,
    hardware_manager_factory: Callable[[], HardwareManagerLike] = HardwareManager,
) -> GamepadOutputChoiceSet:
    count = count_loader()
    hardware_configs = load_gamepad_output_hardware_configs(hardware_manager_factory)
    device_config = config_loader()
    return GamepadOutputChoiceSet(
        choices=gamepad_output_choices_for(
            selected_id,
            count,
            hardware_configs,
            device_config,
        ),
        count=count,
        hardware_configs=hardware_configs,
        virtual_device_config=device_config,
    )


def selected_gamepad_output_id(
    dropdown_selected: int,
    output_ids: Sequence[str | None],
    current_output_id: str | None,
) -> str | None:
    if 0 <= dropdown_selected < len(output_ids):
        return output_ids[dropdown_selected]
    return current_output_id


def update_gamepad_output_warning_label(
    label: WarningLabelLike | None,
    output_id: str | None,
    *,
    count_loader: Callable[[], int] = virtual_gamepad_count,
) -> str | None:
    message = gamepad_output_unavailable_message(output_id, count_loader())
    if label is not None:
        label.set_label(message or "")
        label.set_visible(bool(message))
    return message
