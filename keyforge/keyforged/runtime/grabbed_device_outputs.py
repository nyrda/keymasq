from typing import cast

import evdev

from keyforge.common.models import ActionType, MappingAction
from keyforge.keyforged.output_helpers import emit_mouse_move
from keyforge.keyforged.runtime.grabbed_device_types import (
    EvdevModule,
    GrabbedDeviceRuntime,
    InputEventLike,
    UInputWriter,
    WritableUInput,
)


def bucket_for_uinput(
    device_runtime: GrabbedDeviceRuntime, uinput_dev: object | None
) -> str | None:
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
    device_runtime: GrabbedDeviceRuntime, uinput_dev: object | None, code: int, value: int
) -> None:
    bucket = bucket_for_uinput(device_runtime, uinput_dev)
    if not bucket:
        return
    held = device_runtime.state.held_output_keys[bucket]
    if int(value) == 1:
        held.add(int(code))
    elif int(value) == 0:
        held.discard(int(code))


def write_key(
    device_runtime: GrabbedDeviceRuntime,
    uinput_dev: object | None,
    code: int,
    value: int,
    *,
    evdev_mod: EvdevModule,
    uinput_writer: UInputWriter,
) -> None:
    writer = uinput_writer(uinput_dev)
    if writer is None:
        return
    writer.write(evdev_mod.ecodes.EV_KEY, int(code), int(value))
    writer.syn()
    track_key_state(device_runtime, uinput_dev, int(code), int(value))


def track_superkey_output(
    device_runtime: GrabbedDeviceRuntime, action_type: str, code: int, value: int
) -> bool:
    bucket = (
        action_type if action_type in device_runtime.state.superkey_output_refcounts else None
    )
    if bucket is None:
        return True

    refcounts = device_runtime.state.superkey_output_refcounts[bucket]
    current = refcounts.get(int(code), 0)

    if int(value) == 1:
        refcounts[int(code)] = current + 1
        device_runtime.state.held_output_keys[bucket].add(int(code))
        return current == 0

    if int(value) == 0:
        if current <= 1:
            refcounts.pop(int(code), None)
            device_runtime.state.held_output_keys[bucket].discard(int(code))
            return current == 1

        refcounts[int(code)] = current - 1
        return False

    return True


def passthrough(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    *,
    evdev_mod: EvdevModule,
    uinput_writer: UInputWriter,
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
        )
        return
    uinput = device_runtime.uinput
    writer = uinput_writer(uinput)
    if writer is None:
        return
    writer.write(event.type, event.code, event.value)
    writer.syn()


def ensure_trigger_released(
    device_runtime: GrabbedDeviceRuntime,
    axis_code: int,
    *,
    evdev_mod: EvdevModule,
    uinput_writer: UInputWriter,
) -> None:
    try:
        gamepad_uinput = uinput_writer(device_runtime.gamepad_uinput)
        if gamepad_uinput is not None:
            gamepad_uinput.write(evdev_mod.ecodes.EV_ABS, axis_code, 0)
            gamepad_uinput.syn()
    except Exception:
        pass


def ensure_key_released(
    device_runtime: GrabbedDeviceRuntime, code: int, uinput_dev: object | None
) -> None:
    try:
        if uinput_dev:
            write_key(
                device_runtime,
                uinput_dev,
                code,
                0,
                evdev_mod=evdev,
                uinput_writer=_uinput_writer,
            )
    except Exception:
        pass


def emit_configured_mouse_move(
    device_runtime: GrabbedDeviceRuntime, action: MappingAction
) -> None:
    emit_mouse_move(
        cast(WritableUInput | None, device_runtime.mouse_uinput),
        int(action.move_x),
        int(action.move_y),
        absolute=action.action_type == ActionType.MOUSE_MOVE_ABS,
    )


def release_all_keys(
    device_runtime: GrabbedDeviceRuntime,
    *,
    evdev_mod: EvdevModule,
    uinput_writer: UInputWriter,
) -> None:
    devices = {
        "passthrough": device_runtime.uinput,
        "keyboard": device_runtime.keyboard_uinput,
        "mouse": device_runtime.mouse_uinput,
        "gamepad": device_runtime.gamepad_uinput,
    }
    for bucket, uinput_dev in devices.items():
        writer = uinput_writer(uinput_dev)
        if writer is None:
            continue
        held = sorted(device_runtime.state.held_output_keys.get(bucket, set()))
        if not held:
            continue
        try:
            for code in held:
                writer.write(evdev_mod.ecodes.EV_KEY, int(code), 0)
            writer.syn()
        except Exception:
            pass
        finally:
            device_runtime.state.held_output_keys[bucket].clear()
            if bucket in device_runtime.state.superkey_output_refcounts:
                device_runtime.state.superkey_output_refcounts[bucket].clear()

    gamepad_uinput = uinput_writer(device_runtime.gamepad_uinput)
    if gamepad_uinput is not None:
        try:
            gamepad_uinput.write(evdev_mod.ecodes.EV_ABS, evdev_mod.ecodes.ABS_Z, 0)
            gamepad_uinput.write(evdev_mod.ecodes.EV_ABS, evdev_mod.ecodes.ABS_RZ, 0)
            gamepad_uinput.syn()
        except Exception:
            pass

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


def _uinput_writer(device: object | None) -> WritableUInput | None:
    return cast(WritableUInput | None, device)
