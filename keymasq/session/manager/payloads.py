import json
import logging
from enum import Enum
from typing import TYPE_CHECKING, Literal, cast

from keymasq.common.models import (
    ActionType,
    AnalogActionThreshold,
    AnalogControlConfig,
    MappingAction,
    SuperkeyAction,
    SuperkeyConfig,
    SuperkeyMode,
    combo_effective_superkey_config,
    normalize_analog_control_features,
    normalize_mpris_command,
    profile_deactivation_policy_to_dict,
    superkey_action_to_mapping_action,
)
from keymasq.session.profiles import ResolvedCombo, ResolvedDeviceProfile

from .common import JsonObject, json_object
from .state import ExecBinding

if TYPE_CHECKING:
    from .core import SessionManager


log = logging.getLogger(__name__)


_TARGET_ACTION_TYPES = frozenset(
    {
        ActionType.KEYBOARD,
        ActionType.MOUSE,
        ActionType.GAMEPAD,
        ActionType.GAMEPAD_AXIS,
        ActionType.MOUSE_MOVE_REL,
        ActionType.MOUSE_MOVE_ABS,
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


class _ActionPayloadPurpose(Enum):
    INSPECTOR = "inspector"
    SIGNATURE = "signature"
    COMBO_SIGNATURE = "combo_signature"
    DEVICE = "device"
    COMBO = "combo"
    OVERLOAD = "overload"


def _new_action_payload(action: MappingAction) -> dict[str, object]:
    data: dict[str, object] = {"action": action.action_type.value}
    if action.source_profile_name:
        data["source_profile_name"] = action.source_profile_name
    return data


def _set_optional_string(data: dict[str, object], key: str, value: object) -> None:
    if value is not None and str(value):
        data[key] = str(value)


def _require_action_manager(manager: "SessionManager | None") -> "SessionManager":
    if manager is None:
        raise ValueError("action payload purpose requires a session manager")
    return manager


def _signature_purpose(purpose: _ActionPayloadPurpose) -> bool:
    return purpose in (
        _ActionPayloadPurpose.SIGNATURE,
        _ActionPayloadPurpose.COMBO_SIGNATURE,
    )


def _add_inspector_base_fields(data: dict[str, object], action: MappingAction) -> None:
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


def _add_inspector_target_action_fields(
    data: dict[str, object],
    action: MappingAction,
) -> None:
    if action.action_type in (ActionType.MOUSE_MOVE_REL, ActionType.MOUSE_MOVE_ABS):
        data["x"] = int(action.move_x)
        data["y"] = int(action.move_y)
    if action.action_type == ActionType.GAMEPAD_AXIS:
        data["value"] = int(action.axis_value)


def _add_inspector_macro_fields(data: dict[str, object], action: MappingAction) -> None:
    data["target"] = action.macro_name or ""
    data["replay_mouse_movement"] = bool(action.macro_replay_mouse_movement)
    data["replay_mouse_clicks"] = bool(action.macro_replay_mouse_clicks)
    data["speed"] = float(action.macro_speed)
    data["loop_mode"] = action.macro_loop_mode
    data["loop_count"] = int(action.macro_loop_count)
    data["loop_stop_behavior"] = action.macro_loop_stop_behavior
    data["move_to_start"] = bool(action.macro_move_to_start)
    data["start_x"] = int(action.macro_start_x)
    data["start_y"] = int(action.macro_start_y)
    data["block_mouse_movement"] = bool(action.macro_block_mouse_movement)


def _finish_action_payload(
    data: dict[str, object],
    action: MappingAction,
    purpose: _ActionPayloadPurpose,
) -> dict[str, object]:
    if purpose == _ActionPayloadPurpose.INSPECTOR:
        _add_rapidfire_and_tap_fields(data, action)
    return data


def _add_target_action_fields(
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
    if action.action_type in (ActionType.MOUSE_MOVE_REL, ActionType.MOUSE_MOVE_ABS):
        data["x"] = int(action.move_x)
        data["y"] = int(action.move_y)
    _add_rapidfire_and_tap_fields(data, action)


def _add_rapidfire_and_tap_fields(data: dict[str, object], action: MappingAction) -> None:
    if action.rapidfire_enabled:
        data["rapidfire_enabled"] = True
        data["rapidfire_hold_ms"] = int(action.rapidfire_hold_ms)
        data["rapidfire_wait_ms"] = int(action.rapidfire_wait_ms)
    if action.tap_enabled:
        data["tap_enabled"] = True
        data["tap_hold_ms"] = int(action.tap_hold_ms)


def _add_repeat_action_fields(data: dict[str, object], action: MappingAction) -> None:
    data["repeat_categories"] = list(action.repeat_categories or [])
    if action.rapidfire_enabled:
        data["rapidfire_enabled"] = True
        data["rapidfire_hold_ms"] = int(action.rapidfire_hold_ms)
        data["rapidfire_wait_ms"] = int(action.rapidfire_wait_ms)


def _add_compositor_dispatch_fields(
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


def _add_mpris_action_fields(data: dict[str, object], action: MappingAction) -> None:
    data["command"] = normalize_mpris_command(action.mpris_command)


def _add_recording_action_fields(data: dict[str, object], action: MappingAction) -> None:
    if action.action_type in _RECORDING_SLOT_ACTION_TYPES:
        data["recording_slot"] = int(action.macro_recording_slot)


def _add_profile_action_fields(
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


def _add_macro_payload_fields(
    data: dict[str, object],
    action: MappingAction,
    *,
    include_empty: bool,
) -> bool:
    if not include_empty and not action.macro_name:
        return False
    data["macro_name"] = action.macro_name or ""
    data["macro_replay_mouse_movement"] = bool(action.macro_replay_mouse_movement)
    data["macro_replay_mouse_clicks"] = bool(action.macro_replay_mouse_clicks)
    data["macro_speed"] = float(action.macro_speed)
    data["macro_loop_mode"] = action.macro_loop_mode
    data["macro_loop_count"] = int(action.macro_loop_count)
    data["macro_loop_stop_behavior"] = action.macro_loop_stop_behavior
    data["macro_move_to_start"] = bool(action.macro_move_to_start)
    data["macro_start_x"] = int(action.macro_start_x)
    data["macro_start_y"] = int(action.macro_start_y)
    data["macro_block_mouse_movement"] = bool(action.macro_block_mouse_movement)
    return True


def _serialize_action_payload(
    manager: "SessionManager | None",
    action: MappingAction,
    *,
    purpose: _ActionPayloadPurpose,
    hardware_id: str = "",
    step_count: int = 0,
    track_combo_refs: bool = False,
) -> dict[str, object] | None:
    data = _new_action_payload(action)
    if purpose == _ActionPayloadPurpose.INSPECTOR:
        _add_inspector_base_fields(data, action)

    if action.action_type in _TARGET_ACTION_TYPES:
        if purpose == _ActionPayloadPurpose.INSPECTOR:
            _add_inspector_target_action_fields(data, action)
        else:
            _add_target_action_fields(
                data,
                action,
                empty_target=_signature_purpose(purpose),
            )
        return _finish_action_payload(data, action, purpose)

    if action.action_type == ActionType.REPEAT:
        if purpose == _ActionPayloadPurpose.INSPECTOR:
            data["repeat_categories"] = list(action.repeat_categories or [])
        else:
            _add_repeat_action_fields(data, action)
        return _finish_action_payload(data, action, purpose)

    if action.action_type == ActionType.EXEC:
        if purpose == _ActionPayloadPurpose.INSPECTOR:
            return _finish_action_payload(data, action, purpose)
        if _signature_purpose(purpose):
            data["cmd"] = action.cmd or ""
            if purpose == _ActionPayloadPurpose.COMBO_SIGNATURE and not str(
                data.get("cmd", "") or ""
            ):
                return None
            return data
        if purpose == _ActionPayloadPurpose.COMBO:
            if not action.cmd:
                return None
            data["exec_ref"] = _allocate_exec_ref(
                _require_action_manager(manager),
                action.cmd,
                owner="combo",
            )
            return data
        if action.cmd:
            owner: Literal["device", "combo"] = "device"
            exec_hardware_id = hardware_id
            if purpose == _ActionPayloadPurpose.OVERLOAD and track_combo_refs:
                owner = "combo"
                exec_hardware_id = None
            data["exec_ref"] = _allocate_exec_ref(
                _require_action_manager(manager),
                action.cmd,
                owner=owner,
                hardware_id=exec_hardware_id,
            )
        return data

    if action.action_type == ActionType.COMPOSITOR_DISPATCH:
        if purpose == _ActionPayloadPurpose.COMBO:
            if not _add_compositor_dispatch_fields(data, action, trim_dispatcher=True):
                return None
        else:
            _add_compositor_dispatch_fields(data, action)
            if purpose == _ActionPayloadPurpose.COMBO_SIGNATURE and not str(
                data.get("dispatcher", "") or ""
            ):
                return None
        return _finish_action_payload(data, action, purpose)

    if action.action_type == ActionType.MPRIS:
        _add_mpris_action_fields(data, action)
        return _finish_action_payload(data, action, purpose)

    if action.action_type in _RECORDING_ACTION_TYPES:
        _add_recording_action_fields(data, action)
        return _finish_action_payload(data, action, purpose)

    if action.action_type in _PROFILE_ACTION_TYPES:
        _add_profile_action_fields(
            data,
            action,
            fallback_target=purpose != _ActionPayloadPurpose.INSPECTOR,
            include_target=purpose == _ActionPayloadPurpose.INSPECTOR,
        )
        return _finish_action_payload(data, action, purpose)

    if action.action_type == ActionType.MACRO:
        if purpose == _ActionPayloadPurpose.INSPECTOR:
            _add_inspector_macro_fields(data, action)
            return _finish_action_payload(data, action, purpose)
        if _signature_purpose(purpose):
            _add_macro_payload_fields(data, action, include_empty=True)
            if purpose == _ActionPayloadPurpose.COMBO_SIGNATURE and not str(
                data.get("macro_name", "") or ""
            ):
                return None
            return data
        if purpose == _ActionPayloadPurpose.COMBO:
            if _add_macro_payload_fields(data, action, include_empty=False):
                return data
            return None
        _add_macro_payload_fields(data, action, include_empty=False)
        return data

    if action.action_type == ActionType.SUPERKEY:
        if purpose == _ActionPayloadPurpose.INSPECTOR:
            return _finish_action_payload(data, action, purpose)
        runtime_manager = _require_action_manager(manager)
        if purpose in (
            _ActionPayloadPurpose.COMBO,
            _ActionPayloadPurpose.COMBO_SIGNATURE,
        ):
            config = _resolved_combo_superkey_config(
                runtime_manager,
                action,
                step_count=step_count,
            )
            if config is None:
                return None
            if purpose == _ActionPayloadPurpose.COMBO_SIGNATURE:
                data["superkey"] = serialize_superkey_signature(
                    runtime_manager,
                    config,
                    "combo",
                )
            else:
                data["superkey"] = serialize_superkey(
                    runtime_manager,
                    config,
                    "combo",
                    track_combo_refs=True,
                )
            return data
        if action.superkey_name:
            superkey_config = runtime_manager.superkeys.get_superkey(action.superkey_name)
            if superkey_config:
                if purpose == _ActionPayloadPurpose.SIGNATURE:
                    data["superkey"] = serialize_superkey_signature(
                        runtime_manager,
                        superkey_config,
                        hardware_id,
                    )
                elif purpose == _ActionPayloadPurpose.DEVICE:
                    data["superkey"] = serialize_superkey(
                        runtime_manager,
                        superkey_config,
                        hardware_id,
                    )
        return data

    if action.action_type == ActionType.ANALOG_CONTROL:
        if purpose == _ActionPayloadPurpose.INSPECTOR:
            return _finish_action_payload(data, action, purpose)
        if purpose in (
            _ActionPayloadPurpose.COMBO,
            _ActionPayloadPurpose.COMBO_SIGNATURE,
        ):
            log.warning("Ignoring unsupported combo action: analog_control")
            return None
        if purpose == _ActionPayloadPurpose.OVERLOAD:
            return data
        runtime_manager = _require_action_manager(manager)
        configs = _resolved_analog_control_configs(runtime_manager, action)
        if len(configs) == 1:
            if purpose == _ActionPayloadPurpose.SIGNATURE:
                data["analog_control"] = serialize_analog_control_signature(
                    runtime_manager,
                    configs[0],
                    hardware_id,
                )
            else:
                data["analog_control"] = serialize_analog_control(
                    runtime_manager,
                    configs[0],
                    hardware_id,
                )
        elif configs:
            if purpose == _ActionPayloadPurpose.SIGNATURE:
                data["analog_controls"] = [
                    serialize_analog_control_signature(runtime_manager, config, hardware_id)
                    for config in configs
                ]
            else:
                data["analog_controls"] = [
                    serialize_analog_control(runtime_manager, config, hardware_id)
                    for config in configs
                ]
        return data

    if action.action_type == ActionType.SUPPRESS:
        return _finish_action_payload(data, action, purpose)

    if purpose == _ActionPayloadPurpose.COMBO:
        return None
    return _finish_action_payload(data, action, purpose)


def serialize_mapping_action(action: MappingAction | None) -> JsonObject | None:
    if action is None:
        return None

    data = _serialize_action_payload(
        None,
        action,
        purpose=_ActionPayloadPurpose.INSPECTOR,
    )
    assert data is not None
    return data


def clear_exec_refs(manager: "SessionManager", hardware_id: str) -> None:
    refs = manager.exec_state.device_exec_refs.pop(hardware_id, set())
    for ref in refs:
        manager.exec_state.exec_refs.pop(ref, None)


def clear_combo_exec_refs(manager: "SessionManager") -> None:
    refs = list(manager.exec_state.combo_exec_refs)
    manager.exec_state.combo_exec_refs.clear()
    for ref in refs:
        manager.exec_state.exec_refs.pop(ref, None)


def clear_all_exec_refs(manager: "SessionManager") -> None:
    for hardware_id in list(manager.exec_state.device_exec_refs):
        clear_exec_refs(manager, hardware_id)
    clear_combo_exec_refs(manager)


def mapping_update_needed(
    manager: "SessionManager",
    hardware_id: str,
    resolved: ResolvedDeviceProfile,
) -> bool:
    return manager.profile_state.last_sent_mapping_signatures.get(
        hardware_id, ""
    ) != resolved_mapping_signature(manager, resolved, hardware_id)


def resolved_mapping_signature(
    manager: "SessionManager",
    resolved: ResolvedDeviceProfile,
    hardware_id: str,
) -> str:
    mapping: dict[str, dict[str, object]] = {}
    for button_id in sorted(resolved.mappings):
        mapping[button_id] = action_signature_payload(
            manager,
            resolved.mappings[button_id],
            hardware_id,
        )
    return json.dumps(mapping, sort_keys=True, separators=(",", ":"))


def resolved_combos_signature(
    manager: "SessionManager",
    combos: list[ResolvedCombo],
) -> str:
    payload: list[dict[str, object]] = []
    for combo in combos:
        if combo.action is None:
            continue
        action_data = combo_action_signature_payload(
            manager,
            combo.action,
            step_count=len(combo.steps),
        )
        if action_data is None:
            continue
        steps: list[dict[str, object]] = []
        for step in combo.steps:
            events = [
                {
                    "hardware_id": str(event.hardware_id or ""),
                    "source": str(event.source or ""),
                    "evdev": str(event.evdev or ""),
                }
                for event in step.events
                if event.evdev
            ]
            if not events:
                continue
            events.sort(
                key=lambda event: (
                    str(event["hardware_id"]),
                    str(event["source"]),
                    str(event["evdev"]),
                )
            )
            step_payload: dict[str, object] = {"events": events}
            if step.timeout_ms is not None:
                step_payload["timeout_ms"] = int(step.timeout_ms)
            steps.append(step_payload)
        if not steps:
            continue
        payload.append(
            {
                "id": combo.id,
                "name": combo.name,
                "profile_name": combo.profile_name,
                "steps": steps,
                "action": action_data,
                "match_across_devices": bool(combo.match_across_devices),
                **({"recall_trigger_keys": True} if combo.recall_trigger_keys else {}),
                **(
                    {"restore_trigger_keys": list(combo.restore_trigger_keys)}
                    if combo.restore_trigger_keys
                    else {}
                ),
            }
        )
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def action_signature_payload(
    manager: "SessionManager",
    action: MappingAction,
    hardware_id: str,
) -> dict[str, object]:
    data = _serialize_action_payload(
        manager,
        action,
        purpose=_ActionPayloadPurpose.SIGNATURE,
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
    return _serialize_action_payload(
        manager,
        action,
        purpose=_ActionPayloadPurpose.COMBO_SIGNATURE,
        step_count=step_count,
    )


def profile_to_mapping(
    manager: "SessionManager",
    resolved: ResolvedDeviceProfile,
    hardware_id: str,
) -> JsonObject:
    if hardware_id not in manager.exec_state.device_exec_refs:
        manager.exec_state.device_exec_refs[hardware_id] = set()

    mapping: dict[str, dict[str, object]] = {}
    for button_id, action in resolved.mappings.items():
        action_data = _serialize_action_payload(
            manager,
            action,
            purpose=_ActionPayloadPurpose.DEVICE,
            hardware_id=hardware_id,
        )
        assert action_data is not None

        mapping[button_id] = action_data

    return cast(JsonObject, mapping)


def resolved_combos_payload(
    manager: "SessionManager",
    combos: list[ResolvedCombo],
) -> list[JsonObject]:
    payload: list[JsonObject] = []
    for combo in combos:
        combo_payload = resolved_combo_payload(manager, combo)
        if combo_payload is not None:
            payload.append(combo_payload)
    return payload


def resolved_combo_payload(
    manager: "SessionManager",
    combo: ResolvedCombo,
) -> JsonObject | None:
    if combo.action is None:
        return None
    action_data = combo_action_to_payload(
        manager,
        combo.action,
        step_count=len(combo.steps),
    )
    if action_data is None:
        return None
    steps: list[dict[str, object]] = []
    for step in combo.steps:
        events: list[dict[str, str]] = []
        for event in step.events:
            if not event.evdev:
                continue
            event_data = {
                "evdev": event.evdev,
            }
            if event.hardware_id:
                event_data["hardware_id"] = event.hardware_id
            if event.source:
                event_data["source"] = event.source
            events.append(event_data)
        if events:
            step_payload: dict[str, object] = {"events": events}
            if step.timeout_ms is not None:
                step_payload["timeout_ms"] = int(step.timeout_ms)
            steps.append(step_payload)
    if not steps:
        return None
    return {
        "id": combo.id,
        "name": combo.name,
        "profile_name": combo.profile_name,
        "steps": steps,
        "action": action_data,
        "match_across_devices": bool(combo.match_across_devices),
        **({"recall_trigger_keys": True} if combo.recall_trigger_keys else {}),
        **(
            {"restore_trigger_keys": list(combo.restore_trigger_keys)}
            if combo.restore_trigger_keys
            else {}
        ),
    }


def combo_action_to_payload(
    manager: "SessionManager",
    action: MappingAction,
    *,
    step_count: int,
) -> JsonObject | None:
    data = _serialize_action_payload(
        manager,
        action,
        purpose=_ActionPayloadPurpose.COMBO,
        step_count=step_count,
    )
    return cast(JsonObject | None, data)


def _resolved_analog_control_configs(
    manager: "SessionManager",
    action: MappingAction,
) -> list[AnalogControlConfig]:
    configs: list[AnalogControlConfig] = []
    if action.analog_control_configs:
        configs.extend(action.analog_control_configs)
    elif action.analog_control_config is not None:
        configs.append(action.analog_control_config)

    names = action.analog_control_names
    if not names and action.analog_control_name:
        names = [action.analog_control_name]
    if not names:
        return configs
    analog_controls = getattr(manager, "analog_controls", None)
    get_analog_control = getattr(analog_controls, "get_analog_control", None)
    if not callable(get_analog_control):
        return configs
    for name in names:
        config = get_analog_control(name)
        if isinstance(config, AnalogControlConfig):
            configs.append(config)
    return configs


def _resolved_combo_superkey_config(
    manager: "SessionManager",
    action: MappingAction,
    *,
    step_count: int,
) -> SuperkeyConfig | None:
    if not action.superkey_name:
        return None
    config = manager.superkeys.get_superkey(action.superkey_name)
    if config is None:
        return None
    return combo_effective_superkey_config(config, step_count=step_count)


def serialize_superkey(
    manager: "SessionManager",
    config: SuperkeyConfig,
    hardware_id: str,
    *,
    track_combo_refs: bool = False,
) -> JsonObject:
    data: JsonObject = {
        "name": config.name,
        "mode": config.mode.value,
        "tap_timeout_ms": config.tap_timeout_ms,
        "double_tap_window_ms": config.double_tap_window_ms,
        "hold_threshold_ms": config.hold_threshold_ms,
    }

    if config.mode == SuperkeyMode.PATTERN:
        if config.tap_actions:
            data["tap_actions"] = [
                serialize_superkey_action(
                    manager,
                    action,
                    hardware_id,
                    track_combo_refs=track_combo_refs,
                )
                for action in config.tap_actions
            ]
        if config.double_tap_actions:
            data["double_tap_actions"] = [
                serialize_superkey_action(
                    manager,
                    action,
                    hardware_id,
                    track_combo_refs=track_combo_refs,
                )
                for action in config.double_tap_actions
            ]
        if config.hold_actions:
            data["hold_actions"] = [
                serialize_superkey_action(
                    manager,
                    action,
                    hardware_id,
                    track_combo_refs=track_combo_refs,
                )
                for action in config.hold_actions
            ]
        if config.tap_hold_actions:
            data["tap_hold_actions"] = [
                serialize_superkey_action(
                    manager,
                    action,
                    hardware_id,
                    track_combo_refs=track_combo_refs,
                )
                for action in config.tap_hold_actions
            ]
    elif config.overload_actions:
        data["overload_actions"] = [
            serialize_superkey_overload_action(
                manager,
                action,
                hardware_id,
                track_combo_refs=track_combo_refs,
            )
            for action in config.overload_actions
        ]
    if config.mode == SuperkeyMode.OVERLOAD:
        if config.overload_down_actions:
            data["overload_down_actions"] = [
                serialize_superkey_overload_action(
                    manager,
                    action,
                    hardware_id,
                    track_combo_refs=track_combo_refs,
                )
                for action in config.overload_down_actions
            ]
        if config.overload_up_actions:
            data["overload_up_actions"] = [
                serialize_superkey_overload_action(
                    manager,
                    action,
                    hardware_id,
                    track_combo_refs=track_combo_refs,
                )
                for action in config.overload_up_actions
            ]

    return data


def serialize_analog_control(
    manager: "SessionManager",
    config: AnalogControlConfig,
    hardware_id: str,
) -> JsonObject:
    config = normalize_analog_control_features(config)
    return {
        "name": config.name,
        "input_type": config.input_type,
        "mouse_motion": {
            "enabled": bool(config.mouse_motion.enabled),
            "mode": config.mouse_motion.mode,
            "speed": float(config.mouse_motion.speed),
            "speed_x": float(
                config.mouse_motion.speed_x
                if config.mouse_motion.speed_x is not None
                else config.mouse_motion.speed
            ),
            "speed_y": float(
                config.mouse_motion.speed_y
                if config.mouse_motion.speed_y is not None
                else config.mouse_motion.speed
            ),
            "area_radius_x": float(config.mouse_motion.area_radius_x),
            "area_radius_y": float(config.mouse_motion.area_radius_y),
            "area_start_enabled": bool(config.mouse_motion.area_start_enabled),
            "area_start_x": int(config.mouse_motion.area_start_x),
            "area_start_y": int(config.mouse_motion.area_start_y),
            "deadzone": float(config.mouse_motion.deadzone),
            "sensitivity": float(config.mouse_motion.sensitivity),
            "response_curve": float(config.mouse_motion.response_curve),
            "direction": config.mouse_motion.direction,
            "invert_x": bool(config.mouse_motion.invert_x),
            "invert_y": bool(config.mouse_motion.invert_y),
            "tick_ms": int(config.mouse_motion.tick_ms),
        },
        "gamepad_output": {
            "enabled": bool(config.gamepad_output.enabled),
            "output_id": config.gamepad_output.output_id,
            "deadzone": float(config.gamepad_output.deadzone),
            "target": config.gamepad_output.target,
            "target_analog_id": config.gamepad_output.target_analog_id,
            "output_rest": config.gamepad_output.output_rest,
            "output_direction": config.gamepad_output.output_direction,
            "output_invert": bool(config.gamepad_output.output_invert),
            "sensitivity": float(config.gamepad_output.sensitivity),
            "response_curve": float(config.gamepad_output.response_curve),
        },
        "thresholds": [
            serialize_analog_threshold(manager, threshold, hardware_id)
            for threshold in config.thresholds
        ],
    }


def serialize_analog_threshold(
    manager: "SessionManager",
    threshold: AnalogActionThreshold,
    hardware_id: str,
) -> JsonObject:
    return {
        "axis": threshold.axis,
        "trigger_min": float(threshold.trigger_min),
        "trigger_max": float(threshold.trigger_max),
        "release_min": float(threshold.release_min),
        "release_max": float(threshold.release_max),
        "actions": [
            serialize_overload_action(manager, action, hardware_id)
            for action in threshold.actions
        ],
    }


def serialize_analog_control_signature(
    manager: "SessionManager",
    config: AnalogControlConfig,
    hardware_id: str,
) -> JsonObject:
    config = normalize_analog_control_features(config)
    return {
        "name": config.name,
        "input_type": config.input_type,
        "mouse_motion": {
            "enabled": bool(config.mouse_motion.enabled),
            "mode": config.mouse_motion.mode,
            "speed": float(config.mouse_motion.speed),
            "speed_x": float(
                config.mouse_motion.speed_x
                if config.mouse_motion.speed_x is not None
                else config.mouse_motion.speed
            ),
            "speed_y": float(
                config.mouse_motion.speed_y
                if config.mouse_motion.speed_y is not None
                else config.mouse_motion.speed
            ),
            "area_radius_x": float(config.mouse_motion.area_radius_x),
            "area_radius_y": float(config.mouse_motion.area_radius_y),
            "area_start_enabled": bool(config.mouse_motion.area_start_enabled),
            "area_start_x": int(config.mouse_motion.area_start_x),
            "area_start_y": int(config.mouse_motion.area_start_y),
            "deadzone": float(config.mouse_motion.deadzone),
            "sensitivity": float(config.mouse_motion.sensitivity),
            "response_curve": float(config.mouse_motion.response_curve),
            "direction": config.mouse_motion.direction,
            "invert_x": bool(config.mouse_motion.invert_x),
            "invert_y": bool(config.mouse_motion.invert_y),
            "tick_ms": int(config.mouse_motion.tick_ms),
        },
        "gamepad_output": {
            "enabled": bool(config.gamepad_output.enabled),
            "output_id": config.gamepad_output.output_id,
            "deadzone": float(config.gamepad_output.deadzone),
            "target": config.gamepad_output.target,
            "target_analog_id": config.gamepad_output.target_analog_id,
            "output_rest": config.gamepad_output.output_rest,
            "output_direction": config.gamepad_output.output_direction,
            "output_invert": bool(config.gamepad_output.output_invert),
            "sensitivity": float(config.gamepad_output.sensitivity),
            "response_curve": float(config.gamepad_output.response_curve),
        },
        "thresholds": [
            serialize_analog_threshold_signature(manager, threshold, hardware_id)
            for threshold in config.thresholds
        ],
    }


def serialize_analog_threshold_signature(
    manager: "SessionManager",
    threshold: AnalogActionThreshold,
    hardware_id: str,
) -> JsonObject:
    return {
        "axis": threshold.axis,
        "trigger_min": float(threshold.trigger_min),
        "trigger_max": float(threshold.trigger_max),
        "release_min": float(threshold.release_min),
        "release_max": float(threshold.release_max),
        "actions": [
            action_signature_payload(manager, action, hardware_id)
            for action in threshold.actions
        ],
    }


def serialize_superkey_signature(
    manager: "SessionManager",
    config: SuperkeyConfig,
    hardware_id: str,
) -> JsonObject:
    data: JsonObject = {
        "name": config.name,
        "mode": config.mode.value,
        "tap_timeout_ms": int(config.tap_timeout_ms),
        "double_tap_window_ms": int(config.double_tap_window_ms),
        "hold_threshold_ms": int(config.hold_threshold_ms),
    }

    if config.mode == SuperkeyMode.PATTERN:
        if config.tap_actions:
            data["tap_actions"] = [
                serialize_superkey_action_signature(manager, action, hardware_id)
                for action in config.tap_actions
            ]
        if config.double_tap_actions:
            data["double_tap_actions"] = [
                serialize_superkey_action_signature(manager, action, hardware_id)
                for action in config.double_tap_actions
            ]
        if config.hold_actions:
            data["hold_actions"] = [
                serialize_superkey_action_signature(manager, action, hardware_id)
                for action in config.hold_actions
            ]
        if config.tap_hold_actions:
            data["tap_hold_actions"] = [
                serialize_superkey_action_signature(manager, action, hardware_id)
                for action in config.tap_hold_actions
            ]
    elif config.overload_actions:
        data["overload_actions"] = [
            action_signature_payload(manager, action, hardware_id)
            for action in config.overload_actions
        ]
    if config.mode == SuperkeyMode.OVERLOAD:
        if config.overload_down_actions:
            data["overload_down_actions"] = [
                action_signature_payload(manager, action, hardware_id)
                for action in config.overload_down_actions
            ]
        if config.overload_up_actions:
            data["overload_up_actions"] = [
                action_signature_payload(manager, action, hardware_id)
                for action in config.overload_up_actions
            ]

    return data


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
    return action_signature_payload(
        manager,
        superkey_action_to_mapping_action(action),
        hardware_id,
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
    data = _serialize_action_payload(
        manager,
        action,
        purpose=_ActionPayloadPurpose.OVERLOAD,
        hardware_id=hardware_id,
        track_combo_refs=track_combo_refs,
    )
    assert data is not None
    return cast(JsonObject, data)


def _allocate_exec_ref(
    manager: "SessionManager",
    cmd: str,
    *,
    owner: Literal["device", "combo"],
    hardware_id: str | None = None,
) -> int:
    exec_ref = manager.exec_state.next_exec_ref
    manager.exec_state.next_exec_ref += 1
    if owner == "device":
        if not hardware_id:
            raise ValueError("device exec refs require a hardware_id")
        manager.exec_state.device_exec_refs.setdefault(hardware_id, set()).add(exec_ref)
        manager.exec_state.exec_refs[exec_ref] = ExecBinding(
            cmd=cmd,
            owner="device",
            hardware_id=hardware_id,
        )
    elif owner == "combo":
        manager.exec_state.combo_exec_refs.add(exec_ref)
        manager.exec_state.exec_refs[exec_ref] = ExecBinding(cmd=cmd, owner="combo")
    else:
        raise ValueError(f"unknown exec ref owner: {owner}")
    return exec_ref


def mapping_log_view(mapping: JsonObject) -> JsonObject:
    view: JsonObject = {}
    for button_id, action_data in mapping.items():
        action_data_dict = json_object(action_data)
        if action_data_dict is None:
            view[button_id] = action_data
            continue

        data = dict(action_data_dict)
        events = data.get("macro_events")
        if isinstance(events, list):
            event_items = cast(list[object], events)
            data["macro_events"] = f"<{len(event_items)} events>"
        view[button_id] = data

    return view
