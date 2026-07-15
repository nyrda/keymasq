"""Profile layering, window rules, and combo configuration models."""

from dataclasses import dataclass, field
from datetime import datetime

from keymasq.common.model.actions import MappingAction
from keymasq.common.model.core import ProfileState


@dataclass
class WindowRule:
    field: str
    pattern: str


@dataclass
class DeviceProfileLayer:
    hardware_id: str
    always_grab_all: bool = False
    mappings: dict[str, MappingAction] = field(default_factory=dict)


@dataclass
class ComboEvent:
    evdev: str
    hardware_id: str = ""
    source: str | None = None


@dataclass
class ComboStep:
    events: list[ComboEvent] = field(default_factory=list)
    timeout_ms: int | None = None


@dataclass
class ComboConfig:
    id: str
    name: str = ""
    steps: list[ComboStep] = field(default_factory=list)
    action: MappingAction | None = None
    recall_trigger_keys: bool = False
    restore_trigger_keys: list[str] = field(default_factory=list)
    match_across_devices: bool = False


@dataclass
class ProfileConfig:
    name: str
    enabled: bool = True
    is_permanent: bool = False
    priority: int = 0
    notify_on_activation: bool = True
    activation_macro_name: str | None = None
    deactivation_macro_name: str | None = None
    window_rules: list[WindowRule] = field(default_factory=list)
    device_layers: dict[str, DeviceProfileLayer] = field(default_factory=dict)
    combos: list[ComboConfig] = field(default_factory=list)
    image: str | None = None
    created_at: datetime | None = None

    @property
    def state(self) -> ProfileState:
        if not self.enabled:
            return ProfileState.INACTIVE
        if self.is_permanent:
            return ProfileState.STANDBY
        if self.window_rules:
            return ProfileState.WAITING
        return ProfileState.INACTIVE

    def get_layer(self, hardware_id: str) -> DeviceProfileLayer | None:
        return self.device_layers.get(hardware_id)

    def ensure_layer(self, hardware_id: str) -> DeviceProfileLayer:
        layer = self.device_layers.get(hardware_id)
        if layer is None:
            layer = DeviceProfileLayer(hardware_id=hardware_id)
            self.device_layers[hardware_id] = layer
        return layer
