import asyncio
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import evdev
import pytest

from keymasq.common.ipc import CommandType
from keymasq.common.model.core import DeviceType
from keymasq.keymasqd.device_manager import DeviceManager
from keymasq.keymasqd.runtime import adapters, outputs, topology
from keymasq.keymasqd.runtime.grab.state import DesiredGrabConfig
from keymasq.keymasqd.runtime.grabbed_device import device as grabbed_device
from keymasq.keymasqd.runtime.grabbed_device.event import pipeline
from tests.keymasqd.device_manager_support import make_grabbed_device

PYTHON_EVDEV_DEFAULT_PHYS = "py-evdev-uinput"


class _LiveDevice:
    def __init__(self, path: str, *, name: str, phys: str, vendor: int, product: int) -> None:
        self.path = path
        self.name = name
        self.phys = phys
        self.info = SimpleNamespace(vendor=vendor, product=product)

    def capabilities(self) -> dict[int, list[int]]:
        return {evdev.ecodes.EV_KEY: [evdev.ecodes.KEY_A]}

    def input_props(self) -> list[int]:
        return []

    def close(self) -> None:
        pass


def _scan(devices: dict[str, _LiveDevice]) -> topology.Snapshot:
    return topology.scan_live_interfaces_sync(
        clear_device_path_cache_fn=lambda: None,
        device_paths_fn=lambda: list(devices),
        device_input_fn=lambda path: devices[path],
        detect_input_classes_fn=lambda _device: ["keyboard"],
        primary_input_class_fn=lambda _classes: DeviceType.KEYBOARD,
        resolve_stable_path_fn=lambda path: path,
        get_interface_id_fn=lambda _stable_path: "kbd",
        log=logging.getLogger("test"),
    )


def _live_device_from_uinput_kwargs(path: str, kwargs: dict[str, Any]) -> _LiveDevice:
    return _LiveDevice(
        path,
        name=str(kwargs["name"]),
        phys=str(kwargs.get("phys", PYTHON_EVDEV_DEFAULT_PHYS)),
        vendor=int(kwargs.get("vendor", 1)),
        product=int(kwargs.get("product", 1)),
    )


async def _create_every_keymasq_output(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    created: list[dict[str, Any]] = []

    def record_uinput(**kwargs: Any) -> MagicMock:
        created.append(kwargs)
        return MagicMock()

    evdev_mod: Any = SimpleNamespace(
        ecodes=evdev.ecodes, AbsInfo=evdev.AbsInfo, UInput=record_uinput
    )
    outputs.create_global_uinputs(
        SimpleNamespace(output_state=outputs.OutputRuntimeState(virtual_gamepad_count=2)),
        evdev_mod=evdev_mod,
        log=logging.getLogger("test"),
        uinput_writer=lambda device: device,
    )

    async def idle_event_loop(*_args: object, **_kwargs: object) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(grabbed_device.evdev, "UInput", record_uinput)
    monkeypatch.setattr(grabbed_device.grab, "wait_for_active_keys_to_clear", AsyncMock())
    monkeypatch.setattr(pipeline, "event_loop", idle_event_loop)
    sources = [
        ("2dc8:6012", DeviceType.GAMEPAD, "8BitDo Pro 2", evdev.ecodes.BTN_SOUTH),
        ("1234:5678", DeviceType.KEYBOARD, "Plain Keyboard", evdev.ecodes.KEY_A),
    ]
    for hardware_id, device_type, name, code in sources:
        vendor, product = (int(part, 16) for part in hardware_id.split(":"))
        source = SimpleNamespace(
            name=name,
            info=SimpleNamespace(vendor=vendor, product=product, version=1, bustype=3),
            capabilities=lambda code=code: {evdev.ecodes.EV_KEY: [code]},
            input_props=lambda: [],
            close=MagicMock(),
            grab=MagicMock(),
            ungrab=MagicMock(),
        )
        monkeypatch.setattr(grabbed_device, "_device_input", lambda _path, source=source: source)
        grabbed = make_grabbed_device(
            monkeypatch,
            hardware_id=hardware_id,
            device_type=device_type,
            device_types=[device_type.value],
        )
        await grabbed.grab()
        await grabbed.release()
    return created


@pytest.mark.asyncio
async def test_topology_scan_skips_every_keymasq_output_but_keeps_foreign_uinputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = await _create_every_keymasq_output(monkeypatch)
    devices = {
        f"/dev/input/event{100 + index}": _live_device_from_uinput_kwargs(
            f"/dev/input/event{100 + index}", kwargs
        )
        for index, kwargs in enumerate(created)
    }
    devices["/dev/input/event1"] = _LiveDevice(
        "/dev/input/event1", name="8BitDo Pro 2", phys="usb-1/input0", vendor=0x2DC8, product=0x6012
    )
    devices["/dev/input/event2"] = _LiveDevice(
        "/dev/input/event2",
        name="input-remapper keyboard",
        phys=PYTHON_EVDEV_DEFAULT_PHYS,
        vendor=0xCAFE,
        product=0x0001,
    )

    snapshot = _scan(devices)

    assert len(created) == 6
    assert sorted(snapshot) == ["/dev/input/event1", "/dev/input/event2"]


@pytest.mark.asyncio
async def test_grab_of_foreign_uinput_survives_hotplug_and_replug_reconnects() -> None:
    foreign = _LiveDevice(
        "/dev/input/event5",
        name="input-remapper keyboard",
        phys=PYTHON_EVDEV_DEFAULT_PHYS,
        vendor=0xCAFE,
        product=0x0001,
    )
    unrelated = _LiveDevice(
        "/dev/input/event6", name="USB Mouse", phys="usb-2/input0", vendor=0x046D, product=0xC08B
    )
    broadcasts: list[tuple[CommandType, dict[str, object]]] = []
    released: list[tuple[str, str]] = []

    async def broadcast(event_type: CommandType, payload: dict[str, object]) -> None:
        broadcasts.append((event_type, payload))

    manager = DeviceManager(broadcast_callback=broadcast)

    async def release_interface(_manager: object, hardware_id: str, path: str) -> None:
        released.append((hardware_id, path))
        manager.grabbed_devices.pop(hardware_id, None)

    devices = {foreign.path: foreign}
    deps = topology.TopologyRuntimeDeps(
        asyncio_mod=adapters.ASYNCIO_RUNTIME,
        clear_device_path_cache_fn=lambda: None,
        device_paths_fn=lambda: list(devices),
        device_input_fn=lambda path: devices[path],
        detect_input_classes_fn=lambda _device: ["keyboard"],
        primary_input_class_fn=lambda _classes: DeviceType.KEYBOARD,
        resolve_stable_path_fn=lambda path: path,
        get_interface_id_fn=lambda _stable_path: "kbd",
        release_interface_fn=release_interface,
    )
    manager.grab_state.desired_grabs["cafe:0001"] = DesiredGrabConfig(
        paths={foreign.path}, button_map={}
    )
    manager.grabbed_devices["cafe:0001"] = [
        SimpleNamespace(
            path=foreign.path,
            resolved_event_path=foreign.path,
            stable_path=foreign.path,
            interface_id="kbd",
        )
    ]
    manager.topology_state.reconciled_snapshot = _scan(devices)

    async def reconcile() -> None:
        await topology.reconcile_topology(
            manager, _scan(devices), log=logging.getLogger("test"), deps=deps
        )

    devices[unrelated.path] = unrelated
    await reconcile()
    assert released == []
    assert "cafe:0001" in manager.grabbed_devices

    del devices[foreign.path]
    await reconcile()
    assert released == [("cafe:0001", foreign.path)]

    devices[foreign.path] = foreign
    await reconcile()
    assert [
        payload["path"]
        for event_type, payload in broadcasts
        if event_type == CommandType.DEVICE_CONNECTED and payload["hardware_id"] == "cafe:0001"
    ] == [foreign.path]
