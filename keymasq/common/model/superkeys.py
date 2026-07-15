"""Reusable multi-role key models and action conversion."""

from dataclasses import dataclass, field, fields, replace
from typing import TYPE_CHECKING, Any, Protocol, cast, overload

from keymasq.common.model.actions import (
    DEFAULT_MACRO_LOOP_STOP_BEHAVIOR,
    DEFAULT_NATURAL_MOUSE_MOVE_CURVE,
    DEFAULT_NATURAL_MOUSE_MOVE_JITTER,
    DEFAULT_NATURAL_MOUSE_MOVE_MAX_DURATION_MS,
    DEFAULT_NATURAL_MOUSE_MOVE_SPEED,
    DEFAULT_NATURAL_MOUSE_MOVE_TOLERANCE,
    DEFAULT_RAPIDFIRE_HOLD_MS,
    DEFAULT_RAPIDFIRE_WAIT_MS,
    MappingAction,
    ProfileDeactivationPolicy,
    normalize_common_action_fields,
    normalize_macro_loop_stop_behavior,
    normalize_rapidfire_fields,
)
from keymasq.common.model.core import ActionType, SuperkeyMode

if TYPE_CHECKING:
    from keymasq.keymasqd import superkey_state

SUPERKEY_ACTION_TYPES = frozenset(
    {
        ActionType.KEYBOARD,
        ActionType.MOUSE,
        ActionType.MOUSE_MOVE_REL,
        ActionType.MOUSE_MOVE_ABS,
        ActionType.MOUSE_MOVE_NATURAL_ABS,
        ActionType.GAMEPAD,
        ActionType.GAMEPAD_AXIS,
        ActionType.EXEC,
        ActionType.COMPOSITOR_DISPATCH,
        ActionType.START_MACRO_RECORDING,
        ActionType.STOP_MACRO_RECORDING,
        ActionType.PLAY_MACRO_SLOT,
        ActionType.CANCEL_MACRO_PLAYBACK,
        ActionType.EMERGENCY_RESET,
        ActionType.MACRO,
        ActionType.PROFILE_ENABLE,
        ActionType.PROFILE_DISABLE,
        ActionType.PROFILE_TOGGLE,
        ActionType.MPRIS,
    }
)


@dataclass
class SuperkeyAction:
    action_type: ActionType
    target: str | None = None
    output_id: str | None = None
    cmd: str | None = None
    exec_ref: int | None = None
    macro_name: str | None = None
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
    profile_deactivation: ProfileDeactivationPolicy | None = None

    def is_valid(self) -> bool:
        return self.action_type in SUPERKEY_ACTION_TYPES

    def __post_init__(self) -> None:
        normalize_common_action_fields(self)


SUPERKEY_ACTION_SHARED_FIELDS = tuple(
    dataclass_field.name
    for dataclass_field in fields(SuperkeyAction)
    if dataclass_field.name != "action_type"
)


def superkey_action_shared_kwargs(action: object) -> dict[str, Any]:
    typed_action = cast(MappingAction | SuperkeyAction, action)
    kwargs = {
        field_name: getattr(typed_action, field_name)
        for field_name in SUPERKEY_ACTION_SHARED_FIELDS
    }
    rapidfire_enabled, rapidfire_hold_ms, rapidfire_wait_ms = normalize_rapidfire_fields(
        typed_action.action_type,
        rapidfire_enabled=bool(kwargs["rapidfire_enabled"]),
        rapidfire_hold_ms=int(kwargs["rapidfire_hold_ms"]),
        rapidfire_wait_ms=int(kwargs["rapidfire_wait_ms"]),
    )
    kwargs["rapidfire_enabled"] = rapidfire_enabled
    kwargs["rapidfire_hold_ms"] = rapidfire_hold_ms
    kwargs["rapidfire_wait_ms"] = rapidfire_wait_ms
    if typed_action.action_type == ActionType.MACRO:
        kwargs["macro_loop_stop_behavior"] = normalize_macro_loop_stop_behavior(
            kwargs["macro_loop_stop_behavior"]
        )
    return kwargs


def mapping_action_to_superkey_action(action: MappingAction) -> SuperkeyAction:
    if action.action_type not in SUPERKEY_ACTION_TYPES:
        raise ValueError(f"invalid pattern superkey action type: {action.action_type.value}")
    return SuperkeyAction(
        action_type=action.action_type,
        **superkey_action_shared_kwargs(action),
    )


def superkey_action_to_mapping_action(action: SuperkeyAction) -> MappingAction:
    return MappingAction(
        action_type=action.action_type,
        **superkey_action_shared_kwargs(action),
    )


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
    overload_down_actions: list[MappingAction] = field(default_factory=list)
    overload_up_actions: list[MappingAction] = field(default_factory=list)

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

    def __post_init__(self) -> None:
        for actions in (
            self.tap_actions,
            self.double_tap_actions,
            self.hold_actions,
            self.tap_hold_actions,
        ):
            for action in actions:
                if action.action_type == ActionType.SUPERKEY:
                    raise ValueError("nested superkeys are not allowed inside superkeys")
        for action in (
            *self.overload_actions,
            *self.overload_down_actions,
            *self.overload_up_actions,
        ):
            if action.action_type == ActionType.SUPERKEY:
                raise ValueError("nested superkeys are not allowed inside superkeys")
            if action.action_type == ActionType.REPEAT:
                raise ValueError("repeat is not allowed inside overload superkeys")

    def has_overload_actions(self) -> bool:
        return bool(self.overload_actions or self.overload_down_actions or self.overload_up_actions)

    def has_any_action(self) -> bool:
        return self.has_pattern_actions() or self.has_overload_actions()


class _ComboSuperkeyConfig(Protocol):
    mode: SuperkeyMode


@overload
def combo_effective_superkey_config(
    config: SuperkeyConfig,
    *,
    step_count: int,
) -> SuperkeyConfig: ...


@overload
def combo_effective_superkey_config(
    config: "superkey_state.SuperkeyConfig",
    *,
    step_count: int,
) -> "superkey_state.SuperkeyConfig": ...


def combo_effective_superkey_config[T: _ComboSuperkeyConfig](
    config: T,
    *,
    step_count: int,
) -> T:
    if config.mode != SuperkeyMode.PATTERN or step_count <= 1:
        return config
    return cast(
        T,
        replace(
            cast(Any, config),
            double_tap_actions=[],
            tap_hold_actions=[],
        ),
    )
