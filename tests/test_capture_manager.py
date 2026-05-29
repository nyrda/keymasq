from typing import cast
from unittest.mock import Mock

import evdev
import pytest

from keymasq.common.devices import detect_input_classes, primary_input_class
from keymasq.keymasqd.capture_manager import CaptureManager
from keymasq.keymasqd.runtime import device_path_resolver


class _FakeInfo:
    def __init__(self, vendor: int, product: int) -> None:
        self.vendor = vendor
        self.product = product


class _FakeDevice:
    def __init__(
        self, path: str, vendor: int, product: int, events: list[evdev.InputEvent]
    ) -> None:
        self.path = path
        self.name = "Fake Device"
        self.info = _FakeInfo(vendor, product)
        self._events = list(events)
        self.grabbed = False
        self.closed = False
        self.close_count = 0
        self.fd = hash(path) & 0xFFFF
        self._absinfo = {
            evdev.ecodes.ABS_X: evdev.AbsInfo(0, -32768, 32767, 0, 0, 0),
            evdev.ecodes.ABS_Y: evdev.AbsInfo(0, -32768, 32767, 0, 0, 0),
        }

    def grab(self) -> None:
        self.grabbed = True

    def ungrab(self) -> None:
        self.grabbed = False

    def close(self) -> None:
        self.closed = True
        self.close_count += 1

    def capabilities(self):
        return {evdev.ecodes.EV_KEY: [evdev.ecodes.KEY_A, evdev.ecodes.KEY_LEFTCTRL]}

    def read(self):
        while self._events:
            yield self._events.pop(0)

    def read_one(self):
        if not self._events:
            return None
        return self._events.pop(0)

    def absinfo(self, axis: int):
        return self._absinfo[axis]


def test_capture_manager_begin_read_end(monkeypatch) -> None:
    keyboard_event = evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1)
    fake = _FakeDevice("/dev/input/event1", 0x1234, 0x5678, [keyboard_event])

    def fake_list_devices():
        return ["/dev/input/event1"]

    def fake_input_device(path: str):
        assert path == "/dev/input/event1"
        return fake

    monkeypatch.setattr(evdev, "list_devices", fake_list_devices)
    monkeypatch.setattr(evdev, "InputDevice", fake_input_device)

    manager = CaptureManager()
    begin = manager.begin("1234:5678")
    token = begin["token"]
    assert token

    first = manager.read(token)
    captured = cast(dict[str, object], first["captured"])
    assert captured["evdev"] == "key_a"

    second = manager.read(token)
    assert second["captured"] is None

    ended = manager.end(token)
    assert ended["ended"] is True


def test_capture_manager_begin_can_target_explicit_paths(monkeypatch) -> None:
    wanted_event = evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1)
    wanted = _FakeDevice("/dev/input/event2", 0x1234, 0x5678, [wanted_event])
    other = _FakeDevice("/dev/input/event1", 0x1234, 0x5678, [])

    def fake_input_device(path: str):
        if path == "/dev/input/event2":
            return wanted
        if path == "/dev/input/event1":
            return other
        raise OSError("missing")

    monkeypatch.setattr(evdev, "InputDevice", fake_input_device)

    manager = CaptureManager()
    begin = manager.begin("1234:5678@slot2", ["/dev/input/event2"])
    token = str(begin["token"])

    assert wanted.grabbed is True
    assert other.grabbed is False

    captured = cast(dict[str, object], manager.read(token)["captured"])
    assert captured["evdev"] == "key_a"


def test_capture_manager_resolves_keymasq_paths(monkeypatch) -> None:
    key_event = evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1)
    fake = _FakeDevice("/dev/input/event7", 0x2DC8, 0x3106, [key_event])

    monkeypatch.setattr(evdev, "list_devices", lambda: ["/dev/input/event7"])
    monkeypatch.setattr(evdev, "InputDevice", lambda path: fake)
    device_path_resolver.refresh_cached_devices_sync(
        device_paths_fn=evdev.list_devices,
        device_input_fn=evdev.InputDevice,
        detect_input_classes_fn=detect_input_classes,
        primary_input_class_fn=primary_input_class,
    )

    manager = CaptureManager()
    token: str | None = None
    try:
        begin = manager.begin(
            "2dc8:3106",
            evdev_interfaces=[
                {
                    "id": "gamepad",
                    "path": "keymasq:2dc8:3106",
                    "type": "keyboard",
                    "capabilities": ["key_a"],
                }
            ],
        )
        token = str(begin["token"])

        captured = cast(dict[str, object], manager.read(token)["captured"])
        assert captured["evdev"] == "key_a"
        assert captured["source"] == "gamepad"
    finally:
        if token is not None:
            manager.end(token)
        device_path_resolver.clear_cached_devices()


def test_capture_manager_analog_mode_reads_abs_events(monkeypatch) -> None:
    abs_event = evdev.InputEvent(0, 0, evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X, 12000)
    key_event = evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1)
    fake = _FakeDevice("/dev/input/event2", 0x1234, 0x5678, [key_event, abs_event])

    monkeypatch.setattr(evdev, "InputDevice", lambda path: fake)

    manager = CaptureManager()
    begin = manager.begin("1234:5678", ["/dev/input/event2"], mode="analog")
    token = str(begin["token"])

    assert manager.read(token)["captured"] is None
    captured = cast(dict[str, object], manager.read(token)["captured"])
    assert captured["evdev"] == "abs_x"
    assert captured["code"] == evdev.ecodes.ABS_X
    assert captured["value"] == 12000
    assert captured["absinfo"] == {
        "value": 0,
        "minimum": -32768,
        "maximum": 32767,
        "fuzz": 0,
        "flat": 0,
        "resolution": 0,
    }


def test_capture_manager_begin_numbered_hardware_id_falls_back_to_model_id(monkeypatch) -> None:
    keyboard_event = evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1)
    fake = _FakeDevice("/dev/input/event1", 0x045E, 0x02A1, [keyboard_event])

    monkeypatch.setattr(evdev, "list_devices", lambda: ["/dev/input/event1"])
    monkeypatch.setattr(evdev, "InputDevice", lambda path: fake)

    manager = CaptureManager()
    begin = manager.begin("045e:02a1@2")

    assert begin["hardware_id"] == "045e:02a1@2"
    assert fake.grabbed is True


def test_capture_manager_end_invalid_token_is_safe() -> None:
    manager = CaptureManager()
    result = manager.end("missing")
    assert result == {"status": "ok", "ended": False}


def test_capture_manager_combo_begin_read_end(monkeypatch) -> None:
    press_event = evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1)
    release_event = evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0)
    fake = _FakeDevice("/dev/input/event2", 0x1234, 0x5678, [press_event, release_event])

    def fake_list_devices():
        return ["/dev/input/event2"]

    def fake_input_device(path: str):
        assert path == "/dev/input/event2"
        return fake

    def fake_start_combo_reader(self, session) -> None:
        for event in session.devices[0].read():
            parsed = self._parse_combo_event(session.devices[0], event)
            if parsed is not None and session.event_queue is not None:
                session.event_queue.put(parsed)

    monkeypatch.setattr(evdev, "list_devices", fake_list_devices)
    monkeypatch.setattr(evdev, "InputDevice", fake_input_device)
    monkeypatch.setattr(CaptureManager, "_start_combo_reader", fake_start_combo_reader)

    manager = CaptureManager()
    begin = manager.begin_combo(authorization=manager._authorize_combo_capture())
    token = begin["token"]

    first = manager.read_combo(token)
    second = manager.read_combo(token)
    ended = manager.end(token)

    first_event = cast(dict[str, object], first["event"])
    second_event = cast(dict[str, object], second["event"])
    assert first_event["evdev"] == "key_a"
    assert first_event["value"] == 1
    assert second_event["value"] == 0
    assert ended == {"status": "ok", "ended": True}
    assert fake.closed is True


def test_capture_manager_combo_reader_notifies_async_waiter(monkeypatch) -> None:
    press_event = evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1)
    fake = _FakeDevice("/dev/input/event2", 0x1234, 0x5678, [press_event])

    monkeypatch.setattr(evdev, "list_devices", lambda: ["/dev/input/event2"])
    monkeypatch.setattr(evdev, "InputDevice", lambda path: fake)
    monkeypatch.setattr(CaptureManager, "_start_combo_reader", lambda self, session: None)

    manager = CaptureManager()
    begin = manager.begin_combo(authorization=manager._authorize_combo_capture())
    token = begin["token"]
    session = manager._sessions[token]
    session.notify_loop = Mock()
    session.notify_event = Mock()
    session.stop_event = Mock()
    session.stop_event.is_set.side_effect = [False, True]

    monkeypatch.setattr("select.select", lambda _r, _w, _x, _timeout: ([fake.fd], [], []))

    manager._combo_reader_loop(session)
    manager.end(token)

    session.notify_loop.call_soon_threadsafe.assert_called_once_with(session.notify_event.set)


def test_capture_manager_begin_reports_grab_warnings(monkeypatch) -> None:
    busy = _FakeDevice("/dev/input/event1", 0x1234, 0x5678, [])
    denied = _FakeDevice("/dev/input/event2", 0x1234, 0x5678, [])

    def _grab_busy() -> None:
        raise OSError(16, "busy")

    def _grab_denied() -> None:
        raise OSError(13, "denied")

    busy.grab = _grab_busy
    denied.grab = _grab_denied

    devices = {
        busy.path: busy,
        denied.path: denied,
    }
    monkeypatch.setattr(evdev, "list_devices", lambda: list(devices))
    monkeypatch.setattr(evdev, "InputDevice", lambda path: devices[path])

    manager = CaptureManager()
    with pytest.raises(RuntimeError, match="No readable/grabbable interfaces found"):
        manager.begin("1234:5678")

    assert busy.closed is True
    assert denied.closed is True
    assert busy.close_count == 1
    assert denied.close_count == 1


def test_capture_manager_begin_closes_failed_grab_devices_with_partial_success(
    monkeypatch,
) -> None:
    grabbed = _FakeDevice("/dev/input/event1", 0x1234, 0x5678, [])
    busy = _FakeDevice("/dev/input/event2", 0x1234, 0x5678, [])

    def _grab_busy() -> None:
        raise OSError(16, "busy")

    busy.grab = _grab_busy

    devices = {
        grabbed.path: grabbed,
        busy.path: busy,
    }
    monkeypatch.setattr(evdev, "list_devices", lambda: list(devices))
    monkeypatch.setattr(evdev, "InputDevice", lambda path: devices[path])

    manager = CaptureManager()
    begin = manager.begin("1234:5678")

    assert begin["warnings"] == ["/dev/input/event2: busy"]
    assert grabbed.closed is False
    assert busy.closed is True
    assert busy.close_count == 1

    manager.end(str(begin["token"]))
    assert grabbed.closed is True


def test_capture_manager_find_devices_closes_nonmatching_devices(monkeypatch) -> None:
    wanted = _FakeDevice("/dev/input/event1", 0x1234, 0x5678, [])
    other = _FakeDevice("/dev/input/event2", 0x0001, 0x0002, [])
    devices = {
        wanted.path: wanted,
        other.path: other,
    }
    monkeypatch.setattr(evdev, "list_devices", lambda: list(devices))
    monkeypatch.setattr(evdev, "InputDevice", lambda path: devices[path])

    matched = CaptureManager()._find_devices("1234", "5678")

    assert matched == [wanted]
    assert wanted.closed is False
    assert other.closed is True
    assert other.close_count == 1


def test_capture_manager_begin_combo_allow_empty_and_read_nowait(monkeypatch) -> None:
    monkeypatch.setattr(evdev, "list_devices", lambda: [])

    manager = CaptureManager()
    begin = manager.begin_combo(
        allow_empty=True,
        authorization=manager._authorize_combo_capture(),
    )

    assert begin["warnings"] == []
    assert manager.read_combo_nowait(begin["token"]) == {"event": None}


def test_capture_manager_begin_combo_requires_authorization(monkeypatch) -> None:
    monkeypatch.setattr(evdev, "list_devices", lambda: [])

    manager = CaptureManager()

    with pytest.raises(PermissionError, match="missing authorization"):
        manager.begin_combo(allow_empty=True)


def test_capture_manager_begin_combo_rejects_duplicate_token(monkeypatch) -> None:
    monkeypatch.setattr(evdev, "list_devices", lambda: [])

    manager = CaptureManager()
    manager.begin_combo(
        token="same",
        allow_empty=True,
        authorization=manager._authorize_combo_capture(),
    )

    with pytest.raises(ValueError, match="Capture token already active"):
        manager.begin_combo(
            token="same",
            allow_empty=True,
            authorization=manager._authorize_combo_capture(),
        )

    assert manager.end("same") == {"status": "ok", "ended": True}


def test_capture_manager_begin_combo_closes_devices_on_duplicate_token(monkeypatch) -> None:
    opened: list[_FakeDevice] = []

    def fake_input_device(path: str) -> _FakeDevice:
        device = _FakeDevice(path, 0x1234, 0x5678, [])
        opened.append(device)
        return device

    monkeypatch.setattr(evdev, "list_devices", lambda: ["/dev/input/event1"])
    monkeypatch.setattr(evdev, "InputDevice", fake_input_device)

    manager = CaptureManager()
    manager.begin_combo(
        token="same",
        authorization=manager._authorize_combo_capture(),
    )

    with pytest.raises(ValueError, match="Capture token already active"):
        manager.begin_combo(
            token="same",
            authorization=manager._authorize_combo_capture(),
        )

    assert len(opened) == 2
    assert opened[0].closed is False
    assert opened[1].closed is True
    assert manager.end("same") == {"status": "ok", "ended": True}
    assert opened[0].closed is True


def test_capture_manager_register_notifier_invalid_token() -> None:
    manager = CaptureManager()

    with pytest.raises(ValueError, match="Invalid capture token"):
        manager.register_combo_notifier("missing", Mock(), Mock())


def test_capture_manager_parse_helpers(monkeypatch) -> None:
    manager = CaptureManager()
    device = _FakeDevice("/dev/input/event3", 0x1234, 0x5678, [])

    monkeypatch.setattr(
        "keymasq.keymasqd.capture_manager.resolve_stable_path",
        lambda path: f"/stable{path}",
    )
    monkeypatch.setattr(
        "keymasq.keymasqd.capture_manager.get_interface_id",
        lambda stable_path: f"iface:{stable_path.rsplit('/', 1)[-1]}",
    )

    wheel = manager._parse_event(
        device,
        evdev.InputEvent(0, 0, evdev.ecodes.EV_REL, evdev.ecodes.REL_WHEEL, -1),
    )
    hwheel = manager._parse_event(
        device,
        evdev.InputEvent(0, 0, evdev.ecodes.EV_REL, evdev.ecodes.REL_HWHEEL, 3),
    )
    hi_res_wheel = manager._parse_event(
        device,
        evdev.InputEvent(
            0,
            0,
            evdev.ecodes.EV_REL,
            evdev.ecodes.REL_WHEEL_HI_RES,
            -120,
        ),
    )
    combo_press = manager._parse_combo_event(
        device,
        evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
    )
    mapped_combo_press = manager._parse_combo_event(
        device,
        evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
        {"/stable/dev/input/event3": "1234:5678@2"},
    )
    combo_wheel = manager._parse_combo_event(
        device,
        evdev.InputEvent(0, 0, evdev.ecodes.EV_REL, evdev.ecodes.REL_WHEEL, -1),
    )
    combo_unsupported = manager._parse_combo_event(
        device,
        evdev.InputEvent(0, 0, evdev.ecodes.EV_REL, evdev.ecodes.REL_X, 1),
    )

    assert wheel == {
        "evdev": "rel_wheel",
        "code": evdev.ecodes.REL_WHEEL,
        "direction": "down",
        "value": -1,
        "source": "iface:event3",
        "stable_path": "/stable/dev/input/event3",
        "device_path": "/dev/input/event3",
    }
    assert hwheel is not None
    assert combo_press is not None
    assert hwheel["direction"] == "right"
    assert hwheel["code"] == evdev.ecodes.REL_HWHEEL
    assert hwheel["value"] == 1
    assert hi_res_wheel is None
    assert combo_press == {
        "evdev": "key_a",
        "code": evdev.ecodes.KEY_A,
        "value": 1,
        "hardware_id": "1234:5678",
        "source": "iface:event3",
        "stable_path": "/stable/dev/input/event3",
        "device_path": "/dev/input/event3",
    }
    assert mapped_combo_press is not None
    assert mapped_combo_press["hardware_id"] == "1234:5678@2"
    assert combo_wheel == {
        "evdev": "wheel_down",
        "code": evdev.ecodes.REL_WHEEL,
        "value": 1,
        "hardware_id": "1234:5678",
        "source": "iface:event3",
        "stable_path": "/stable/dev/input/event3",
        "device_path": "/dev/input/event3",
    }
    assert combo_unsupported is None


def test_capture_manager_find_combo_devices_filters_inputs(monkeypatch) -> None:
    manager = CaptureManager()

    excluded = _FakeDevice("/dev/input/event1", 0x1234, 0x5678, [])
    virtual = _FakeDevice("/dev/input/event2", 0x1234, 0x5678, [])
    virtual.name = "keymasq-virtual"
    wrong_hwid = _FakeDevice("/dev/input/event3", 0x0001, 0x0002, [])
    no_keys = _FakeDevice("/dev/input/event4", 0x1234, 0x5678, [])
    no_keys.capabilities = lambda: {evdev.ecodes.EV_KEY: [999999]}
    good = _FakeDevice("/dev/input/event5", 0x1234, 0x5678, [])

    devices = {
        excluded.path: excluded,
        virtual.path: virtual,
        wrong_hwid.path: wrong_hwid,
        no_keys.path: no_keys,
        good.path: good,
    }
    monkeypatch.setattr(evdev, "list_devices", lambda: list(devices))
    monkeypatch.setattr(evdev, "InputDevice", lambda path: devices[path])

    matched = manager._find_combo_devices(
        exclude_paths={excluded.path},
        hardware_ids={"1234:5678"},
    )

    assert matched == [good]
    assert excluded.closed is False
    assert virtual.closed is True
    assert wrong_hwid.closed is True
    assert no_keys.closed is True
    assert good.closed is False
    assert virtual.close_count == 1
    assert wrong_hwid.close_count == 1
    assert no_keys.close_count == 1

    matched_by_path = manager._find_combo_devices(
        exclude_paths=set(),
        hardware_ids={"0000:0000"},
        path_hardware_ids={"/dev/input/event5": "1234:5678@2"},
    )

    assert matched_by_path == [good]


def test_capture_manager_parse_hardware_id_rejects_invalid_value() -> None:
    manager = CaptureManager()

    with pytest.raises(ValueError, match="Invalid hardware_id"):
        manager._parse_hardware_id("1234")


def test_capture_manager_parse_hardware_id_strips_duplicate_suffix() -> None:
    manager = CaptureManager()

    assert manager._parse_hardware_id("045E:02A1@2") == ("045e", "02a1")
