from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

PROTECTED_BUTTONS = frozenset({"btn_left", "btn_right"})


class ActionType(Enum):
    PASSTHROUGH = "passthrough"
    KEYBOARD = "keyboard"
    MOUSE = "mouse"
    GAMEPAD = "gamepad"
    EXEC = "exec"
    COMPOSITOR_DISPATCH = "compositor_dispatch"
    SUPPRESS = "suppress"
    SUPERKEY = "superkey"
    START_MACRO_RECORDING = "start_macro_recording"
    STOP_MACRO_RECORDING = "stop_macro_recording"
    CANCEL_MACRO_PLAYBACK = "cancel_macro_playback"
    MACRO = "macro"
    MOUSE_MOVE_REL = "mouse_move_rel"
    MOUSE_MOVE_ABS = "mouse_move_abs"
    PROFILE_ENABLE = "profile_enable"
    PROFILE_DISABLE = "profile_disable"
    PROFILE_TOGGLE = "profile_toggle"


class SuperkeyMode(Enum):
    PATTERN = "pattern"
    OVERLOAD = "overload"


SUPERKEY_ACTION_TYPES = frozenset(
    {
        ActionType.KEYBOARD,
        ActionType.MOUSE,
        ActionType.GAMEPAD,
        ActionType.EXEC,
        ActionType.MACRO,
    }
)


class DeviceType(Enum):
    MOUSE = "mouse"
    KEYBOARD = "keyboard"
    GAMEPAD = "gamepad"
    OTHER = "other"


class ProfileState(Enum):
    INACTIVE = "inactive"
    WAITING = "waiting"
    ACTIVE = "active"
    STANDBY = "standby"


class WindowFieldType(Enum):
    CLASS = "class"
    TITLE = "title"
    INITIAL_CLASS = "initial_class"
    INITIAL_TITLE = "initial_title"
    TAG = "tag"


def is_protected_button(button_id: str) -> bool:
    return button_id.lower() in PROTECTED_BUTTONS


@dataclass
class EvdevDevice:
    path: str
    device_type: DeviceType
    id: str | None = None
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
class HardwareConfig:
    vendor_id: str
    product_id: str
    name: str
    evdev_devices: list[EvdevDevice]
    buttons: list[ButtonDefinition]
    image: str | None = None

    @property
    def hardware_id(self) -> str:
        return f"{self.vendor_id}:{self.product_id}"


@dataclass
class MappingAction:
    action_type: ActionType
    target: str | None = None
    keys: list[str] | None = None
    cmd: str | None = None
    exec_ref: int | None = None
    superkey_name: str | None = None
    superkey_config: "SuperkeyConfig | None" = None
    macro_name: str | None = None
    macro_events: list[dict[str, object]] | None = None
    macro_replay_mouse_movement: bool = True
    macro_replay_mouse_clicks: bool = True
    macro_speed: float = 1.0
    macro_loop_mode: str = "none"
    macro_loop_count: int = 1
    macro_move_to_start: bool = False
    macro_start_x: int = 0
    macro_start_y: int = 0
    macro_block_mouse_movement: bool = False
    profile_name: str | None = None
    compositor_id: str | None = None
    compositor_dispatcher: str | None = None
    compositor_args: str | None = None
    move_x: int = 0
    move_y: int = 0
    move_speed: float = 1.0
    move_jitter: float = 0.3

    rapidfire_enabled: bool = False
    rapidfire_hold_ms: int = 20
    rapidfire_wait_ms: int = 20

    tap_enabled: bool = False
    tap_hold_ms: int = 10


@dataclass
class SuperkeyAction:
    action_type: ActionType
    target: str | None = None
    cmd: str | None = None
    exec_ref: int | None = None
    macro_name: str | None = None

    rapidfire_enabled: bool = False
    rapidfire_hold_ms: int = 20
    rapidfire_wait_ms: int = 20

    def is_valid(self) -> bool:
        return self.action_type in SUPERKEY_ACTION_TYPES


@dataclass
class SuperkeyConfig:
    name: str
    description: str | None = None
    mode: SuperkeyMode = SuperkeyMode.PATTERN

    tap_actions: list[SuperkeyAction] = field(default_factory=list)
    double_tap_actions: list[SuperkeyAction] = field(default_factory=list)
    hold_actions: list[SuperkeyAction] = field(default_factory=list)
    tap_hold_actions: list[SuperkeyAction] = field(default_factory=list)
    overload_actions: list[MappingAction] = field(default_factory=list)

    tap_timeout_ms: int = 200
    double_tap_window_ms: int = 300
    hold_threshold_ms: int = 300

    def has_pattern_actions(self) -> bool:
        return any(
            (
                self.tap_actions,
                self.double_tap_actions,
                self.hold_actions,
                self.tap_hold_actions,
            )
        )

    def has_overload_actions(self) -> bool:
        return bool(self.overload_actions)

    def has_any_action(self) -> bool:
        return self.has_pattern_actions() or self.has_overload_actions()

    @property
    def tap_action(self) -> SuperkeyAction | None:
        return self.tap_actions[0] if self.tap_actions else None

    @tap_action.setter
    def tap_action(self, value: SuperkeyAction | None) -> None:
        self.tap_actions = [value] if value is not None else []

    @property
    def double_tap_action(self) -> SuperkeyAction | None:
        return self.double_tap_actions[0] if self.double_tap_actions else None

    @double_tap_action.setter
    def double_tap_action(self, value: SuperkeyAction | None) -> None:
        self.double_tap_actions = [value] if value is not None else []

    @property
    def hold_action(self) -> SuperkeyAction | None:
        return self.hold_actions[0] if self.hold_actions else None

    @hold_action.setter
    def hold_action(self, value: SuperkeyAction | None) -> None:
        self.hold_actions = [value] if value is not None else []

    @property
    def tap_hold_action(self) -> SuperkeyAction | None:
        return self.tap_hold_actions[0] if self.tap_hold_actions else None

    @tap_hold_action.setter
    def tap_hold_action(self, value: SuperkeyAction | None) -> None:
        self.tap_hold_actions = [value] if value is not None else []


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
    hardware_id: str
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


@dataclass
class ProfileConfig:
    name: str
    enabled: bool = True
    is_permanent: bool = False
    priority: int = 0
    notify_on_activation: bool = True
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


@dataclass
class DeviceInfo:
    path: str
    name: str
    vendor_id: str
    product_id: str
    capabilities: list[str]
    device_type: DeviceType
