import asyncio
import math
import os
import struct
from collections.abc import AsyncGenerator
from dataclasses import replace
from pathlib import Path

import evdev
import pytest

from keymasq.common.model.core import DeviceType
from keymasq.keymasqd.input_sources import discovery
from keymasq.keymasqd.input_sources.drivers.eightbitdo_ultimate2 import Ultimate2Driver
from keymasq.keymasqd.input_sources.evdev_adapter import (
    NativeInputDevice,
    frame_events,
    motion_axes,
)
from keymasq.keymasqd.input_sources.manager import SourceManager, Subscription
from keymasq.keymasqd.input_sources.types import Binding, Channel, Endpoint, InputFrame
from keymasq.keymasqd.runtime import device_path_resolver as resolver

DESCRIPTOR = bytes.fromhex(
    "05010905a1018501050115002507463b0195017504651409398142750195048101150026ff00"
    "09300931093209359504750881020502150026ff0009c409c5950275088102050919012918"
    "150025017501951881020600ff0920750895178102050f0970850515002564750895049102c0"
)
CAPTURED_REPORT = bytes.fromhex(
    "010f7f807f7f00000000000000006443fe04000f1003001700020000000000000000"
)


def binding(node: int = 7) -> Binding:
    return Binding(
        Ultimate2Driver(),
        Endpoint(
            f"/dev/hidraw{node}",
            f"/sys/devices/usb/0003:2DC8:6012.00{node}",
            "/sys/devices/usb",
            3,
            0x2DC8,
            0x6012,
            DESCRIPTOR,
        ),
        (f"/dev/input/event{node}",),
    )


def test_ultimate2_captured_report_and_normalization():
    device = binding()
    assert device.driver.matches(device.endpoint)
    values = device.driver.decode(CAPTURED_REPORT)
    assert values == {
        "accel_x": -445,
        "accel_y": 4,
        "accel_z": 4111,
        "gyro_x": 3,
        "gyro_y": 23,
        "gyro_z": 2,
    }
    axes = motion_axes(device)
    assert axes["gyro_axes"][0]["role"] == "roll"
    assert axes["gyro_axes"][0]["invert"] is True
    assert axes["gyro_axes"][0]["scale"] == pytest.approx(math.radians(2000) / 32767)
    assert axes["accelerometer_axes"][2]["scale"] == pytest.approx(9.80665 / 4096)
    assert values is not None
    events = frame_events(device, InputFrame(values, 2_000_000_000, 2_000_000_000))
    assert len(events) == 7
    assert {event.type for event in events} == {evdev.ecodes.EV_ABS, evdev.ecodes.EV_SYN}
    assert events[-1].code == evdev.ecodes.SYN_REPORT


@pytest.mark.parametrize(
    "report",
    [
        b"",
        CAPTURED_REPORT[:12],
        CAPTURED_REPORT[:-1],
        b"\x02" + CAPTURED_REPORT[1:],
        CAPTURED_REPORT + b"\x00",
    ],
)
def test_ultimate2_rejects_unvalidated_formats(report):
    assert Ultimate2Driver().decode(report) is None


def test_ultimate2_does_not_claim_other_modes_or_interfaces():
    endpoint = binding().endpoint
    for altered in (
        replace(endpoint, product=0x6013),
        replace(endpoint, bus=5),
        replace(endpoint, vendor=0x057E, product=0x2009),
        replace(endpoint, descriptor=b"\x06\xa0\xff"),
    ):
        assert not Ultimate2Driver().matches(altered)
    report = bytearray(CAPTURED_REPORT)
    struct.pack_into("<6h", report, 15, -32768, 32767, 0, -1, -200, 300)
    assert list(Ultimate2Driver().decode(bytes(report)).values()) == [
        -32768,
        32767,
        0,
        -1,
        -200,
        300,
    ]


def make_sysfs(root: Path, node: int) -> Path:
    parent = root / f"devices/usb/0003:2DC8:6012.00{node}"
    parent.mkdir(parents=True)
    (parent / "subsystem").symlink_to(root / "bus/hid")
    (parent / "uevent").write_text(
        "HID_ID=0003:00002DC8:00006012\nHID_NAME=8BitDo\nHID_PHYS=usb-test/input0\n"
    )
    (parent / "report_descriptor").write_bytes(DESCRIPTOR)
    input_parent = parent / f"input/input{node}"
    (input_parent / f"event{node}").mkdir(parents=True)
    event = root / f"class/input/event{node}"
    event.mkdir(parents=True)
    (event / "device").symlink_to(input_parent)
    raw = root / f"class/hidraw/hidraw{node}"
    raw.mkdir(parents=True)
    (raw / "device").symlink_to(parent)
    return parent


def test_discovery_pairs_hid_ancestors_not_model_or_hub(tmp_path, monkeypatch):
    first = make_sysfs(tmp_path, 7)
    second = make_sysfs(tmp_path, 8)
    discovered = discovery.discover_bindings(sysfs=tmp_path)
    assert len(discovered) == 2
    assert {item.endpoint.hid_parent for item in discovered} == {str(first), str(second)}
    original = discovery.hid_parent
    monkeypatch.setattr(discovery, "hid_parent", lambda path: original(path, tmp_path))
    assert (
        discovery.companion_binding(
            discovered, "8bitdo-ultimate2", "/dev/input/event8"
        ).endpoint.path
        == "/dev/hidraw8"
    )
    assert discovery.companion_binding(discovered, "8bitdo-ultimate2", "/dev/input/event9") is None


def test_driver_can_associate_different_hid_interfaces_on_one_usb_device(tmp_path, monkeypatch):
    raw_parent = make_sysfs(tmp_path, 7)
    keyboard_parent = make_sysfs(tmp_path, 8)
    (raw_parent / "input/input7/event7").rmdir()
    (keyboard_parent / "report_descriptor").write_bytes(b"keyboard descriptor")
    usb = raw_parent.parent
    (usb / "subsystem").symlink_to(tmp_path / "bus/usb")
    (usb / "idVendor").write_text("2dc8")
    driver = Ultimate2Driver()
    driver.association = "same_usb"
    discovered = discovery.discover_bindings(sysfs=tmp_path, drivers=iter([driver]))
    assert len(discovered) == 1
    assert discovered[0].companions == ("/dev/input/event8",)
    original = discovery.hid_parent
    monkeypatch.setattr(discovery, "hid_parent", lambda path: original(path, tmp_path))
    assert discovery.companion_binding(discovered, driver.id, "/dev/input/event8") is discovered[0]


def test_resolver_keeps_two_identical_controllers_together(monkeypatch):
    devices = [binding(8), binding(7)]
    monkeypatch.setattr(discovery, "discover_bindings", lambda: devices)
    monkeypatch.setattr(
        discovery,
        "hid_parent",
        lambda path: next(item.endpoint.hid_parent for item in devices if path in item.companions),
    )
    cache = resolver.DeviceCache()
    cache._devices = {
        item.companions[0]: resolver.CachedDeviceInfo(
            item.companions[0],
            "2dc8",
            "6012",
            "",
            DeviceType.GAMEPAD,
            {"ev_key_304"},
            False,
        )
        for item in devices
    }
    deps = resolver.DevicePathResolverDeps(
        device_paths_fn=lambda: list(cache._devices),
        device_input_fn=lambda _path: None,
        detect_input_classes_fn=lambda _dev: [],
        primary_input_class_fn=lambda _types: DeviceType.GAMEPAD,
        cache=cache,
    )
    anchor = {"id": "gamepad", "path": "keymasq:2dc8:6012", "type": "gamepad"}
    native = {
        "id": "imu",
        "path": "keymasq-source:imu",
        "backend": "hidraw",
        "driver": "8bitdo-ultimate2",
        "anchor": anchor,
        "companion_of": "gamepad",
    }
    first = resolver.resolve_evdev_interfaces(
        [native, anchor], deps=deps, hardware_id="2dc8:6012", preferred_paths=["/dev/input/event8"]
    )
    assert [item.path for item in first] == ["/dev/input/event8", devices[0].path]
    # Even when only motion is active, another hardware config cannot borrow
    # the first controller's evdev anchor for its own native source.
    second = resolver.resolve_evdev_interfaces(
        [native], deps=deps, hardware_id="2dc8:6012@2", excluded_paths=[devices[0].path]
    )
    assert [item.path for item in second] == [devices[1].path]
    devices.reverse()
    again = resolver.resolve_evdev_interfaces(
        [native], deps=deps, hardware_id="2dc8:6012", preferred_paths=[first[-1].path]
    )
    assert again[0].path == first[-1].path
    native["anchor"] = {"id": "missing", "path": "keymasq:ffff:ffff"}
    assert not resolver.resolve_evdev_interfaces([native], deps=deps, hardware_id="2dc8:6012")


async def test_source_manager_shares_reader_and_preserves_equal_reports():
    packets: asyncio.Queue[bytes] = asyncio.Queue()
    opened = 0
    closed = 0

    async def reader(_path: str) -> AsyncGenerator[bytes]:
        nonlocal opened, closed
        opened += 1
        try:
            while True:
                yield await packets.get()
        finally:
            closed += 1

    manager = SourceManager(reader)
    async with manager.subscribe(binding()) as runtime:
        async with manager.subscribe(binding()) as inspector:
            await packets.put(CAPTURED_REPORT)
            first = await asyncio.wait_for(runtime.read(), 1)
            assert (await inspector.read()).values == first.values
            assert opened == 1
        await packets.put(CAPTURED_REPORT)
        assert (await asyncio.wait_for(runtime.read(), 1)).values == first.values
        assert closed == 0
    assert closed == 1
    assert not manager._sessions


def test_slow_subscriber_has_bounded_queue_and_discontinuity():
    subscriber = Subscription(queue=asyncio.Queue(2))
    for timestamp in range(3):
        subscriber.offer(InputFrame({"gyro_x": 1}, timestamp, timestamp))
    frame = subscriber.queue.get_nowait()
    assert isinstance(frame, InputFrame)
    assert frame.discontinuity and frame.sample_ns == 2
    assert subscriber.dropped_frames == 2


async def test_reader_failure_wakes_all_subscribers_and_reconnect_opens_fresh():
    unplugged = asyncio.Event()

    async def reader(_path: str) -> AsyncGenerator[bytes]:
        yield CAPTURED_REPORT
        await unplugged.wait()
        raise OSError("unplugged")

    manager = SourceManager(reader)
    for _ in range(2):
        unplugged.clear()
        async with manager.subscribe(binding()) as first, manager.subscribe(binding()) as second:
            await first.read()
            await second.read()
            unplugged.set()
            with pytest.raises(OSError, match="unplugged"):
                await first.read()
            with pytest.raises(OSError, match="unplugged"):
                await second.read()


async def test_raw_only_non_motion_driver_uses_shared_manager():
    class ExtraButtonDriver:
        id = "test-extra-buttons"
        label = "Extra buttons"
        association = "same_usb"
        channels = (Channel("rear", "button", "rear"),)

        def matches(self, endpoint):
            return endpoint.vendor == 0x1234

        def decode(self, report):
            return {"rear": report[0]} if len(report) == 1 else None

    async def reader(_path: str) -> AsyncGenerator[bytes]:
        yield b"\x01"
        await asyncio.Event().wait()

    native = Binding(ExtraButtonDriver(), replace(binding().endpoint, vendor=0x1234))
    async with SourceManager(reader).subscribe(native) as subscriber:
        assert (await subscriber.read()).values == {"rear": 1}
    assert native.companions == ()


def test_native_adapter_does_not_open_or_grab_evdev(monkeypatch):
    monkeypatch.setattr(
        "keymasq.keymasqd.input_sources.evdev_adapter.binding_for_path", lambda _path: binding()
    )
    device = NativeInputDevice(binding().path)
    assert device.input_props() == [evdev.ecodes.INPUT_PROP_ACCELEROMETER]
    assert evdev.ecodes.EV_KEY not in device.capabilities()
    assert evdev.ecodes.EV_ABS in device.capabilities(absinfo=True)
    device.close()


async def test_calibration_shares_runtime_reader_and_closes_its_subscription(monkeypatch):
    from keymasq.keymasqd.capture_manager import CaptureManager

    packets: asyncio.Queue[bytes] = asyncio.Queue()
    opens = 0
    closed = 0

    async def reader(_path: str) -> AsyncGenerator[bytes]:
        nonlocal opens, closed
        opens += 1
        try:
            while True:
                yield await packets.get()
        finally:
            closed += 1

    shared = SourceManager(reader)
    source = binding()
    monkeypatch.setattr(discovery, "binding_for_path", lambda _path: source)
    monkeypatch.setattr("keymasq.keymasqd.input_sources.manager.source_manager", lambda: shared)
    monkeypatch.setattr(
        resolver,
        "resolve_evdev_interfaces",
        lambda *_args, **_kwargs: [
            resolver.ResolvedInterface(
                source.path, "keymasq-source:imu", "imu", DeviceType.MOTION, []
            )
        ],
    )
    captures = CaptureManager()
    async with shared.subscribe(source) as runtime:
        await packets.put(CAPTURED_REPORT)
        await runtime.read()
        result = await captures.begin_native("2dc8:6012", [], [3, 4, 5])
        token = result["token"]
        frames = captures.read(token)["frames"]
        assert frames[0]["values"] == {"3": 3, "4": 23, "5": 2}
        assert frames[0]["source"] == "imu"
        assert opens == 1
        await captures.stop_native(token)
        captures.end(token)
        assert closed == 0
        await packets.put(CAPTURED_REPORT)
        await runtime.read()
    assert closed == 1
    assert not captures._sessions


async def test_cancelled_calibration_start_releases_native_reader(monkeypatch):
    from keymasq.keymasqd.capture_manager import CaptureManager

    opened = asyncio.Event()
    closed = asyncio.Event()

    async def reader(_path: str) -> AsyncGenerator[bytes]:
        opened.set()
        try:
            await asyncio.Event().wait()
            yield CAPTURED_REPORT
        finally:
            closed.set()

    shared = SourceManager(reader)
    source = binding()
    monkeypatch.setattr(discovery, "binding_for_path", lambda _path: source)
    monkeypatch.setattr("keymasq.keymasqd.input_sources.manager.source_manager", lambda: shared)
    monkeypatch.setattr(
        resolver,
        "resolve_evdev_interfaces",
        lambda *_args, **_kwargs: [
            resolver.ResolvedInterface(
                source.path, "keymasq-source:imu", "imu", DeviceType.MOTION, []
            )
        ],
    )
    captures = CaptureManager()
    starting = asyncio.create_task(captures.begin_native("2dc8:6012", [], [3]))
    await asyncio.wait_for(opened.wait(), 1)
    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starting
    assert closed.is_set()
    assert not captures._sessions
    assert not shared._sessions


async def test_hidraw_transport_uses_readiness_and_closes_fd(monkeypatch):
    from keymasq.keymasqd.input_sources.transports import hidraw

    read_fd, write_fd = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
    monkeypatch.setattr(hidraw, "_open", lambda _path, _parent: read_fd)
    stream = hidraw.reports("test")
    try:
        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(0.01)
        assert not pending.done()
        os.write(write_fd, CAPTURED_REPORT)
        assert await asyncio.wait_for(pending, 1) == CAPTURED_REPORT
        await stream.aclose()
        with pytest.raises(OSError):
            os.fstat(read_fd)
    finally:
        os.close(write_fd)


def test_transport_rejects_recycled_hidraw_number_before_open(monkeypatch):
    from keymasq.keymasqd.input_sources.transports import hidraw

    def unexpected_open(*_args):
        pytest.fail("Stale connection must not be opened")

    monkeypatch.setattr(hidraw.os, "open", unexpected_open)
    with pytest.raises(OSError, match="connection changed"):
        hidraw._open("/dev/hidraw999999", "/sys/devices/old-connection")
