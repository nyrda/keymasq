from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TypedDict
from unittest.mock import AsyncMock

import pytest

from keymasq.common import devices
from keymasq.common.coercion import coerce_int, coerce_str
from keymasq.common.model.actions import MappingAction
from keymasq.common.model.core import DeviceType
from keymasq.keymasqd import device_manager
from keymasq.keymasqd.device_manager import DeviceManager
from keymasq.keymasqd.output_helpers import resolve_output_code
from keymasq.keymasqd.runtime import adapters
from keymasq.keymasqd.runtime.combo import events, state
from keymasq.keymasqd.runtime.grabbed_device import device as grabbed_device
from keymasq.keymasqd.runtime.grabbed_device.device import GrabbedDevice
from keymasq.keymasqd.runtime.grabbed_device.event import pipeline
from keymasq.keymasqd.runtime.grabbed_device.types import EventProcessingDeps
from keymasq.keymasqd.task_helpers import fire_and_observe


class FakeUInput:
    def __init__(self, *args, **kwargs) -> None:
        self.args = args
        self.kwargs = kwargs
        self.writes: list[tuple[int, int, int]] = []

    def write(self, event_type: int, code: int, value: int) -> None:
        self.writes.append((int(event_type), int(code), int(value)))

    def syn(self) -> None:
        return

    def close(self) -> None:
        return


@dataclass
class ComboRuntimeSetup:
    manager: DeviceManager
    device: GrabbedDevice
    passthrough: FakeUInput
    keyboard: FakeUInput
    hardware_id: str


def make_grabbed_device(
    monkeypatch: pytest.MonkeyPatch,
    **kwargs,
) -> GrabbedDevice:
    def resolve_stable_path_fn(path: str) -> str:
        return path

    monkeypatch.setattr(grabbed_device, "resolve_stable_path", resolve_stable_path_fn)
    monkeypatch.setattr(device_manager, "resolve_stable_path", resolve_stable_path_fn)
    interface_id = kwargs.pop("interface_id", "kbd")

    def get_interface_id_fn(_path: str) -> str:
        return interface_id

    monkeypatch.setattr(grabbed_device, "get_interface_id", get_interface_id_fn)
    monkeypatch.setattr(device_manager, "get_interface_id", get_interface_id_fn)
    path = kwargs.pop("path", "/dev/input/event-test")
    hardware_id = kwargs.pop("hardware_id", "1234:5678")
    button_map = kwargs.pop("button_map", {})
    button_codes = kwargs.pop("button_codes", None)
    button_values = kwargs.pop("button_values", None)
    mapping = kwargs.pop("mapping", None)
    mapping_getter = kwargs.pop("mapping_getter", None)
    if mapping_getter is None:
        active_mapping = {} if mapping is None else mapping

        def mapping_getter():
            return active_mapping

    event_callback = kwargs.pop("event_callback", AsyncMock(return_value=None))
    keyboard_uinput = kwargs.pop("keyboard_uinput", FakeUInput())
    mouse_uinput = kwargs.pop("mouse_uinput", FakeUInput())
    gamepad_uinput = kwargs.pop("gamepad_uinput", FakeUInput())
    passthrough_uinput = kwargs.pop("passthrough_uinput", None)
    running = kwargs.pop("running", False)
    device = GrabbedDevice(
        path=path,
        hardware_id=hardware_id,
        button_map=button_map,
        button_codes=button_codes,
        button_values=button_values,
        mapping_getter=mapping_getter,
        event_callback=event_callback,
        interface_id=interface_id,
        keyboard_uinput=keyboard_uinput,  # type: ignore[arg-type]
        mouse_uinput=mouse_uinput,  # type: ignore[arg-type]
        gamepad_uinput=gamepad_uinput,  # type: ignore[arg-type]
        **kwargs,
    )
    if passthrough_uinput is not None:
        device.uinput = passthrough_uinput  # type: ignore[assignment]
    if running:
        device.running = True
    return device


def make_combo_grabbed_device(
    monkeypatch: pytest.MonkeyPatch,
    manager: DeviceManager,
    *,
    button_map: dict[str, str],
    hardware_id: str = "1234:5678",
    path: str = "/dev/input/event-test",
    source: str = "kbd",
    device_type: DeviceType = DeviceType.KEYBOARD,
    mapping: dict[str, MappingAction] | None = None,
    mapping_getter: Callable[[], dict[str, MappingAction]] | None = None,
    passthrough_uinput: object | None = None,
    keyboard_uinput: object | None = None,
    mouse_uinput: object | None = None,
    gamepad_uinput: object | None = None,
    register: bool = True,
) -> GrabbedDevice:
    if passthrough_uinput is None:
        passthrough_uinput = FakeUInput()

    device = make_grabbed_device(
        monkeypatch,
        path=path,
        hardware_id=hardware_id,
        button_map=button_map,
        mapping_getter=mapping_getter,
        mapping=mapping,
        event_callback=lambda *args, **kwargs: events.on_device_event(
            manager,
            *args,
            **kwargs,
            **combo_event_runtime_kwargs(),
        ),
        device_type=device_type,
        keyboard_uinput=keyboard_uinput,  # type: ignore[arg-type]
        mouse_uinput=mouse_uinput,  # type: ignore[arg-type]
        gamepad_uinput=gamepad_uinput,  # type: ignore[arg-type]
        interface_id=source,
        passthrough_uinput=passthrough_uinput,
        running=True,
    )
    if register:
        manager.grabbed_devices.setdefault(hardware_id, []).append(device)
    return device


async def make_combo_runtime_setup(
    monkeypatch: pytest.MonkeyPatch,
    combos: Sequence[object],
    *,
    button_map: dict[str, str],
    hardware_id: str = "1234:5678",
    mapping: dict[str, MappingAction] | None = None,
    passthrough_uinput: FakeUInput | None = None,
    keyboard_uinput: FakeUInput | None = None,
) -> ComboRuntimeSetup:
    manager = DeviceManager()
    passthrough = passthrough_uinput or FakeUInput()
    keyboard = keyboard_uinput or FakeUInput()

    await manager.set_combos(combos)
    manager.output_state.keyboard_uinput = keyboard

    device = make_combo_grabbed_device(
        monkeypatch,
        manager,
        hardware_id=hardware_id,
        button_map=button_map,
        mapping=mapping,
        passthrough_uinput=passthrough,
        keyboard_uinput=keyboard,
    )
    return ComboRuntimeSetup(
        manager=manager,
        device=device,
        passthrough=passthrough,
        keyboard=keyboard,
        hardware_id=hardware_id,
    )


class ComboEventRuntimeKwargs(TypedDict):
    resolve_stable_path_fn: state.ResolveStablePathFn
    get_interface_id_fn: state.GetInterfaceIdFn
    int_value_fn: state.IntValueFn
    str_value_fn: state.StrValueFn
    deps: state.ComboRuntimeDeps


def combo_runtime_deps(
    *,
    resolve_code_fn: state.ResolveCodeFn = resolve_output_code,
    fire_and_observe_fn: state.FireAndObserve = fire_and_observe,
) -> state.ComboRuntimeDeps:
    return state.ComboRuntimeDeps(
        asyncio_mod=adapters.ASYNCIO_RUNTIME,
        evdev_mod=adapters.COMBO_EVDEV_RUNTIME,
        uinput_writer=adapters.identity_uinput_writer,
        resolve_code_fn=resolve_code_fn,
        fire_and_observe_fn=fire_and_observe_fn,
    )


def combo_event_runtime_kwargs() -> ComboEventRuntimeKwargs:
    return {
        "resolve_stable_path_fn": devices.resolve_stable_path,
        "get_interface_id_fn": devices.get_interface_id,
        "int_value_fn": coerce_int,
        "str_value_fn": coerce_str,
        "deps": combo_runtime_deps(),
    }


def grabbed_event_processing_deps() -> EventProcessingDeps:
    return pipeline.build_event_processing_deps(log=grabbed_device.log)
