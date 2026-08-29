import logging
from typing import cast

import evdev

from keymasq.common.model.actions import MappingAction
from keymasq.common.model.core import ActionType
from keymasq.keymasqd.output_helpers import emit_mouse_move
from keymasq.keymasqd.runtime.adapters import (
    UInputWriter,
    WritableUInput,
    identity_uinput_writer,
)
from keymasq.keymasqd.runtime.grabbed_device.types import (
    ActionRuntime,
    EvdevModule,
    InputEventLike,
)

log = logging.getLogger("keymasqd.devices")


def mark_passthrough_frame_open(
    device_runtime: ActionRuntime,
    uinput_dev: object | None,
) -> None:
    if uinput_dev is None:
        return
    device_runtime.state.passthrough_frame_output = uinput_dev


def mark_passthrough_frame_closed(
    device_runtime: ActionRuntime,
    uinput_dev: object | None,
) -> None:
    if uinput_dev is None:
        return
    if device_runtime.state.passthrough_frame_output is uinput_dev:
        device_runtime.state.passthrough_frame_output = None


def passthrough_frame_open(
    device_runtime: ActionRuntime,
    uinput_dev: object | None,
) -> bool:
    if uinput_dev is None:
        return False
    return device_runtime.state.passthrough_frame_output is uinput_dev


def syn_if_passthrough_frame_closed(
    uinput_dev: object | None,
    writer: WritableUInput,
    *,
    device_runtime: ActionRuntime | None = None,
    force: bool = False,
) -> None:
    if force or device_runtime is None or not passthrough_frame_open(device_runtime, uinput_dev):
        writer.syn()


def flush_passthrough_frame(
    device_runtime: ActionRuntime,
    uinput_dev: object | None,
    *,
    uinput_writer: UInputWriter,
) -> None:
    if not passthrough_frame_open(device_runtime, uinput_dev):
        return
    writer = uinput_writer(uinput_dev)
    if writer is None:
        mark_passthrough_frame_closed(device_runtime, uinput_dev)
        return
    writer.syn()
    mark_passthrough_frame_closed(device_runtime, uinput_dev)


def bucket_for_uinput(device_runtime: ActionRuntime, uinput_dev: object | None) -> str | None:
    if uinput_dev is None:
        return None
    if device_runtime.uinput is not None and uinput_dev is device_runtime.uinput:
        return "passthrough"
    if device_runtime.keyboard_uinput is not None and uinput_dev is device_runtime.keyboard_uinput:
        return "keyboard"
    if device_runtime.mouse_uinput is not None and uinput_dev is device_runtime.mouse_uinput:
        return "mouse"
    if device_runtime.gamepad_uinput is not None and uinput_dev is device_runtime.gamepad_uinput:
        return "gamepad"
    return None


def track_key_state(
    device_runtime: ActionRuntime,
    uinput_dev: object | None,
    code: int,
    value: int,
    *,
    bucket: str | None = None,
) -> None:
    bucket = bucket or bucket_for_uinput(device_runtime, uinput_dev)
    if not bucket:
        return
    held = device_runtime.state.held_output_keys.setdefault(bucket, set())
    if int(value) == 1:
        held.add(int(code))
    elif int(value) == 0:
        held.discard(int(code))


def track_abs_state(
    device_runtime: ActionRuntime,
    axis_code: int,
    value: int,
    *,
    bucket: str | None,
) -> None:
    if not bucket:
        return
    held = device_runtime.state.held_output_abs.setdefault(bucket, set())
    if int(value) != 0:
        held.add(int(axis_code))
    else:
        held.discard(int(axis_code))


def track_refcounted_held_output(
    refcounts: dict[int, int],
    held: set[int],
    code: int,
    *,
    pressed: bool,
    released: bool,
) -> bool:
    """Update shared held-output state and return whether to emit the event."""
    output_code = int(code)
    current = refcounts.get(output_code, 0)

    if pressed:
        refcounts[output_code] = current + 1
        held.add(output_code)
        return current == 0

    if released:
        if current <= 1:
            refcounts.pop(output_code, None)
            held.discard(output_code)
            return current == 1

        refcounts[output_code] = current - 1
        return False

    return True


def track_refcounted_output_bucket(
    refcount_buckets: dict[str, dict[int, int]],
    held_buckets: dict[str, set[int]],
    bucket: str,
    code: int,
    value: int,
    *,
    pressed_value: int | None = 1,
    release_value: int = 0,
) -> bool:
    """Update a bucketed shared-output refcount and held set."""
    event_value = int(value)
    normalized_release_value = int(release_value)
    pressed = (
        event_value != normalized_release_value
        if pressed_value is None
        else event_value == int(pressed_value)
    )
    return track_refcounted_held_output(
        refcount_buckets.setdefault(bucket, {}),
        held_buckets.setdefault(bucket, set()),
        code,
        pressed=pressed,
        released=event_value == normalized_release_value,
    )


def write_abs_axis(
    device_runtime: ActionRuntime,
    uinput_dev: object | None,
    axis_code: int,
    value: int,
    *,
    evdev_mod: EvdevModule,
    uinput_writer: UInputWriter,
    bucket: str | None = None,
    defer_syn_to_passthrough_frame: bool = True,
) -> None:
    writer = uinput_writer(uinput_dev)
    if writer is None:
        return
    writer.write(evdev_mod.ecodes.EV_ABS, int(axis_code), int(value))
    if defer_syn_to_passthrough_frame:
        syn_if_passthrough_frame_closed(
            uinput_dev,
            writer,
            device_runtime=device_runtime,
        )
    else:
        writer.syn()
    track_abs_state(device_runtime, int(axis_code), int(value), bucket=bucket)


def write_key(
    device_runtime: ActionRuntime,
    uinput_dev: object | None,
    code: int,
    value: int,
    *,
    evdev_mod: EvdevModule,
    uinput_writer: UInputWriter,
    bucket: str | None = None,
    sync: bool = True,
    defer_syn_to_passthrough_frame: bool = True,
) -> None:
    writer = uinput_writer(uinput_dev)
    if writer is None:
        return
    writer.write(evdev_mod.ecodes.EV_KEY, int(code), int(value))
    if sync:
        if defer_syn_to_passthrough_frame:
            syn_if_passthrough_frame_closed(
                uinput_dev,
                writer,
                device_runtime=device_runtime,
            )
        else:
            writer.syn()
    track_key_state(device_runtime, uinput_dev, int(code), int(value), bucket=bucket)


def track_superkey_output(
    device_runtime: ActionRuntime, action_type: str, code: int, value: int
) -> bool:
    return track_refcounted_output_bucket(
        device_runtime.state.superkey_output_refcounts,
        device_runtime.state.held_output_keys,
        action_type,
        code,
        value,
    )


def track_superkey_abs_output(
    device_runtime: ActionRuntime,
    bucket: str,
    axis_code: int,
    value: int,
    *,
    release_value: int = 0,
) -> bool:
    return track_refcounted_output_bucket(
        device_runtime.state.superkey_abs_refcounts,
        device_runtime.state.held_output_abs,
        bucket,
        axis_code,
        value,
        pressed_value=None,
        release_value=release_value,
    )


def passthrough(
    device_runtime: ActionRuntime,
    event: InputEventLike,
    *,
    evdev_mod: EvdevModule,
    uinput_writer: UInputWriter,
    sync: bool = True,
) -> None:
    if (
        device_runtime.suppress_rel_getter
        and event.type == evdev_mod.ecodes.EV_REL
        and event.code in (evdev_mod.ecodes.REL_X, evdev_mod.ecodes.REL_Y)
        and device_runtime.suppress_rel_getter()
    ):
        return
    if event.type == evdev_mod.ecodes.EV_KEY:
        write_key(
            device_runtime,
            device_runtime.uinput,
            int(event.code),
            int(event.value),
            evdev_mod=evdev_mod,
            uinput_writer=uinput_writer,
            sync=sync,
            defer_syn_to_passthrough_frame=False,
        )
        if not sync:
            mark_passthrough_frame_open(device_runtime, device_runtime.uinput)
        return
    uinput = device_runtime.uinput
    writer = uinput_writer(uinput)
    if writer is None:
        return
    writer.write(event.type, event.code, event.value)
    if event.type == evdev_mod.ecodes.EV_ABS:
        track_passthrough_abs_state(
            device_runtime,
            int(event.code),
            int(event.value),
            evdev_mod=evdev_mod,
        )
    if sync:
        writer.syn()
    else:
        mark_passthrough_frame_open(device_runtime, uinput)


def track_passthrough_abs_state(
    device_runtime: ActionRuntime,
    code: int,
    value: int,
    *,
    evdev_mod: EvdevModule,
) -> None:
    state = device_runtime.state
    slot_code = int(getattr(evdev_mod.ecodes, "ABS_MT_SLOT", -1))
    tracking_id_code = int(getattr(evdev_mod.ecodes, "ABS_MT_TRACKING_ID", -1))
    if code == slot_code:
        state.passthrough_mt_slot = value
        return
    if code == tracking_id_code:
        if value < 0:
            state.passthrough_mt_active_slots.discard(state.passthrough_mt_slot)
        else:
            state.passthrough_mt_active_slots.add(state.passthrough_mt_slot)
        return

    mt_first = int(getattr(evdev_mod.ecodes, "ABS_MT_TOUCH_MAJOR", 0x30))
    mt_last = int(getattr(evdev_mod.ecodes, "ABS_MT_TOOL_Y", 0x3D))
    if mt_first <= code <= mt_last:
        return

    neutral = state.passthrough_abs_neutral_values.get(code, 0)
    held = state.held_output_abs.setdefault("passthrough", set())
    if value == neutral:
        held.discard(code)
    else:
        held.add(code)


def neutralize_passthrough_abs(
    device_runtime: ActionRuntime,
    *,
    evdev_mod: EvdevModule,
    uinput_writer: UInputWriter,
) -> None:
    state = device_runtime.state
    held_axes = set(state.held_output_abs.get("passthrough", set()))
    active_slots = set(state.passthrough_mt_active_slots)
    if not held_axes and not active_slots:
        return
    writer = uinput_writer(device_runtime.uinput)
    if writer is None:
        state.held_output_abs.setdefault("passthrough", set()).clear()
        state.passthrough_mt_active_slots.clear()
        return

    slot_code = int(getattr(evdev_mod.ecodes, "ABS_MT_SLOT", -1))
    tracking_id_code = int(getattr(evdev_mod.ecodes, "ABS_MT_TRACKING_ID", -1))
    for slot in sorted(active_slots):
        writer.write(evdev_mod.ecodes.EV_ABS, slot_code, slot)
        writer.write(evdev_mod.ecodes.EV_ABS, tracking_id_code, -1)
    if active_slots:
        writer.write(
            evdev_mod.ecodes.EV_ABS,
            slot_code,
            state.passthrough_mt_slot,
        )
    for code in sorted(held_axes):
        writer.write(
            evdev_mod.ecodes.EV_ABS,
            code,
            state.passthrough_abs_neutral_values.get(code, 0),
        )
    writer.syn()
    state.held_output_abs.setdefault("passthrough", set()).clear()
    state.passthrough_mt_active_slots.clear()


def ensure_abs_axis_released(
    device_runtime: ActionRuntime,
    axis_code: int,
    *,
    evdev_mod: EvdevModule,
    uinput_writer: UInputWriter,
    uinput_dev: object | None = None,
    bucket: str | None = None,
    release_value: int = 0,
) -> None:
    try:
        write_abs_axis(
            device_runtime,
            uinput_dev or device_runtime.gamepad_uinput,
            axis_code,
            release_value,
            evdev_mod=evdev_mod,
            uinput_writer=uinput_writer,
            bucket=bucket,
        )
    except OSError as exc:
        log.debug(
            "Failed to release gamepad ABS axis %s on %s: %s",
            axis_code,
            device_runtime.path,
            exc,
            exc_info=True,
        )
    except Exception:
        log.exception(
            "Unexpected failure releasing gamepad ABS axis %s on %s",
            axis_code,
            device_runtime.path,
        )


def ensure_key_released(
    device_runtime: ActionRuntime,
    code: int,
    uinput_dev: object | None,
    *,
    bucket: str | None = None,
) -> None:
    try:
        if uinput_dev:
            write_key(
                device_runtime,
                uinput_dev,
                code,
                0,
                evdev_mod=evdev,
                uinput_writer=identity_uinput_writer,
                bucket=bucket,
            )
    except OSError as exc:
        log.debug(
            "Failed to release output key %s on %s bucket=%s: %s",
            code,
            device_runtime.path,
            bucket_for_uinput(device_runtime, uinput_dev) or "unknown",
            exc,
            exc_info=True,
        )
    except Exception:
        log.exception(
            "Unexpected failure releasing output key %s on %s bucket=%s",
            code,
            device_runtime.path,
            bucket_for_uinput(device_runtime, uinput_dev) or "unknown",
        )


def emit_configured_mouse_move(device_runtime: ActionRuntime, action: MappingAction) -> None:
    emit_mouse_move(
        cast(WritableUInput | None, device_runtime.mouse_uinput),
        int(action.move_x),
        int(action.move_y),
        absolute=action.action_type == ActionType.MOUSE_MOVE_ABS,
    )


def _clear_held_output_bucket(
    device_runtime: ActionRuntime,
    bucket: str,
    *,
    held_keys: bool = True,
    held_abs: bool = True,
    key_refcounts: bool = True,
    abs_refcounts: bool = True,
) -> None:
    state = device_runtime.state
    if held_keys:
        state.held_output_keys.get(bucket, set()).clear()
    if held_abs:
        state.held_output_abs.get(bucket, set()).clear()
    if key_refcounts:
        for refcounts in (
            state.superkey_output_refcounts,
            state.analog_threshold_output_refcounts,
        ):
            if bucket in refcounts:
                refcounts[bucket].clear()
    if abs_refcounts:
        for refcounts in (
            state.superkey_abs_refcounts,
            state.analog_threshold_abs_refcounts,
        ):
            if bucket in refcounts:
                refcounts[bucket].clear()


def release_all_keys(
    device_runtime: ActionRuntime,
    *,
    evdev_mod: EvdevModule,
    uinput_writer: UInputWriter,
) -> None:
    devices: dict[str, object | None] = {
        "passthrough": device_runtime.uinput,
        "keyboard": device_runtime.keyboard_uinput,
        "mouse": device_runtime.mouse_uinput,
        "gamepad": device_runtime.gamepad_uinput,
    }
    for state in device_runtime.state.rapidfire_outputs.values():
        if state.kind == "axis" and state.bucket:
            devices.setdefault(state.bucket, state.uinput)
    for bucket in set(device_runtime.state.held_output_keys) | set(
        device_runtime.state.held_output_abs
    ):
        if bucket.startswith("gamepad:") and bucket not in devices:
            target = device_runtime.resolve_gamepad_output(
                bucket.removeprefix("gamepad:"),
                f"release tracked {bucket}",
            )
            devices[bucket] = getattr(target, "uinput", None) if target is not None else None
    for bucket, uinput_dev in devices.items():
        writer = uinput_writer(uinput_dev)
        if writer is None:
            _clear_held_output_bucket(device_runtime, bucket)
            continue
        held = sorted(device_runtime.state.held_output_keys.get(bucket, set()))
        if not held:
            continue
        try:
            for code in held:
                writer.write(evdev_mod.ecodes.EV_KEY, int(code), 0)
            writer.syn()
        except OSError as exc:
            log.debug(
                "Failed to release held output keys on %s bucket=%s keys=%s: %s",
                device_runtime.path,
                bucket,
                held,
                exc,
                exc_info=True,
            )
        except Exception:
            log.exception(
                "Unexpected failure releasing held output keys on %s bucket=%s keys=%s",
                device_runtime.path,
                bucket,
                held,
            )
        else:
            _clear_held_output_bucket(device_runtime, bucket, held_abs=False)

    for bucket, held_abs in list(device_runtime.state.held_output_abs.items()):
        if not bucket.startswith("gamepad") and not held_abs:
            continue
        uinput_dev = devices.get(bucket)
        writer = uinput_writer(uinput_dev)
        if writer is None:
            _clear_held_output_bucket(
                device_runtime,
                bucket,
                held_keys=False,
                key_refcounts=False,
            )
            continue
        try:
            axes = held_abs or {evdev_mod.ecodes.ABS_Z, evdev_mod.ecodes.ABS_RZ}
            for axis_code in sorted(axes):
                writer.write(evdev_mod.ecodes.EV_ABS, int(axis_code), 0)
            writer.syn()
        except OSError as exc:
            log.debug(
                "Failed to release gamepad ABS axes on %s bucket=%s: %s",
                device_runtime.path,
                bucket,
                exc,
                exc_info=True,
            )
        except Exception:
            log.exception(
                "Unexpected failure releasing gamepad ABS axes on %s bucket=%s",
                device_runtime.path,
                bucket,
            )
        else:
            _clear_held_output_bucket(
                device_runtime,
                bucket,
                held_keys=False,
                key_refcounts=False,
            )

    for task in list(device_runtime.state.rapidfire_tasks.values()):
        if not task.done():
            task.cancel()
    device_runtime.state.rapidfire_tasks.clear()
    device_runtime.state.rapidfire_outputs.clear()
    device_runtime.state.rapidfire_active.clear()
    device_runtime.state.tap_active.clear()
    device_runtime.state.held_source_keys.clear()
    device_runtime.state.combo_passthrough_held.clear()
    device_runtime.state.combo_recalled_bindings.clear()
    device_runtime.state.held_source_actions.clear()
