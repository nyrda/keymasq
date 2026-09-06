from collections.abc import Sequence
from typing import Any, TypedDict

from keymasq.common.model.hardware import EvdevDevice
from keymasq.common.model.motion import MotionSensorDefinition


class EvdevDeviceSelection(list[EvdevDevice]):
    """Selected evdev devices plus layout data derived during discovery."""

    def __init__(
        self,
        devices: Sequence[EvdevDevice],
        motion_sensors: Sequence[MotionSensorDefinition] = (),
    ) -> None:
        super().__init__(devices)
        self.motion_sensors = list(motion_sensors)


class DetectedInterface(TypedDict, total=False):
    id: str
    path: str
    stable_path: str
    config_path: str
    name: str
    phys: str
    interface_id: str
    device_type: Any
    device_types: list[str]
    capabilities: list[str]
    abs_info: dict[str, object]
    raw_capabilities: dict[int, list[object]]
    driver: str
    grabbed_by_keymasq: bool
    source_hardware_id: str
    source_interface_id: str
    source_stable_path: str
    source_path: str
    configured_hardware_id: str
    recording_kind: str


class DetectedDevice(TypedDict, total=False):
    name: str
    display_name: str
    hardware_id: str
    model_id: str
    vendor_id: str
    product_id: str
    paths: list[str]
    interfaces: list[DetectedInterface]
    device_type: Any
    device_types: list[str]
