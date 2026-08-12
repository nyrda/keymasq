import logging
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from hashlib import blake2b
from typing import Any, Final, Protocol, cast

from evdev.uinput import UInputError

from keymasq.common.virtual_devices import (
    DEFAULT_VIRTUAL_GAMEPADS,
    clamp_virtual_gamepad_count,
    virtual_gamepad_output_id,
)
from keymasq.keymasqd.permission_hints import (
    is_uinput_permission_error,
    uinput_permission_message,
)
from keymasq.keymasqd.runtime.adapters import (
    ClosableUInput,
    UInputWriter,
    WritableUInput,
)


@dataclass
class OutputRuntimeState:
    """Tracks the shared virtual output devices owned by the daemon."""

    device_count: int = 0
    keyboard_uinput: ClosableUInput | None = None
    mouse_uinput: ClosableUInput | None = None
    virtual_gamepad_uinputs: dict[str, ClosableUInput] = field(default_factory=dict)
    virtual_gamepad_count: int = DEFAULT_VIRTUAL_GAMEPADS

    @property
    def gamepad_uinput(self) -> ClosableUInput | None:
        return self.virtual_gamepad_uinputs.get("virtual-gamepad-1")

    @gamepad_uinput.setter
    def gamepad_uinput(self, value: ClosableUInput | None) -> None:
        if value is None:
            self.virtual_gamepad_uinputs.pop("virtual-gamepad-1", None)
        else:
            self.virtual_gamepad_uinputs["virtual-gamepad-1"] = value


TEST_UINPUT_ENV = "KEYMASQ_TEST_UINPUT"
TEST_UINPUT_PREFIX = "keymasq-test"
UINPUT_NAME_MAX_BYTES = 80
TEST_UINPUT_VENDOR = 0x4B46
TEST_UINPUT_PRODUCTS = {
    "keyboard": 0x1001,
    "mouse": 0x1002,
    "gamepad": 0x1003,
    "passthrough": 0x1004,
}


class _OutputState(Protocol):
    device_count: int
    keyboard_uinput: ClosableUInput | None
    mouse_uinput: ClosableUInput | None
    gamepad_uinput: ClosableUInput | None
    virtual_gamepad_uinputs: dict[str, ClosableUInput]
    virtual_gamepad_count: int


class _OutputManager(Protocol):
    output_state: _OutputState


class _AbsInfoFactory(Protocol):
    def __call__(
        self, value: int, min: int, max: int, fuzz: int, flat: int, resolution: int
    ) -> object: ...


class _UInputFactory(Protocol):
    def __call__(
        self,
        *,
        events: Mapping[int, Sequence[object]],
        name: str,
        vendor: int = ...,
        product: int = ...,
        version: int = ...,
        bustype: int = ...,
    ) -> ClosableUInput: ...


class _Ecodes(Protocol):
    EV_KEY: Final[int]
    EV_SYN: Final[int]
    EV_REL: Final[int]
    EV_ABS: Final[int]
    KEY_ESC: Final[int]
    KEY_1: Final[int]
    KEY_2: Final[int]
    KEY_3: Final[int]
    KEY_4: Final[int]
    KEY_5: Final[int]
    KEY_6: Final[int]
    KEY_7: Final[int]
    KEY_8: Final[int]
    KEY_9: Final[int]
    KEY_0: Final[int]
    KEY_MINUS: Final[int]
    KEY_EQUAL: Final[int]
    KEY_BACKSPACE: Final[int]
    KEY_TAB: Final[int]
    KEY_Q: Final[int]
    KEY_W: Final[int]
    KEY_E: Final[int]
    KEY_R: Final[int]
    KEY_T: Final[int]
    KEY_Y: Final[int]
    KEY_U: Final[int]
    KEY_I: Final[int]
    KEY_O: Final[int]
    KEY_P: Final[int]
    KEY_LEFTBRACE: Final[int]
    KEY_RIGHTBRACE: Final[int]
    KEY_ENTER: Final[int]
    KEY_LEFTCTRL: Final[int]
    KEY_A: Final[int]
    KEY_S: Final[int]
    KEY_D: Final[int]
    KEY_F: Final[int]
    KEY_G: Final[int]
    KEY_H: Final[int]
    KEY_J: Final[int]
    KEY_K: Final[int]
    KEY_L: Final[int]
    KEY_SEMICOLON: Final[int]
    KEY_APOSTROPHE: Final[int]
    KEY_GRAVE: Final[int]
    KEY_LEFTSHIFT: Final[int]
    KEY_BACKSLASH: Final[int]
    KEY_102ND: Final[int]
    KEY_Z: Final[int]
    KEY_X: Final[int]
    KEY_C: Final[int]
    KEY_V: Final[int]
    KEY_B: Final[int]
    KEY_N: Final[int]
    KEY_M: Final[int]
    KEY_COMMA: Final[int]
    KEY_DOT: Final[int]
    KEY_SLASH: Final[int]
    KEY_RIGHTSHIFT: Final[int]
    KEY_LEFTALT: Final[int]
    KEY_LEFTMETA: Final[int]
    KEY_SPACE: Final[int]
    KEY_CAPSLOCK: Final[int]
    KEY_F1: Final[int]
    KEY_F2: Final[int]
    KEY_F3: Final[int]
    KEY_F4: Final[int]
    KEY_F5: Final[int]
    KEY_F6: Final[int]
    KEY_F7: Final[int]
    KEY_F8: Final[int]
    KEY_F9: Final[int]
    KEY_F10: Final[int]
    KEY_F11: Final[int]
    KEY_F12: Final[int]
    KEY_F13: Final[int]
    KEY_F14: Final[int]
    KEY_F15: Final[int]
    KEY_F16: Final[int]
    KEY_F17: Final[int]
    KEY_F18: Final[int]
    KEY_F19: Final[int]
    KEY_F20: Final[int]
    KEY_F21: Final[int]
    KEY_F22: Final[int]
    KEY_F23: Final[int]
    KEY_F24: Final[int]
    KEY_RIGHTCTRL: Final[int]
    KEY_RIGHTALT: Final[int]
    KEY_RIGHTMETA: Final[int]
    KEY_MENU: Final[int]
    KEY_SYSRQ: Final[int]
    KEY_SCROLLLOCK: Final[int]
    KEY_PAUSE: Final[int]
    KEY_HOME: Final[int]
    KEY_UP: Final[int]
    KEY_PAGEUP: Final[int]
    KEY_LEFT: Final[int]
    KEY_RIGHT: Final[int]
    KEY_END: Final[int]
    KEY_DOWN: Final[int]
    KEY_PAGEDOWN: Final[int]
    KEY_INSERT: Final[int]
    KEY_DELETE: Final[int]
    KEY_MUTE: Final[int]
    KEY_VOLUMEDOWN: Final[int]
    KEY_VOLUMEUP: Final[int]
    KEY_MICMUTE: Final[int]
    KEY_BRIGHTNESSDOWN: Final[int]
    KEY_BRIGHTNESSUP: Final[int]
    KEY_PREVIOUSSONG: Final[int]
    KEY_PLAYPAUSE: Final[int]
    KEY_NEXTSONG: Final[int]
    KEY_STOP: Final[int]
    KEY_PLAY: Final[int]
    KEY_NUMLOCK: Final[int]
    KEY_KPSLASH: Final[int]
    KEY_KPASTERISK: Final[int]
    KEY_KPMINUS: Final[int]
    KEY_KP7: Final[int]
    KEY_KP8: Final[int]
    KEY_KP9: Final[int]
    KEY_KPPLUS: Final[int]
    KEY_KP4: Final[int]
    KEY_KP5: Final[int]
    KEY_KP6: Final[int]
    KEY_KP1: Final[int]
    KEY_KP2: Final[int]
    KEY_KP3: Final[int]
    KEY_KPENTER: Final[int]
    KEY_KP0: Final[int]
    KEY_KPDOT: Final[int]
    BTN_LEFT: Final[int]
    BTN_RIGHT: Final[int]
    BTN_MIDDLE: Final[int]
    BTN_SIDE: Final[int]
    BTN_EXTRA: Final[int]
    BTN_FORWARD: Final[int]
    BTN_BACK: Final[int]
    REL_X: Final[int]
    REL_Y: Final[int]
    REL_WHEEL: Final[int]
    REL_HWHEEL: Final[int]
    BTN_SOUTH: Final[int]
    BTN_EAST: Final[int]
    BTN_NORTH: Final[int]
    BTN_WEST: Final[int]
    BTN_TL: Final[int]
    BTN_TR: Final[int]
    BTN_TL2: Final[int]
    BTN_TR2: Final[int]
    BTN_SELECT: Final[int]
    BTN_START: Final[int]
    BTN_MODE: Final[int]
    BTN_THUMBL: Final[int]
    BTN_THUMBR: Final[int]
    BTN_DPAD_UP: Final[int]
    BTN_DPAD_DOWN: Final[int]
    BTN_DPAD_LEFT: Final[int]
    BTN_DPAD_RIGHT: Final[int]
    ABS_X: Final[int]
    ABS_Y: Final[int]
    ABS_RX: Final[int]
    ABS_RY: Final[int]
    ABS_Z: Final[int]
    ABS_RZ: Final[int]
    ABS_HAT0X: Final[int]
    ABS_HAT0Y: Final[int]


class _EvdevModule(Protocol):
    ecodes: Final[_Ecodes]
    UInput: Final[_UInputFactory]
    AbsInfo: Final[_AbsInfoFactory]


def create_uinput_with_permission_hint[T](context: str, create: Callable[[], T]) -> T:
    try:
        return create()
    except (OSError, UInputError) as exc:
        if is_uinput_permission_error(exc):
            raise PermissionError(
                uinput_permission_message(f"Failed to create {context} uinput device: {exc}")
            ) from exc
        raise


def _test_uinput_enabled() -> bool:
    value = str(os.environ.get(TEST_UINPUT_ENV, "")).strip().lower()
    return value not in {"", "0", "false", "no"}


def bounded_uinput_name(
    prefix: str,
    identity: str,
    *,
    max_bytes: int = UINPUT_NAME_MAX_BYTES,
) -> str:
    identity = identity.replace("/dev/input/by-id/", "")
    identity = identity.replace("/dev/input/", "")
    name = f"{prefix}-{identity}" if identity else prefix
    if len(name.encode("utf-8")) <= max_bytes:
        return name

    ascii_identity = re.sub(r"[^A-Za-z0-9_.:-]+", "-", identity).strip("-")
    digest = blake2b(identity.encode("utf-8"), digest_size=4).hexdigest()
    suffix = f"-{digest}"
    budget = max_bytes - len(prefix.encode("utf-8")) - len(b"-") - len(suffix)
    if budget <= 0:
        return f"{prefix[: max(1, max_bytes - len(suffix))]}{suffix}"

    kept = ascii_identity.encode("utf-8")[:budget].decode("utf-8", "ignore").strip("-")
    if not kept:
        kept = "device"
    return f"{prefix}-{kept}{suffix}"


def bounded_passthrough_name(
    name: str,
    *,
    max_bytes: int = UINPUT_NAME_MAX_BYTES,
) -> str:
    normalized = str(name or "").strip() or "Keymasq Passthrough"
    if len(normalized.encode("utf-8")) <= max_bytes:
        return normalized

    ascii_name = re.sub(r"[^A-Za-z0-9_.:-]+", "-", normalized).strip("-")
    digest = blake2b(normalized.encode("utf-8"), digest_size=4).hexdigest()
    suffix = f"-{digest}"
    budget = max_bytes - len(suffix.encode("utf-8"))
    if budget <= 0:
        return suffix[-max_bytes:]

    kept = ascii_name.encode("utf-8")[:budget].decode("utf-8", "ignore").strip("-")
    if not kept:
        kept = "device"
    return f"{kept}{suffix}"


def uinput_identity(
    normal_name: str,
    kind: str,
    *,
    test_name: str | None = None,
) -> tuple[str, int | None, int | None]:
    if not _test_uinput_enabled():
        if normal_name.startswith("keymasq-"):
            return bounded_uinput_name("keymasq", normal_name.removeprefix("keymasq-")), None, None
        return bounded_passthrough_name(normal_name), None, None
    return (
        bounded_uinput_name(TEST_UINPUT_PREFIX, test_name or kind),
        TEST_UINPUT_VENDOR,
        TEST_UINPUT_PRODUCTS[kind],
    )


def gamepad_caps(evdev_mod: _EvdevModule) -> dict[int, Sequence[object]]:
    return {
        evdev_mod.ecodes.EV_KEY: [
            evdev_mod.ecodes.BTN_SOUTH,
            evdev_mod.ecodes.BTN_EAST,
            evdev_mod.ecodes.BTN_NORTH,
            evdev_mod.ecodes.BTN_WEST,
            evdev_mod.ecodes.BTN_TL,
            evdev_mod.ecodes.BTN_TR,
            evdev_mod.ecodes.BTN_TL2,
            evdev_mod.ecodes.BTN_TR2,
            evdev_mod.ecodes.BTN_SELECT,
            evdev_mod.ecodes.BTN_START,
            evdev_mod.ecodes.BTN_MODE,
            evdev_mod.ecodes.BTN_THUMBL,
            evdev_mod.ecodes.BTN_THUMBR,
            evdev_mod.ecodes.BTN_DPAD_UP,
            evdev_mod.ecodes.BTN_DPAD_DOWN,
            evdev_mod.ecodes.BTN_DPAD_LEFT,
            evdev_mod.ecodes.BTN_DPAD_RIGHT,
        ],
        evdev_mod.ecodes.EV_ABS: [
            (evdev_mod.ecodes.ABS_X, evdev_mod.AbsInfo(0, -32768, 32767, 16, 128, 0)),
            (evdev_mod.ecodes.ABS_Y, evdev_mod.AbsInfo(0, -32768, 32767, 16, 128, 0)),
            (evdev_mod.ecodes.ABS_RX, evdev_mod.AbsInfo(0, -32768, 32767, 16, 128, 0)),
            (evdev_mod.ecodes.ABS_RY, evdev_mod.AbsInfo(0, -32768, 32767, 16, 128, 0)),
            (evdev_mod.ecodes.ABS_Z, evdev_mod.AbsInfo(0, 0, 255, 0, 0, 0)),
            (evdev_mod.ecodes.ABS_RZ, evdev_mod.AbsInfo(0, 0, 255, 0, 0, 0)),
            (evdev_mod.ecodes.ABS_HAT0X, evdev_mod.AbsInfo(0, -1, 1, 0, 0, 0)),
            (evdev_mod.ecodes.ABS_HAT0Y, evdev_mod.AbsInfo(0, -1, 1, 0, 0, 0)),
        ],
        evdev_mod.ecodes.EV_SYN: [],
    }


def _initialize_gamepad_axes(
    uinput_dev: WritableUInput | None,
    evdev_mod: _EvdevModule,
) -> None:
    if uinput_dev is None:
        return
    for axis in (
        evdev_mod.ecodes.ABS_X,
        evdev_mod.ecodes.ABS_Y,
        evdev_mod.ecodes.ABS_RX,
        evdev_mod.ecodes.ABS_RY,
        evdev_mod.ecodes.ABS_Z,
        evdev_mod.ecodes.ABS_RZ,
        evdev_mod.ecodes.ABS_HAT0X,
        evdev_mod.ecodes.ABS_HAT0Y,
    ):
        uinput_dev.write(evdev_mod.ecodes.EV_ABS, axis, 0)
    uinput_dev.syn()


def create_virtual_gamepad(
    index: int,
    evdev_mod: _EvdevModule,
    uinput_writer: UInputWriter,
) -> ClosableUInput:
    normal_name = "keymasq-gamepad" if index == 1 else f"keymasq-gamepad-{index}"
    gamepad_name, gamepad_vendor, gamepad_product = uinput_identity(
        normal_name,
        "gamepad",
        test_name="gamepad" if index == 1 else f"gamepad-{index}",
    )
    uinput_dev = create_uinput_with_permission_hint(
        "virtual gamepad",
        lambda: evdev_mod.UInput(
            events=cast(dict[int, Sequence[int]], gamepad_caps(evdev_mod)),
            name=gamepad_name,
            vendor=0x045E if gamepad_vendor is None else gamepad_vendor,
            product=0x028E if gamepad_product is None else gamepad_product,
            version=0x0110,
            bustype=0x0003,
        ),
    )
    _initialize_gamepad_axes(uinput_writer(uinput_dev), evdev_mod)
    return uinput_dev


def configure_virtual_gamepads(
    manager: _OutputManager,
    count: int,
    *,
    evdev_mod: _EvdevModule,
    log: logging.Logger,
    uinput_writer: UInputWriter,
) -> int:
    count = clamp_virtual_gamepad_count(count)
    output_state = cast(Any, manager.output_state)
    if not hasattr(output_state, "virtual_gamepad_uinputs"):
        output_state.virtual_gamepad_uinputs = {}
    current = cast(dict[str, ClosableUInput], output_state.virtual_gamepad_uinputs)
    desired_ids = {virtual_gamepad_output_id(index) for index in range(1, count + 1)}

    for output_id in sorted(set(current) - desired_ids):
        uinput_dev = current.pop(output_id)
        try:
            uinput_dev.close()
        except OSError as exc:
            log.warning("Failed to close virtual gamepad %s: %s", output_id, exc)
        except Exception:
            log.exception("Unexpected failure closing virtual gamepad %s", output_id)

    for index in range(1, count + 1):
        output_id = virtual_gamepad_output_id(index)
        if output_id in current:
            continue
        current[output_id] = create_virtual_gamepad(
            index,
            evdev_mod,
            uinput_writer,
        )
        log.info("Created virtual gamepad %s", output_id)

    output_state.virtual_gamepad_count = count
    return count


def create_global_uinputs(
    manager: _OutputManager,
    *,
    evdev_mod: _EvdevModule,
    log: logging.Logger,
    uinput_writer: UInputWriter,
) -> None:
    if manager.output_state.device_count == 0:
        log.info("Creating global output uinput devices")

        keyboard_caps = {
            evdev_mod.ecodes.EV_KEY: [
                evdev_mod.ecodes.KEY_ESC,
                evdev_mod.ecodes.KEY_1,
                evdev_mod.ecodes.KEY_2,
                evdev_mod.ecodes.KEY_3,
                evdev_mod.ecodes.KEY_4,
                evdev_mod.ecodes.KEY_5,
                evdev_mod.ecodes.KEY_6,
                evdev_mod.ecodes.KEY_7,
                evdev_mod.ecodes.KEY_8,
                evdev_mod.ecodes.KEY_9,
                evdev_mod.ecodes.KEY_0,
                evdev_mod.ecodes.KEY_MINUS,
                evdev_mod.ecodes.KEY_EQUAL,
                evdev_mod.ecodes.KEY_BACKSPACE,
                evdev_mod.ecodes.KEY_TAB,
                evdev_mod.ecodes.KEY_Q,
                evdev_mod.ecodes.KEY_W,
                evdev_mod.ecodes.KEY_E,
                evdev_mod.ecodes.KEY_R,
                evdev_mod.ecodes.KEY_T,
                evdev_mod.ecodes.KEY_Y,
                evdev_mod.ecodes.KEY_U,
                evdev_mod.ecodes.KEY_I,
                evdev_mod.ecodes.KEY_O,
                evdev_mod.ecodes.KEY_P,
                evdev_mod.ecodes.KEY_LEFTBRACE,
                evdev_mod.ecodes.KEY_RIGHTBRACE,
                evdev_mod.ecodes.KEY_ENTER,
                evdev_mod.ecodes.KEY_LEFTCTRL,
                evdev_mod.ecodes.KEY_A,
                evdev_mod.ecodes.KEY_S,
                evdev_mod.ecodes.KEY_D,
                evdev_mod.ecodes.KEY_F,
                evdev_mod.ecodes.KEY_G,
                evdev_mod.ecodes.KEY_H,
                evdev_mod.ecodes.KEY_J,
                evdev_mod.ecodes.KEY_K,
                evdev_mod.ecodes.KEY_L,
                evdev_mod.ecodes.KEY_SEMICOLON,
                evdev_mod.ecodes.KEY_APOSTROPHE,
                evdev_mod.ecodes.KEY_GRAVE,
                evdev_mod.ecodes.KEY_LEFTSHIFT,
                evdev_mod.ecodes.KEY_BACKSLASH,
                evdev_mod.ecodes.KEY_102ND,
                evdev_mod.ecodes.KEY_Z,
                evdev_mod.ecodes.KEY_X,
                evdev_mod.ecodes.KEY_C,
                evdev_mod.ecodes.KEY_V,
                evdev_mod.ecodes.KEY_B,
                evdev_mod.ecodes.KEY_N,
                evdev_mod.ecodes.KEY_M,
                evdev_mod.ecodes.KEY_COMMA,
                evdev_mod.ecodes.KEY_DOT,
                evdev_mod.ecodes.KEY_SLASH,
                evdev_mod.ecodes.KEY_RIGHTSHIFT,
                evdev_mod.ecodes.KEY_LEFTALT,
                evdev_mod.ecodes.KEY_LEFTMETA,
                evdev_mod.ecodes.KEY_SPACE,
                evdev_mod.ecodes.KEY_CAPSLOCK,
                evdev_mod.ecodes.KEY_F1,
                evdev_mod.ecodes.KEY_F2,
                evdev_mod.ecodes.KEY_F3,
                evdev_mod.ecodes.KEY_F4,
                evdev_mod.ecodes.KEY_F5,
                evdev_mod.ecodes.KEY_F6,
                evdev_mod.ecodes.KEY_F7,
                evdev_mod.ecodes.KEY_F8,
                evdev_mod.ecodes.KEY_F9,
                evdev_mod.ecodes.KEY_F10,
                evdev_mod.ecodes.KEY_F11,
                evdev_mod.ecodes.KEY_F12,
                evdev_mod.ecodes.KEY_F13,
                evdev_mod.ecodes.KEY_F14,
                evdev_mod.ecodes.KEY_F15,
                evdev_mod.ecodes.KEY_F16,
                evdev_mod.ecodes.KEY_F17,
                evdev_mod.ecodes.KEY_F18,
                evdev_mod.ecodes.KEY_F19,
                evdev_mod.ecodes.KEY_F20,
                evdev_mod.ecodes.KEY_F21,
                evdev_mod.ecodes.KEY_F22,
                evdev_mod.ecodes.KEY_F23,
                evdev_mod.ecodes.KEY_F24,
                evdev_mod.ecodes.KEY_RIGHTCTRL,
                evdev_mod.ecodes.KEY_RIGHTALT,
                evdev_mod.ecodes.KEY_RIGHTMETA,
                evdev_mod.ecodes.KEY_MENU,
                evdev_mod.ecodes.KEY_SYSRQ,
                evdev_mod.ecodes.KEY_SCROLLLOCK,
                evdev_mod.ecodes.KEY_PAUSE,
                evdev_mod.ecodes.KEY_HOME,
                evdev_mod.ecodes.KEY_UP,
                evdev_mod.ecodes.KEY_PAGEUP,
                evdev_mod.ecodes.KEY_LEFT,
                evdev_mod.ecodes.KEY_RIGHT,
                evdev_mod.ecodes.KEY_END,
                evdev_mod.ecodes.KEY_DOWN,
                evdev_mod.ecodes.KEY_PAGEDOWN,
                evdev_mod.ecodes.KEY_INSERT,
                evdev_mod.ecodes.KEY_DELETE,
                evdev_mod.ecodes.KEY_MUTE,
                evdev_mod.ecodes.KEY_VOLUMEDOWN,
                evdev_mod.ecodes.KEY_VOLUMEUP,
                evdev_mod.ecodes.KEY_MICMUTE,
                evdev_mod.ecodes.KEY_BRIGHTNESSDOWN,
                evdev_mod.ecodes.KEY_BRIGHTNESSUP,
                evdev_mod.ecodes.KEY_PREVIOUSSONG,
                evdev_mod.ecodes.KEY_PLAYPAUSE,
                evdev_mod.ecodes.KEY_NEXTSONG,
                evdev_mod.ecodes.KEY_STOP,
                evdev_mod.ecodes.KEY_PLAY,
                evdev_mod.ecodes.KEY_NUMLOCK,
                evdev_mod.ecodes.KEY_KPSLASH,
                evdev_mod.ecodes.KEY_KPASTERISK,
                evdev_mod.ecodes.KEY_KPMINUS,
                evdev_mod.ecodes.KEY_KP7,
                evdev_mod.ecodes.KEY_KP8,
                evdev_mod.ecodes.KEY_KP9,
                evdev_mod.ecodes.KEY_KPPLUS,
                evdev_mod.ecodes.KEY_KP4,
                evdev_mod.ecodes.KEY_KP5,
                evdev_mod.ecodes.KEY_KP6,
                evdev_mod.ecodes.KEY_KP1,
                evdev_mod.ecodes.KEY_KP2,
                evdev_mod.ecodes.KEY_KP3,
                evdev_mod.ecodes.KEY_KPENTER,
                evdev_mod.ecodes.KEY_KP0,
                evdev_mod.ecodes.KEY_KPDOT,
            ],
            evdev_mod.ecodes.EV_SYN: [],
        }
        keyboard_name, keyboard_vendor, keyboard_product = uinput_identity(
            "keymasq-keyboard",
            "keyboard",
        )
        if keyboard_vendor is None or keyboard_product is None:
            manager.output_state.keyboard_uinput = create_uinput_with_permission_hint(
                "keyboard",
                lambda: evdev_mod.UInput(
                    events=cast(dict[int, Sequence[int]], keyboard_caps),
                    name=keyboard_name,
                ),
            )
        else:
            manager.output_state.keyboard_uinput = create_uinput_with_permission_hint(
                "keyboard",
                lambda: evdev_mod.UInput(
                    events=cast(dict[int, Sequence[int]], keyboard_caps),
                    name=keyboard_name,
                    vendor=keyboard_vendor,
                    product=keyboard_product,
                ),
            )

        mouse_caps = {
            evdev_mod.ecodes.EV_KEY: [
                evdev_mod.ecodes.BTN_LEFT,
                evdev_mod.ecodes.BTN_RIGHT,
                evdev_mod.ecodes.BTN_MIDDLE,
                evdev_mod.ecodes.BTN_SIDE,
                evdev_mod.ecodes.BTN_EXTRA,
                evdev_mod.ecodes.BTN_FORWARD,
                evdev_mod.ecodes.BTN_BACK,
            ],
            evdev_mod.ecodes.EV_REL: [
                evdev_mod.ecodes.REL_X,
                evdev_mod.ecodes.REL_Y,
                evdev_mod.ecodes.REL_WHEEL,
                evdev_mod.ecodes.REL_HWHEEL,
            ],
            evdev_mod.ecodes.EV_SYN: [],
        }
        mouse_rel_caps = list(mouse_caps[evdev_mod.ecodes.EV_REL])
        for high_res_code in (
            getattr(evdev_mod.ecodes, "REL_WHEEL_HI_RES", None),
            getattr(evdev_mod.ecodes, "REL_HWHEEL_HI_RES", None),
        ):
            if high_res_code is not None and high_res_code not in mouse_rel_caps:
                mouse_rel_caps.append(high_res_code)
        mouse_caps[evdev_mod.ecodes.EV_REL] = mouse_rel_caps
        mouse_name, mouse_vendor, mouse_product = uinput_identity(
            "keymasq-mouse",
            "mouse",
        )
        if mouse_vendor is None or mouse_product is None:
            manager.output_state.mouse_uinput = create_uinput_with_permission_hint(
                "mouse",
                lambda: evdev_mod.UInput(
                    events=cast(dict[int, Sequence[int]], mouse_caps),
                    name=mouse_name,
                ),
            )
        else:
            manager.output_state.mouse_uinput = create_uinput_with_permission_hint(
                "mouse",
                lambda: evdev_mod.UInput(
                    events=cast(dict[int, Sequence[int]], mouse_caps),
                    name=mouse_name,
                    vendor=mouse_vendor,
                    product=mouse_product,
                ),
            )

        configure_virtual_gamepads(
            manager,
            getattr(manager.output_state, "virtual_gamepad_count", 1),
            evdev_mod=evdev_mod,
            log=log,
            uinput_writer=uinput_writer,
        )

    manager.output_state.device_count += 1


def destroy_global_uinputs(manager: _OutputManager, *, log: logging.Logger) -> None:
    manager.output_state.device_count = max(0, manager.output_state.device_count - 1)

    if manager.output_state.device_count == 0:
        log.info("Destroying global output uinput devices")

        for uinput_dev in [
            manager.output_state.keyboard_uinput,
            manager.output_state.mouse_uinput,
            *manager.output_state.virtual_gamepad_uinputs.values(),
        ]:
            if uinput_dev:
                try:
                    uinput_dev.close()
                except OSError as exc:
                    log.warning("Failed to close global uinput device: %s", exc)
                except Exception:
                    log.exception("Unexpected failure closing global uinput device")

        manager.output_state.keyboard_uinput = None
        manager.output_state.mouse_uinput = None
        manager.output_state.virtual_gamepad_uinputs.clear()
