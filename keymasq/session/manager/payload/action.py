"""Mapping-action serialization policies for inspector, signature, and runtime use."""

import logging
from enum import Enum
from typing import TYPE_CHECKING, Literal, cast

from keymasq.common.model.actions import (
    MappingAction,
    normalize_mpris_command,
    normalize_natural_mouse_move_curve,
    profile_deactivation_policy_to_dict,
)
from keymasq.common.model.core import ActionType
from keymasq.common.model.superkeys import (
    SuperkeyAction,
    superkey_action_to_mapping_action,
)

from ..common import JsonObject
from . import macro
from .references import allocate

if TYPE_CHECKING:
    from ..core import SessionManager


log = logging.getLogger(__name__)


_TARGET_ACTION_TYPES = frozenset(
    {
        ActionType.KEYBOARD,
        ActionType.MOUSE,
        ActionType.GAMEPAD,
        ActionType.GAMEPAD_AXIS,
        ActionType.MOUSE_MOVE_REL,
        ActionType.MOUSE_MOVE_ABS,
        ActionType.MOUSE_MOVE_NATURAL_ABS,
    }
)
_RECORDING_ACTION_TYPES = frozenset(
    {
        ActionType.START_MACRO_RECORDING,
        ActionType.STOP_MACRO_RECORDING,
        ActionType.PLAY_MACRO_SLOT,
        ActionType.CANCEL_MACRO_PLAYBACK,
        ActionType.EMERGENCY_RESET,
    }
)
_RECORDING_SLOT_ACTION_TYPES = frozenset(
    {
        ActionType.START_MACRO_RECORDING,
        ActionType.STOP_MACRO_RECORDING,
        ActionType.PLAY_MACRO_SLOT,
    }
)
_PROFILE_ACTION_TYPES = frozenset(
    {
        ActionType.PROFILE_ENABLE,
        ActionType.PROFILE_DISABLE,
        ActionType.PROFILE_TOGGLE,
    }
)


class _Purpose(Enum):
    INSPECTOR = "inspector"
    SIGNATURE = "signature"
    COMBO_SIGNATURE = "combo_signature"
    DEVICE = "device"
    COMBO = "combo"
    OVERLOAD = "overload"


def _new_payload(action: MappingAction) -> dict[str, object]:
    data: dict[str, object] = {"action": action.action_type.value}
    if action.source_profile_name:
        data["source_profile_name"] = action.source_profile_name
    return data


def _set_optional_string(data: dict[str, object], key: str, value: object) -> None:
    if value is not None and str(value):
        data[key] = str(value)


def _require_manager(manager: "SessionManager | None") -> "SessionManager":
    if manager is None:
        raise ValueError("action payload purpose requires a session manager")
    return manager


def _signature_purpose(purpose: _Purpose) -> bool:
    return purpose in (_Purpose.SIGNATURE, _Purpose.COMBO_SIGNATURE)


def _add_inspector_base_fields(
    data: dict[str, object],
    action: MappingAction,
) -> None:
    _set_optional_string(data, "target", action.target)
    _set_optional_string(data, "output_id", action.output_id)
    if action.keys:
        data["keys"] = list(action.keys)
    _set_optional_string(data, "cmd", action.cmd)
    _set_optional_string(data, "superkey_name", action.superkey_name)
    if action.analog_control_names:
        data["analog_control_names"] = list(action.analog_control_names)
    elif action.analog_control_name:
        data["analog_control_name"] = action.analog_control_name


def _add_inspector_target_fields(
    data: dict[str, object],
    action: MappingAction,
) -> None:
    if action.action_type in (
        ActionType.MOUSE_MOVE_REL,
        ActionType.MOUSE_MOVE_ABS,
        ActionType.MOUSE_MOVE_NATURAL_ABS,
    ):
        data["x"] = int(action.move_x)
        data["y"] = int(action.move_y)
    if action.action_type == ActionType.MOUSE_MOVE_NATURAL_ABS:
        data["speed"] = float(action.move_speed)
        data["jitter"] = float(action.move_jitter)
        data["curve"] = normalize_natural_mouse_move_curve(action.move_curve)
        data["tolerance"] = int(action.move_tolerance)
        data["max_duration_ms"] = int(action.move_max_duration_ms)
    if action.action_type == ActionType.GAMEPAD_AXIS:
        data["value"] = int(action.axis_value)


def _finish(
    data: dict[str, object],
    action: MappingAction,
    purpose: _Purpose,
) -> dict[str, object]:
    if purpose == _Purpose.INSPECTOR:
        _add_rapidfire_and_tap_fields(data, action)
    return data


def _add_target_fields(
    data: dict[str, object],
    action: MappingAction,
    *,
    empty_target: bool,
) -> None:
    data["target"] = action.target or "" if empty_target else action.target
    if action.action_type in (ActionType.GAMEPAD, ActionType.GAMEPAD_AXIS) and action.output_id:
        data["output_id"] = action.output_id
    if action.action_type == ActionType.GAMEPAD_AXIS:
        data["value"] = int(action.axis_value)
    if action.action_type in (
        ActionType.MOUSE_MOVE_REL,
        ActionType.MOUSE_MOVE_ABS,
        ActionType.MOUSE_MOVE_NATURAL_ABS,
    ):
        data["x"] = int(action.move_x)
        data["y"] = int(action.move_y)
    if action.action_type == ActionType.MOUSE_MOVE_NATURAL_ABS:
        data["speed"] = float(action.move_speed)
        data["jitter"] = float(action.move_jitter)
        data["curve"] = normalize_natural_mouse_move_curve(action.move_curve)
        data["tolerance"] = int(action.move_tolerance)
        data["max_duration_ms"] = int(action.move_max_duration_ms)
    _add_rapidfire_and_tap_fields(data, action)


def _add_rapidfire_and_tap_fields(
    data: dict[str, object],
    action: MappingAction,
) -> None:
    if action.rapidfire_enabled:
        data["rapidfire_enabled"] = True
        data["rapidfire_hold_ms"] = int(action.rapidfire_hold_ms)
        data["rapidfire_wait_ms"] = int(action.rapidfire_wait_ms)
    if action.tap_enabled:
        data["tap_enabled"] = True
        data["tap_hold_ms"] = int(action.tap_hold_ms)


def _add_repeat_fields(data: dict[str, object], action: MappingAction) -> None:
    data["repeat_categories"] = list(action.repeat_categories or [])
    if action.rapidfire_enabled:
        data["rapidfire_enabled"] = True
        data["rapidfire_hold_ms"] = int(action.rapidfire_hold_ms)
        data["rapidfire_wait_ms"] = int(action.rapidfire_wait_ms)


def _add_compositor_fields(
    data: dict[str, object],
    action: MappingAction,
    *,
    trim_dispatcher: bool = False,
) -> bool:
    dispatcher = action.compositor_dispatcher or ""
    if trim_dispatcher:
        dispatcher = str(dispatcher).strip()
        if not dispatcher:
            return False
    if action.compositor_id:
        data["compositor"] = action.compositor_id
    data["dispatcher"] = dispatcher
    data["args"] = action.compositor_args or ""
    return True


def _add_profile_fields(
    data: dict[str, object],
    action: MappingAction,
    *,
    fallback_target: bool = True,
    include_target: bool = False,
) -> None:
    profile_name = action.profile_name or (action.target if fallback_target else "") or ""
    data["profile_name"] = profile_name
    if include_target:
        data["target"] = profile_name
    deactivation = profile_deactivation_policy_to_dict(action.profile_deactivation)
    if deactivation is not None and action.action_type != ActionType.PROFILE_DISABLE:
        data["deactivation"] = deactivation


def _serialize(
    manager: "SessionManager | None",
    action: MappingAction,
    *,
    purpose: _Purpose,
    hardware_id: str = "",
    step_count: int = 0,
    track_combo_refs: bool = False,
) -> dict[str, object] | None:
    data = _new_payload(action)
    if purpose == _Purpose.INSPECTOR:
        _add_inspector_base_fields(data, action)

    if action.action_type in _TARGET_ACTION_TYPES:
        if purpose == _Purpose.INSPECTOR:
            _add_inspector_target_fields(data, action)
        else:
            _add_target_fields(data, action, empty_target=_signature_purpose(purpose))
        return _finish(data, action, purpose)

    if action.action_type == ActionType.REPEAT:
        if purpose == _Purpose.INSPECTOR:
            data["repeat_categories"] = list(action.repeat_categories or [])
        else:
            _add_repeat_fields(data, action)
        return _finish(data, action, purpose)

    if action.action_type == ActionType.EXEC:
        if purpose == _Purpose.INSPECTOR:
            return _finish(data, action, purpose)
        if _signature_purpose(purpose):
            data["cmd"] = action.cmd or ""
            if purpose == _Purpose.COMBO_SIGNATURE and not str(data.get("cmd", "") or ""):
                return None
            return data
        if purpose == _Purpose.COMBO:
            if not action.cmd:
                return None
            data["exec_ref"] = allocate(
                _require_manager(manager),
                action.cmd,
                owner="combo",
            )
            return data
        if action.cmd:
            owner: Literal["device", "combo"] = "device"
            exec_hardware_id: str | None = hardware_id
            if purpose == _Purpose.OVERLOAD and track_combo_refs:
                owner = "combo"
                exec_hardware_id = None
            data["exec_ref"] = allocate(
                _require_manager(manager),
                action.cmd,
                owner=owner,
                hardware_id=exec_hardware_id,
            )
        return data

    if action.action_type == ActionType.COMPOSITOR_DISPATCH:
        if purpose == _Purpose.COMBO:
            if not _add_compositor_fields(data, action, trim_dispatcher=True):
                return None
        else:
            _add_compositor_fields(data, action)
            if purpose == _Purpose.COMBO_SIGNATURE and not str(data.get("dispatcher", "") or ""):
                return None
        return _finish(data, action, purpose)

    if action.action_type == ActionType.MPRIS:
        data["command"] = normalize_mpris_command(action.mpris_command)
        return _finish(data, action, purpose)

    if action.action_type in _RECORDING_ACTION_TYPES:
        if action.action_type in _RECORDING_SLOT_ACTION_TYPES:
            data["recording_slot"] = int(action.macro_recording_slot)
        return _finish(data, action, purpose)

    if action.action_type in _PROFILE_ACTION_TYPES:
        _add_profile_fields(
            data,
            action,
            fallback_target=purpose != _Purpose.INSPECTOR,
            include_target=purpose == _Purpose.INSPECTOR,
        )
        return _finish(data, action, purpose)

    if action.action_type == ActionType.MACRO:
        if purpose == _Purpose.INSPECTOR:
            macro.add_inspector_fields(data, action)
            return _finish(data, action, purpose)
        if _signature_purpose(purpose):
            macro.add_runtime_fields(data, action, include_empty=True)
            if purpose == _Purpose.COMBO_SIGNATURE and not str(data.get("macro_name", "") or ""):
                return None
            return data
        if purpose == _Purpose.COMBO:
            if macro.add_runtime_fields(data, action, include_empty=False):
                return data
            return None
        macro.add_runtime_fields(data, action, include_empty=False)
        return data

    if action.action_type == ActionType.SUPERKEY:
        if purpose == _Purpose.INSPECTOR:
            return _finish(data, action, purpose)
        from . import superkey

        runtime_manager = _require_manager(manager)
        if purpose in (_Purpose.COMBO, _Purpose.COMBO_SIGNATURE):
            config = superkey.resolve_combo(runtime_manager, action, step_count=step_count)
            if config is None:
                return None
            if purpose == _Purpose.COMBO_SIGNATURE:
                data["superkey"] = superkey.serialize_signature(
                    runtime_manager,
                    config,
                    "combo",
                )
            else:
                data["superkey"] = superkey.serialize(
                    runtime_manager,
                    config,
                    "combo",
                    track_combo_refs=True,
                )
            return data
        if action.superkey_name:
            config = runtime_manager.superkeys.get_superkey(action.superkey_name)
            if config:
                if purpose == _Purpose.SIGNATURE:
                    data["superkey"] = superkey.serialize_signature(
                        runtime_manager,
                        config,
                        hardware_id,
                    )
                elif purpose == _Purpose.DEVICE:
                    data["superkey"] = superkey.serialize(
                        runtime_manager,
                        config,
                        hardware_id,
                    )
        return data

    if action.action_type == ActionType.ANALOG_CONTROL:
        if purpose == _Purpose.INSPECTOR:
            return _finish(data, action, purpose)
        if purpose in (_Purpose.COMBO, _Purpose.COMBO_SIGNATURE):
            log.warning("Ignoring unsupported combo action: analog_control")
            return None
        if purpose == _Purpose.OVERLOAD:
            return data
        from . import analog

        runtime_manager = _require_manager(manager)
        configs = analog.resolve(runtime_manager, action)
        serializer = (
            analog.serialize_signature if purpose == _Purpose.SIGNATURE else analog.serialize
        )
        if len(configs) == 1:
            data["analog_control"] = serializer(
                runtime_manager,
                configs[0],
                hardware_id,
            )
        elif configs:
            data["analog_controls"] = [
                serializer(runtime_manager, config, hardware_id) for config in configs
            ]
        return data

    if action.action_type == ActionType.SUPPRESS:
        return _finish(data, action, purpose)

    if purpose == _Purpose.COMBO:
        return None
    return _finish(data, action, purpose)


def serialize_mapping_action(action: MappingAction | None) -> JsonObject | None:
    """Serialize an action for GUI inspector events."""
    if action is None:
        return None
    data = _serialize(None, action, purpose=_Purpose.INSPECTOR)
    assert data is not None
    return data


def action_signature_payload(
    manager: "SessionManager",
    action: MappingAction,
    hardware_id: str,
) -> dict[str, object]:
    data = _serialize(
        manager,
        action,
        purpose=_Purpose.SIGNATURE,
        hardware_id=hardware_id,
    )
    assert data is not None
    return data


def combo_action_signature_payload(
    manager: "SessionManager",
    action: MappingAction,
    *,
    step_count: int,
) -> dict[str, object] | None:
    return _serialize(
        manager,
        action,
        purpose=_Purpose.COMBO_SIGNATURE,
        step_count=step_count,
    )


def mapping_action_payload(
    manager: "SessionManager",
    action: MappingAction,
    hardware_id: str,
) -> JsonObject:
    data = _serialize(
        manager,
        action,
        purpose=_Purpose.DEVICE,
        hardware_id=hardware_id,
    )
    assert data is not None
    return cast(JsonObject, data)


def combo_action_to_payload(
    manager: "SessionManager",
    action: MappingAction,
    *,
    step_count: int,
) -> JsonObject | None:
    data = _serialize(
        manager,
        action,
        purpose=_Purpose.COMBO,
        step_count=step_count,
    )
    return cast(JsonObject | None, data)


def serialize_superkey_action(
    manager: "SessionManager",
    action: SuperkeyAction,
    hardware_id: str,
    *,
    track_combo_refs: bool = False,
) -> JsonObject:
    return serialize_overload_action(
        manager,
        superkey_action_to_mapping_action(action),
        hardware_id,
        track_combo_refs=track_combo_refs,
    )


def serialize_superkey_overload_action(
    manager: "SessionManager",
    action: MappingAction,
    hardware_id: str,
    *,
    track_combo_refs: bool = False,
) -> JsonObject:
    if action.action_type == ActionType.REPEAT:
        raise ValueError("repeat is not allowed inside overload superkeys")
    return serialize_overload_action(
        manager,
        action,
        hardware_id,
        track_combo_refs=track_combo_refs,
    )


def serialize_superkey_action_signature(
    manager: "SessionManager",
    action: SuperkeyAction,
    hardware_id: str,
) -> JsonObject:
    return cast(
        JsonObject,
        action_signature_payload(
            manager,
            superkey_action_to_mapping_action(action),
            hardware_id,
        ),
    )


def serialize_overload_action(
    manager: "SessionManager",
    action: MappingAction,
    hardware_id: str,
    *,
    track_combo_refs: bool = False,
) -> JsonObject:
    if action.action_type == ActionType.SUPERKEY:
        raise ValueError("nested superkeys are not allowed inside superkeys")
    if action.action_type == ActionType.ANALOG_CONTROL:
        raise ValueError("nested analog controls are not allowed inside analog controls")
    data = _serialize(
        manager,
        action,
        purpose=_Purpose.OVERLOAD,
        hardware_id=hardware_id,
        track_combo_refs=track_combo_refs,
    )
    assert data is not None
    return cast(JsonObject, data)
