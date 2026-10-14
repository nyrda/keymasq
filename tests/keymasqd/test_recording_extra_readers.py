import asyncio
import gc
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import evdev
import pytest
from evdev import eventio_async

from keymasq.keymasqd import recording
from keymasq.keymasqd.recording import RecordingManager


class _PipeInputDevice(eventio_async.EventIO):
    def __init__(self) -> None:
        self.fd, self._write_fd = os.pipe()
        os.set_blocking(self.fd, False)
        self.close_count = 0
        self.reader_registered_at_close: bool | None = None
        self.closed_after: list[bool] = []
        self.reader_task: asyncio.Task[object] | None = None

    def push(self, *codes: int) -> None:
        os.write(self._write_fd, bytes(codes))

    def read(self) -> Iterator[evdev.InputEvent]:
        for code in os.read(self.fd, 4096):
            yield evdev.InputEvent(1, code, evdev.ecodes.EV_KEY, code, 1)

    def close(self) -> None:
        self.close_count += 1
        if self.reader_task is not None:
            self.closed_after.append(self.reader_task.done())
        if self.fd < 0:
            return
        self.reader_registered_at_close = asyncio.get_running_loop().remove_reader(self.fd)
        super().close()
        os.close(self.fd)
        self.fd = -1

    def release(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1
        os.close(self._write_fd)


@pytest.fixture
def pipe_device(monkeypatch: pytest.MonkeyPatch) -> Iterator[_PipeInputDevice]:
    device = _PipeInputDevice()
    monkeypatch.setattr(recording.evdev, "InputDevice", lambda _path: device)
    yield device
    device.release()


@pytest.fixture
async def loop_errors() -> AsyncIterator[list[dict[str, object]]]:
    loop = asyncio.get_running_loop()
    errors: list[dict[str, object]] = []
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    yield errors
    loop.set_exception_handler(None)


async def _start(recorder: RecordingManager) -> None:
    await recorder.start([{"path": "/dev/input/event10", "device_type": "keyboard"}])


async def _wait_for_event_count(recorder: RecordingManager, count: int) -> None:
    async with asyncio.timeout(1.0):
        while recorder._spool is None or recorder._spool.event_count < count:
            await asyncio.sleep(0.001)


async def _recorded_codes(recorder: RecordingManager, result: dict[str, object]) -> list[object]:
    snapshot = await recorder.pending_recording(str(result["pending_recording_id"]))
    return [event["code"] for event in snapshot.iter_events()]


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)
    gc.collect()
    await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("queued_input", [False, True])
async def test_stop_closes_extra_device_only_after_its_reader_is_unregistered(
    tmp_path: Path,
    pipe_device: _PipeInputDevice,
    loop_errors: list[dict[str, object]],
    queued_input: bool,
) -> None:
    recorder = RecordingManager(spool_dir=tmp_path)
    await _start(recorder)
    pipe_device.push(evdev.ecodes.KEY_A)
    await _wait_for_event_count(recorder, 1)

    if queued_input:
        pipe_device.push(evdev.ecodes.KEY_B)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    concurrent_stop = asyncio.create_task(recorder.stop())
    result = await recorder.stop()
    concurrent = await concurrent_stop
    await _settle()

    assert loop_errors == []
    assert pipe_device.close_count == 1
    assert pipe_device.reader_registered_at_close is False
    assert await _recorded_codes(recorder, result) == [evdev.ecodes.KEY_A]
    assert concurrent == {"status": "ok"}


@pytest.mark.asyncio
async def test_stop_called_from_an_extra_reader_closes_its_device_after_the_reader_ends(
    tmp_path: Path,
    pipe_device: _PipeInputDevice,
    loop_errors: list[dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = RecordingManager(spool_dir=tmp_path)
    stopped: list[dict[str, object]] = []

    async def reader_that_stops(device: _PipeInputDevice, _device_types: list[str]) -> None:
        device.reader_task = asyncio.current_task()
        stopped.append(await recorder.stop())

    monkeypatch.setattr(recorder, "_read_extra_device", reader_that_stops)
    await _start(recorder)
    await _settle()

    assert loop_errors == []
    assert len(stopped) == 1
    assert "pending_recording_id" in stopped[0]
    assert pipe_device.closed_after == [True]


@pytest.mark.asyncio
async def test_stop_closes_extra_device_when_its_reader_fails_on_cancellation(
    tmp_path: Path,
    pipe_device: _PipeInputDevice,
    loop_errors: list[dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = RecordingManager(spool_dir=tmp_path)

    async def reader_failing_on_cancel(device: _PipeInputDevice, _device_types: list[str]) -> None:
        device.reader_task = asyncio.current_task()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise RuntimeError("reader cleanup failed") from None

    monkeypatch.setattr(recorder, "_read_extra_device", reader_failing_on_cancel)
    await _start(recorder)
    await asyncio.sleep(0)
    result = await recorder.stop()
    await _settle()

    assert loop_errors == []
    assert "pending_recording_id" in result
    assert pipe_device.closed_after == [True]
