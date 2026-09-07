import inspect
import logging
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from hashlib import blake2b
from typing import Any, Final, Protocol, cast

from evdev.uinput import UInputError

from keymasq.common.virtual_device_templates import (
    ResolvedVirtualDevice,
    VirtualDeviceConfig,
    resolve_virtual_devices,
)
from keymasq.common.virtual_devices import (
    DEFAULT_VIRTUAL_GAMEPADS,
    clamp_virtual_gamepad_count,
    is_virtual_gamepad_output_id,
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
    virtual_device_config: VirtualDeviceConfig = field(default_factory=VirtualDeviceConfig)
    virtual_device_specs: dict[str, ResolvedVirtualDevice] = field(default_factory=dict)

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
    virtual_device_config: VirtualDeviceConfig
    virtual_device_specs: dict[str, ResolvedVirtualDevice]


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
    BTN_TASK: Final[int]
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
        int(code) for code in ecodes.KEY if ecodes.KEY_RESERVED < int(code) < ecodes.KEY_MAX
    )
    return {
        ecodes.EV_KEY: key_codes,
        ecodes.EV_SYN: [],
    }


def virtual_device_caps(
    device: ResolvedVirtualDevice,
    evdev_mod: _EvdevModule,
) -> dict[int, Sequence[object]]:
    return {
        evdev_mod.ecodes.EV_KEY: [
            cast(int, getattr(evdev_mod.ecodes, button.evdev.upper()))
            for button in device.template.buttons
        ],
        evdev_mod.ecodes.EV_ABS: [
            (
                cast(int, getattr(evdev_mod.ecodes, axis.evdev.upper())),
                evdev_mod.AbsInfo(
                    axis.rest,
                    axis.minimum,
                    axis.maximum,
                    axis.fuzz,
                    axis.flat,
                    axis.resolution,
                ),
            )
            for axis in device.template.axes
        ],
        evdev_mod.ecodes.EV_SYN: [],
    }


def gamepad_caps(evdev_mod: _EvdevModule) -> dict[int, Sequence[object]]:
    """Return the built-in Xbox template capabilities for compatibility."""
    device = resolve_virtual_devices(1, VirtualDeviceConfig())[0]
    return virtual_device_caps(device, evdev_mod)


def _initialize_virtual_device_axes(
    uinput_dev: WritableUInput | None,
    device: ResolvedVirtualDevice,
    evdev_mod: _EvdevModule,
) -> None:
    if uinput_dev is None:
        return
    for axis in device.template.axes:
        code = cast(int, getattr(evdev_mod.ecodes, axis.evdev.upper()))
        uinput_dev.write(evdev_mod.ecodes.EV_ABS, code, axis.rest)
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


def create_virtual_device(
    device: ResolvedVirtualDevice,
    evdev_mod: _EvdevModule,
    uinput_writer: UInputWriter,
) -> ClosableUInput:
    test_name = device.output_id
    if is_virtual_gamepad_output_id(device.output_id):
        index = int(device.output_id.removeprefix("virtual-gamepad-"))
        test_name = "gamepad" if index == 1 else f"gamepad-{index}"
    gamepad_name, gamepad_vendor, gamepad_product = uinput_identity(
        device.name,
        "gamepad",
        test_name=test_name,
    )
    uinput_dev = _create_synthetic_uinput(
        f"virtual gaming device {device.output_id}",
        evdev_mod,
        events=virtual_device_caps(device, evdev_mod),
        name=gamepad_name,
        vendor=device.vendor_id if gamepad_vendor is None else gamepad_vendor,
        product=device.product_id if gamepad_product is None else gamepad_product,
        version=device.version,
        bustype=device.bustype,
    )
    try:
        _initialize_virtual_device_axes(uinput_writer(uinput_dev), device, evdev_mod)
    except Exception:
        uinput_dev.close()
        raise
    return uinput_dev


def create_virtual_gamepad(
    index: int,
    evdev_mod: _EvdevModule,
    uinput_writer: UInputWriter,
) -> ClosableUInput:
    device = resolve_virtual_devices(index, VirtualDeviceConfig())[index - 1]
    return create_virtual_device(device, evdev_mod, uinput_writer)


def configure_virtual_gamepads(
    manager: _OutputManager,
    count: int,
    *,
    evdev_mod: _EvdevModule,
    log: logging.Logger,
    uinput_writer: UInputWriter,
    config: VirtualDeviceConfig | None = None,
) -> int:
    count = clamp_virtual_gamepad_count(count)
    output_state = cast(Any, manager.output_state)
    if not hasattr(output_state, "virtual_gamepad_uinputs"):
        output_state.virtual_gamepad_uinputs = {}
    if not hasattr(output_state, "virtual_device_config"):
        output_state.virtual_device_config = VirtualDeviceConfig()
    if not hasattr(output_state, "virtual_device_specs"):
        output_state.virtual_device_specs = {}
    if config is None:
        config = cast(VirtualDeviceConfig, output_state.virtual_device_config)
    current = cast(dict[str, ClosableUInput], output_state.virtual_gamepad_uinputs)
    current_specs = cast(dict[str, ResolvedVirtualDevice], output_state.virtual_device_specs)
    desired = {device.output_id: device for device in resolve_virtual_devices(count, config)}
    desired_ids = set(desired)

    prepared: dict[str, ClosableUInput] = {}
    try:
        for output_id, device in desired.items():
            if output_id in current and current_specs.get(output_id) == device:
                continue
            prepared[output_id] = create_virtual_device(device, evdev_mod, uinput_writer)
    except Exception:
        for output_id, uinput_dev in prepared.items():
            try:
                uinput_dev.close()
            except Exception:
                log.exception("Failed to close prepared virtual gamepad %s", output_id)
        raise

    for output_id in sorted((set(current) - desired_ids) | (set(current) & set(prepared))):
        uinput_dev = current.pop(output_id)
        try:
            uinput_dev.close()
        except OSError as exc:
            log.warning("Failed to close virtual gamepad %s: %s", output_id, exc)
        except Exception:
            log.exception("Unexpected failure closing virtual gamepad %s", output_id)
        current_specs.pop(output_id, None)

    for output_id, uinput_dev in prepared.items():
        device = desired[output_id]
        current[output_id] = uinput_dev
        log.info("Created virtual gaming device %s from %s", output_id, device.template.id)

    output_state.virtual_gamepad_count = count
    output_state.virtual_device_config = config
    output_state.virtual_device_specs = desired
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
                evdev_mod.ecodes.BTN_TASK,
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
    virtual_device_specs = getattr(manager.output_state, "virtual_device_specs", None)
    if isinstance(virtual_device_specs, dict):
        virtual_device_specs.clear()


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
