"""EVIOCGKEY drops unread key events; a mapping update must replay lost releases."""

import asyncio
from collections import deque
from types import SimpleNamespace

import evdev
import pytest

from keymasq.keymasqd.runtime.grabbed_device import grab
from keymasq.keymasqd.runtime.grabbed_device.event import pipeline
from keymasq.keymasqd.runtime.grabbed_device.event.input_stream import queue_input_events
from tests.keymasqd.device_manager_support import (
    grabbed_event_processing_deps,
    make_combo_runtime_setup,
)

HARDWARE_ID = "1234:5678"
MAPPING = {"key_l": {"action": "keyboard", "target": "key_play"}}


class _KeyStateDevice:
    def __init__(self, active: list[int]) -> None:
        self.active = active

    def active_keys(self) -> list[int]:
        return list(self.active)


def _key(code: int, value: int) -> SimpleNamespace:
    return SimpleNamespace(type=evdev.ecodes.EV_KEY, code=code, value=value)


def _key_writes(uinput) -> list[tuple[int, int]]:
    return [(code, value) for kind, code, value in uinput.writes if kind == evdev.ecodes.EV_KEY]


async def _drain_buffer(device) -> None:
    deps = grabbed_event_processing_deps()
    while device.state.input_event_buffer:
        await pipeline.process_event(device, device.state.input_event_buffer.popleft(), deps=deps)


@pytest.mark.asyncio
async def test_mapping_update_replays_release_flushed_by_key_state_query(monkeypatch) -> None:
    setup = await make_combo_runtime_setup(
        monkeypatch, [], hardware_id=HARDWARE_ID, button_map={"key_l": "key_l"}
    )
    manager, device, keyboard = setup.manager, setup.device, setup.keyboard
    device.mapping_getter = lambda: manager.active_mappings.get(HARDWARE_ID, {})
    await manager.set_mapping(HARDWARE_ID, MAPPING)
    await pipeline.process_event(
        device, _key(evdev.ecodes.KEY_L, 1), deps=grabbed_event_processing_deps()
    )

    # The release was unread when the update queried the key state, so the
    # kernel discarded it and reports the key as up.
    device.device = _KeyStateDevice(active=[])  # type: ignore[assignment]
    ready = asyncio.Event()
    device.state.input_event_ready = ready
    await manager.set_mapping(HARDWARE_ID, MAPPING)

    assert ready.is_set()
    await _drain_buffer(device)
    assert _key_writes(keyboard) == [(evdev.ecodes.KEY_PLAY, 1), (evdev.ecodes.KEY_PLAY, 0)]
    assert "key_l" not in device.state.held_source_actions
    assert "key_l" not in device.state.held_source_keys


@pytest.mark.asyncio
async def test_mapping_update_keeps_keys_that_are_still_down(monkeypatch) -> None:
    setup = await make_combo_runtime_setup(
        monkeypatch, [], hardware_id=HARDWARE_ID, button_map={"key_l": "key_l"}
    )
    manager, device, keyboard = setup.manager, setup.device, setup.keyboard
    device.mapping_getter = lambda: manager.active_mappings.get(HARDWARE_ID, {})
    await manager.set_mapping(HARDWARE_ID, MAPPING)
    await pipeline.process_event(
        device, _key(evdev.ecodes.KEY_L, 1), deps=grabbed_event_processing_deps()
    )

    device.device = _KeyStateDevice(active=[evdev.ecodes.KEY_L])  # type: ignore[assignment]
    await manager.set_mapping(HARDWARE_ID, MAPPING)

    assert not device.state.input_event_buffer
    assert _key_writes(keyboard) == [(evdev.ecodes.KEY_PLAY, 1)]


def test_release_already_read_from_the_kernel_is_not_replayed(monkeypatch) -> None:
    device = SimpleNamespace(
        device=_KeyStateDevice(active=[]),
        state=SimpleNamespace(
            input_event_buffer=[_key(evdev.ecodes.KEY_L, 0)],
            analog_deferred_keys=[],
            held_source_keys={"key_l"},
            held_source_actions={"key_l": None},
            input_event_ready=None,
        ),
    )

    grab.queue_flushed_key_releases(device, set())  # type: ignore[arg-type]

    assert len(device.state.input_event_buffer) == 1


def test_release_is_replayed_for_a_key_tracked_by_number() -> None:
    unnamed_code = 0x2FE
    assert unnamed_code not in evdev.ecodes.bytype[evdev.ecodes.EV_KEY]
    device = SimpleNamespace(
        device=_KeyStateDevice(active=[]),
        state=SimpleNamespace(
            input_event_buffer=deque(),
            analog_deferred_keys=[],
            held_source_keys={str(unnamed_code)},
            held_source_actions={},
            input_event_ready=None,
        ),
    )

    grab.queue_flushed_key_releases(device, set())  # type: ignore[arg-type]

    queued = [(int(e.type), int(e.code), int(e.value)) for e in device.state.input_event_buffer]
    assert queued == [
        (evdev.ecodes.EV_KEY, unnamed_code, 0),
        (evdev.ecodes.EV_SYN, 0, 0),
    ]


def test_queue_input_events_wakes_a_parked_reader() -> None:
    ready = asyncio.Event()
    runtime = SimpleNamespace(
        state=SimpleNamespace(input_event_buffer=deque(), input_event_ready=ready)
    )

    queue_input_events(runtime, [_key(evdev.ecodes.KEY_L, 0)])  # type: ignore[arg-type]

    assert ready.is_set()
    assert len(runtime.state.input_event_buffer) == 1
