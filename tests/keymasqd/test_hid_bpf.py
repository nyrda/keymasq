import asyncio
import contextlib
import errno
import mmap
import os
import struct
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import evdev
import pytest

from keymasq.common.model.actions import MappingAction
from keymasq.common.model.core import ActionType, DeviceType
from keymasq.keymasqd import fd_handoff
from keymasq.keymasqd.input_sources import bpf, discovery, hid_bpf_attach
from keymasq.keymasqd.input_sources.drivers.steam_deck_touch import SteamDeckTouchDriver
from keymasq.keymasqd.input_sources.manager import SourceManager
from keymasq.keymasqd.input_sources.transports import hid_bpf
from keymasq.keymasqd.input_sources.types import Binding, Endpoint

GAMEPAD_DESCRIPTOR = bytes.fromhex("06ffff0901a101150026ff0075089540090181020901b102c0")
KEYBOARD_DESCRIPTOR = bytes.fromhex(
    "05010906a101050719e029e71500250175019508810281011900296515002565750895068100c0"
)
DECK = "0003:28DE:1205.0019"
CLIENT = "0003:28DE:1205.0050"
TOKEN = "0123456789abcdef0123456789abcdef"
BTF_INT, BTF_ENUM, BTF_STRUCT, BTF_FUNC, BTF_FUNC_PROTO = 1, 6, 4, 12, 13


def deck_hid(
    root: Path,
    name: str,
    *,
    group: int = 0x0001,
    descriptor: bytes = GAMEPAD_DESCRIPTOR,
    events: tuple[str, ...] = (),
) -> Path:
    usb = root / "devices/pci/usb3/3-3"
    usb.mkdir(parents=True, exist_ok=True)
    if not (usb / "subsystem").is_symlink():
        (usb / "subsystem").symlink_to(root / "bus/usb")
        (usb / "idVendor").write_text("28de\n")
    parent = usb / "3-3:1.2" / name
    parent.mkdir(parents=True)
    (parent / "subsystem").symlink_to(root / "bus/hid")
    (parent / "uevent").write_text(
        "HID_ID=0003:000028DE:00001205\n"
        "HID_NAME=Valve Software Steam Deck Controller\n"
        "HID_PHYS=usb-0000:04:00.4-3/input2\n"
        f"MODALIAS=hid:b0003g{group:04X}v000028DEp00001205\n"
    )
    (parent / "report_descriptor").write_bytes(descriptor)
    for event in events:
        (parent / "input/input1" / event).mkdir(parents=True)
    devices = root / "bus/hid/devices"
    devices.mkdir(parents=True, exist_ok=True)
    (devices / name).symlink_to(parent)
    return parent.resolve()


def deck_binding() -> Binding:
    parent = f"/sys/devices/pci/usb3/3-3/3-3:1.2/{DECK}"
    endpoint = Endpoint(
        parent, parent, "/sys/devices/pci/usb3/3-3", 3, 0x28DE, 0x1205, GAMEPAD_DESCRIPTOR, group=1
    )
    return Binding(SteamDeckTouchDriver(), endpoint, ("/dev/input/event27",))


def touch_state(touch: int) -> bytes:
    return struct.pack("<Q", touch)


def open_memfds(*names: str) -> list[int]:
    return [os.memfd_create(f"keymasq-test-{name}") for name in names]


def open_fd_targets() -> list[str]:
    targets = []
    for entry in os.listdir("/proc/self/fd"):
        with contextlib.suppress(OSError):
            targets.append(os.readlink(f"/proc/self/fd/{entry}"))
    return targets


def test_discovery_binds_deck_gamepad_interface_without_hidraw(tmp_path):
    gamepad = deck_hid(tmp_path, DECK, events=("event27", "event28"))
    deck_hid(tmp_path, CLIENT, group=0x0103)
    deck_hid(tmp_path, "0003:28DE:1205.0017", descriptor=KEYBOARD_DESCRIPTOR, events=("event4",))
    bindings = discovery.discover_bindings(sysfs=tmp_path)
    assert [(item.driver.id, item.endpoint.hid_parent, item.companions) for item in bindings] == [
        ("steam-deck-touch", str(gamepad), ("/dev/input/event27", "/dev/input/event28"))
    ]
    assert bindings[0].path == f"/dev/keymasq-sources/steam-deck-touch/{DECK}/hid-bpf"


def test_assembler_counts_wide_instructions_in_jump_offsets():
    code = bpf.assemble(
        [
            bpf.Jump(bpf.JEQ_K, 0),
            bpf.ld_map_fd(1, 5),
            bpf.insn(bpf.MOV_K, 0, imm=1),
            bpf.Label("exit"),
            bpf.insn(bpf.EXIT),
        ]
    )
    assert len(code) == 5 * 8
    assert struct.unpack_from("<h", code, 2)[0] == 3


def btf_blob(types: list[tuple[str, int, int, list[tuple[str, int]]]]) -> bytes:
    strings = bytearray(b"\0")

    def name(value: str) -> int:
        if not value:
            return 0
        strings.extend(value.encode() + b"\0")
        return len(strings) - len(value) - 1

    records = bytearray()
    for type_name, kind, size, items in types:
        if kind == BTF_INT:
            extra = struct.pack("<I", 32)
        elif kind == BTF_ENUM:
            extra = b"".join(struct.pack("<Ii", name(key), value) for key, value in items)
        elif kind == BTF_STRUCT:
            extra = b"".join(struct.pack("<III", name(key), 1, at * 8) for key, at in items)
        elif kind == BTF_FUNC_PROTO:
            extra = b"".join(struct.pack("<II", name(key), 1) for key, _ in items)
        else:
            extra = b""
        records += struct.pack("<III", name(type_name), (kind << 24) | len(items), size) + extra
    header = struct.pack("<HBBIIIII", 0xEB9F, 1, 0, 24, 0, len(records), len(records), len(strings))
    return header + bytes(records) + bytes(strings)


def test_btf_layout_locates_hid_bpf_struct_ops():
    ops = [("hid_id", 0), ("flags", 4), ("list", 8), ("hid_device_event", 24), ("fixup", 32)]
    btf = bpf.KernelBtf(
        btf_blob(
            [
                ("int", BTF_INT, 4, []),
                ("hid_report_type", BTF_ENUM, 4, [("HID_INPUT_REPORT", 0), ("HID_FEATURE", 2)]),
                ("", BTF_FUNC_PROTO, 1, [("ctx", 0)]),
                ("hid_bpf_ops_extra", BTF_STRUCT, 8, [("hid_id", 0)]),
                ("hid_bpf_ops", BTF_STRUCT, 64, ops),
                ("bpf_struct_ops_hid_bpf_ops", BTF_STRUCT, 128, [("common", 0), ("data", 64)]),
                ("hid_bpf_get_data", BTF_FUNC, 3, []),
            ]
        )
    )
    assert bpf.HidBpfLayout.from_btf(btf) == bpf.HidBpfLayout(
        ops_type_id=5,
        value_type_id=6,
        value_size=128,
        data_offset=64,
        hid_id_offset=0,
        event_offset=24,
        event_member=3,
        get_data_btf_id=7,
    )
    without_hid_bpf = bpf.KernelBtf(btf_blob([("int", BTF_INT, 4, [])]))
    with pytest.raises(OSError, match="HID-BPF"):
        bpf.HidBpfLayout.from_btf(without_hid_bpf)


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"device": f"../../{DECK}"}, "HID device name"),
        ({"driver": "8bitdo-ultimate2"}, "Unknown HID-BPF driver"),
        ({"token": "not-a-token"}, "handoff token"),
        ({"device": CLIENT}, "does not match"),
    ],
)
def test_attach_request_loads_only_bundled_programs_on_matching_devices(
    tmp_path, monkeypatch, change, error
):
    deck_hid(tmp_path, DECK)
    deck_hid(tmp_path, CLIENT, group=0x0103)
    monkeypatch.setattr(bpf, "attach", Mock(side_effect=AssertionError("loaded a program")))
    message = {"driver": "steam-deck-touch", "device": DECK, "token": TOKEN, **change}
    with pytest.raises(ValueError, match=error):
        hid_bpf_attach.attach_request(message, sysfs=tmp_path)


async def test_attach_request_hands_the_attachment_to_the_daemon(
    tmp_path, temp_socket_dir, monkeypatch
):
    deck_hid(tmp_path, DECK)
    loads: list[tuple[str, int]] = []

    def attach(driver, hid_id):
        loads.append((driver.id, hid_id))
        return bpf.AttachedProgram(*open_memfds("link", "ring", "state"))

    monkeypatch.setattr(bpf, "attach", attach)
    monkeypatch.setattr(
        hid_bpf_attach.pwd, "getpwnam", lambda _name: SimpleNamespace(pw_uid=os.getuid())
    )
    server = fd_handoff.FdHandoffServer(temp_socket_dir / "handoff", trusted_uid=os.getuid())
    await server.start()
    try:
        async with server.expect(TOKEN) as pending:
            result = await asyncio.to_thread(
                hid_bpf_attach.attach_request,
                {"driver": "steam-deck-touch", "device": DECK, "token": TOKEN},
                sysfs=tmp_path,
                handoff_path=server.path,
            )
            received = await pending.claim(1.0)
    finally:
        await server.stop()
    assert result == {"device": DECK}
    assert loads == [("steam-deck-touch", 0x19)]
    targets = [os.readlink(f"/proc/self/fd/{fd}") for fd in received]
    names = ("link", "ring", "state")
    assert targets == [f"/memfd:keymasq-test-{name} (deleted)" for name in names]
    assert sum(target in targets for target in open_fd_targets()) == 3
    for fd in received:
        os.close(fd)


async def test_handoff_delivers_descriptors_only_for_the_awaited_token(temp_socket_dir):
    server = fd_handoff.FdHandoffServer(temp_socket_dir / "handoff", trusted_uid=os.getuid())
    await server.start()
    delivered_read, delivered_write = os.pipe()
    stray_read, stray_write = os.pipe()
    try:
        async with server.expect(TOKEN) as pending:
            for token, fd in (("f" * 32, stray_write), (TOKEN, delivered_write)):
                await asyncio.to_thread(
                    fd_handoff.send, server.path, token, [fd], expected_uid=os.getuid()
                )
            (received,) = await pending.claim(1.0)
    finally:
        await server.stop()
        os.close(delivered_write)
        os.close(stray_write)
    os.write(received, b"x")
    os.close(received)
    assert os.read(delivered_read, 1) == b"x"
    assert await asyncio.wait_for(asyncio.to_thread(os.read, stray_read, 1), 1.0) == b""


async def test_handoff_rejects_untrusted_peers_on_both_sides(temp_socket_dir):
    server = fd_handoff.FdHandoffServer(temp_socket_dir / "handoff", trusted_uid=os.getuid() + 1)
    await server.start()
    read_end, write_end = os.pipe()
    try:
        with pytest.raises(PermissionError):
            fd_handoff.send(server.path, TOKEN, [write_end], expected_uid=os.getuid() + 1)
        async with server.expect(TOKEN) as pending:
            with contextlib.suppress(ConnectionError):
                await asyncio.to_thread(
                    fd_handoff.send, server.path, TOKEN, [write_end], expected_uid=os.getuid()
                )
            with pytest.raises(TimeoutError):
                await pending.claim(0.2)
    finally:
        await server.stop()
        os.close(write_end)
    assert await asyncio.wait_for(asyncio.to_thread(os.read, read_end, 1), 1.0) == b""


class HidBpfMaps:
    def __init__(self, monkeypatch) -> None:
        self.page = mmap.PAGESIZE
        self.fds = open_memfds("link", "ring", "state")
        os.ftruncate(self.fds[1], 2 * self.page + 2 * bpf.RING_SIZE)
        os.ftruncate(self.fds[2], self.page)
        self.ring = mmap.mmap(self.fds[1], 2 * self.page + 2 * bpf.RING_SIZE)
        self.state = mmap.mmap(self.fds[2], self.page)
        self.reports = 0
        monkeypatch.setattr(hid_bpf, "HEARTBEAT_S", 0.01)
        monkeypatch.setattr(hid_bpf, "STALL_S", 0.05)
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "add_reader", lambda *_args: None)
        monkeypatch.setattr(loop, "remove_reader", lambda *_args: True)

    async def attach(self, _binding):
        return tuple(os.dup(fd) for fd in self.fds)

    def report(self, sequence: int, touch: int, *records: tuple[int, int]) -> None:
        self.reports += 1
        struct.pack_into("<QQQ", self.state, 0, self.reports, sequence, touch)
        position = struct.unpack_from("<Q", self.ring, self.page)[0]
        for record_sequence, record_touch in records:
            offset = 2 * self.page + position
            struct.pack_into(
                "<IIQQ", self.ring, offset, bpf.RECORD_SIZE, 0, record_sequence, record_touch
            )
            position += 8 + bpf.RECORD_SIZE
        struct.pack_into("<Q", self.ring, self.page, position)

    def close(self) -> None:
        self.ring.close()
        self.state.close()
        for fd in self.fds:
            os.close(fd)


def right_stick(report: bytes) -> int | None:
    values = SteamDeckTouchDriver().decode(report)
    return None if values is None else values["right_stick_touch"]


async def test_hid_bpf_reports_deliver_buffered_transitions_in_order(monkeypatch):
    maps = HidBpfMaps(monkeypatch)
    baseline = open_fd_targets()
    maps.report(2, 0x01, (1, 0x81), (2, 0x01))
    stream = hid_bpf.reports(deck_binding(), attach=maps.attach)
    assert [right_stick(await anext(stream)) for _ in range(2)] == [1, 0]
    maps.report(3, 0x81, (2, 0x01), (3, 0x81))
    assert right_stick(await anext(stream)) == 1
    with pytest.raises(OSError) as stopped:
        async with asyncio.timeout(1):
            while True:
                assert right_stick(await anext(stream)) == 1
    assert stopped.value.errno == errno.ENODEV
    assert sorted(open_fd_targets()) == sorted(baseline)
    maps.close()


async def test_hid_bpf_reports_keep_transitions_committed_after_a_drain(monkeypatch):
    maps = HidBpfMaps(monkeypatch)
    maps.report(1, 0x01, (1, 0x01))
    drain = hid_bpf._drain
    commits = [lambda: maps.report(3, 0x01, (2, 0x81), (3, 0x01))]

    def drain_before_commits(consumer, producer):
        records = drain(consumer, producer)
        while commits:
            commits.pop()()
        return records

    monkeypatch.setattr(hid_bpf, "_drain", drain_before_commits)
    stream = hid_bpf.reports(deck_binding(), attach=maps.attach)
    assert [right_stick(await anext(stream)) for _ in range(3)] == [0, 1, 0]
    await stream.aclose()
    maps.close()


async def test_hid_bpf_reports_recover_the_state_of_lost_records(monkeypatch):
    maps = HidBpfMaps(monkeypatch)
    maps.report(4, 0x89)
    stream = hid_bpf.reports(deck_binding(), attach=maps.attach)
    assert SteamDeckTouchDriver().decode(await anext(stream)) == {
        "left_stick_touch": 0,
        "right_stick_touch": 1,
        "left_pad_touch": 1,
        "right_pad_touch": 0,
    }
    maps.report(4, 0x89, (4, 0x89))
    assert right_stick(await anext(stream)) == 1
    await stream.aclose()
    maps.close()


async def test_hid_bpf_reports_survive_a_suspend_without_reports(monkeypatch):
    maps = HidBpfMaps(monkeypatch)
    suspended = [0]
    monkeypatch.setattr(hid_bpf.clock, "suspended_ns", lambda: suspended[0])
    maps.report(1, 0x01, (1, 0x01))
    stream = hid_bpf.reports(deck_binding(), attach=maps.attach)
    assert right_stick(await anext(stream)) == 0
    reading = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    time.sleep(0.1)
    suspended[0] += 30_000_000_000
    assert right_stick(await reading) == 0
    maps.report(2, 0x81, (2, 0x81))
    async with asyncio.timeout(1):
        while right_stick(await anext(stream)) != 1:
            pass
    await stream.aclose()
    maps.close()


async def test_stick_touch_acts_as_a_mapped_button_until_the_source_detaches(monkeypatch):
    from tests.keymasqd.device_manager_support import FakeUInput, make_grabbed_device

    reports: asyncio.Queue[bytes | OSError] = asyncio.Queue()

    async def reader(_path):
        while True:
            item = await reports.get()
            if isinstance(item, OSError):
                raise item
            yield item

    shared = SourceManager(reader)
    source = deck_binding()
    monkeypatch.setattr(
        "keymasq.keymasqd.input_sources.evdev_adapter.source_manager", lambda: shared
    )
    monkeypatch.setattr(
        "keymasq.keymasqd.input_sources.evdev_adapter.binding_for_path", lambda _path: source
    )
    monkeypatch.setattr(evdev, "UInput", Mock(side_effect=AssertionError("created an output")))
    keyboard = FakeUInput()
    device = make_grabbed_device(
        monkeypatch,
        path=source.path,
        hardware_id="28de:1205",
        interface_id="touch",
        button_map={"btn_touch_rs": "btn_touch_rs"},
        button_codes={"btn_touch_rs": 769},
        mapping={"btn_touch_rs": MappingAction(ActionType.KEYBOARD, target="key_a")},
        device_type=DeviceType.OTHER,
        keyboard_uinput=keyboard,
    )

    def key_a() -> list[int]:
        return [value for _, code, value in keyboard.writes if code == evdev.ecodes.KEY_A]

    await device.grab()
    try:
        for touch in (0x00, 0x81, 0x01, 0x81):
            await reports.put(touch_state(touch))
        async with asyncio.timeout(2):
            while key_a() != [1, 0, 1]:
                await asyncio.sleep(0.01)
            await reports.put(OSError(errno.ENODEV, "detached"))
            while device.task is not None and not device.task.done():
                await asyncio.sleep(0.01)
    finally:
        await device.release()
    assert key_a() == [1, 0, 1, 0]
    assert device.uinput is None


async def test_hid_bpf_attachment_survives_a_brief_release_without_false_gaps(monkeypatch):
    from keymasq.keymasqd.input_sources import manager as manager_module

    attaches = 0
    closed = asyncio.Event()
    snapshots: asyncio.Queue[bytes] = asyncio.Queue()

    async def reports(_binding):
        nonlocal attaches
        attaches += 1
        try:
            while True:
                yield await snapshots.get()
        finally:
            closed.set()

    monkeypatch.setattr(manager_module.hid_bpf, "reports", reports)
    monkeypatch.setitem(manager_module.LINGER_S, "hid-bpf", 0.2)
    sources = SourceManager()
    binding = deck_binding()
    async with sources.subscribe(binding) as first:
        await snapshots.put(touch_state(0x01))
        await first.read()
        await asyncio.sleep(0.15)
        await snapshots.put(touch_state(0x41))
        frame = await first.read()
        assert frame.values["left_stick_touch"] == 1
        assert not frame.discontinuity
    async with sources.subscribe(binding) as second:
        assert (await second.read()).values["left_stick_touch"] == 1
    assert attaches == 1
    await asyncio.wait_for(closed.wait(), 1.0)


async def test_combo_capture_records_touch_inputs_without_an_active_mapping(monkeypatch):
    from keymasq.keymasqd import capture_manager as capture_module
    from keymasq.keymasqd import daemon_capture_commands
    from keymasq.keymasqd.runtime import device_path_resolver as resolver

    reports: asyncio.Queue[bytes] = asyncio.Queue()
    closed = asyncio.Event()

    async def reader(_path):
        try:
            while True:
                yield await reports.get()
        finally:
            closed.set()

    shared = SourceManager(reader)
    source = deck_binding()
    monkeypatch.setattr("keymasq.keymasqd.input_sources.manager.source_manager", lambda: shared)
    monkeypatch.setattr(capture_module, "binding_for_path", lambda _path: source)
    monkeypatch.setattr(capture_module.evdev, "list_devices", lambda: [])
    monkeypatch.setattr(
        resolver,
        "resolve_evdev_interfaces",
        lambda *_args, **_kwargs: [
            resolver.ResolvedInterface(
                source.path, "keymasq-source:input", "input", DeviceType.OTHER, []
            )
        ],
    )
    daemon = SimpleNamespace(
        device_manager=SimpleNamespace(
            grabbed_devices={},
            begin_combo_capture=Mock(),
            end_combo_capture=Mock(),
            read_combo_capture=Mock(return_value={"event": None}),
        ),
        capture_manager=capture_module.CaptureManager(),
    )
    touch_source = {
        "id": "input",
        "path": "keymasq-source:input",
        "type": "other",
        "backend": "hid-bpf",
        "driver": "steam-deck-touch",
    }
    capture = asyncio.create_task(
        daemon_capture_commands.capture_combo(
            daemon,
            {"28de:1205@2"},
            2.0,
            hardware_interfaces={"28de:1205@2": [touch_source]},
        )
    )
    for touch in (0x81, 0x01):
        await reports.put(touch_state(touch))
    result = await asyncio.wait_for(capture, 2.0)
    assert result["events"] == [
        {"evdev": "btn_touch_rs", "hardware_id": "28de:1205@2", "source": "input"}
    ]
    await asyncio.wait_for(closed.wait(), 1.0)
