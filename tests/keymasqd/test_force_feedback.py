import asyncio
import ctypes
import errno
from collections.abc import Callable
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import evdev
import pytest

from keymasq.common.models import DeviceType
from keymasq.keymasqd.runtime import grabbed_device as gdm
from keymasq.keymasqd.runtime.force_feedback import (
    PassthroughForceFeedbackProxy,
    disable_force_feedback,
    passthrough_ff_max_effects,
)
from keymasq.keymasqd.runtime.grabbed_device import GrabbedDevice


class _Upload:
    def __init__(self, effect_id: int) -> None:
        self.request_id = 0
        self.retval = 0
        self.effect = evdev.ff.Effect()
        self.effect.type = evdev.ecodes.FF_RUMBLE
        self.effect.id = effect_id


class _Erase:
    def __init__(self, effect_id: int) -> None:
        self.request_id = 0
        self.retval = 0
        self.effect_id = effect_id


class _FakeDll:
    def __init__(self, owner: "_FakeUInput") -> None:
        self.owner = owner

    def _uinput_begin_upload(self, fd: int, upload_ptr: object) -> int:
        assert fd == self.owner.fd
        upload = ctypes.cast(upload_ptr, ctypes.POINTER(evdev.ff.UInputUpload)).contents
        template = self.owner.uploads[int(upload.request_id)]
        upload.effect.type = template.effect.type
        upload.effect.id = template.effect.id
        return 0

    def _uinput_begin_erase(self, fd: int, erase_ptr: object) -> int:
        assert fd == self.owner.fd
        erase = ctypes.cast(erase_ptr, ctypes.POINTER(evdev.ff.UInputErase)).contents
        template = self.owner.erases[int(erase.request_id)]
        erase.effect_id = int(template.effect_id)
        return 0


class _FakeUInput:
    fd = 42

    def __init__(self) -> None:
        self.dll = _FakeDll(self)
        self.uploads: dict[int, _Upload] = {}
        self.erases: dict[int, _Erase] = {}
        self.ended_uploads: list[evdev.ff.UInputUpload] = []
        self.ended_erases: list[evdev.ff.UInputErase] = []
        self.events: list[object] = []
        self.end_upload_error: Exception | None = None
        self.end_upload_hook: Callable[[evdev.ff.UInputUpload], None] | None = None

    def read(self):
        events = list(self.events)
        self.events.clear()
        return events

    def end_upload(self, upload: evdev.ff.UInputUpload) -> None:
        self.ended_uploads.append(upload)
        if self.end_upload_hook is not None:
            self.end_upload_hook(upload)
        if self.end_upload_error is not None:
            raise self.end_upload_error

    def end_erase(self, erase: evdev.ff.UInputErase) -> None:
        self.ended_erases.append(erase)


class _FakePhysicalDevice:
    path = "/dev/input/event-physical"
    ff_effects_count = 8

    def __init__(self) -> None:
        self.next_effect_id = 23
        self.upload_ids: list[int] = []
        self.erased_ids: list[int] = []
        self.writes: list[tuple[int, int, int]] = []
        self.upload_error: OSError | None = None

    def upload_effect(self, effect: object) -> int:
        if self.upload_error is not None:
            raise self.upload_error
        self.upload_ids.append(int(cast(evdev.ff.Effect, effect).id))
        effect_id = self.next_effect_id
        self.next_effect_id += 1
        return effect_id

    def erase_effect(self, ff_id: int) -> None:
        self.erased_ids.append(int(ff_id))

    def write(self, event_type: int, code: int, value: int) -> None:
        self.writes.append((int(event_type), int(code), int(value)))


def _event(event_type: int, code: int, value: int) -> object:
    return SimpleNamespace(type=event_type, code=code, value=value)


def test_upload_play_replace_and_erase_translate_effect_ids() -> None:
    uinput = _FakeUInput()
    physical = _FakePhysicalDevice()
    proxy = PassthroughForceFeedbackProxy(uinput, physical, label="test")
    uinput.uploads[100] = _Upload(effect_id=7)

    proxy.handle_event(_event(evdev.ecodes.EV_UINPUT, evdev.ecodes.UI_FF_UPLOAD, 100))

    assert physical.upload_ids == [-1]
    assert len(uinput.ended_uploads) == 1
    assert uinput.ended_uploads[0].request_id == 100
    assert uinput.ended_uploads[0].retval == 0
    assert proxy.effect_mappings[7].physical_id == 23

    proxy.handle_event(_event(evdev.ecodes.EV_FF, 7, 1))
    assert physical.writes == [(evdev.ecodes.EV_FF, 23, 1)]

    uinput.uploads[101] = _Upload(effect_id=7)
    proxy.handle_event(_event(evdev.ecodes.EV_UINPUT, evdev.ecodes.UI_FF_UPLOAD, 101))

    assert physical.upload_ids == [-1, 23]
    assert len(uinput.ended_uploads) == 2
    assert uinput.ended_uploads[1].request_id == 101
    assert uinput.ended_uploads[1].retval == 0
    assert proxy.effect_mappings[7].physical_id == 24

    uinput.erases[200] = _Erase(effect_id=7)
    proxy.handle_event(_event(evdev.ecodes.EV_UINPUT, evdev.ecodes.UI_FF_ERASE, 200))

    assert physical.erased_ids == [24]
    assert len(uinput.ended_erases) == 1
    assert uinput.ended_erases[0].request_id == 200
    assert uinput.ended_erases[0].retval == 0
    assert 7 not in proxy.effect_mappings


def test_upload_failure_sets_negative_errno_and_does_not_record_mapping() -> None:
    uinput = _FakeUInput()
    physical = _FakePhysicalDevice()
    physical.upload_error = OSError(errno.ENOSPC, "no slots")
    proxy = PassthroughForceFeedbackProxy(uinput, physical, label="test")
    uinput.uploads[100] = _Upload(effect_id=7)

    proxy.handle_event(_event(evdev.ecodes.EV_UINPUT, evdev.ecodes.UI_FF_UPLOAD, 100))

    assert len(uinput.ended_uploads) == 1
    assert uinput.ended_uploads[0].retval == -errno.ENOSPC
    assert proxy.effect_mappings == {}


def test_upload_mapping_is_available_before_ack_returns() -> None:
    uinput = _FakeUInput()
    physical = _FakePhysicalDevice()
    proxy = PassthroughForceFeedbackProxy(uinput, physical, label="test")
    uinput.uploads[100] = _Upload(effect_id=7)

    def play_during_ack(_upload: evdev.ff.UInputUpload) -> None:
        proxy.handle_event(_event(evdev.ecodes.EV_FF, 7, 1))

    uinput.end_upload_hook = play_during_ack

    proxy.handle_event(_event(evdev.ecodes.EV_UINPUT, evdev.ecodes.UI_FF_UPLOAD, 100))

    assert physical.writes == [(evdev.ecodes.EV_FF, 23, 1)]
    assert proxy.effect_mappings[7].physical_id == 23


def test_upload_end_failure_rolls_back_physical_effect() -> None:
    uinput = _FakeUInput()
    physical = _FakePhysicalDevice()
    proxy = PassthroughForceFeedbackProxy(uinput, physical, label="test")
    uinput.uploads[100] = _Upload(effect_id=7)
    uinput.end_upload_error = OSError(errno.EIO, "end failed")

    proxy.handle_event(_event(evdev.ecodes.EV_UINPUT, evdev.ecodes.UI_FF_UPLOAD, 100))

    assert physical.upload_ids == [-1]
    assert physical.erased_ids == [23]
    assert proxy.effect_mappings == {}


def test_global_force_feedback_events_forward_without_translation() -> None:
    uinput = _FakeUInput()
    physical = _FakePhysicalDevice()
    proxy = PassthroughForceFeedbackProxy(uinput, physical, label="test")

    proxy.handle_event(_event(evdev.ecodes.EV_FF, evdev.ecodes.FF_GAIN, 40))
    proxy.handle_event(_event(evdev.ecodes.EV_FF, evdev.ecodes.FF_AUTOCENTER, 1))

    assert physical.writes == [
        (evdev.ecodes.EV_FF, evdev.ecodes.FF_GAIN, 40),
        (evdev.ecodes.EV_FF, evdev.ecodes.FF_AUTOCENTER, 1),
    ]


@pytest.mark.asyncio
async def test_force_feedback_play_and_global_writes_use_thread_adapter() -> None:
    uinput = _FakeUInput()
    physical = _FakePhysicalDevice()
    runtime = _InlineAsyncioRuntime()
    proxy = PassthroughForceFeedbackProxy(
        uinput,
        physical,
        label="test",
        asyncio_mod=runtime,  # type: ignore[arg-type]
    )
    uinput.uploads[100] = _Upload(effect_id=7)
    proxy.handle_event(_event(evdev.ecodes.EV_UINPUT, evdev.ecodes.UI_FF_UPLOAD, 100))
    runtime.to_thread_calls.clear()

    proxy.handle_event(_event(evdev.ecodes.EV_FF, evdev.ecodes.FF_GAIN, 40))
    proxy.handle_event(_event(evdev.ecodes.EV_FF, 7, 1))

    assert physical.writes == []

    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert runtime.to_thread_calls == [
        "_write_physical_ff_sync",
        "_write_physical_ff_sync",
    ]
    assert physical.writes == [
        (evdev.ecodes.EV_FF, evdev.ecodes.FF_GAIN, 40),
        (evdev.ecodes.EV_FF, 23, 1),
    ]


def test_passthrough_ff_capability_helpers() -> None:
    caps = {
        evdev.ecodes.EV_KEY: [evdev.ecodes.BTN_SOUTH],
        evdev.ecodes.EV_FF: [evdev.ecodes.FF_RUMBLE],
    }
    physical = _FakePhysicalDevice()

    assert passthrough_ff_max_effects(caps, physical) == 8

    disable_force_feedback(caps)
    assert evdev.ecodes.EV_FF not in caps
    assert passthrough_ff_max_effects(caps, physical) == 0


def test_passthrough_uinput_kwargs_can_omit_unsupported_max_effects() -> None:
    kwargs = gdm._passthrough_uinput_kwargs(
        caps={evdev.ecodes.EV_KEY: [evdev.ecodes.BTN_SOUTH]},
        passthrough_name="pad",
        passthrough_vendor=None,
        passthrough_product=None,
        passthrough_version=None,
        passthrough_bustype=None,
        passthrough_input_props=None,
        ff_max_effects=0,
        supports_max_effects=False,
    )

    assert "max_effects" not in kwargs


def test_uinput_supports_max_effects_detects_evdev_constructor_shapes() -> None:
    class _Evdev16StyleUInput:
        def __init__(self, *, events=None, name="py-evdev-uinput", input_props=None) -> None:
            self.events = events
            self.name = name
            self.input_props = input_props

    class _Evdev17StyleUInput:
        def __init__(
            self,
            *,
            events=None,
            name="py-evdev-uinput",
            input_props=None,
            max_effects=96,
        ) -> None:
            self.events = events
            self.name = name
            self.input_props = input_props
            self.max_effects = max_effects

    assert gdm._uinput_supports_max_effects(_Evdev16StyleUInput) is False
    assert gdm._uinput_supports_max_effects(_Evdev17StyleUInput) is True


class _FakeLoop:
    def __init__(self) -> None:
        self.reader: tuple[int, Callable[[], object]] | None = None
        self.removed: list[int] = []

    def add_reader(self, fd: int, callback: Callable[[], object]) -> None:
        self.reader = (fd, callback)

    def remove_reader(self, fd: int) -> bool:
        self.removed.append(int(fd))
        return True


class _InlineAsyncioRuntime:
    def __init__(self) -> None:
        self.loop = _FakeLoop()
        self.to_thread_calls: list[str] = []

    def get_running_loop(self) -> _FakeLoop:
        return self.loop

    def create_task(self, coro):
        return asyncio.create_task(coro)

    async def to_thread(self, func, /, *args: object, **kwargs: object):
        self.to_thread_calls.append(str(getattr(func, "__name__", "")))
        return func(*args, **kwargs)


class _DelayedAsyncioRuntime(_InlineAsyncioRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.to_thread_started = asyncio.Event()
        self.finish_to_thread = asyncio.Event()

    async def to_thread(self, func, /, *args: object, **kwargs: object):
        self.to_thread_calls.append(str(getattr(func, "__name__", "")))
        self.to_thread_started.set()
        await self.finish_to_thread.wait()
        return func(*args, **kwargs)


@pytest.mark.asyncio
async def test_upload_requests_are_queued_to_worker_thread_adapter() -> None:
    uinput = _FakeUInput()
    physical = _FakePhysicalDevice()
    runtime = _InlineAsyncioRuntime()
    proxy = PassthroughForceFeedbackProxy(
        uinput,
        physical,
        label="test",
        asyncio_mod=runtime,  # type: ignore[arg-type]
    )
    uinput.uploads[100] = _Upload(effect_id=7)

    proxy.start()
    assert runtime.loop.reader is not None
    _fd, callback = runtime.loop.reader
    uinput.events.append(_event(evdev.ecodes.EV_UINPUT, evdev.ecodes.UI_FF_UPLOAD, 100))
    callback()

    assert physical.upload_ids == []

    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert runtime.to_thread_calls == ["_handle_upload"]
    assert physical.upload_ids == [-1]
    assert proxy.effect_mappings[7].physical_id == 23

    proxy.stop()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_stop_and_wait_drains_inflight_worker_before_returning() -> None:
    uinput = _FakeUInput()
    physical = _FakePhysicalDevice()
    runtime = _DelayedAsyncioRuntime()
    proxy = PassthroughForceFeedbackProxy(
        uinput,
        physical,
        label="test",
        asyncio_mod=runtime,  # type: ignore[arg-type]
    )
    uinput.uploads[100] = _Upload(effect_id=7)

    proxy.start()
    assert runtime.loop.reader is not None
    _fd, callback = runtime.loop.reader
    uinput.events.append(_event(evdev.ecodes.EV_UINPUT, evdev.ecodes.UI_FF_UPLOAD, 100))
    callback()
    await runtime.to_thread_started.wait()

    stop_task = asyncio.create_task(proxy.stop_and_wait())
    await asyncio.sleep(0)

    assert runtime.loop.removed == [uinput.fd]
    assert stop_task.done() is False
    assert physical.upload_ids == []

    runtime.finish_to_thread.set()
    await stop_task

    assert physical.upload_ids == [-1]
    assert proxy.effect_mappings[7].physical_id == 23


@pytest.mark.asyncio
async def test_stop_and_wait_propagates_worker_cancellation_after_write_tasks() -> None:
    proxy = PassthroughForceFeedbackProxy(
        _FakeUInput(),
        _FakePhysicalDevice(),
        label="test",
    )
    worker_task = asyncio.create_task(asyncio.sleep(60))
    write_done = asyncio.Event()
    release_write = asyncio.Event()

    async def pending_write() -> None:
        await release_write.wait()
        write_done.set()

    write_task = asyncio.create_task(pending_write())
    proxy._worker_task = worker_task
    proxy._write_tasks.add(write_task)

    stop_task = asyncio.create_task(proxy.stop_and_wait())
    await asyncio.sleep(0)
    worker_task.cancel()
    await asyncio.sleep(0)

    assert stop_task.done() is False
    assert write_done.is_set() is False

    release_write.set()
    with pytest.raises(asyncio.CancelledError):
        await stop_task

    assert write_done.is_set()


def test_read_eintr_does_not_stop_proxy() -> None:
    class _EintrUInput(_FakeUInput):
        def read(self):
            raise OSError(errno.EINTR, "interrupted")

    proxy = PassthroughForceFeedbackProxy(
        _EintrUInput(),
        _FakePhysicalDevice(),
        label="test",
    )
    proxy._running = True
    proxy._fd = 42

    proxy._on_readable()

    assert proxy._running is True


@pytest.mark.asyncio
async def test_grab_starts_passthrough_force_feedback_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
    monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "pad")
    monkeypatch.setattr(gdm.source_hiding, "node_kernel_names", lambda _path: [])
    monkeypatch.setattr(gdm.source_hiding, "hide_source", AsyncMock(return_value=[]))

    class _LifecycleInputDevice:
        ff_effects_count = 4
        path = "/dev/input/event-pad"
        name = "Pad"
        info = SimpleNamespace(vendor=0x1234, product=0x5678, version=1, bustype=3)

        def capabilities(self) -> dict[int, list[int]]:
            return {
                evdev.ecodes.EV_KEY: [evdev.ecodes.BTN_SOUTH],
                evdev.ecodes.EV_FF: [evdev.ecodes.FF_RUMBLE],
                evdev.ecodes.EV_SYN: [],
            }

        def input_props(self) -> list[int]:
            return []

        def active_keys(self) -> list[int]:
            return []

        def grab(self) -> None:
            return

        def ungrab(self) -> None:
            return

        def close(self) -> None:
            return

    class _LifecycleUInput:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    class _Proxy:
        instances: list["_Proxy"] = []

        def __init__(self, uinput, physical_device, *, label: str) -> None:
            self.uinput = uinput
            self.physical_device = physical_device
            self.label = label
            self.started = False
            self.stopped = False
            _Proxy.instances.append(self)

        def start(self) -> None:
            self.started = True

        def stop(self) -> None:
            self.stopped = True

    created_uinputs: list[_LifecycleUInput] = []
    original_create_task = gdm.asyncio.create_task
    original_sleep = gdm.asyncio.sleep

    def fake_create_task(coro):
        coro.close()
        return original_create_task(original_sleep(0))

    def fake_uinput(**kwargs) -> _LifecycleUInput:
        uinput = _LifecycleUInput(**kwargs)
        created_uinputs.append(uinput)
        return uinput

    physical = _LifecycleInputDevice()
    monkeypatch.setattr(gdm.evdev, "InputDevice", lambda _path: physical)
    monkeypatch.setattr(gdm.evdev, "UInput", fake_uinput)
    monkeypatch.setattr(gdm.asyncio, "create_task", fake_create_task)
    monkeypatch.setattr(gdm.runtime_force_feedback, "PassthroughForceFeedbackProxy", _Proxy)

    device = GrabbedDevice(
        path="/dev/input/event-pad",
        hardware_id="1234:5678",
        button_map={},
        mapping_getter=lambda: {},
        event_callback=AsyncMock(return_value=None),
        device_type=DeviceType.GAMEPAD,
        device_types=[DeviceType.GAMEPAD.value],
    )

    await device.grab()
    await original_sleep(0)

    assert len(created_uinputs) == 1
    assert created_uinputs[0].kwargs["events"][evdev.ecodes.EV_FF] == [evdev.ecodes.FF_RUMBLE]
    assert created_uinputs[0].kwargs["max_effects"] == 4
    assert len(_Proxy.instances) == 1
    assert _Proxy.instances[0].uinput is created_uinputs[0]
    assert _Proxy.instances[0].physical_device is physical
    assert _Proxy.instances[0].started is True

    await device.release()

    assert _Proxy.instances[0].stopped is True
    assert created_uinputs[0].close_calls == 1


@pytest.mark.asyncio
async def test_grab_retries_without_force_feedback_when_proxy_start_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
    monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "pad")
    monkeypatch.setattr(gdm.source_hiding, "node_kernel_names", lambda _path: [])
    monkeypatch.setattr(gdm.source_hiding, "hide_source", AsyncMock(return_value=[]))

    class _LifecycleInputDevice:
        ff_effects_count = 4
        path = "/dev/input/event-pad"
        name = "Pad"
        info = SimpleNamespace(vendor=0x1234, product=0x5678, version=1, bustype=3)

        def capabilities(self) -> dict[int, list[int]]:
            return {
                evdev.ecodes.EV_KEY: [evdev.ecodes.BTN_SOUTH],
                evdev.ecodes.EV_FF: [evdev.ecodes.FF_RUMBLE],
                evdev.ecodes.EV_SYN: [],
            }

        def input_props(self) -> list[int]:
            return []

        def active_keys(self) -> list[int]:
            return []

        def grab(self) -> None:
            return

    class _LifecycleUInput:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    class _FailingProxy:
        def __init__(self, _uinput, _physical_device, *, label: str) -> None:
            self.label = label

        def start(self) -> None:
            raise RuntimeError("reader unsupported")

    created_uinputs: list[_LifecycleUInput] = []
    original_create_task = gdm.asyncio.create_task
    original_sleep = gdm.asyncio.sleep

    def fake_create_task(coro):
        coro.close()
        return original_create_task(original_sleep(0))

    def fake_uinput(**kwargs) -> _LifecycleUInput:
        uinput = _LifecycleUInput(**kwargs)
        created_uinputs.append(uinput)
        return uinput

    monkeypatch.setattr(gdm.evdev, "InputDevice", lambda _path: _LifecycleInputDevice())
    monkeypatch.setattr(gdm.evdev, "UInput", fake_uinput)
    monkeypatch.setattr(gdm.asyncio, "create_task", fake_create_task)
    monkeypatch.setattr(gdm.runtime_force_feedback, "PassthroughForceFeedbackProxy", _FailingProxy)

    device = GrabbedDevice(
        path="/dev/input/event-pad",
        hardware_id="1234:5678",
        button_map={},
        mapping_getter=lambda: {},
        event_callback=AsyncMock(return_value=None),
        device_type=DeviceType.GAMEPAD,
        device_types=[DeviceType.GAMEPAD.value],
    )

    await device.grab()
    await original_sleep(0)

    assert len(created_uinputs) == 2
    assert evdev.ecodes.EV_FF in created_uinputs[0].kwargs["events"]
    assert created_uinputs[0].close_calls == 1
    assert evdev.ecodes.EV_FF not in created_uinputs[1].kwargs["events"]
    assert created_uinputs[1].kwargs["max_effects"] == 0
    assert device.force_feedback_proxy is None
