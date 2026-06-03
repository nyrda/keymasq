import asyncio
import logging
import os
import time
import uuid
from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any, cast

from keymasq.common.ipc import Command, CommandType
from keymasq.common.models import (
    MAX_MACRO_RECORDING_SLOTS,
    ActionType,
    ProfileDeactivationPolicy,
    normalize_macro_recording_slot,
    normalize_profile_deactivation_policy,
    parse_profile_deactivation_policy,
    profile_deactivation_policy_to_dict,
)

from . import compositor as runtime_compositor
from . import device_inspector as runtime_device_inspector
from . import profiles as runtime_profiles
from . import recording as runtime_recording
from .common import JsonObject
from .common import int_value as _int_value
from .common import json_list as _json_list
from .common import str_value as _str_value
from .state import RuntimeProfileActivation

if TYPE_CHECKING:
    from .core import SessionManager

log = logging.getLogger("keymasq-session")
GRAB_RETRY_DELAY_S = 5.0
TOPOLOGY_REFRESH_DEBOUNCE_S = 0.5
TOPOLOGY_REFRESH_RETRY_S = 1.0


def create_event_task[TaskResult](
    manager: "SessionManager",
    coro: Coroutine[Any, Any, TaskResult],
    *,
    name: str,
    extra_task_set: set[asyncio.Task[TaskResult]] | None = None,
) -> asyncio.Task[TaskResult]:
    task = asyncio.create_task(coro, name=f"keymasq-session:{name}")
    manager.event_state.tasks.add(task)
    if extra_task_set is not None:
        extra_task_set.add(task)

    def _discard(done: asyncio.Task[TaskResult]) -> None:
        manager.event_state.tasks.discard(done)
        if extra_task_set is not None:
            extra_task_set.discard(done)
        try:
            exc = done.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            log.error(
                "Unhandled exception in %s event task",
                name,
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    task.add_done_callback(_discard)
    return task


async def cancel_event_tasks(manager: "SessionManager") -> None:
    tasks = list(manager.event_state.tasks)
    if not tasks:
        return

    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    manager.event_state.tasks.difference_update(tasks)


async def handle_event(
    manager: "SessionManager",
    event_type: CommandType,
    data: JsonObject,
) -> None:
    if manager.verbosity >= 1:
        log.debug("Event: %s -> %s", event_type.value, event_log_view(data))

    if event_type == CommandType.ACTION_TRIGGER:
        exec_ref_raw = data.get("exec_ref")
        exec_ref = _int_value(exec_ref_raw, -1) if exec_ref_raw is not None else None
        if exec_ref is not None:
            binding = manager.exec_state.exec_refs.get(exec_ref)
            if binding:
                exec_data = dict(data)
                exec_data["cmd"] = binding.cmd
                if binding.hardware_id:
                    exec_data["hardware_id"] = binding.hardware_id
                create_event_task(manager, handle_exec_trigger(manager, exec_data), name="exec")
            else:
                log.warning("Unknown exec_ref: %s", exec_ref)

        action_type_str = str(data.get("action_type", "") or "")
        if action_type_str == "start_macro_recording":
            create_event_task(
                manager,
                handle_start_macro_trigger(manager, data),
                name="start_macro_recording",
            )
        elif action_type_str == "stop_macro_recording":
            create_event_task(
                manager,
                handle_stop_macro_trigger(manager, data),
                name="stop_macro_recording",
            )
        elif action_type_str == "play_macro_slot":
            create_event_task(
                manager,
                runtime_recording.play_macro_slot_trigger(manager, data),
                name="play_macro_slot",
            )
        elif action_type_str == "cancel_macro_playback":
            create_event_task(
                manager,
                handle_cancel_macro_trigger(manager),
                name="cancel_macro_playback",
            )
        elif action_type_str == "emergency_reset":
            create_event_task(
                manager,
                handle_emergency_reset_trigger(manager),
                name="emergency_reset",
            )
        elif action_type_str in {"profile_enable", "profile_disable", "profile_toggle"}:
            create_event_task(
                manager,
                handle_profile_trigger(manager, data),
                name="profile_trigger",
            )
        elif action_type_str == "exec" and exec_ref is None:
            create_event_task(manager, handle_exec_trigger(manager, data), name="exec")
        elif action_type_str == "compositor_dispatch":
            create_event_task(
                manager,
                runtime_compositor.handle_compositor_dispatch_trigger(manager, data),
                name="compositor_dispatch",
            )
        elif action_type_str == "macro":
            create_event_task(
                manager,
                runtime_recording.play_macro_trigger(manager, data),
                name="macro_playback",
            )
        return

    if event_type == CommandType.DEVICE_CONNECTED:
        log.info("Device connected: %s", data)
        await on_device_connected(manager, data)
        return

    if event_type == CommandType.DEVICE_DISCONNECTED:
        log.info("Device disconnected: %s", data)
        await on_device_disconnected(manager, data)
        return

    if event_type == CommandType.DEVICE_GRAB_STATUS:
        handle_device_grab_status_event(manager, data)
        return

    if event_type == CommandType.MACRO_PLAYBACK_CANCELLED:
        handle_macro_playback_cancelled_event(manager, data)
        return

    if event_type == CommandType.RUNTIME_RESET:
        create_event_task(
            manager,
            handle_runtime_reset_event(manager, data),
            name="runtime_reset",
        )
        return

    if event_type == CommandType.PROFILE_DEACTIVATE_REQUESTED:
        create_event_task(
            manager,
            handle_profile_deactivate_requested(manager, data),
            name="profile_deactivate_requested",
        )
        return

    if event_type == CommandType.DIAGNOSTICS_SNAPSHOT:
        manager.broadcast_to_session_clients({"event": "diagnostics_snapshot", **data})
        return

    if event_type == CommandType.DEVICE_INSPECTOR_EVENT:
        runtime_device_inspector.broadcast_event_to_owners(manager, data)
        return

    if event_type == CommandType.DEVICE_INSPECTOR_STATUS:
        runtime_device_inspector.update_status_from_daemon_event(manager, data)
        manager.broadcast_to_session_clients({"event": "device_inspector_status", **data})
        return

    if event_type == CommandType.RECORDING_STARTED:
        manager.recording_state.active = True
        event_data = dict(data)
        recording_slot = normalize_macro_recording_slot(
            event_data.get("recording_slot")
        ) or normalize_macro_recording_slot(manager.recording_state.active_slot)
        if recording_slot:
            manager.recording_state.active_slot = recording_slot
            event_data["recording_slot"] = int(recording_slot)
        _notify_recording_started(manager, recording_slot)
        manager.broadcast_to_session_clients({"event": "recording_started", **event_data})
        return

    if event_type == CommandType.RECORDING_STOPPED:
        manager.recording_state.active = False
        recording_slot = runtime_recording.normalize_pending_macro_recording_slot(
            data.get("recording_slot", manager.recording_state.active_slot),
            default=1,
        )
        recording_data = dict(data)
        recording_data["recording_slot"] = recording_slot
        if manager.recording_state.start_cursor:
            recording_data["start_x"] = int(manager.recording_state.start_cursor[0])
            recording_data["start_y"] = int(manager.recording_state.start_cursor[1])
            recording_data["move_to_start"] = True
        pending_save_token = await runtime_recording.store_pending_macro_save(
            manager,
            recording_data,
            recording_slot=recording_slot,
        )
        _notify_recording_stopped(manager, recording_slot, recording_data)
        manager.recording_state.start_cursor = None
        manager.recording_state.active_slot = 0
        manager.broadcast_to_session_clients(
            {
                "event": "recording_stopped",
                "pending_save_token": pending_save_token,
                "recording_slot": recording_slot,
                "duration_ms": recording_data.get("duration_ms", 0),
                "event_count": _int_value(recording_data.get("event_count"), 0),
                "device_types": recording_data.get("device_types", []),
                "start_x": recording_data.get("start_x"),
                "start_y": recording_data.get("start_y"),
                "move_to_start": recording_data.get("move_to_start", False),
            }
        )
        return

    if event_type == CommandType.RECORDING_PROGRESS:
        progress_data = dict(data)
        if manager.recording_state.active_slot:
            progress_data["recording_slot"] = int(manager.recording_state.active_slot)
        manager.broadcast_to_session_clients({"event": "recording_progress", **progress_data})


def _notify_recording_started(manager: "SessionManager", recording_slot: int) -> None:
    slot = normalize_macro_recording_slot(recording_slot)
    suffix = f"Slot {slot} is recording." if slot else "Macro recording is active."
    manager.send_notification("Keymasq: Macro Recording Started", suffix)


def _notify_recording_stopped(
    manager: "SessionManager",
    recording_slot: int,
    recording_data: JsonObject,
) -> None:
    slot = normalize_macro_recording_slot(recording_slot)
    event_count = _int_value(recording_data.get("event_count"), 0)
    duration_ms = _int_value(recording_data.get("duration_ms"), 0)
    if duration_ms >= 1000:
        duration_text = f"{duration_ms / 1000.0:.1f}s"
    else:
        duration_text = f"{duration_ms}ms"
    event_word = "event" if event_count == 1 else "events"
    prefix = f"Slot {slot}" if slot else "Macro recording"
    manager.send_notification(
        "Keymasq: Macro Recording Stopped",
        f"{prefix} captured {event_count} {event_word} over {duration_text}.",
    )


async def handle_start_macro_trigger(
    manager: "SessionManager",
    data: JsonObject | None = None,
) -> None:
    data = data or {}
    recording_slot = normalize_macro_recording_slot(data.get("recording_slot"))
    if not recording_slot:
        manager.send_notification(
            "Keymasq: Recording Slot Required",
            f"Macro recording triggers must choose a slot from 1 to {MAX_MACRO_RECORDING_SLOTS}.",
        )
        log.info("Ignored start_macro_recording trigger: missing explicit recording slot")
        return

    if manager.recording_state.active:
        active_slot = normalize_macro_recording_slot(manager.recording_state.active_slot)
        if active_slot == recording_slot:
            await handle_stop_macro_trigger(manager, data)
            return
        manager.send_notification(
            "Keymasq: Macro Recording Active",
            f"Slot {active_slot or '?'} is already recording.",
        )
        return

    status = await runtime_recording.resolve_macro_recording_status_async(
        manager,
        os.getuid(),
    )
    if not bool(status.get("unlocked", False)):
        log.info("Ignored start_macro_recording trigger: macro recording is disabled")
        runtime_recording.notify_macro_recording_disabled(manager)
        manager.broadcast_to_session_clients(
            {
                "event": "macro_recording_disabled",
                **runtime_recording.serialize_macro_recording_state(status),
            }
        )
        return

    result = await runtime_recording.start_recording(
        manager,
        reset_if_active=False,
        recording_slot=recording_slot,
    )
    if result.get("status") != "ok":
        if runtime_recording.is_macro_recording_disabled_error(result):
            runtime_recording.notify_macro_recording_disabled(manager)
            manager.broadcast_to_session_clients({"event": "macro_recording_disabled"})
            return
        if runtime_recording.is_recording_unlock_required_error(result):
            runtime_recording.notify_recording_unlock_required(manager, result)
            manager.broadcast_to_session_clients({"event": "recording_auth_requested"})


async def handle_stop_macro_trigger(
    manager: "SessionManager",
    data: JsonObject | None = None,
) -> None:
    if not manager.recording_state.active:
        return
    data = data or {}
    recording_slot = normalize_macro_recording_slot(data.get("recording_slot"))
    active_slot = normalize_macro_recording_slot(manager.recording_state.active_slot)
    if recording_slot and active_slot and recording_slot != active_slot:
        log.info(
            "Ignored stop_macro_recording trigger for slot %s while slot %s is active",
            recording_slot,
            active_slot,
        )
        return
    try:
        await runtime_recording.stop_recording(
            manager,
            error_if_idle=False,
            recording_slot=recording_slot or active_slot,
        )
    except Exception:
        log.debug("Failed to handle stop macro recording trigger", exc_info=True)


async def handle_cancel_macro_trigger(manager: "SessionManager") -> None:
    try:
        await manager.client.send_command(Command(command=CommandType.CANCEL_MACRO_PLAYBACK))
    except Exception:
        log.debug("Failed to send cancel macro playback trigger", exc_info=True)


async def handle_emergency_reset_trigger(manager: "SessionManager") -> None:
    try:
        await manager.client.send_command(Command(command=CommandType.EMERGENCY_RESET))
    except Exception:
        log.debug("Failed to send emergency reset trigger", exc_info=True)


async def handle_profile_trigger(manager: "SessionManager", data: JsonObject) -> None:
    action_type = str(data.get("action_type", "") or "").strip().lower()
    profile_name = str(data.get("profile_name", "") or "").strip()
    if not profile_name:
        return

    deactivation = _trigger_deactivation_policy(action_type, data)
    if deactivation is not None:
        await _handle_lifetime_profile_trigger(
            manager,
            action_type,
            profile_name,
            deactivation,
            data,
        )
        return

    enabled: bool | None
    if action_type == "profile_enable":
        enabled = True
    elif action_type == "profile_disable":
        enabled = False
    else:
        enabled = None

    if (
        action_type in {"profile_disable", "profile_toggle"}
        and profile_name in manager.profile_state.runtime_profile_activations
    ):
        await runtime_profiles.cancel_runtime_profile_activation(manager, profile_name)

    result = await runtime_profiles.set_profile_enabled(manager, profile_name, enabled)
    if result.get("status") != "ok":
        log.warning(
            "Profile trigger failed action=%s profile=%s message=%s",
            action_type,
            profile_name,
            result.get("message", "unknown error"),
        )
        return

    if action_type == "profile_disable" or (
        action_type == "profile_toggle" and result.get("enabled") is False
    ):
        await runtime_profiles.cancel_runtime_profile_activation(manager, profile_name)


def _trigger_deactivation_policy(
    action_type: str,
    data: JsonObject,
) -> ProfileDeactivationPolicy | None:
    try:
        model_action_type = ActionType(action_type)
    except ValueError:
        return None
    return normalize_profile_deactivation_policy(
        model_action_type,
        parse_profile_deactivation_policy(data.get("deactivation")),
    )


async def _handle_lifetime_profile_trigger(
    manager: "SessionManager",
    action_type: str,
    profile_name: str,
    deactivation: ProfileDeactivationPolicy,
    data: JsonObject,
) -> None:
    if manager.profiles.get_profile(profile_name) is None:
        manager.send_notification(
            "Keymasq: Profile Not Found",
            f"Profile '{profile_name}' was not found.",
        )
        log.warning(
            "Profile trigger failed action=%s profile=%s not found",
            action_type,
            profile_name,
        )
        return

    if action_type == ActionType.PROFILE_TOGGLE.value:
        if await runtime_profiles.cancel_runtime_profile_activation(manager, profile_name):
            return
    elif action_type != ActionType.PROFILE_ENABLE.value:
        result = await runtime_profiles.set_profile_enabled(manager, profile_name, False)
        if result.get("status") == "ok":
            await runtime_profiles.cancel_runtime_profile_activation(manager, profile_name)
        return

    manager.profile_state.runtime_profile_activation_seq += 1
    activation_id = uuid.uuid4().hex
    trigger_id = str(data.get("trigger_id", "") or "").strip()
    if not trigger_id:
        trigger_id = f"{data.get('source_device', '')}:{data.get('source_button', '')}"
    activation = RuntimeProfileActivation(
        profile_name=profile_name,
        activation_id=activation_id,
        sequence=manager.profile_state.runtime_profile_activation_seq,
        deactivation=deactivation,
        source_device=str(data.get("source_device", "") or ""),
        source_button=str(data.get("source_button", "") or ""),
        trigger_id=trigger_id,
        created_at=time.time(),
    )
    manager.profile_state.runtime_profile_activations[profile_name] = activation
    await runtime_profiles.reevaluate_profiles(
        manager,
        reason=f"runtime profile activation {profile_name}",
    )
    if not await _track_runtime_profile_activation(manager, activation):
        current = manager.profile_state.runtime_profile_activations.get(profile_name)
        if current is not None and current.activation_id == activation.activation_id:
            manager.profile_state.runtime_profile_activations.pop(profile_name, None)
            await runtime_profiles.reevaluate_profiles(
                manager,
                reason=f"runtime profile activation tracking failed {profile_name}",
            )
    else:
        current = manager.profile_state.runtime_profile_activations.get(profile_name)
        if current is not None and current.activation_id == activation.activation_id:
            current.tracked = True


async def _track_runtime_profile_activation(
    manager: "SessionManager",
    activation: RuntimeProfileActivation,
) -> bool:
    deactivation_data = profile_deactivation_policy_to_dict(activation.deactivation)
    if deactivation_data is None:
        return False
    try:
        response = await manager.client.send_command(
            Command(
                command=CommandType.TRACK_PROFILE_ACTIVATION,
                data={
                    "profile_name": activation.profile_name,
                    "activation_id": activation.activation_id,
                    "trigger_id": activation.trigger_id,
                    "deactivation": deactivation_data,
                },
            )
        )
    except Exception as exc:
        log.warning(
            "Failed to track runtime profile activation profile=%s activation=%s: %s",
            activation.profile_name,
            activation.activation_id,
            exc,
        )
        return False
    if response.status != "ok":
        log.warning(
            "Runtime profile activation tracking rejected profile=%s activation=%s: %s",
            activation.profile_name,
            activation.activation_id,
            response.error or response.status,
        )
        return False
    response_data = response.data
    if isinstance(response_data, dict):
        tracked_data = cast(dict[str, object], response_data)
    else:
        tracked_data = {}
    if tracked_data.get("tracked") is False:
        log.warning(
            "Runtime profile activation was not tracked profile=%s activation=%s",
            activation.profile_name,
            activation.activation_id,
        )
        return False
    return True


async def handle_profile_deactivate_requested(
    manager: "SessionManager",
    data: JsonObject,
) -> None:
    profile_name = str(data.get("profile_name", "") or "").strip()
    activation_id = str(data.get("activation_id", "") or "").strip()
    if not profile_name or not activation_id:
        return
    activation = manager.profile_state.runtime_profile_activations.get(profile_name)
    if activation is None or activation.activation_id != activation_id:
        return
    manager.profile_state.runtime_profile_activations.pop(profile_name, None)
    await runtime_profiles.reevaluate_profiles(
        manager,
        reason=f"runtime profile activation expired {profile_name}",
    )


async def handle_exec_trigger(manager: "SessionManager", data: JsonObject) -> None:
    cmd = str(data.get("cmd", "") or "").strip()
    if not cmd:
        return

    wait_id = str(data.get("macro_exec_wait_id", "") or "").strip()
    is_async = bool(data.get("macro_exec_async", False))

    action_handler = manager.action_handler
    if action_handler is None:
        return

    if wait_id:
        policy_timeout_ms = max(1, int(manager.security_policy.macro_exec_timeout_max_ms))
        timeout_ms = max(
            1,
            _int_value(data.get("macro_exec_timeout_ms"), policy_timeout_ms),
        )
        timeout_ms = min(timeout_ms, policy_timeout_ms)
        returncode = await action_handler.execute_command(
            cmd,
            timeout_s=timeout_ms / 1000.0,
        )
        try:
            await manager.client.send_command(
                Command(
                    command=CommandType.MACRO_EXEC_COMPLETE,
                    data={"wait_id": wait_id, "returncode": int(returncode)},
                )
            )
        except Exception:
            log.debug("Failed to report macro exec completion", exc_info=True)
        return

    if is_async:
        action_handler.execute_command_sync(cmd)
        return

    await action_handler.execute_command(cmd)


def handle_device_grab_status_event(manager: "SessionManager", data: JsonObject) -> None:
    hardware_id = str(data.get("hardware_id", "") or "")
    state = str(data.get("state", "") or "").strip().lower()
    active_keys = [str(key) for key in _json_list(data.get("active_keys")) if str(key)]
    summary = ", ".join(active_keys) if active_keys else "unknown keys"

    manager.broadcast_to_session_clients({"event": "device_grab_status", **data})

    if not hardware_id:
        return

    device_name = device_name_for_hardware(manager, hardware_id)
    if state == "waiting":
        if hardware_id in manager.profile_state.grab_waiting_devices:
            return
        manager.profile_state.grab_waiting_devices.add(hardware_id)
        manager.send_notification(
            "Keymasq: Grab Pending",
            f"{device_name}: waiting for keys to be released ({summary}).",
        )
        return

    if state == "ready":
        manager.profile_state.grab_waiting_devices.discard(hardware_id)
        return

    if state == "timed_out":
        manager.profile_state.grab_waiting_devices.discard(hardware_id)
        manager.send_notification(
            "Keymasq: Grab Timed Out",
            f"{device_name}: keys stayed down too long ({summary}). Retrying automatically.",
        )
        runtime_profiles.schedule_grab_retry(manager, hardware_id, GRAB_RETRY_DELAY_S)


def handle_macro_playback_cancelled_event(
    manager: "SessionManager",
    data: JsonObject,
) -> None:
    manager.broadcast_to_session_clients({"event": "macro_playback_cancelled", **data})
    manager.send_notification(
        "Keymasq: Macro Playback Cancelled",
        "Stopped all running macro playback.",
    )


async def handle_runtime_reset_event(manager: "SessionManager", data: JsonObject) -> None:
    runtime_device_inspector.clear_all_device_inspector_state(manager)
    manager.broadcast_to_session_clients({"event": "runtime_reset", **data})
    manager.send_notification(
        "Keymasq: Emergency Reset",
        "Released all grabbed devices. Reapplying active profiles.",
    )
    runtime_profiles.invalidate_grabbed_state(manager)
    manager.profile_state.runtime_profile_activations.clear()
    try:
        await runtime_profiles.reevaluate_profiles(manager, reason="runtime reset")
    except Exception as exc:
        log.warning("Failed to reapply profiles after runtime reset: %s", exc)
        manager.send_notification(
            "Keymasq: Reapply Failed",
            "Emergency reset completed, but active profiles could not be reapplied.",
        )


async def on_device_connected(manager: "SessionManager", device_info: JsonObject) -> None:
    hardware_id = (
        f"{_str_value(device_info.get('vendor_id'), '')}:"
        f"{_str_value(device_info.get('product_id'), '')}"
    )
    if not hardware_id or ":" not in hardware_id:
        return
    if not _hardware_or_model_known(manager, hardware_id):
        return
    runtime_profiles.schedule_topology_refresh(
        manager,
        TOPOLOGY_REFRESH_DEBOUNCE_S,
        TOPOLOGY_REFRESH_RETRY_S,
    )
    create_event_task(
        manager,
        _refresh_recording_devices_cache_after_topology(manager),
        name="recording_devices_refresh",
    )


async def on_device_disconnected(manager: "SessionManager", device_info: JsonObject) -> None:
    hardware_id = _str_value(device_info.get("hardware_id"), "")
    if not hardware_id or ":" not in hardware_id:
        hardware_id = (
            f"{_str_value(device_info.get('vendor_id'), '')}:"
            f"{_str_value(device_info.get('product_id'), '')}"
        )
    if not hardware_id or ":" not in hardware_id:
        return
    if not _hardware_or_model_known(manager, hardware_id):
        return
    runtime_profiles.schedule_topology_refresh(
        manager,
        TOPOLOGY_REFRESH_DEBOUNCE_S,
        TOPOLOGY_REFRESH_RETRY_S,
    )
    create_event_task(
        manager,
        _refresh_recording_devices_cache_after_topology(manager),
        name="recording_devices_refresh",
    )


async def _refresh_recording_devices_cache_after_topology(manager: "SessionManager") -> None:
    await asyncio.sleep(TOPOLOGY_REFRESH_DEBOUNCE_S + 0.1)
    await runtime_recording.refresh_recording_devices_cache(manager)


def device_name_for_hardware(manager: "SessionManager", hardware_id: str) -> str:
    hardware = manager.hardware.get_hardware(hardware_id)
    if hardware is None:
        return hardware_id
    return str(getattr(hardware, "name", "") or hardware_id)


def _hardware_or_model_known(manager: "SessionManager", hardware_id: str) -> bool:
    if manager.hardware.get_hardware(hardware_id) is not None:
        return True
    if hardware_id in manager.profile_state.resolved_devices:
        return True
    return any(
        getattr(hardware, "model_id", "") == hardware_id
        for hardware in manager.hardware.list_hardware()
    )


def event_log_view(data: JsonObject) -> JsonObject:
    view = dict(data)
    events = _json_list(view.get("events"))
    if events:
        view["events"] = f"<{len(events)} events>"
        if "event_count" not in view:
            view["event_count"] = len(events)
    return view
