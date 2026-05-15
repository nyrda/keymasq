import json
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
    superkey_action_to_mapping_action,
)
from keymasq.session.profiles import ResolvedCombo, ResolvedDeviceProfile

from .common import JsonObject, json_object
from .state import ExecBinding

if TYPE_CHECKING:
    from .core import SessionManager


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
                if event.hardware_id and event.evdev
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
    action_type = action.action_type.value
    data: dict[str, object] = {"action": action_type}

    if action_type in (
        "keyboard",
        "mouse",
        "gamepad",
        "gamepad_axis",
        "mouse_move_rel",
        "mouse_move_abs",
    ):
        data["target"] = action.target or ""
        if action_type in ("gamepad", "gamepad_axis") and action.output_id:
            data["output_id"] = action.output_id
        if action_type == "gamepad_axis":
            data["value"] = int(action.axis_value)
        if action_type in ("mouse_move_rel", "mouse_move_abs"):
            data["x"] = int(action.move_x)
            data["y"] = int(action.move_y)
        if action.rapidfire_enabled:
            data["rapidfire_enabled"] = True
            data["rapidfire_hold_ms"] = int(action.rapidfire_hold_ms)
            data["rapidfire_wait_ms"] = int(action.rapidfire_wait_ms)
        if action.tap_enabled:
            data["tap_enabled"] = True
            data["tap_hold_ms"] = int(action.tap_hold_ms)
        return data

    if action_type == "exec":
        data["cmd"] = action.cmd or ""
        return data

    if action_type == "compositor_dispatch":
        if action.compositor_id:
            data["compositor"] = action.compositor_id
        data["dispatcher"] = action.compositor_dispatcher or ""
        data["args"] = action.compositor_args or ""
        return data

    if action_type in (
        "start_macro_recording",
        "stop_macro_recording",
        "cancel_macro_playback",
        "emergency_reset",
    ):
        return data

    if action_type in (
        "profile_enable",
        "profile_disable",
        "profile_toggle",
    ):
        data["profile_name"] = action.profile_name or action.target or ""
        return data

    if action_type == "macro":
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
        return data

    if action_type == "superkey":
        if action.superkey_name:
            superkey_config = manager.superkeys.get_superkey(action.superkey_name)
            if superkey_config:
                data["superkey"] = serialize_superkey_signature(
                    manager,
                    superkey_config,
                    hardware_id,
                )
        return data

    if action_type == "analog_control":
        config = _resolved_analog_control_config(manager, action)
        if config is not None:
            data["analog_control"] = serialize_analog_control_signature(
                manager,
                config,
                hardware_id,
            )
        return data

    return data


def combo_action_signature_payload(
    manager: "SessionManager",
    action: MappingAction,
    *,
    step_count: int,
) -> dict[str, object] | None:
    if action.action_type == ActionType.SUPERKEY:
        config = _resolved_combo_superkey_config(manager, action, step_count=step_count)
        if config is None:
            return None
        return {
            "action": action.action_type.value,
            "superkey": serialize_superkey_signature(manager, config, "combo"),
        }

    data = action_signature_payload(manager, action, "")
    if data.get("action") == "superkey":
        return None
    if data.get("action") == "analog_control":
        return None
    if data.get("action") == "exec" and not str(data.get("cmd", "") or ""):
        return None
    if data.get("action") == "compositor_dispatch" and not str(data.get("dispatcher", "") or ""):
        return None
    if data.get("action") == "macro" and not str(data.get("macro_name", "") or ""):
        return None
    return data


def profile_to_mapping(
    manager: "SessionManager",
    resolved: ResolvedDeviceProfile,
    hardware_id: str,
) -> JsonObject:
    if hardware_id not in manager.exec_state.device_exec_refs:
        manager.exec_state.device_exec_refs[hardware_id] = set()

    mapping: dict[str, dict[str, object]] = {}
    for button_id, action in resolved.mappings.items():
        action_data: dict[str, object] = {"action": action.action_type.value}

        if action.action_type.value in (
            "keyboard",
            "mouse",
            "gamepad",
            "gamepad_axis",
            "mouse_move_rel",
            "mouse_move_abs",
        ):
            action_data["target"] = action.target
            if action.action_type.value in ("gamepad", "gamepad_axis") and action.output_id:
                action_data["output_id"] = action.output_id
            if action.action_type.value == "gamepad_axis":
                action_data["value"] = int(action.axis_value)
            if action.action_type.value in ("mouse_move_rel", "mouse_move_abs"):
                action_data["x"] = int(action.move_x)
                action_data["y"] = int(action.move_y)
            if action.rapidfire_enabled:
                action_data["rapidfire_enabled"] = True
                action_data["rapidfire_hold_ms"] = action.rapidfire_hold_ms
                action_data["rapidfire_wait_ms"] = action.rapidfire_wait_ms
            if action.tap_enabled:
                action_data["tap_enabled"] = True
                action_data["tap_hold_ms"] = action.tap_hold_ms
        elif action.action_type.value == "exec":
            if action.cmd:
                exec_ref = _allocate_exec_ref(
                    manager,
                    action.cmd,
                    owner="device",
                    hardware_id=hardware_id,
                )
                action_data["exec_ref"] = exec_ref
        elif action.action_type.value == "compositor_dispatch":
            if action.compositor_id:
                action_data["compositor"] = action.compositor_id
            action_data["dispatcher"] = action.compositor_dispatcher or ""
            action_data["args"] = action.compositor_args or ""
        elif action.action_type.value in (
            "start_macro_recording",
            "stop_macro_recording",
            "cancel_macro_playback",
            "emergency_reset",
        ):
            pass
        elif action.action_type.value in (
            "profile_enable",
            "profile_disable",
            "profile_toggle",
        ):
            action_data["profile_name"] = action.profile_name or action.target or ""
        elif action.action_type.value == "macro":
            if action.macro_name:
                action_data["macro_name"] = action.macro_name
                action_data["macro_replay_mouse_movement"] = action.macro_replay_mouse_movement
                action_data["macro_replay_mouse_clicks"] = action.macro_replay_mouse_clicks
                action_data["macro_speed"] = action.macro_speed
                action_data["macro_loop_mode"] = action.macro_loop_mode
                action_data["macro_loop_count"] = int(action.macro_loop_count)
                action_data["macro_loop_stop_behavior"] = action.macro_loop_stop_behavior
                action_data["macro_move_to_start"] = bool(action.macro_move_to_start)
                action_data["macro_start_x"] = int(action.macro_start_x)
                action_data["macro_start_y"] = int(action.macro_start_y)
                action_data["macro_block_mouse_movement"] = bool(action.macro_block_mouse_movement)
        elif action.action_type.value == "superkey":
            if action.superkey_name:
                superkey_config = manager.superkeys.get_superkey(action.superkey_name)
                if superkey_config:
                    action_data["superkey"] = serialize_superkey(
                        manager,
                        superkey_config,
                        hardware_id,
                    )
        elif action.action_type.value == "analog_control":
            if action.analog_control_name:
                analog_config = _resolved_analog_control_config(manager, action)
                if analog_config:
                    action_data["analog_control"] = serialize_analog_control(
                        manager,
                        analog_config,
                        hardware_id,
                    )

        mapping[button_id] = action_data

    return cast(JsonObject, mapping)


def resolved_combos_payload(
    manager: "SessionManager",
    combos: list[ResolvedCombo],
) -> list[JsonObject]:
    payload: list[JsonObject] = []
    for combo in combos:
        if combo.action is None:
            continue
        action_data = combo_action_to_payload(
            manager,
            combo.action,
            step_count=len(combo.steps),
        )
        if action_data is None:
            continue
        steps: list[dict[str, object]] = []
        for step in combo.steps:
            events: list[dict[str, str]] = []
            for event in step.events:
                if not event.hardware_id or not event.evdev:
                    continue
                event_data = {
                    "hardware_id": event.hardware_id,
                    "evdev": event.evdev,
                }
                if event.source:
                    event_data["source"] = event.source
                events.append(event_data)
            if events:
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
                **({"recall_trigger_keys": True} if combo.recall_trigger_keys else {}),
                **(
                    {"restore_trigger_keys": list(combo.restore_trigger_keys)}
                    if combo.restore_trigger_keys
                    else {}
                ),
            }
        )
    return payload


def combo_action_to_payload(
    manager: "SessionManager",
    action: MappingAction,
    *,
    step_count: int,
) -> JsonObject | None:
    action_type = action.action_type.value
    action_data: dict[str, object] = {"action": action_type}

    if action_type in (
        "keyboard",
        "mouse",
        "gamepad",
        "gamepad_axis",
        "mouse_move_rel",
        "mouse_move_abs",
    ):
        action_data["target"] = action.target
        if action_type in ("gamepad", "gamepad_axis") and action.output_id:
            action_data["output_id"] = action.output_id
        if action_type == "gamepad_axis":
            action_data["value"] = int(action.axis_value)
        if action_type in ("mouse_move_rel", "mouse_move_abs"):
            action_data["x"] = int(action.move_x)
            action_data["y"] = int(action.move_y)
        if action.rapidfire_enabled:
            action_data["rapidfire_enabled"] = True
            action_data["rapidfire_hold_ms"] = action.rapidfire_hold_ms
            action_data["rapidfire_wait_ms"] = action.rapidfire_wait_ms
        if action.tap_enabled:
            action_data["tap_enabled"] = True
            action_data["tap_hold_ms"] = action.tap_hold_ms
        return action_data

    if action_type == "exec":
        if not action.cmd:
            return None
        exec_ref = _allocate_exec_ref(manager, action.cmd, owner="combo")
        action_data["exec_ref"] = exec_ref
        return action_data

    if action_type == "compositor_dispatch":
        dispatcher = str(action.compositor_dispatcher or "").strip()
        if not dispatcher:
            return None
        if action.compositor_id:
            action_data["compositor"] = action.compositor_id
        action_data["dispatcher"] = dispatcher
        action_data["args"] = action.compositor_args or ""
        return action_data

    if action_type in (
        "start_macro_recording",
        "stop_macro_recording",
        "cancel_macro_playback",
        "emergency_reset",
    ):
        return action_data

    if action_type in ("profile_enable", "profile_disable", "profile_toggle"):
        action_data["profile_name"] = action.profile_name or action.target or ""
        return action_data

    if action_type == "macro":
        if action.macro_name:
            action_data["macro_name"] = action.macro_name
            action_data["macro_replay_mouse_movement"] = action.macro_replay_mouse_movement
            action_data["macro_replay_mouse_clicks"] = action.macro_replay_mouse_clicks
            action_data["macro_speed"] = action.macro_speed
            action_data["macro_loop_mode"] = action.macro_loop_mode
            action_data["macro_loop_count"] = int(action.macro_loop_count)
            action_data["macro_loop_stop_behavior"] = action.macro_loop_stop_behavior
            action_data["macro_move_to_start"] = bool(action.macro_move_to_start)
            action_data["macro_start_x"] = int(action.macro_start_x)
            action_data["macro_start_y"] = int(action.macro_start_y)
            action_data["macro_block_mouse_movement"] = bool(action.macro_block_mouse_movement)
            return action_data
        return None

    if action_type == "suppress":
        return action_data

    if action_type == "superkey":
        config = _resolved_combo_superkey_config(manager, action, step_count=step_count)
        if config is None:
            return None
        action_data["superkey"] = serialize_superkey(
            manager,
            config,
            "combo",
            track_combo_refs=True,
        )
        return action_data

    if action_type == "analog_control":
        return None

    return None


def _resolved_analog_control_config(
    manager: "SessionManager",
    action: MappingAction,
) -> AnalogControlConfig | None:
    if not action.analog_control_name:
        return None
    analog_controls = getattr(manager, "analog_controls", None)
    get_analog_control = getattr(analog_controls, "get_analog_control", None)
    if not callable(get_analog_control):
        return None
    config = get_analog_control(action.analog_control_name)
    return config if isinstance(config, AnalogControlConfig) else None


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
            serialize_overload_action(
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
                serialize_overload_action(
                    manager,
                    action,
                    hardware_id,
                    track_combo_refs=track_combo_refs,
                )
                for action in config.overload_down_actions
            ]
        if config.overload_up_actions:
            data["overload_up_actions"] = [
                serialize_overload_action(
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
    return {
        "name": config.name,
        "input_type": config.input_type,
        "mouse_motion": {
            "enabled": bool(config.mouse_motion.enabled),
            "speed": float(config.mouse_motion.speed),
            "deadzone": float(config.mouse_motion.deadzone),
            "curve": config.mouse_motion.curve,
            "invert_x": bool(config.mouse_motion.invert_x),
            "invert_y": bool(config.mouse_motion.invert_y),
            "tick_ms": int(config.mouse_motion.tick_ms),
        },
        "gamepad_output": {
            "enabled": bool(config.gamepad_output.enabled),
            "output_id": config.gamepad_output.output_id,
            "deadzone": float(config.gamepad_output.deadzone),
            "target": config.gamepad_output.target,
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
    return {
        "name": config.name,
        "input_type": config.input_type,
        "mouse_motion": {
            "enabled": bool(config.mouse_motion.enabled),
            "speed": float(config.mouse_motion.speed),
            "deadzone": float(config.mouse_motion.deadzone),
            "curve": config.mouse_motion.curve,
            "invert_x": bool(config.mouse_motion.invert_x),
            "invert_y": bool(config.mouse_motion.invert_y),
            "tick_ms": int(config.mouse_motion.tick_ms),
        },
        "gamepad_output": {
            "enabled": bool(config.gamepad_output.enabled),
            "output_id": config.gamepad_output.output_id,
            "deadzone": float(config.gamepad_output.deadzone),
            "target": config.gamepad_output.target,
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
    action_type = action.action_type.value
    if action.action_type == ActionType.SUPERKEY:
        raise ValueError("nested superkeys are not allowed inside superkeys")
    if action.action_type == ActionType.ANALOG_CONTROL:
        raise ValueError("nested analog controls are not allowed inside analog controls")
    action_data: JsonObject = {"action": action_type}

    if action_type in (
        "keyboard",
        "mouse",
        "gamepad",
        "gamepad_axis",
        "mouse_move_rel",
        "mouse_move_abs",
    ):
        action_data["target"] = action.target
        if action_type in ("gamepad", "gamepad_axis") and action.output_id:
            action_data["output_id"] = action.output_id
        if action_type == "gamepad_axis":
            action_data["value"] = int(action.axis_value)
        if action_type in ("mouse_move_rel", "mouse_move_abs"):
            action_data["x"] = int(action.move_x)
            action_data["y"] = int(action.move_y)
        if action.rapidfire_enabled:
            action_data["rapidfire_enabled"] = True
            action_data["rapidfire_hold_ms"] = action.rapidfire_hold_ms
            action_data["rapidfire_wait_ms"] = action.rapidfire_wait_ms
        if action.tap_enabled:
            action_data["tap_enabled"] = True
            action_data["tap_hold_ms"] = action.tap_hold_ms
        return action_data

    if action_type == "exec":
        if action.cmd:
            exec_ref = _allocate_exec_ref(
                manager,
                action.cmd,
                owner="combo" if track_combo_refs else "device",
                hardware_id=None if track_combo_refs else hardware_id,
            )
            action_data["exec_ref"] = exec_ref
        return action_data

    if action_type == "compositor_dispatch":
        if action.compositor_id:
            action_data["compositor"] = action.compositor_id
        action_data["dispatcher"] = action.compositor_dispatcher or ""
        action_data["args"] = action.compositor_args or ""
        return action_data

    if action_type in (
        "start_macro_recording",
        "stop_macro_recording",
        "cancel_macro_playback",
    ):
        return action_data

    if action_type in (
        "profile_enable",
        "profile_disable",
        "profile_toggle",
    ):
        action_data["profile_name"] = action.profile_name or action.target or ""
        return action_data

    if action_type == "macro":
        if action.macro_name:
            action_data["macro_name"] = action.macro_name
            action_data["macro_replay_mouse_movement"] = action.macro_replay_mouse_movement
            action_data["macro_replay_mouse_clicks"] = action.macro_replay_mouse_clicks
            action_data["macro_speed"] = action.macro_speed
            action_data["macro_loop_mode"] = action.macro_loop_mode
            action_data["macro_loop_count"] = int(action.macro_loop_count)
            action_data["macro_loop_stop_behavior"] = action.macro_loop_stop_behavior
            action_data["macro_move_to_start"] = bool(action.macro_move_to_start)
            action_data["macro_start_x"] = int(action.macro_start_x)
            action_data["macro_start_y"] = int(action.macro_start_y)
            action_data["macro_block_mouse_movement"] = bool(action.macro_block_mouse_movement)
        return action_data

    return action_data


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
