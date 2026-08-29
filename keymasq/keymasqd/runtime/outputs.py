import inspect
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
        max_effects: int = ...,
    ) -> ClosableUInput: ...


class _Ecodes(Protocol):
    EV_KEY: Final[int]
    EV_SYN: Final[int]
    EV_REL: Final[int]
    EV_ABS: Final[int]
    KEY: Final[Mapping[int, object]]
    KEY_RESERVED: Final[int]
    KEY_MAX: Final[int]
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


def uinput_supports_max_effects(uinput_factory: Callable[..., object]) -> bool:
    try:
        parameters = inspect.signature(uinput_factory).parameters
    except (TypeError, ValueError):
        return True
    return "max_effects" in parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
    )


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


def keyboard_caps(
    evdev_mod: _EvdevModule,
) -> dict[int, Sequence[object]]:
    ecodes = evdev_mod.ecodes
    key_codes = sorted(
        int(code)
        for code in ecodes.KEY
        if ecodes.KEY_RESERVED < int(code) < ecodes.KEY_MAX
    )
    return {
        ecodes.EV_KEY: key_codes,
        ecodes.EV_SYN: [],
    }


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


def _synthetic_uinput_kwargs(
    uinput_factory: Callable[..., object],
    **kwargs: object,
) -> dict[str, object]:
    if uinput_supports_max_effects(uinput_factory):
        kwargs["max_effects"] = 0
    return kwargs


def _create_synthetic_uinput(
    context: str,
    evdev_mod: _EvdevModule,
    *,
    events: Mapping[int, Sequence[object]],
    name: str,
    vendor: int | None = None,
    product: int | None = None,
    version: int | None = None,
    bustype: int | None = None,
) -> ClosableUInput:
    kwargs: dict[str, object] = {
        "events": dict(events),
        "name": name,
    }
    if vendor is not None and product is not None:
        kwargs["vendor"] = vendor
        kwargs["product"] = product
    if version is not None:
        kwargs["version"] = version
    if bustype is not None:
        kwargs["bustype"] = bustype
    kwargs = _synthetic_uinput_kwargs(evdev_mod.UInput, **kwargs)
    return create_uinput_with_permission_hint(
        context,
        lambda: evdev_mod.UInput(**cast(Any, kwargs)),
    )


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
    uinput_dev = _create_synthetic_uinput(
        "virtual gamepad",
        evdev_mod,
        events=gamepad_caps(evdev_mod),
        name=gamepad_name,
        vendor=0x045E if gamepad_vendor is None else gamepad_vendor,
        product=0x028E if gamepad_product is None else gamepad_product,
        version=0x0110,
        bustype=0x0003,
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


def _acquire_global_uinputs(
    manager: _OutputManager,
    *,
    evdev_mod: _EvdevModule,
    log: logging.Logger,
    uinput_writer: UInputWriter,
) -> None:
    if manager.output_state.device_count == 0:
        log.info("Creating global output uinput devices")

        keyboard_name, keyboard_vendor, keyboard_product = uinput_identity(
            "keymasq-keyboard",
            "keyboard",
        )
        manager.output_state.keyboard_uinput = _create_synthetic_uinput(
            "keyboard",
            evdev_mod,
            events=keyboard_caps(evdev_mod),
            name=keyboard_name,
            vendor=keyboard_vendor,
            product=keyboard_product,
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
        manager.output_state.mouse_uinput = _create_synthetic_uinput(
            "mouse",
            evdev_mod,
            events=mouse_caps,
            name=mouse_name,
            vendor=mouse_vendor,
            product=mouse_product,
        )

        configure_virtual_gamepads(
            manager,
            getattr(manager.output_state, "virtual_gamepad_count", 1),
            evdev_mod=evdev_mod,
            log=log,
            uinput_writer=uinput_writer,
        )

    manager.output_state.device_count += 1


def _close_global_uinputs(manager: _OutputManager, *, log: logging.Logger) -> None:
    virtual_gamepad_uinputs = manager.output_state.virtual_gamepad_uinputs
    for uinput_dev in [
        manager.output_state.keyboard_uinput,
        manager.output_state.mouse_uinput,
        *virtual_gamepad_uinputs.values(),
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
    virtual_gamepad_uinputs.clear()


def create_global_uinputs(
    manager: _OutputManager,
    *,
    evdev_mod: _EvdevModule,
    log: logging.Logger,
    uinput_writer: UInputWriter,
) -> None:
    should_roll_back = manager.output_state.device_count == 0
    try:
        _acquire_global_uinputs(
            manager,
            evdev_mod=evdev_mod,
            log=log,
            uinput_writer=uinput_writer,
        )
    except Exception:
        if should_roll_back:
            _close_global_uinputs(manager, log=log)
        raise


def destroy_global_uinputs(manager: _OutputManager, *, log: logging.Logger) -> None:
    manager.output_state.device_count = max(0, manager.output_state.device_count - 1)

    if manager.output_state.device_count == 0:
        log.info("Destroying global output uinput devices")
        _close_global_uinputs(manager, log=log)
