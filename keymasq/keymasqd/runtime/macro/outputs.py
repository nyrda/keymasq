from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from keymasq.keymasqd.runtime.grabbed_device.outputs import syn_if_passthrough_frame_closed
from keymasq.keymasqd.runtime.macro.state import MacroRuntimeDeps

type MacroManager = Any


@dataclass(frozen=True)
class MacroOutput:
    raw_uinput: object
    writer: Any
    output_class: str


def gamepad_abs_cleanup_codes(evdev_mod: Any) -> frozenset[int]:
    return frozenset(
        int(code)
        for code in (
            evdev_mod.ecodes.ABS_X,
            evdev_mod.ecodes.ABS_Y,
            evdev_mod.ecodes.ABS_RX,
            evdev_mod.ecodes.ABS_RY,
            evdev_mod.ecodes.ABS_Z,
            evdev_mod.ecodes.ABS_RZ,
            evdev_mod.ecodes.ABS_HAT0X,
            evdev_mod.ecodes.ABS_HAT0Y,
        )
    )


def gamepad_output_class(device_class: str) -> str | None:
    if device_class == "gamepad":
        return "virtual-gamepad-1"
    if device_class.startswith("gamepad:"):
        output_id = device_class.removeprefix("gamepad:").strip()
        return output_id or None
    return None


def resolve_macro_output(
    manager: MacroManager,
    *,
    event_type: int,
    device_type: str,
    output_id: str | None,
    macro_name: str,
    deps: MacroRuntimeDeps,
) -> MacroOutput | None:
    evdev_mod = deps.evdev_mod
    if device_type == "keyboard":
        raw_uinput = manager.output_state.keyboard_uinput
        output_class = "keyboard"
    elif device_type == "mouse":
        raw_uinput = manager.output_state.mouse_uinput
        output_class = "mouse"
    elif device_type == "gamepad":
        target = manager.resolve_gamepad_output(
            output_id,
            context=f"macro {macro_name or '<unnamed>'}",
        )
        if target is None:
            return None
        raw_uinput = target.uinput
        output_class = target.bucket
    elif event_type == evdev_mod.ecodes.EV_KEY:
        raw_uinput = manager.output_state.keyboard_uinput
        output_class = "keyboard"
    elif event_type in (evdev_mod.ecodes.EV_REL, evdev_mod.ecodes.EV_ABS):
        raw_uinput = manager.output_state.mouse_uinput
        output_class = "mouse"
    else:
        return None

    if not raw_uinput:
        return None
    writer = deps.uinput_writer(raw_uinput)
    if writer is None:
        return None
    return MacroOutput(raw_uinput, writer, output_class)


def emit_macro_event(
    manager: MacroManager,
    *,
    instance_id: int,
    event_type: int,
    event_code: int,
    event_value: int,
    device_type: str,
    output_id: str | None,
    macro_name: str,
    deps: MacroRuntimeDeps,
) -> bool:
    output = resolve_macro_output(
        manager,
        event_type=event_type,
        device_type=device_type,
        output_id=output_id,
        macro_name=macro_name,
        deps=deps,
    )
    if output is None:
        return False

    output.writer.write(event_type, event_code, event_value)
    syn_if_passthrough_frame_closed(output.raw_uinput, output.writer)
    if event_type == deps.evdev_mod.ecodes.EV_KEY:
        if event_value == 1:
            track_macro_key_press(manager, instance_id, output.output_class, event_code)
        elif event_value == 0:
            track_macro_key_release(manager, instance_id, output.output_class, event_code)
    elif event_type == deps.evdev_mod.ecodes.EV_ABS:
        track_macro_abs_value(
            manager,
            instance_id,
            output.output_class,
            event_code,
            event_value,
            deps=deps,
        )
    return True


def emit_relative_mouse_move(
    manager: MacroManager,
    x: int,
    y: int,
    *,
    deps: MacroRuntimeDeps,
) -> bool:
    raw_uinput = manager.output_state.mouse_uinput
    writer = deps.uinput_writer(raw_uinput) if raw_uinput else None
    if writer is None:
        return False
    writer.write(deps.evdev_mod.ecodes.EV_REL, deps.evdev_mod.ecodes.REL_X, x)
    writer.write(deps.evdev_mod.ecodes.EV_REL, deps.evdev_mod.ecodes.REL_Y, y)
    syn_if_passthrough_frame_closed(raw_uinput, writer)
    return True


def track_macro_key_press(
    manager: MacroManager,
    instance_id: int,
    device_class: str,
    code: int,
) -> None:
    key = (device_class, int(code))
    held = manager.macro_state.instance_held.setdefault(instance_id, set())
    if key in held:
        return
    held.add(key)
    held_refcount = manager.macro_state.held_refcount
    held_refcount[key] = held_refcount.get(key, 0) + 1


def track_macro_key_release(
    manager: MacroManager,
    instance_id: int,
    device_class: str,
    code: int,
) -> None:
    key = (device_class, int(code))
    held = manager.macro_state.instance_held.get(instance_id)
    if not held or key not in held:
        return
    held.remove(key)
    held_refcount = manager.macro_state.held_refcount
    count = held_refcount.get(key, 0)
    if count <= 1:
        held_refcount.pop(key, None)
    else:
        held_refcount[key] = count - 1


def track_macro_abs_value(
    manager: MacroManager,
    instance_id: int,
    device_class: str,
    code: int,
    value: int,
    *,
    deps: MacroRuntimeDeps,
) -> None:
    if not gamepad_output_class(device_class):
        return
    if int(code) not in gamepad_abs_cleanup_codes(deps.evdev_mod):
        return

    key = (device_class, int(code))
    held = manager.macro_state.instance_held_abs.setdefault(instance_id, set())
    held_refcount = manager.macro_state.held_abs_refcount
    if int(value) == 0:
        if key not in held:
            return
        held.remove(key)
        count = held_refcount.get(key, 0)
        if count <= 1:
            held_refcount.pop(key, None)
        else:
            held_refcount[key] = count - 1
        return

    if key in held:
        return
    held.add(key)
    held_refcount[key] = held_refcount.get(key, 0) + 1


def release_macro_held_for_instance(
    manager: MacroManager,
    instance_id: int,
    *,
    deps: MacroRuntimeDeps,
    sync_fn: Callable[[object, Any], None] = syn_if_passthrough_frame_closed,
) -> None:
    held = manager.macro_state.instance_held.pop(instance_id, set())
    held_abs = manager.macro_state.instance_held_abs.pop(instance_id, set())
    if not held and not held_abs:
        return

    uinputs = {
        "keyboard": (
            manager.output_state.keyboard_uinput,
            deps.uinput_writer(manager.output_state.keyboard_uinput),
        ),
        "mouse": (
            manager.output_state.mouse_uinput,
            deps.uinput_writer(manager.output_state.mouse_uinput),
        ),
        "gamepad": (
            manager.output_state.gamepad_uinput,
            deps.uinput_writer(manager.output_state.gamepad_uinput),
        ),
    }
    for output_id, uinput_dev in getattr(
        manager.output_state, "virtual_gamepad_uinputs", {}
    ).items():
        uinputs[f"gamepad:{output_id}"] = (uinput_dev, deps.uinput_writer(uinput_dev))
    for key in [*held, *held_abs]:
        device_class = str(key[0])
        output_id = gamepad_output_class(device_class)
        if output_id is None or device_class in uinputs:
            continue
        target = manager.resolve_gamepad_output(output_id, context="macro cleanup")
        raw_uinput = target.uinput if target is not None else None
        uinputs[device_class] = (raw_uinput, deps.uinput_writer(raw_uinput))

    synced: set[str] = set()
    held_refcount = manager.macro_state.held_refcount
    held_abs_refcount = manager.macro_state.held_abs_refcount

    for device_class, code in held:
        key = (device_class, code)
        count = held_refcount.get(key, 0)
        if count > 1:
            held_refcount[key] = count - 1
            continue
        held_refcount.pop(key, None)
        uinput_pair = uinputs.get(device_class)
        if not uinput_pair or not uinput_pair[1]:
            continue
        try:
            uinput_pair[1].write(deps.evdev_mod.ecodes.EV_KEY, int(code), 0)
            synced.add(device_class)
        except OSError:
            deps.log.debug("Failed to release macro-held output key", exc_info=True)
        except Exception:
            deps.log.exception(
                "Unexpected failure releasing macro-held output key device_class=%s code=%s",
                device_class,
                code,
            )

    for device_class, code in held_abs:
        key = (device_class, code)
        count = held_abs_refcount.get(key, 0)
        if count > 1:
            held_abs_refcount[key] = count - 1
            continue
        held_abs_refcount.pop(key, None)
        uinput_pair = uinputs.get(device_class)
        if not uinput_pair or not uinput_pair[1]:
            continue
        try:
            uinput_pair[1].write(deps.evdev_mod.ecodes.EV_ABS, int(code), 0)
            synced.add(device_class)
        except OSError:
            deps.log.debug("Failed to release macro-held ABS output", exc_info=True)
        except Exception:
            deps.log.exception(
                "Unexpected failure releasing macro-held ABS output device_class=%s code=%s",
                device_class,
                code,
            )

    for device_class in synced:
        uinput_pair = uinputs.get(device_class)
        if not uinput_pair or not uinput_pair[1]:
            continue
        try:
            sync_fn(uinput_pair[0], uinput_pair[1])
        except OSError:
            deps.log.debug("Failed to synchronize macro cleanup outputs", exc_info=True)
        except Exception:
            deps.log.exception(
                "Unexpected failure synchronizing macro cleanup outputs device_class=%s",
                device_class,
            )
