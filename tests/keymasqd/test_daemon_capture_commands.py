import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import evdev
import pytest

from keymasq.common.devices import detect_input_classes, primary_input_class
from keymasq.common.ipc import CommandType
from keymasq.common.security import SecurityPolicy
from keymasq.keymasqd import capture_manager as capture_manager_module
from keymasq.keymasqd import daemon as daemon_module
from keymasq.keymasqd import daemon_capture_commands
from keymasq.keymasqd.capture_manager import CaptureManager
from keymasq.keymasqd.runtime import device_path_resolver
from keymasq.keymasqd.runtime.macro.options import MacroPlaybackOptions
from tests.keymasqd.daemon_support import macro_meta

_PENDING_RECORDING_EVENTS: list[dict[str, object]] = [
    {"type": 1, "code": 30, "value": 1, "t_us": 0}
]


class _PendingRecordingSnapshot:
    def __init__(
        self,
        *,
        recording_id: str = "recording-1",
        recording_slot: int | None = None,
        duration_ms: int = 5,
        device_types: list[str] | None = None,
        events: list[dict[str, object]] | None = None,
    ) -> None:
        self.recording_id = recording_id
        if recording_slot is not None:
            self.recording_slot = recording_slot
        self.duration_ms = duration_ms
        self.device_types = device_types if device_types is not None else ["keyboard"]
        self._events = list(events if events is not None else _PENDING_RECORDING_EVENTS)
        self.event_count = len(self._events)

    def iter_events(self):
        yield from self._events


@pytest.mark.asyncio
async def test_macro_play_by_name_loads_store_and_forwards_runtime_options(daemon_testbed):
    daemon, device_manager, _recording_manager, macro_store, _capture_manager = daemon_testbed
    macro_store.get_meta.return_value = macro_meta()

    result = await daemon._handle_command(
        CommandType.MACRO_PLAY_BY_NAME,
        {
            "name": "combo",
            "speed": "2.5",
            "replay_mouse_movement": False,
            "replay_mouse_clicks": True,
        },
    )

    assert result == {"played": True}
    macro_store.get_meta.assert_called_once_with("combo")
    macro_store.get.assert_not_called()
    device_manager.play_macro.assert_awaited_once_with(
        MacroPlaybackOptions(
            macro_events=[],
            macro_name="combo",
            replay_mouse_movement=False,
            replay_mouse_clicks=True,
            speed=2.5,
            loop_mode="count",
            loop_count=3,
            loop_stop_behavior="cancel_run",
            move_to_start=True,
            start_x=111,
            start_y=222,
            block_mouse_movement=True,
            source_device="",
            source_button="",
            trigger_value=1,
            load_stored_macro=True,
        )
    )


@pytest.mark.asyncio
async def test_macro_list_recordings_returns_pending_slot_meta(daemon_testbed):
    daemon, _device_manager, recording_manager, _macro_store, _capture_manager = daemon_testbed
    recording_manager.list_pending_recordings.return_value = [
        {
            "pending_recording_id": "recording-2",
            "recording_slot": 2,
            "duration_ms": 100,
        }
    ]

    result = await daemon._handle_command(CommandType.MACRO_LIST_RECORDINGS, {})

    assert result == {
        "recordings": [
            {
                "pending_recording_id": "recording-2",
                "recording_slot": 2,
                "duration_ms": 100,
            }
        ]
    }
    recording_manager.list_pending_recordings.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_macro_play_payload_loads_store_and_forwards_runtime_options(daemon_testbed):
    daemon, device_manager, _recording_manager, macro_store, _capture_manager = daemon_testbed
    macro_store.get_meta.return_value = macro_meta()

    result = await daemon._handle_command(
        CommandType.PLAY_MACRO,
        {
            "macro_name": "combo",
            "speed": "2.5",
            "replay_mouse_movement": False,
            "replay_mouse_clicks": True,
            "source_device": "kbd",
            "source_button": "a",
            "trigger_value": "0",
        },
    )

    assert result == {"played": True}
    macro_store.get_meta.assert_called_once_with("combo")
    macro_store.get.assert_not_called()
    device_manager.play_macro.assert_awaited_once_with(
        MacroPlaybackOptions(
            macro_events=[],
            macro_name="combo",
            replay_mouse_movement=False,
            replay_mouse_clicks=True,
            speed=2.5,
            loop_mode="count",
            loop_count=3,
            loop_stop_behavior="cancel_run",
            move_to_start=True,
            start_x=111,
            start_y=222,
            block_mouse_movement=True,
            source_device="kbd",
            source_button="a",
            trigger_value=0,
            load_stored_macro=True,
        )
    )


@pytest.mark.parametrize(
    ("command_type", "data"),
    [
        (
            CommandType.MACRO_PLAY_BY_NAME,
            {
                "name": "combo",
                "loop_mode": "none",
                "loop_count": 1,
                "loop_stop_behavior": "finish_run",
                "move_to_start": False,
                "start_x": 9,
                "start_y": 10,
                "block_mouse_movement": False,
            },
        ),
        (
            CommandType.PLAY_MACRO,
            {
                "macro_name": "combo",
                "loop_mode": "none",
                "loop_count": 1,
                "loop_stop_behavior": "finish_run",
                "move_to_start": False,
                "start_x": 9,
                "start_y": 10,
                "block_mouse_movement": False,
            },
        ),
    ],
)
@pytest.mark.asyncio
async def test_macro_play_request_runtime_options_override_stored_options(
    daemon_testbed,
    command_type,
    data,
):
    daemon, device_manager, _recording_manager, macro_store, _capture_manager = daemon_testbed
    macro_store.get_meta.return_value = macro_meta()

    result = await daemon._handle_command(command_type, data)

    assert result == {"played": True}
    options = device_manager.play_macro.await_args.args[0]
    assert isinstance(options, MacroPlaybackOptions)
    assert options.macro_name == "combo"
    assert options.loop_mode == "none"
    assert options.loop_count == 1
    assert options.loop_stop_behavior == "finish_run"
    assert options.move_to_start is False
    assert options.start_x == 9
    assert options.start_y == 10
    assert options.block_mouse_movement is False


@pytest.mark.asyncio
async def test_macro_save_recording_claims_snapshot_and_restores_slot(daemon_testbed):
    daemon, _device_manager, recording_manager, macro_store, _capture_manager = daemon_testbed
    stored_events: list[dict[str, object]] = []

    def create_from_events(payload, events, *, return_full: bool = False):
        assert payload["name"] == "saved"
        assert payload["duration_us"] == 5000
        assert return_full is False
        stored_events.extend(events)
        return {"name": payload["name"]}

    recording_manager.claim_pending_recording.return_value = _PendingRecordingSnapshot(
        recording_slot=2
    )
    macro_store.create_from_events.side_effect = create_from_events

    result = await daemon._handle_command(
        CommandType.MACRO_SAVE_RECORDING,
        {"pending_recording_id": "recording-1", "name": "saved"},
    )

    assert result == {"macro": {"name": "saved"}}
    recording_manager.claim_pending_recording.assert_awaited_once_with("recording-1")
    recording_manager.release_pending_recording_claim.assert_awaited_once_with(
        "recording-1",
        saved=False,
    )
    recording_manager.discard_pending_recording.assert_not_awaited()
    assert stored_events == [{"type": 1, "code": 30, "value": 1, "t_us": 0}]


@pytest.mark.asyncio
async def test_macro_save_recording_does_not_create_start_move_on_save(daemon_testbed):
    daemon, _device_manager, recording_manager, macro_store, _capture_manager = daemon_testbed
    stored_payloads: list[dict[str, object]] = []
    stored_events: list[dict[str, object]] = []

    def create_from_events(payload, events, *, return_full: bool = False):
        stored_payloads.append(dict(payload))
        stored_events.extend(events)
        return {"name": payload["name"]}

    recording_manager.claim_pending_recording.return_value = _PendingRecordingSnapshot()
    macro_store.create_from_events.side_effect = create_from_events

    result = await daemon._handle_command(
        CommandType.MACRO_SAVE_RECORDING,
        {
            "pending_recording_id": "recording-1",
            "name": "saved",
            "start_x": 123,
            "start_y": 456,
        },
    )

    assert result == {"macro": {"name": "saved"}}
    assert stored_payloads[0]["event_count"] == 1
    assert stored_payloads[0]["device_types"] == ["keyboard"]
    assert "move_to_start" not in stored_payloads[0]
    assert "start_x" not in stored_payloads[0]
    assert "start_y" not in stored_payloads[0]
    assert stored_events == [{"type": 1, "code": 30, "value": 1, "t_us": 0}]


@pytest.mark.asyncio
async def test_macro_save_recording_releases_unslotted_snapshot_as_saved(daemon_testbed):
    daemon, _device_manager, recording_manager, macro_store, _capture_manager = daemon_testbed

    recording_manager.claim_pending_recording.return_value = _PendingRecordingSnapshot()
    macro_store.create_from_events.return_value = {"name": "saved"}

    result = await daemon._handle_command(
        CommandType.MACRO_SAVE_RECORDING,
        {"pending_recording_id": "recording-1", "name": "saved"},
    )

    assert result == {"macro": {"name": "saved"}}
    recording_manager.release_pending_recording_claim.assert_awaited_once_with(
        "recording-1",
        saved=True,
    )


@pytest.mark.asyncio
async def test_macro_save_recording_keeps_failed_unslotted_snapshot_retryable(
    daemon_testbed,
):
    daemon, _device_manager, recording_manager, macro_store, _capture_manager = daemon_testbed

    recording_manager.claim_pending_recording.return_value = _PendingRecordingSnapshot()
    macro_store.create_from_events.side_effect = RuntimeError("duplicate macro")

    with pytest.raises(RuntimeError, match="duplicate macro"):
        await daemon._handle_command(
            CommandType.MACRO_SAVE_RECORDING,
            {"pending_recording_id": "recording-1", "name": "saved"},
        )

    recording_manager.release_pending_recording_claim.assert_awaited_once_with(
        "recording-1",
        saved=False,
    )


@pytest.mark.asyncio
async def test_macro_play_recording_claims_snapshot_and_releases_without_saving(
    daemon_testbed,
):
    daemon, device_manager, recording_manager, _macro_store, _capture_manager = daemon_testbed

    recording_manager.claim_pending_recording.return_value = _PendingRecordingSnapshot()

    result = await daemon._handle_command(
        CommandType.MACRO_PLAY_RECORDING,
        {
            "pending_recording_id": "recording-1",
            "macro_name": "recording-slot-4",
            "speed": "1.5",
            "source_device": "kbd",
            "source_button": "key_f13",
            "trigger_value": "1",
        },
    )

    assert result == {"played": True}
    recording_manager.claim_pending_recording.assert_awaited_once_with("recording-1")
    recording_manager.release_pending_recording_claim.assert_awaited_once_with(
        "recording-1",
        saved=False,
    )
    device_manager.play_macro.assert_awaited_once()
    options = device_manager.play_macro.await_args.args[0]
    assert isinstance(options, MacroPlaybackOptions)
    assert options.macro_events == [{"type": 1, "code": 30, "value": 1, "t_us": 0}]
    assert options.macro_name == "recording-slot-4"
    assert options.speed == 1.5
    assert options.source_device == "kbd"
    assert options.source_button == "key_f13"
    assert options.trigger_value == 1
    assert options.load_stored_macro is False


@pytest.mark.asyncio
async def test_macro_play_recording_does_not_create_start_move_on_play(daemon_testbed):
    daemon, device_manager, recording_manager, _macro_store, _capture_manager = daemon_testbed

    recording_manager.claim_pending_recording.return_value = _PendingRecordingSnapshot()

    result = await daemon._handle_command(
        CommandType.MACRO_PLAY_RECORDING,
        {
            "pending_recording_id": "recording-1",
            "start_x": 123,
            "start_y": 456,
        },
    )

    assert result == {"played": True}
    options = device_manager.play_macro.await_args.args[0]
    assert isinstance(options, MacroPlaybackOptions)
    assert options.move_to_start is False
    assert options.macro_events == [{"type": 1, "code": 30, "value": 1, "t_us": 0}]
    assert options.load_stored_macro is False


@pytest.mark.asyncio
async def test_start_recording_resolves_recording_ids_before_start(daemon_testbed):
    daemon, device_manager, recording_manager, _macro_store, _capture_manager = daemon_testbed
    selected = {
        "path": "/dev/input/event10",
        "recording_id": "keymasq:passthrough:1234:5678:mouse",
        "recording_kind": "keymasq_passthrough",
        "device_type": "mouse",
        "device_types": ["mouse"],
    }
    device_manager.list_devices.return_value = {
        "devices": [
            selected,
            {
                "path": "/dev/input/event0",
                "recording_id": "physical:/dev/input/by-id/raw",
                "recording_kind": "physical",
                "device_type": "mouse",
                "device_types": ["mouse"],
            },
        ]
    }

    result = await daemon._handle_command(
        CommandType.START_RECORDING,
        {
            "recording_ids": ["keymasq:passthrough:1234:5678:mouse"],
            "recording_slot": 2,
            "include_mouse_movement": True,
            "include_mouse_clicks": False,
        },
    )

    assert result == {"recording": "started"}
    recording_manager.start.assert_awaited_once_with(
        [selected],
        include_mouse_movement=True,
        include_mouse_clicks=False,
        recording_slot=2,
        start_position=None,
    )


@pytest.mark.parametrize(
    "extra_payload",
    [
        {"record_start_position": True},
        {"move_to_start": True},
    ],
)
@pytest.mark.asyncio
async def test_start_recording_forwards_requested_start_position_to_recording_manager(
    daemon_testbed,
    extra_payload,
):
    daemon, _device_manager, recording_manager, _macro_store, _capture_manager = daemon_testbed

    result = await daemon._handle_command(
        CommandType.START_RECORDING,
        {
            "devices": [],
            "recording_slot": 1,
            "start_x": 123,
            "start_y": 456,
            **extra_payload,
        },
    )

    assert result == {"recording": "started"}
    recording_manager.start.assert_awaited_once_with(
        [],
        include_mouse_movement=False,
        include_mouse_clicks=False,
        recording_slot=1,
        start_position=(123, 456),
    )


@pytest.mark.parametrize(
    "extra_payload",
    [
        {},
        {"record_start_position": False},
        {"move_to_start": False},
    ],
)
@pytest.mark.asyncio
async def test_start_recording_ignores_unrequested_start_coordinates(
    daemon_testbed,
    extra_payload,
):
    daemon, _device_manager, recording_manager, _macro_store, _capture_manager = daemon_testbed

    result = await daemon._handle_command(
        CommandType.START_RECORDING,
        {
            "devices": [],
            "recording_slot": 1,
            "start_x": 123,
            "start_y": 456,
            **extra_payload,
        },
    )

    assert result == {"recording": "started"}
    recording_manager.start.assert_awaited_once_with(
        [],
        include_mouse_movement=False,
        include_mouse_clicks=False,
        recording_slot=1,
        start_position=None,
    )


@pytest.mark.parametrize(
    ("command_type", "data", "manager_method", "expected_call", "expected_result"),
    [
        (
            CommandType.CAPTURE_BEGIN,
            {"hardware_id": 1234},
            "begin",
            {
                "hardware_id": "1234",
                "evdev_paths": None,
                "evdev_interfaces": None,
                "mode": "button",
            },
            {"token": "capture-session-id"},
        ),
        (
            CommandType.CAPTURE_READ,
            {"token": 42},
            "read",
            "42",
            {"captured": None},
        ),
        (
            CommandType.CAPTURE_END,
            {"token": 42},
            "end",
            "42",
            {"ended": True},
        ),
        (
            CommandType.CAPTURE_COMBO,
            {"hardware_ids": ["1234:5678"], "timeout_s": 9.0},
            "_capture_combo",
            ({"1234:5678"}, 9.0, {}, {}),
            {"events": [{"evdev": "key_a", "hardware_id": "1234:5678", "source": "kbd"}]},
        ),
    ],
)
@pytest.mark.asyncio
async def test_capture_commands_forward_to_capture_manager(
    daemon_testbed,
    monkeypatch: pytest.MonkeyPatch,
    command_type: CommandType,
    data: dict,
    manager_method: str,
    expected_call,
    expected_result: dict,
):
    daemon, _device_manager, _recording_manager, _macro_store, capture_manager = daemon_testbed
    capture_combo = AsyncMock(
        return_value={"events": [{"evdev": "key_a", "hardware_id": "1234:5678", "source": "kbd"}]}
    )
    monkeypatch.setattr(daemon_capture_commands, "capture_combo", capture_combo)

    result = await daemon._handle_command(command_type, data)

    if command_type == CommandType.CAPTURE_COMBO:
        assert result == expected_result
        capture_combo.assert_awaited_once_with(daemon, *expected_call)
        return
    assert result == expected_result
    if isinstance(expected_call, dict):
        getattr(capture_manager, manager_method).assert_called_once_with(**expected_call)
    elif expected_call is None:
        getattr(capture_manager, manager_method).assert_called_once_with()
    else:
        getattr(capture_manager, manager_method).assert_called_once_with(expected_call)


@pytest.mark.asyncio
async def test_capture_begin_forwards_explicit_evdev_paths(daemon_testbed):
    daemon, _device_manager, _recording_manager, _macro_store, capture_manager = daemon_testbed

    result = await daemon._handle_command(
        CommandType.CAPTURE_BEGIN,
        {"hardware_id": "1234:5678@slot2", "evdev_paths": ["/dev/input/event2"]},
    )

    assert result == {"token": "capture-session-id"}
    capture_manager.begin.assert_called_once_with(
        hardware_id="1234:5678@slot2",
        evdev_paths=["/dev/input/event2"],
        evdev_interfaces=None,
        mode="button",
    )


@pytest.mark.asyncio
async def test_capture_begin_forwards_evdev_interfaces(daemon_testbed):
    daemon, _device_manager, _recording_manager, _macro_store, capture_manager = daemon_testbed
    interfaces = [{"id": "gamepad", "path": "keymasq:2dc8:3106", "type": "gamepad"}]

    result = await daemon._handle_command(
        CommandType.CAPTURE_BEGIN,
        {
            "hardware_id": "2dc8:3106",
            "evdev_paths": ["keymasq:2dc8:3106"],
            "evdev_interfaces": interfaces,
        },
    )

    assert result == {"token": "capture-session-id"}
    capture_manager.begin.assert_called_once_with(
        hardware_id="2dc8:3106",
        evdev_paths=["keymasq:2dc8:3106"],
        evdev_interfaces=interfaces,
        mode="button",
    )


def test_capture_event_code_name_handles_list_style_evdev_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ev_key_names = dict(capture_manager_module.evdev.ecodes.bytype[evdev.ecodes.EV_KEY])
    ev_key_names[evdev.ecodes.BTN_SOUTH] = ["BTN_A", "BTN_GAMEPAD", "BTN_SOUTH"]
    bytype = dict(capture_manager_module.evdev.ecodes.bytype)
    bytype[evdev.ecodes.EV_KEY] = ev_key_names
    monkeypatch.setattr(capture_manager_module.evdev.ecodes, "bytype", bytype)

    assert (
        capture_manager_module._event_code_name(evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SOUTH)
        == "btn_south"
    )


@pytest.mark.asyncio
async def test_capture_combo_forwards_explicit_hardware_paths(daemon_testbed, monkeypatch):
    daemon, _device_manager, _recording_manager, _macro_store, _capture_manager = daemon_testbed
    capture_combo = AsyncMock(return_value={"events": []})
    monkeypatch.setattr(daemon_capture_commands, "capture_combo", capture_combo)

    result = await daemon._handle_command(
        CommandType.CAPTURE_COMBO,
        {
            "hardware_ids": ["1234:5678@2"],
            "hardware_paths": {"1234:5678@2": ["/dev/input/by-path/test-event-kbd"]},
            "timeout_s": 2.0,
        },
    )

    assert result == {"events": []}
    capture_combo.assert_awaited_once_with(
        daemon,
        {"1234:5678@2"},
        2.0,
        {"1234:5678@2": ["/dev/input/by-path/test-event-kbd"]},
        {},
    )


@pytest.mark.asyncio
async def test_capture_combo_forwards_hardware_interfaces(daemon_testbed, monkeypatch):
    daemon, _device_manager, _recording_manager, _macro_store, _capture_manager = daemon_testbed
    interfaces = {
        "2dc8:3106": [
            {
                "id": "gamepad",
                "path": "keymasq:2dc8:3106",
                "type": "gamepad",
                "phys": "bluetooth/input0",
                "capabilities": ["btn_south"],
            }
        ]
    }
    capture_combo = AsyncMock(return_value={"events": []})
    monkeypatch.setattr(daemon_capture_commands, "capture_combo", capture_combo)

    result = await daemon._handle_command(
        CommandType.CAPTURE_COMBO,
        {
            "hardware_ids": ["2dc8:3106"],
            "hardware_interfaces": interfaces,
            "timeout_s": 2.0,
        },
    )

    assert result == {"events": []}
    capture_combo.assert_awaited_once_with(
        daemon,
        {"2dc8:3106"},
        2.0,
        {},
        interfaces,
    )


def test_capture_manager_resolves_logical_combo_interfaces(monkeypatch):
    class FakeDevice:
        def __init__(self, path: str, *, phys: str) -> None:
            self.path = path
            self.name = "Bluetooth Pad"
            self.phys = phys
            self.info = SimpleNamespace(vendor=0x2DC8, product=0x3106)

        def capabilities(self):
            return {
                evdev.ecodes.EV_KEY: [evdev.ecodes.BTN_SOUTH],
                evdev.ecodes.EV_ABS: [evdev.ecodes.ABS_X],
            }

        def input_props(self):
            return []

    devices = {
        "/dev/input/event2": FakeDevice("/dev/input/event2", phys="bluetooth/input1"),
        "/dev/input/event9": FakeDevice("/dev/input/event9", phys="bluetooth/input0"),
    }
    monkeypatch.setattr(
        capture_manager_module.evdev,
        "list_devices",
        lambda: list(devices),
    )
    monkeypatch.setattr(
        capture_manager_module.evdev,
        "InputDevice",
        lambda path: devices[path],
    )
    device_path_resolver.refresh_cached_devices_sync(
        device_paths_fn=capture_manager_module.evdev.list_devices,
        device_input_fn=capture_manager_module.evdev.InputDevice,
        detect_input_classes_fn=detect_input_classes,
        primary_input_class_fn=primary_input_class,
    )

    try:
        manager = CaptureManager()
        path_hardware_ids, path_sources = manager._hardware_interface_lookup(
            {
                "2dc8:3106": [
                    {
                        "id": "gamepad",
                        "path": "keymasq:2dc8:3106",
                        "type": "gamepad",
                        "phys": "bluetooth/input0",
                        "capabilities": ["btn_south"],
                    }
                ]
            }
        )

        assert path_hardware_ids["/dev/input/event9"] == "2dc8:3106"
        assert "/dev/input/event2" not in path_hardware_ids
        assert path_sources["/dev/input/event9"] == "gamepad"
    finally:
        device_path_resolver.clear_cached_devices()


def test_capture_manager_hardware_interface_lookup_keeps_first_alias_owner(monkeypatch):
    manager = CaptureManager()
    calls: list[str] = []

    def fake_resolve_evdev_interfaces(_interfaces, **kwargs):
        hardware_id = str(kwargs["hardware_id"])
        calls.append(hardware_id)
        return [
            SimpleNamespace(
                path="/dev/input/event9",
                interface_id=f"{hardware_id}-source",
            )
        ]

    monkeypatch.setattr(
        capture_manager_module.device_path_resolver,
        "resolve_evdev_interfaces",
        fake_resolve_evdev_interfaces,
    )

    path_hardware_ids, path_sources = manager._hardware_interface_lookup(
        {
            "2dc8:3106": [{"path": "keymasq:2dc8:3106"}],
            "2dc8:3106@2": [{"path": "keymasq:2dc8:3106"}],
        }
    )

    assert calls == ["2dc8:3106", "2dc8:3106@2"]
    assert path_hardware_ids["/dev/input/event9"] == "2dc8:3106"
    assert path_sources["/dev/input/event9"] == "2dc8:3106-source"


def test_capture_manager_hardware_interface_lookup_excludes_prior_model_claims(monkeypatch):
    manager = CaptureManager()
    calls: list[tuple[str, set[str]]] = []

    def fake_resolve_evdev_interfaces(_interfaces, **kwargs):
        hardware_id = str(kwargs["hardware_id"])
        excluded = set(kwargs.get("excluded_paths") or set())
        calls.append((hardware_id, excluded))
        path = "/dev/input/event10" if "/dev/input/event9" in excluded else "/dev/input/event9"
        return [
            SimpleNamespace(
                path=path,
                interface_id=f"{hardware_id}-source",
            )
        ]

    monkeypatch.setattr(capture_manager_module, "resolve_stable_path", lambda path: path)
    monkeypatch.setattr(
        capture_manager_module.device_path_resolver,
        "resolve_evdev_interfaces",
        fake_resolve_evdev_interfaces,
    )

    path_hardware_ids, path_sources = manager._hardware_interface_lookup(
        {
            "2dc8:3106": [{"path": "keymasq:2dc8:3106", "type": "gamepad"}],
            "2dc8:3106@2": [{"path": "keymasq:2dc8:3106", "type": "gamepad"}],
        }
    )

    assert calls == [
        ("2dc8:3106", set()),
        ("2dc8:3106@2", {"/dev/input/event9"}),
    ]
    assert path_hardware_ids["/dev/input/event9"] == "2dc8:3106"
    assert path_hardware_ids["/dev/input/event10"] == "2dc8:3106@2"
    assert path_sources["/dev/input/event10"] == "2dc8:3106@2-source"


@pytest.mark.asyncio
async def test_capture_combo_waits_on_event_not_sleep(daemon_testbed, monkeypatch):
    daemon, device_manager, _recording_manager, _macro_store, capture_manager = daemon_testbed
    original_sleep = asyncio.sleep
    queued_events: list[dict] = []
    waiter: dict[str, asyncio.Event] = {}

    def begin_combo_capture(
        _token: str,
        _hardware_ids: set[str],
        notify_event: asyncio.Event,
    ) -> dict:
        waiter["event"] = notify_event
        return {"token": "combo-session-id", "grabbed_devices": 0}

    device_manager.begin_combo_capture = Mock(side_effect=begin_combo_capture)
    device_manager.read_combo_capture = Mock(
        side_effect=lambda _token: {"event": queued_events.pop(0) if queued_events else None}
    )
    capture_manager.read_combo_nowait = Mock(return_value={"event": None})

    async def fail_sleep(delay: float) -> None:
        raise AssertionError(f"unexpected polling sleep: {delay}")

    monkeypatch.setattr(daemon_module.asyncio, "sleep", fail_sleep)

    task = asyncio.create_task(daemon_capture_commands.capture_combo(daemon, {"1234:5678"}, 1.0))

    while "event" not in waiter:
        await original_sleep(0)

    queued_events.append(
        {"evdev": "key_a", "hardware_id": "1234:5678", "source": "kbd", "value": 1}
    )
    waiter["event"].set()
    await original_sleep(0)
    queued_events.append(
        {"evdev": "key_a", "hardware_id": "1234:5678", "source": "kbd", "value": 0}
    )
    waiter["event"].set()

    result = await task
    assert result == {
        "events": [{"evdev": "key_a", "hardware_id": "1234:5678", "source": "kbd"}],
        "warnings": [],
    }
    capture_manager.register_combo_notifier.assert_called_once()


@pytest.mark.asyncio
async def test_capture_combo_clamps_client_timeout_to_daemon_max(daemon_testbed, monkeypatch):
    daemon, _device_manager, _recording_manager, _macro_store, _capture_manager = daemon_testbed
    deadline_offsets: list[float] = []
    queued_events = [
        {"evdev": "key_a", "hardware_id": "1234:5678", "source": "kbd", "value": 1},
        {"evdev": "key_a", "hardware_id": "1234:5678", "source": "kbd", "value": 0},
    ]

    async def read_event(
        _daemon,
        _token: str,
        _notify_event: asyncio.Event,
        deadline: float,
    ) -> dict:
        deadline_offsets.append(deadline - asyncio.get_running_loop().time())
        return queued_events.pop(0)

    monkeypatch.setattr(daemon_capture_commands, "read_capture_combo_event", read_event)

    result = await daemon_capture_commands.capture_combo(
        daemon,
        {"1234:5678"},
        daemon_capture_commands.MAX_CAPTURE_TIMEOUT_S + 3600.0,
    )

    assert result == {
        "events": [{"evdev": "key_a", "hardware_id": "1234:5678", "source": "kbd"}],
        "warnings": [],
    }
    assert deadline_offsets
    assert 0.0 < deadline_offsets[0] <= daemon_capture_commands.MAX_CAPTURE_TIMEOUT_S


@pytest.mark.asyncio
async def test_capture_combo_preserves_begin_failure_when_end_would_fail(daemon_testbed):
    daemon, device_manager, _recording_manager, _macro_store, capture_manager = daemon_testbed
    capture_manager.begin_combo.side_effect = RuntimeError("begin failed")
    capture_manager.end.side_effect = RuntimeError("cleanup failed")

    with pytest.raises(RuntimeError, match="begin failed"):
        await daemon_capture_commands.capture_combo(daemon, {"1234:5678"}, 1.0)

    device_manager.end_combo_capture.assert_called_once()
    capture_manager.end.assert_not_called()


@pytest.mark.asyncio
async def test_capture_combo_preserves_result_when_end_cleanup_fails(
    daemon_testbed,
    monkeypatch,
):
    daemon, _device_manager, _recording_manager, _macro_store, capture_manager = daemon_testbed
    queued_events = [
        {"evdev": "key_a", "hardware_id": "1234:5678", "source": "kbd", "value": 1},
        {"evdev": "key_a", "hardware_id": "1234:5678", "source": "kbd", "value": 0},
    ]
    capture_manager.end.side_effect = RuntimeError("cleanup failed")

    async def read_event(
        _daemon,
        _token: str,
        _notify_event: asyncio.Event,
        _deadline: float,
    ) -> dict:
        return queued_events.pop(0)

    monkeypatch.setattr(daemon_capture_commands, "read_capture_combo_event", read_event)

    result = await daemon_capture_commands.capture_combo(daemon, {"1234:5678"}, 1.0)

    assert result == {
        "events": [{"evdev": "key_a", "hardware_id": "1234:5678", "source": "kbd"}],
        "warnings": [],
    }
    capture_manager.end.assert_called_once()


@pytest.mark.asyncio
async def test_capture_combo_preserves_timeout_when_end_cleanup_fails(
    daemon_testbed,
    monkeypatch,
):
    daemon, _device_manager, _recording_manager, _macro_store, capture_manager = daemon_testbed
    capture_manager.end.side_effect = RuntimeError("cleanup failed")
    monkeypatch.setattr(daemon_capture_commands, "MIN_CAPTURE_TIMEOUT_S", 0.0)
    monkeypatch.setattr(daemon_capture_commands, "MAX_CAPTURE_TIMEOUT_S", 0.0)

    with pytest.raises(TimeoutError, match="Combo capture timed out"):
        await daemon_capture_commands.capture_combo(daemon, {"1234:5678"}, 0.0)

    capture_manager.end.assert_called_once()


@pytest.mark.asyncio
async def test_capture_combo_returns_immediately_on_wheel_pulse(daemon_testbed):
    daemon, device_manager, _recording_manager, _macro_store, capture_manager = daemon_testbed
    notify: dict[str, asyncio.Event] = {}
    queued_events: list[dict] = []

    def begin_combo_capture(
        _token: str,
        _hardware_ids: set[str],
        notify_event: asyncio.Event,
    ) -> dict:
        notify["event"] = notify_event
        return {"token": "combo-session-id", "grabbed_devices": 0}

    device_manager.begin_combo_capture = Mock(side_effect=begin_combo_capture)
    device_manager.read_combo_capture = Mock(
        side_effect=lambda _token: {"event": queued_events.pop(0) if queued_events else None}
    )
    capture_manager.read_combo_nowait = Mock(return_value={"event": None})

    task = asyncio.create_task(daemon_capture_commands.capture_combo(daemon, {"1234:5678"}, 1.0))
    while "event" not in notify:
        await asyncio.sleep(0)

    queued_events.append(
        {"evdev": "key_leftmeta", "hardware_id": "1234:5678", "source": "kbd", "value": 1}
    )
    notify["event"].set()
    await asyncio.sleep(0)
    queued_events.append(
        {"evdev": "wheel_up", "hardware_id": "1234:5678", "source": "mouse", "value": 1}
    )
    notify["event"].set()

    assert await task == {
        "events": [
            {"evdev": "key_leftmeta", "hardware_id": "1234:5678", "source": "kbd"},
            {"evdev": "wheel_up", "hardware_id": "1234:5678", "source": "mouse"},
        ],
        "warnings": [],
    }


@pytest.mark.asyncio
async def test_start_offloads_macro_store_prep_to_thread(
    daemon_testbed,
    monkeypatch,
    tmp_path: Path,
):
    daemon, device_manager, recording_manager, macro_store, _capture_manager = daemon_testbed
    to_thread_calls: list[tuple[object, tuple[object, ...]]] = []
    fake_socket_server = SimpleNamespace(
        start=AsyncMock(side_effect=lambda: daemon._shutdown_event.set()),
        stop=AsyncMock(),
        broadcast_event=AsyncMock(),
    )
    startup_order: list[str] = []

    async def fake_to_thread(func, /, *args, **kwargs):
        assert kwargs == {}
        to_thread_calls.append((func, args))
        return func(*args)

    async def fake_reconcile_all() -> None:
        startup_order.append("reconcile")

    def fake_load_security_policy(_path: Path) -> SecurityPolicy:
        startup_order.append("policy")
        return SecurityPolicy()

    monkeypatch.setattr(daemon_module.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(daemon_module, "SocketServer", lambda *args, **kwargs: fake_socket_server)
    monkeypatch.setattr(daemon_module, "RUN_DIR", tmp_path / "run")
    monkeypatch.setattr(daemon_module, "SOCKET_PATH", tmp_path / "daemon.sock")
    monkeypatch.setattr(daemon_module, "load_security_policy", fake_load_security_policy)
    monkeypatch.setattr(daemon_module.source_hiding, "reconcile_all", fake_reconcile_all)
    monkeypatch.setattr(daemon_module, "sd_notify", lambda _state: None)
    monkeypatch.setattr(daemon, "_secure_run_dir", Mock())

    await daemon.start()

    assert startup_order[:2] == ["reconcile", "policy"]
    assert to_thread_calls[0][0].__name__ == "_prepare_macro_store"
    macro_store.ensure.assert_called_once()
    macro_store.register_internal.assert_called_once()
    assert macro_store.register_internal.call_args.args[0] == "__cursor_position_trigger"
    recording_manager.cleanup_spool_dir.assert_called_once()
    recording_manager.load_persisted_slot_recordings.assert_awaited_once()
    device_manager.initialize_output_devices.assert_called_once()
    device_manager.shutdown_output_devices.assert_called_once()
    device_manager.start_topology_watcher.assert_awaited_once()
    device_manager.stop_topology_watcher.assert_awaited_once()
    assert device_manager.broadcast_callback == fake_socket_server.broadcast_event
    assert recording_manager.broadcast_callback == fake_socket_server.broadcast_event


@pytest.mark.asyncio
async def test_start_cleans_up_resources_when_socket_start_fails(
    daemon_testbed,
    monkeypatch,
    tmp_path: Path,
):
    daemon, device_manager, _recording_manager, _macro_store, _capture_manager = daemon_testbed
    fake_socket_server = SimpleNamespace(
        start=AsyncMock(side_effect=RuntimeError("socket start failed")),
        stop=AsyncMock(),
        broadcast_event=AsyncMock(),
    )

    monkeypatch.setattr(daemon_module, "SocketServer", lambda *args, **kwargs: fake_socket_server)
    monkeypatch.setattr(daemon_module, "RUN_DIR", tmp_path / "run")
    monkeypatch.setattr(daemon_module, "SOCKET_PATH", tmp_path / "daemon.sock")
    monkeypatch.setattr(daemon_module, "load_security_policy", lambda _path: SecurityPolicy())
    monkeypatch.setattr(daemon_module.source_hiding, "reconcile_all", AsyncMock())
    monkeypatch.setattr(daemon_module, "sd_notify", lambda _state: None)
    monkeypatch.setattr(daemon, "_secure_run_dir", Mock())

    with pytest.raises(RuntimeError, match="socket start failed"):
        await daemon.start()

    assert daemon.running is False
    device_manager.initialize_output_devices.assert_called_once()
    device_manager.shutdown_output_devices.assert_called_once()
    fake_socket_server.stop.assert_awaited_once()
    device_manager.start_topology_watcher.assert_not_awaited()
    device_manager.stop_topology_watcher.assert_awaited_once()
    device_manager.cancel_macro_playback.assert_awaited_once()
    device_manager.release_all_devices.assert_awaited_once()


@pytest.mark.asyncio
async def test_stop_lets_socket_server_own_socket_path_cleanup(
    daemon_testbed,
    monkeypatch,
):
    daemon, device_manager, _recording_manager, _macro_store, _capture_manager = daemon_testbed
    socket_server = SimpleNamespace(stop=AsyncMock())
    cleanup_socket_path = Mock()
    daemon.running = True
    daemon.socket_server = socket_server
    monkeypatch.setattr(daemon, "_cleanup_socket_path", cleanup_socket_path)

    await daemon.stop()

    socket_server.stop.assert_awaited_once()
    cleanup_socket_path.assert_not_called()
    device_manager.shutdown_output_devices.assert_called_once()


@pytest.mark.asyncio
async def test_start_cleans_up_resources_when_topology_start_fails(
    daemon_testbed,
    monkeypatch,
    tmp_path: Path,
):
    daemon, device_manager, _recording_manager, _macro_store, _capture_manager = daemon_testbed
    fake_socket_server = SimpleNamespace(
        start=AsyncMock(),
        stop=AsyncMock(),
        broadcast_event=AsyncMock(),
    )
    device_manager.start_topology_watcher.side_effect = RuntimeError("topology start failed")

    monkeypatch.setattr(daemon_module, "SocketServer", lambda *args, **kwargs: fake_socket_server)
    monkeypatch.setattr(daemon_module, "RUN_DIR", tmp_path / "run")
    monkeypatch.setattr(daemon_module, "SOCKET_PATH", tmp_path / "daemon.sock")
    monkeypatch.setattr(daemon_module, "load_security_policy", lambda _path: SecurityPolicy())
    monkeypatch.setattr(daemon_module.source_hiding, "reconcile_all", AsyncMock())
    monkeypatch.setattr(daemon_module, "sd_notify", lambda _state: None)
    monkeypatch.setattr(daemon, "_secure_run_dir", Mock())

    with pytest.raises(RuntimeError, match="topology start failed"):
        await daemon.start()

    assert daemon.running is False
    device_manager.initialize_output_devices.assert_called_once()
    fake_socket_server.start.assert_awaited_once()
    device_manager.start_topology_watcher.assert_awaited_once()
    device_manager.shutdown_output_devices.assert_called_once()
    fake_socket_server.stop.assert_awaited_once()
    device_manager.stop_topology_watcher.assert_awaited_once()
    device_manager.cancel_macro_playback.assert_awaited_once()
    device_manager.release_all_devices.assert_awaited_once()


@pytest.mark.asyncio
async def test_stop_continues_cleanup_after_topology_stop_fails(daemon_testbed, monkeypatch):
    daemon, device_manager, _recording_manager, _macro_store, _capture_manager = daemon_testbed
    fake_socket_server = SimpleNamespace(stop=AsyncMock())
    device_manager.stop_topology_watcher.side_effect = RuntimeError("topology stop failed")
    daemon.socket_server = fake_socket_server
    daemon.running = True

    monkeypatch.setattr(daemon_module, "sd_notify", lambda _state: None)

    await daemon.stop()

    assert daemon.running is False
    device_manager.stop_topology_watcher.assert_awaited_once()
    device_manager.cancel_macro_playback.assert_awaited_once()
    device_manager.release_all_devices.assert_awaited_once()
    device_manager.shutdown_output_devices.assert_called_once()
    fake_socket_server.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_read_capture_combo_event_drains_sources_once_before_waiting(
    daemon_testbed,
    monkeypatch,
):
    daemon, device_manager, _recording_manager, _macro_store, capture_manager = daemon_testbed
    notify_event = asyncio.Event()
    seen_before_wait: dict[str, int] = {}
    released = {"ready": False}

    def read_combo_capture(_token: str) -> dict:
        if released["ready"]:
            return {
                "event": {
                    "evdev": "key_a",
                    "hardware_id": "1234:5678",
                    "source": "kbd",
                    "value": 1,
                }
            }
        return {"event": None}

    async def fake_wait_for(awaitable, timeout):
        seen_before_wait["device"] = device_manager.read_combo_capture.call_count
        seen_before_wait["capture"] = capture_manager.read_combo_nowait.call_count
        released["ready"] = True
        notify_event.set()
        return await awaitable

    device_manager.read_combo_capture = Mock(side_effect=read_combo_capture)
    capture_manager.read_combo_nowait = Mock(return_value={"event": None})
    monkeypatch.setattr(daemon_capture_commands.asyncio, "wait_for", fake_wait_for)

    event = await daemon_capture_commands.read_capture_combo_event(
        daemon,
        "combo-session-id",
        notify_event,
        float("inf"),
    )

    assert event == {
        "evdev": "key_a",
        "hardware_id": "1234:5678",
        "source": "kbd",
        "value": 1,
    }
    assert seen_before_wait == {"device": 1, "capture": 1}
