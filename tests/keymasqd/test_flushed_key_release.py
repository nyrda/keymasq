"""EVIOCGKEY drops unread key events; the reader reconciles without losing any."""

import asyncio
import socket
from collections import deque
from types import SimpleNamespace

import evdev
import pytest

from keymasq.keymasqd.runtime.grabbed_device import grab
from keymasq.keymasqd.runtime.grabbed_device.event.input_stream import read_events

KEY = evdev.ecodes.EV_KEY
SYN = evdev.ecodes.EV_SYN
REL = evdev.ecodes.EV_REL
KEY_L = evdev.ecodes.KEY_L
BTN_LEFT = evdev.ecodes.BTN_LEFT


def _event(kind: int, code: int, value: int) -> SimpleNamespace:
    return SimpleNamespace(type=kind, code=code, value=value)


def _triples(events) -> list[tuple[int, int, int]]:
    return [(int(e.type), int(e.code), int(e.value)) for e in events]


class _KernelDevice:
    """An evdev client queue with the kernel's key-state query semantics."""

    def __init__(self, *, down: set[int] | None = None) -> None:
        self.down = set(down or ())
        self.queue: deque[SimpleNamespace] = deque()
        self.queries = 0
        # Events the kernel delivers after the reader's last read but before the query.
        self.arrive_before_query: list[SimpleNamespace] = []
        self._rx, self._tx = socket.socketpair()
        self._rx.setblocking(False)

    def close(self) -> None:
        self._rx.close()
        self._tx.close()

    def fileno(self) -> int:
        return self._rx.fileno()

    def feed(self, *events: SimpleNamespace) -> None:
        for event in events:
            if event.type == KEY:
                (self.down.discard if event.value == 0 else self.down.add)(event.code)
            self.queue.append(event)
        self._tx.send(b"x")

    def read_one(self) -> SimpleNamespace | None:
        if not self.queue:
            try:
                self._rx.recv(4096)
            except BlockingIOError:
                pass
            return None
        return self.queue.popleft()

    def active_keys(self) -> list[int]:
        self.queries += 1
        self.feed(*self.arrive_before_query)
        self.arrive_before_query = []
        self.queue = deque(event for event in self.queue if event.type != KEY)
        return sorted(self.down)


def _runtime(device: _KernelDevice, *, held: set[str] | None = None) -> SimpleNamespace:
    held = set(held or ())
    return SimpleNamespace(
        device=device,
        running=True,
        hardware_id="1234:5678",
        mapping_getter=lambda: {},
        event_binding_to_button={},
        event_code_to_button={},
        evdev_to_button={},
        state=SimpleNamespace(
            input_event_buffer=deque(),
            analog_deferred_keys=[],
            held_source_keys=set(held),
            held_source_actions=dict.fromkeys(held),
            input_event_ready=None,
            key_state_reconcile_requested=False,
        ),
    )


@pytest.fixture
def kernel_device():
    device = _KernelDevice()
    yield device
    device.close()


def test_unread_release_survives_the_query(kernel_device) -> None:
    kernel_device.down = {KEY_L}
    runtime = _runtime(kernel_device, held={"key_l"})
    kernel_device.feed(_event(KEY, KEY_L, 0), _event(SYN, 0, 0))

    grab.reconcile_live_key_state(runtime)  # type: ignore[arg-type]

    assert _triples(runtime.state.input_event_buffer) == [(KEY, KEY_L, 0), (SYN, 0, 0)]


def test_release_arriving_between_the_last_read_and_the_query_is_repaired(kernel_device) -> None:
    kernel_device.down = {KEY_L}
    runtime = _runtime(kernel_device, held={"key_l"})
    kernel_device.arrive_before_query = [_event(KEY, KEY_L, 0), _event(SYN, 0, 0)]

    grab.reconcile_live_key_state(runtime)  # type: ignore[arg-type]

    # The flushed release is replayed; its own sync report survived the flush.
    assert _triples(runtime.state.input_event_buffer) == [(KEY, KEY_L, 0), (SYN, 0, 0)]


@pytest.mark.parametrize("pending_buffer", ["input_event_buffer", "analog_deferred_keys"])
def test_buffered_press_that_is_not_tracked_yet_gets_its_lost_release(
    kernel_device, pending_buffer: str
) -> None:
    runtime = _runtime(kernel_device)
    getattr(runtime.state, pending_buffer).append(_event(KEY, KEY_L, 1))
    kernel_device.down = {KEY_L}
    kernel_device.arrive_before_query = [_event(KEY, KEY_L, 0)]

    grab.reconcile_live_key_state(runtime)  # type: ignore[arg-type]

    assert _triples(runtime.state.input_event_buffer)[-2:] == [(KEY, KEY_L, 0), (SYN, 0, 0)]
    assert "key_l" not in runtime.state.held_source_actions


@pytest.mark.parametrize("pending_buffer", ["input_event_buffer", "analog_deferred_keys"])
def test_older_buffered_release_does_not_hide_a_later_lost_release(
    kernel_device, pending_buffer: str
) -> None:
    runtime = _runtime(kernel_device, held={"key_l"})
    getattr(runtime.state, pending_buffer).extend([_event(KEY, KEY_L, 0), _event(KEY, KEY_L, 1)])
    kernel_device.down = {KEY_L}
    kernel_device.arrive_before_query = [_event(KEY, KEY_L, 0)]

    grab.reconcile_live_key_state(runtime)  # type: ignore[arg-type]

    assert _triples(runtime.state.input_event_buffer)[-2:] == [(KEY, KEY_L, 0), (SYN, 0, 0)]


def test_lost_press_after_a_buffered_release_is_replayed_after_it(kernel_device) -> None:
    runtime = _runtime(kernel_device, held={"key_l"})
    runtime.state.input_event_buffer.append(_event(KEY, KEY_L, 0))
    kernel_device.arrive_before_query = [_event(KEY, KEY_L, 1)]

    grab.reconcile_live_key_state(runtime)  # type: ignore[arg-type]

    assert _triples(runtime.state.input_event_buffer) == [
        (KEY, KEY_L, 0),
        (KEY, KEY_L, 1),
        (SYN, 0, 0),
    ]


def test_query_is_deferred_while_the_queue_cannot_be_drained(kernel_device, monkeypatch) -> None:
    monkeypatch.setattr(grab, "KEY_STATE_DRAIN_LIMIT", 2)
    kernel_device.down = {KEY_L}
    runtime = _runtime(kernel_device, held={"key_l"})
    kernel_device.feed(
        _event(REL, evdev.ecodes.REL_X, 1),
        _event(SYN, 0, 0),
        _event(KEY, KEY_L, 0),
        _event(SYN, 0, 0),
    )

    grab.reconcile_live_key_state(runtime)  # type: ignore[arg-type]

    # Querying now would discard the unread release, so it has to wait.
    assert kernel_device.queries == 0
    assert runtime.state.key_state_reconcile_requested
    assert len(runtime.state.input_event_buffer) == 2


@pytest.mark.asyncio
async def test_reader_delivers_a_deferred_batch_before_it_queries_again(
    kernel_device, monkeypatch
) -> None:
    monkeypatch.setattr(grab, "KEY_STATE_DRAIN_LIMIT", 2)
    kernel_device.down = {KEY_L}
    runtime = _runtime(kernel_device, held={"key_l"})
    reader = read_events(runtime)  # type: ignore[arg-type]
    kernel_device.feed(_event(SYN, 0, 0))
    await asyncio.wait_for(anext(reader), timeout=1.0)
    kernel_device.feed(
        _event(REL, evdev.ecodes.REL_X, 1),
        _event(SYN, 0, 0),
        _event(KEY, KEY_L, 0),
        _event(SYN, 0, 0),
    )
    grab.request_key_state_reconcile(runtime)  # type: ignore[arg-type]

    delivered = [await asyncio.wait_for(anext(reader), timeout=1.0) for _ in range(4)]
    await reader.aclose()

    assert _triples(delivered) == [
        (REL, evdev.ecodes.REL_X, 1),
        (SYN, 0, 0),
        (KEY, KEY_L, 0),
        (SYN, 0, 0),
    ]


def test_single_buffered_release_is_not_repaired_twice(kernel_device) -> None:
    runtime = _runtime(kernel_device, held={"key_l"})
    runtime.state.input_event_buffer.append(_event(KEY, KEY_L, 0))

    grab.reconcile_live_key_state(runtime)  # type: ignore[arg-type]

    assert _triples(runtime.state.input_event_buffer) == [(KEY, KEY_L, 0)]


def test_keys_that_are_still_down_stay_held(kernel_device) -> None:
    kernel_device.down = {KEY_L}
    runtime = _runtime(kernel_device, held={"key_l"})

    grab.reconcile_live_key_state(runtime)  # type: ignore[arg-type]

    assert not runtime.state.input_event_buffer
    assert runtime.state.held_source_keys == {"key_l"}


def test_key_tracked_by_number_is_repaired(kernel_device) -> None:
    unnamed_code = 0x2FE
    assert unnamed_code not in evdev.ecodes.bytype[KEY]
    runtime = _runtime(kernel_device, held={str(unnamed_code)})

    grab.reconcile_live_key_state(runtime)  # type: ignore[arg-type]

    assert _triples(runtime.state.input_event_buffer) == [(KEY, unnamed_code, 0), (SYN, 0, 0)]


def test_physically_held_key_nobody_tracks_is_seeded(kernel_device) -> None:
    kernel_device.down = {KEY_L}
    runtime = _runtime(kernel_device)

    grab.reconcile_live_key_state(runtime)  # type: ignore[arg-type]

    assert "key_l" in runtime.state.held_source_keys
    assert "key_l" in runtime.state.held_source_actions


def test_request_without_a_reader_queries_right_away(kernel_device) -> None:
    kernel_device.down = {KEY_L}
    runtime = _runtime(kernel_device)

    grab.request_key_state_reconcile(runtime)  # type: ignore[arg-type]

    assert kernel_device.queries == 1
    assert not runtime.state.key_state_reconcile_requested
    assert "key_l" in runtime.state.held_source_keys


@pytest.mark.asyncio
async def test_reader_keeps_unread_motion_ahead_of_a_button_release(kernel_device) -> None:
    kernel_device.down = {BTN_LEFT}
    runtime = _runtime(kernel_device, held={"btn_left"})
    reader = read_events(runtime)  # type: ignore[arg-type]
    kernel_device.feed(_event(SYN, 0, 0))
    await asyncio.wait_for(anext(reader), timeout=1.0)

    kernel_device.feed(
        _event(REL, evdev.ecodes.REL_X, 20),
        _event(SYN, 0, 0),
        _event(KEY, BTN_LEFT, 0),
        _event(SYN, 0, 0),
    )
    grab.request_key_state_reconcile(runtime)  # type: ignore[arg-type]
    delivered = [await asyncio.wait_for(anext(reader), timeout=1.0) for _ in range(4)]
    await reader.aclose()

    assert _triples(delivered) == [
        (REL, evdev.ecodes.REL_X, 20),
        (SYN, 0, 0),
        (KEY, BTN_LEFT, 0),
        (SYN, 0, 0),
    ]


@pytest.mark.asyncio
async def test_request_wakes_a_parked_reader_and_delivers_the_repair(kernel_device) -> None:
    kernel_device.down = {KEY_L}
    runtime = _runtime(kernel_device, held={"key_l"})
    reader = read_events(runtime)  # type: ignore[arg-type]
    parked = asyncio.create_task(anext(reader))
    await asyncio.sleep(0.01)
    assert not parked.done()

    kernel_device.arrive_before_query = [_event(KEY, KEY_L, 0)]
    grab.request_key_state_reconcile(runtime)  # type: ignore[arg-type]
    first = await asyncio.wait_for(parked, timeout=1.0)
    await reader.aclose()

    assert _triples([first]) == [(KEY, KEY_L, 0)]


@pytest.mark.asyncio
async def test_query_waits_until_the_event_in_flight_is_fully_processed(kernel_device) -> None:
    kernel_device.down = {KEY_L}
    runtime = _runtime(kernel_device, held={"key_l"})
    reader = read_events(runtime)  # type: ignore[arg-type]
    kernel_device.feed(_event(KEY, KEY_L, 0))
    in_flight = await asyncio.wait_for(anext(reader), timeout=1.0)
    assert _triples([in_flight]) == [(KEY, KEY_L, 0)]

    # The pipeline is suspended inside this release when the mapping is replaced.
    grab.request_key_state_reconcile(runtime)  # type: ignore[arg-type]
    await asyncio.sleep(0.01)
    assert kernel_device.queries == 0

    # The release finishes and lets go of its retained action; only then the
    # reader queries, and there is nothing left to replay against the new mapping.
    runtime.state.held_source_keys.clear()
    runtime.state.held_source_actions.clear()
    parked = asyncio.create_task(anext(reader))
    await asyncio.sleep(0.01)
    parked.cancel()
    await asyncio.gather(parked, return_exceptions=True)

    assert kernel_device.queries == 1
    assert not runtime.state.input_event_buffer
