"""Physical device and hardware configuration models."""

from dataclasses import dataclass, field

from keymasq.common.model.core import DeviceType


@dataclass
class EvdevDevice:
    path: str
    device_type: DeviceType
    id: str | None = None
    phys: str | None = None
    capabilities: list[str] = field(default_factory=list)


@dataclass
class ButtonDefinition:
    id: str
    label: str
    evdev: str
    evdev_code: int | None = None
    evdev_value: int | None = None
    source: str | None = None
    zone: str | None = None
    row: int | None = None
    col: int | None = None
    type: str | None = None


@dataclass
class AnalogAxisDefinition:
    role: str
    evdev: str
    evdev_code: int | None = None
    minimum: int | None = None
    maximum: int | None = None
    center: int | None = None
    rest: int | None = None
    invert: bool = False


@dataclass
class AnalogInputDefinition:
    id: str
    label: str
    type: str
    source: str | None = None
    axes: list[AnalogAxisDefinition] = field(default_factory=list)


@dataclass
class HardwareConfig:
    vendor_id: str
    product_id: str
    name: str
    evdev_devices: list[EvdevDevice]
    buttons: list[ButtonDefinition]
    analog_inputs: list[AnalogInputDefinition] = field(default_factory=list)
    image: str | None = None
    id: str | None = None

    @property
    def hardware_id(self) -> str:
        return self.id or f"{self.vendor_id}:{self.product_id}"

    @property
    def model_id(self) -> str:
        return f"{self.vendor_id}:{self.product_id}"


@dataclass
class DeviceInfo:
    path: str
    name: str
    vendor_id: str
    product_id: str
    capabilities: list[str]
    device_type: DeviceType
