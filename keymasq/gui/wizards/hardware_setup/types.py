from typing import Any, TypedDict


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
    raw_capabilities: dict[int, list[object]]
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
