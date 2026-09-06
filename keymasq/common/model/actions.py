"""Action models and normalization rules."""

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, cast

from keymasq.common.coercion import bool_value, coerce_bool, coerce_int
from keymasq.common.gamepad_axes import clamp_gamepad_axis_value, normalize_gamepad_axis_target
from keymasq.common.model.core import ActionType

if TYPE_CHECKING:
    from keymasq.common.model.analog import AnalogControlConfig
    from keymasq.common.model.motion import MotionControlConfig
    from keymasq.common.model.superkeys import SuperkeyConfig

log = logging.getLogger("keymasq.common.model.actions")

PROTECTED_BUTTONS = frozenset({"btn_left", "btn_right"})

MPRIS_COMMAND_PLAY_PAUSE = "play_pause"
MPRIS_COMMAND_PAUSE = "pause"
MPRIS_COMMAND_PLAY = "play"
MPRIS_COMMAND_NEXT = "next"
MPRIS_COMMAND_PREVIOUS = "previous"
MPRIS_COMMAND_STOP = "stop"
MPRIS_COMMANDS = frozenset(
    {
        MPRIS_COMMAND_PLAY_PAUSE,
        MPRIS_COMMAND_PAUSE,
        MPRIS_COMMAND_PLAY,
        MPRIS_COMMAND_NEXT,
        MPRIS_COMMAND_PREVIOUS,
        MPRIS_COMMAND_STOP,
    }
)
DEFAULT_MPRIS_COMMAND = MPRIS_COMMAND_PLAY_PAUSE
_MPRIS_COMMAND_ALIASES = {
    "playpause": MPRIS_COMMAND_PLAY_PAUSE,
    "play-pause": MPRIS_COMMAND_PLAY_PAUSE,
    "play/pause": MPRIS_COMMAND_PLAY_PAUSE,
    "toggle": MPRIS_COMMAND_PLAY_PAUSE,
    "prev": MPRIS_COMMAND_PREVIOUS,
}

REPEAT_CATEGORY_KEYBOARD = "keyboard"
REPEAT_CATEGORY_MOUSE = "mouse"
REPEAT_CATEGORY_GAMEPAD = "gamepad"
REPEAT_CATEGORY_MACRO = "macro"
REPEAT_CATEGORY_SPECIAL = "special"
REPEAT_CATEGORIES = frozenset(
    {
        REPEAT_CATEGORY_KEYBOARD,
        REPEAT_CATEGORY_MOUSE,
        REPEAT_CATEGORY_GAMEPAD,
        REPEAT_CATEGORY_MACRO,
        REPEAT_CATEGORY_SPECIAL,
    }
)
DEFAULT_REPEAT_CATEGORIES = (
    REPEAT_CATEGORY_KEYBOARD,
    REPEAT_CATEGORY_MOUSE,
    REPEAT_CATEGORY_GAMEPAD,
    REPEAT_CATEGORY_MACRO,
    REPEAT_CATEGORY_SPECIAL,
)
_REPEAT_CATEGORY_ALIASES = {
    "mouse_button": REPEAT_CATEGORY_MOUSE,
    "mouse_wheel": REPEAT_CATEGORY_MOUSE,
}

RAPIDFIRE_ACTION_TYPES = frozenset(
    {
        ActionType.KEYBOARD,
        ActionType.MOUSE,
        ActionType.GAMEPAD,
        ActionType.GAMEPAD_AXIS,
        ActionType.MOUSE_MOVE_REL,
        ActionType.MOUSE_MOVE_ABS,
        ActionType.REPEAT,
    }
)
DEFAULT_RAPIDFIRE_HOLD_MS = 20
DEFAULT_RAPIDFIRE_WAIT_MS = 20
MIN_RAPIDFIRE_HOLD_MS = 0
MIN_RAPIDFIRE_WAIT_MS = 1

NATURAL_MOUSE_MOVE_CURVES = frozenset({"linear", "natural"})
DEFAULT_NATURAL_MOUSE_MOVE_SPEED = 12000.0
DEFAULT_NATURAL_MOUSE_MOVE_JITTER = 0.3
DEFAULT_NATURAL_MOUSE_MOVE_CURVE = "natural"
DEFAULT_NATURAL_MOUSE_MOVE_TOLERANCE = 2
DEFAULT_NATURAL_MOUSE_MOVE_MAX_DURATION_MS = 3000

MACRO_LOOP_STOP_BEHAVIORS = frozenset({"finish_run", "cancel_run"})
DEFAULT_MACRO_LOOP_STOP_BEHAVIOR = "finish_run"
MAX_MACRO_RECORDING_SLOTS = 4


def parse_mpris_command(value: object) -> str | None:
    command = str(value or "").strip().lower().replace("-", "_")
    command = _MPRIS_COMMAND_ALIASES.get(command, command)
    if command in MPRIS_COMMANDS:
        return command
    return None


def normalize_mpris_command(value: object) -> str:
    command = parse_mpris_command(value)
    if command is not None:
        return command
    return DEFAULT_MPRIS_COMMAND


def normalize_natural_mouse_move_curve(value: object) -> str:
    curve = str(value or "").strip().lower().replace("-", "_")
    if curve in NATURAL_MOUSE_MOVE_CURVES:
        return curve
    return DEFAULT_NATURAL_MOUSE_MOVE_CURVE


def normalize_macro_recording_slot(value: object) -> int:
    try:
        slot = int(cast(int | float | str, value))
    except (TypeError, ValueError):
        return 0
    if 1 <= slot <= MAX_MACRO_RECORDING_SLOTS:
        return slot
    return 0


def is_protected_button(button_id: str) -> bool:
    return button_id.lower() in PROTECTED_BUTTONS


def action_type_supports_rapidfire(action_type: ActionType) -> bool:
    return action_type in RAPIDFIRE_ACTION_TYPES


def clamp_rapidfire_hold_ms(rapidfire_hold_ms: int) -> int:
    return max(MIN_RAPIDFIRE_HOLD_MS, int(rapidfire_hold_ms))


def clamp_rapidfire_wait_ms(rapidfire_wait_ms: int) -> int:
    return max(MIN_RAPIDFIRE_WAIT_MS, int(rapidfire_wait_ms))


def normalize_rapidfire_fields(
    action_type: ActionType,
    *,
    rapidfire_enabled: bool,
    rapidfire_hold_ms: int,
    rapidfire_wait_ms: int,
) -> tuple[bool, int, int]:
    if not action_type_supports_rapidfire(action_type):
        return False, DEFAULT_RAPIDFIRE_HOLD_MS, DEFAULT_RAPIDFIRE_WAIT_MS
    return (
        rapidfire_enabled,
        clamp_rapidfire_hold_ms(rapidfire_hold_ms),
        clamp_rapidfire_wait_ms(rapidfire_wait_ms),
    )


def resolve_rapidfire_fields(
    action_type: ActionType,
    *,
    rapidfire_enabled: bool,
    rapidfire_hold_ms: int,
    rapidfire_wait_ms: int,
) -> tuple[bool, int, int, bool]:
    unsupported_requested = rapidfire_enabled and not action_type_supports_rapidfire(action_type)
    normalized_enabled, normalized_hold_ms, normalized_wait_ms = normalize_rapidfire_fields(
        action_type,
        rapidfire_enabled=rapidfire_enabled,
        rapidfire_hold_ms=rapidfire_hold_ms,
        rapidfire_wait_ms=rapidfire_wait_ms,
    )
    return (
        normalized_enabled,
        normalized_hold_ms,
        normalized_wait_ms,
        unsupported_requested,
    )


def parse_rapidfire_fields(
    action_type: ActionType,
    *,
    rapidfire_enabled: object,
    rapidfire_hold_ms: object,
    rapidfire_wait_ms: object,
) -> tuple[bool, int, int, bool]:
    parsed_hold_ms = coerce_int(rapidfire_hold_ms, DEFAULT_RAPIDFIRE_HOLD_MS)
    parsed_wait_ms = coerce_int(rapidfire_wait_ms, DEFAULT_RAPIDFIRE_WAIT_MS)
    return resolve_rapidfire_fields(
        action_type,
        rapidfire_enabled=bool(rapidfire_enabled),
        rapidfire_hold_ms=parsed_hold_ms,
        rapidfire_wait_ms=parsed_wait_ms,
    )


def normalize_macro_loop_stop_behavior(value: object) -> str:
    behavior = str(value or DEFAULT_MACRO_LOOP_STOP_BEHAVIOR).lower()
    if behavior in MACRO_LOOP_STOP_BEHAVIORS:
        return behavior
    return DEFAULT_MACRO_LOOP_STOP_BEHAVIOR


def normalize_gamepad_output_id(action_type: ActionType, output_id: object) -> str | None:
    if action_type not in (ActionType.GAMEPAD, ActionType.GAMEPAD_AXIS):
        return None
    return normalize_output_id(output_id)


def normalize_output_id(output_id: object) -> str | None:
    if output_id is None:
        return None
    normalized = str(output_id).strip()
    return normalized or None


def normalize_repeat_categories(categories: object) -> list[str]:
    if categories is None:
        return list(DEFAULT_REPEAT_CATEGORIES)
    if isinstance(categories, str):
        raw_values: list[object] = [categories]
    elif isinstance(categories, (list, tuple, set, frozenset)):
        raw_values = list(categories)
    else:
        log.warning(
            "Invalid repeat_categories %r; using default repeat categories",
            categories,
        )
        return list(DEFAULT_REPEAT_CATEGORIES)
    if not raw_values:
        return []

    normalized: list[str] = []
    seen: set[str] = set()
    for value in raw_values:
        category = str(value or "").strip().lower()
        category = _REPEAT_CATEGORY_ALIASES.get(category, category)
        if category not in REPEAT_CATEGORIES or category in seen:
            continue
        normalized.append(category)
        seen.add(category)
    if not normalized:
        log.warning(
            "Invalid repeat_categories %r; using default repeat categories",
            categories,
        )
        return list(DEFAULT_REPEAT_CATEGORIES)
    return normalized


@dataclass
class ProfileDeactivationPolicy:
    on_trigger_end: bool = False
    after_actions: int | None = None
    timeout_ms: int | None = None

    @property
    def has_condition(self) -> bool:
        return bool(
            self.on_trigger_end
            or (self.after_actions is not None and self.after_actions > 0)
            or (self.timeout_ms is not None and self.timeout_ms > 0)
        )


def _positive_int_or_none(value: object) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(cast(int | float | str | bytes, value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def normalize_profile_deactivation_policy(
    action_type: ActionType,
    policy: ProfileDeactivationPolicy | None,
) -> ProfileDeactivationPolicy | None:
    if action_type not in (ActionType.PROFILE_ENABLE, ActionType.PROFILE_TOGGLE):
        return None
    if policy is None:
        return None

    normalized = ProfileDeactivationPolicy(
        on_trigger_end=bool(policy.on_trigger_end),
        after_actions=_positive_int_or_none(policy.after_actions),
        timeout_ms=_positive_int_or_none(policy.timeout_ms),
    )
    return normalized if normalized.has_condition else None


def parse_profile_deactivation_policy(data: object) -> ProfileDeactivationPolicy | None:
    if not isinstance(data, dict):
        return None
    payload = cast(dict[str, object], data)
    policy = ProfileDeactivationPolicy(
        on_trigger_end=coerce_bool(payload.get("on_trigger_end"), False),
        after_actions=_positive_int_or_none(payload.get("after_actions")),
        timeout_ms=_positive_int_or_none(payload.get("timeout_ms")),
    )
    return policy if policy.has_condition else None


def profile_deactivation_policy_to_dict(
    policy: ProfileDeactivationPolicy | None,
) -> dict[str, object] | None:
    if policy is None:
        return None
    normalized = ProfileDeactivationPolicy(
        on_trigger_end=bool(policy.on_trigger_end),
        after_actions=_positive_int_or_none(policy.after_actions),
        timeout_ms=_positive_int_or_none(policy.timeout_ms),
    )
    if not normalized.has_condition:
        return None
    data: dict[str, object] = {}
    if normalized.on_trigger_end:
        data["on_trigger_end"] = True
    if normalized.after_actions is not None:
        data["after_actions"] = int(normalized.after_actions)
    if normalized.timeout_ms is not None:
        data["timeout_ms"] = int(normalized.timeout_ms)
    return data


@dataclass
class MappingAction:
    action_type: ActionType
    target: str | None = None
    output_id: str | None = None
    keys: list[str] | None = None
    cmd: str | None = None
    exec_ref: int | None = None
    superkey_name: str | None = None
    superkey_config: "SuperkeyConfig | None" = None
    analog_control_name: str | None = None
    analog_control_names: list[str] = field(default_factory=list)
    analog_control_config: "AnalogControlConfig | None" = None
    analog_control_configs: list["AnalogControlConfig"] = field(default_factory=list)
    motion_control_name: str | None = None
    motion_control_names: list[str] = field(default_factory=list)
    motion_control_config: "MotionControlConfig | None" = None
    motion_control_configs: list["MotionControlConfig"] = field(default_factory=list)
    macro_name: str | None = None
    macro_events: list[dict[str, object]] | None = None
    macro_replay_mouse_movement: bool = True
    macro_replay_mouse_clicks: bool = True
    macro_speed: float = 1.0
    macro_loop_mode: str = "none"
    macro_loop_count: int = 1
    macro_loop_stop_behavior: str = DEFAULT_MACRO_LOOP_STOP_BEHAVIOR
    macro_move_to_start: bool = False
    macro_start_x: int = 0
    macro_start_y: int = 0
    macro_block_mouse_movement: bool = False
    macro_recording_slot: int = 0
    profile_name: str | None = None
    compositor_id: str | None = None
    compositor_dispatcher: str | None = None
    compositor_args: str | None = None
    mpris_command: str | None = None
    move_x: int = 0
    move_y: int = 0
    axis_value: int = 0
    move_speed: float = DEFAULT_NATURAL_MOUSE_MOVE_SPEED
    move_jitter: float = DEFAULT_NATURAL_MOUSE_MOVE_JITTER
    move_curve: str = DEFAULT_NATURAL_MOUSE_MOVE_CURVE
    move_tolerance: int = DEFAULT_NATURAL_MOUSE_MOVE_TOLERANCE
    move_max_duration_ms: int = DEFAULT_NATURAL_MOUSE_MOVE_MAX_DURATION_MS
    move_stop_on_failure: bool = False

    rapidfire_enabled: bool = False
    rapidfire_hold_ms: int = DEFAULT_RAPIDFIRE_HOLD_MS
    rapidfire_wait_ms: int = DEFAULT_RAPIDFIRE_WAIT_MS

    tap_enabled: bool = False
    tap_hold_ms: int = 10
    profile_deactivation: ProfileDeactivationPolicy | None = None
    source_profile_name: str | None = None
    repeat_categories: list[str] | None = None

    def __post_init__(self) -> None:
        self.source_profile_name = (
            str(self.source_profile_name).strip() if self.source_profile_name else None
        ) or None
        if self.analog_control_name and not self.analog_control_names:
            self.analog_control_names = [self.analog_control_name]
        else:
            self.analog_control_names = [
                str(name).strip() for name in self.analog_control_names if str(name).strip()
            ]
            if self.analog_control_names:
                self.analog_control_name = self.analog_control_names[0]
        if self.analog_control_config and not self.analog_control_configs:
            self.analog_control_configs = [self.analog_control_config]
        elif self.analog_control_configs:
            self.analog_control_config = self.analog_control_configs[0]
        self.motion_control_names = list(
            dict.fromkeys(
                str(name).strip() for name in self.motion_control_names if str(name).strip()
            )
        )
        if not self.motion_control_names and self.motion_control_name:
            name = str(self.motion_control_name).strip()
            if name:
                self.motion_control_names = [name]
        self.motion_control_name = (
            self.motion_control_names[0] if self.motion_control_names else None
        )
        if self.motion_control_config and not self.motion_control_configs:
            self.motion_control_configs = [self.motion_control_config]
        elif self.motion_control_configs:
            self.motion_control_config = self.motion_control_configs[0]
        normalize_common_action_fields(self)
        if self.action_type == ActionType.REPEAT:
            self.repeat_categories = normalize_repeat_categories(self.repeat_categories)
        else:
            self.repeat_categories = None


class _CommonActionFields(Protocol):
    action_type: ActionType
    target: str | None
    output_id: str | None
    axis_value: int
    rapidfire_enabled: bool
    rapidfire_hold_ms: int
    rapidfire_wait_ms: int
    profile_deactivation: ProfileDeactivationPolicy | None
    mpris_command: str | None
    macro_recording_slot: int
    move_speed: float
    move_jitter: float
    move_curve: str
    move_tolerance: int
    move_max_duration_ms: int
    move_stop_on_failure: bool


def normalize_common_action_fields(action: _CommonActionFields) -> None:
    """Normalize fields shared by mapping and superkey actions."""

    action.output_id = normalize_gamepad_output_id(action.action_type, action.output_id)
    if action.action_type == ActionType.GAMEPAD_AXIS:
        action.target = normalize_gamepad_axis_target(action.target)
        action.axis_value = clamp_gamepad_axis_value(
            action.target, action.axis_value, output_id=action.output_id
        )
    rapidfire_enabled, rapidfire_hold_ms, rapidfire_wait_ms = normalize_rapidfire_fields(
        action.action_type,
        rapidfire_enabled=bool(action.rapidfire_enabled),
        rapidfire_hold_ms=int(action.rapidfire_hold_ms),
        rapidfire_wait_ms=int(action.rapidfire_wait_ms),
    )
    action.rapidfire_enabled = rapidfire_enabled
    action.rapidfire_hold_ms = rapidfire_hold_ms
    action.rapidfire_wait_ms = rapidfire_wait_ms
    action.profile_deactivation = normalize_profile_deactivation_policy(
        action.action_type,
        action.profile_deactivation,
    )
    if action.action_type == ActionType.MPRIS:
        action.mpris_command = normalize_mpris_command(action.mpris_command)
    else:
        action.mpris_command = None
    if action.action_type in {
        ActionType.START_MACRO_RECORDING,
        ActionType.STOP_MACRO_RECORDING,
        ActionType.PLAY_MACRO_SLOT,
    }:
        action.macro_recording_slot = normalize_macro_recording_slot(action.macro_recording_slot)
    else:
        action.macro_recording_slot = 0
    if action.action_type == ActionType.MOUSE_MOVE_NATURAL_ABS:
        action.move_speed = max(1.0, float(action.move_speed))
        action.move_jitter = max(0.0, float(action.move_jitter))
        action.move_curve = normalize_natural_mouse_move_curve(action.move_curve)
        action.move_tolerance = max(0, int(action.move_tolerance))
        action.move_max_duration_ms = max(1, int(action.move_max_duration_ms))
        action.move_stop_on_failure = bool_value(action.move_stop_on_failure)
