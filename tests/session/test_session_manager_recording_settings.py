import asyncio
import logging
import threading
import tomllib
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest

import keymasq.session.manager.recording as session_recording_module
from keymasq.common import config_files as config_files_module
from keymasq.common.ipc import Command, CommandType, Response
from keymasq.session.manager import SessionManager


def test_session_manager_recording_settings_path_is_test_isolated(tmp_path) -> None:
    manager = SessionManager()

    assert manager.RECORDING_SETTINGS_PATH == tmp_path / "recording_settings.toml"


@pytest.mark.asyncio
async def test_recording_settings_persistence_applies_latest_snapshot_last(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager.recording_state.settings = {
        "include_mouse_movement": False,
        "include_mouse_clicks": False,
        "record_start_position": False,
    }
    persisted: dict[str, bool] = {}
    writes: list[dict[str, bool]] = []
    writes_lock = threading.Lock()
    stale_started = threading.Event()
    stale_finished = threading.Event()
    latest_finished = threading.Event()
    release_stale = threading.Event()

    def fake_save(_manager, settings: dict | None = None) -> None:
        state = dict(settings or {})
        if state.get("include_mouse_movement", False):
            stale_started.set()
            if not release_stale.wait(timeout=1.0):
                return
        with writes_lock:
            persisted.clear()
            persisted.update(state)
            writes.append(state)
        if state.get("include_mouse_movement", False):
            stale_finished.set()
        else:
            latest_finished.set()

    async def wait_for_event(event: threading.Event, message: str) -> None:
        if not await asyncio.to_thread(event.wait, 1.0):
            pytest.fail(message)

    monkeypatch.setattr(session_recording_module, "save_recording_settings_to_disk", fake_save)

    session_recording_module.update_recording_settings(
        manager,
        {"include_mouse_movement": True},
    )
    first_save_task = manager.recording_state.settings_save_task
    assert first_save_task is not None
    await wait_for_event(stale_started, "stale settings save did not start")

    session_recording_module.update_recording_settings(
        manager,
        {"include_mouse_movement": False, "include_mouse_clicks": True},
    )

    current_save_task = manager.recording_state.settings_save_task
    if current_save_task is not first_save_task:
        await wait_for_event(latest_finished, "latest settings save did not finish")
    release_stale.set()
    await wait_for_event(stale_finished, "stale settings save did not finish")

    if current_save_task is not None:
        await asyncio.wait_for(current_save_task, timeout=1.0)
    await wait_for_event(latest_finished, "latest settings save did not finish")

    assert writes
    assert persisted == {
        "include_mouse_movement": False,
        "include_mouse_clicks": True,
        "record_start_position": False,
    }


def test_recording_settings_save_load_toml_uses_recording_ids(tmp_path) -> None:
    manager = SessionManager()
    manager.RECORDING_SETTINGS_PATH = tmp_path / "recording_settings.toml"
    manager.recording_state.settings = {
        "include_mouse_movement": True,
        "include_mouse_clicks": True,
        "record_start_position": True,
        # Legacy fields may still exist in old in-memory/test payloads, but new
        # TOML writes should prune them.
        "record_keyboard": False,
        "record_mouse": True,
        "record_gamepad": False,
        "device_overrides": {
            "keymasq:passthrough:1234:5678:mouse": True,
            "physical:/dev/input/by-id/usb-test-event-mouse": False,
        },
    }
    expected = {
        "include_mouse_movement": True,
        "include_mouse_clicks": True,
        "record_start_position": True,
        "device_overrides": {
            "keymasq:passthrough:1234:5678:mouse": True,
            "physical:/dev/input/by-id/usb-test-event-mouse": False,
        },
    }

    session_recording_module.save_recording_settings_to_disk(manager)

    with manager.RECORDING_SETTINGS_PATH.open("rb") as f:
        written = tomllib.load(f)
    assert written == expected

    loaded_manager = SessionManager()
    loaded_manager.RECORDING_SETTINGS_PATH = manager.RECORDING_SETTINGS_PATH
    session_recording_module.load_recording_settings_from_disk(loaded_manager)

    assert loaded_manager.recording_state.settings == expected


def test_recording_settings_load_preserves_missing_keys(tmp_path) -> None:
    manager = SessionManager()
    manager.RECORDING_SETTINGS_PATH = tmp_path / "recording_settings.toml"
    manager.recording_state.settings = {
        "include_mouse_movement": True,
        "include_mouse_clicks": True,
        "record_start_position": True,
        "device_overrides": {"physical:/dev/input/event0": True},
    }
    manager.RECORDING_SETTINGS_PATH.write_text(
        "include_mouse_movement = false\n",
        encoding="utf-8",
    )

    session_recording_module.load_recording_settings_from_disk(manager)

    assert manager.recording_state.settings == {
        "include_mouse_movement": False,
        "include_mouse_clicks": True,
        "record_start_position": True,
        "device_overrides": {"physical:/dev/input/event0": True},
    }


def test_recording_settings_load_logs_errors(tmp_path, caplog) -> None:
    manager = SessionManager()
    manager.RECORDING_SETTINGS_PATH = tmp_path / "recording_settings.toml"
    manager.RECORDING_SETTINGS_PATH.write_text("not = [valid toml", encoding="utf-8")

    caplog.set_level(logging.ERROR, logger="keymasq-session")

    session_recording_module.load_recording_settings_from_disk(manager)

    assert (
        f"Failed to load recording settings from {manager.RECORDING_SETTINGS_PATH}"
        in caplog.text
    )


def test_recording_settings_save_logs_errors(tmp_path, caplog, monkeypatch) -> None:
    manager = SessionManager()
    manager.RECORDING_SETTINGS_PATH = tmp_path / "recording_settings.toml"
    manager.recording_state.settings = {
        "include_mouse_movement": True,
        "include_mouse_clicks": False,
        "record_start_position": False,
        "device_overrides": {},
    }

    def raise_dump_error(_data, _fp) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(config_files_module.tomli_w, "dump", raise_dump_error)
    caplog.set_level(logging.ERROR, logger="keymasq-session")

    session_recording_module.save_recording_settings_to_disk(manager)

    assert (
        f"Failed to save recording settings to {manager.RECORDING_SETTINGS_PATH}"
        in caplog.text
    )


@pytest.mark.asyncio
async def test_start_recording_sends_selected_devices_from_cache() -> None:
    manager = SessionManager()
    sent_commands: list[CommandType] = []
    sent_payloads: list[dict[str, object]] = []

    async def send_command(command):
        sent_commands.append(command.command)
        sent_payloads.append(dict(command.data or {}))
        return Response(status="ok", data={"status": "ok"})

    manager.client = SimpleNamespace(send_command=send_command)  # type: ignore[assignment]
    manager.recording_state.settings = {
        "include_mouse_movement": False,
        "include_mouse_clicks": False,
        "record_start_position": False,
        "device_overrides": {
            "keymasq:passthrough:1234:5678:kbd": True,
            "physical:/dev/input/by-id/usb-raw-event-kbd": False,
        },
    }
    manager.recording_state.devices_cache = [
        {
            "path": "/dev/input/event20",
            "recording_id": "keymasq:passthrough:1234:5678:kbd",
            "recording_kind": "keymasq_passthrough",
            "device_type": "keyboard",
            "device_types": ["keyboard"],
        },
        {
            "path": "/dev/input/event0",
            "stable_path": "/dev/input/by-id/usb-raw-event-kbd",
            "recording_id": "physical:/dev/input/by-id/usb-raw-event-kbd",
            "recording_kind": "physical",
            "device_type": "keyboard",
            "device_types": ["keyboard"],
        },
    ]
    session_recording_module.update_selected_recording_devices_cache(manager)

    result = await session_recording_module.start_recording(manager)

    assert result == {"status": "ok", "recording_slot": 1}
    assert sent_commands == [CommandType.START_RECORDING]
    assert sent_payloads[0]["recording_slot"] == 1
    assert sent_payloads[0]["devices"] == [
        {
            "path": "/dev/input/event20",
            "recording_id": "keymasq:passthrough:1234:5678:kbd",
            "recording_kind": "keymasq_passthrough",
            "device_type": "keyboard",
            "device_types": ["keyboard"],
        }
    ]
    assert "recording_ids" not in sent_payloads[0]


@pytest.mark.asyncio
async def test_get_devices_for_recording_uses_daemon_grabbed_state_only() -> None:
    manager = SessionManager()
    manager.profile_state.grabbed_interfaces["045e:02a1"] = {
        "gamepad": "/dev/input/event20"
    }
    manager.client.send_command = AsyncMock(
        return_value=Response(
            status="ok",
            data={
                "devices": [
                    {
                        "path": "/dev/input/event20",
                        "stable_path": "/dev/input/event20",
                        "name": "Xbox 360 Wireless Receiver",
                        "vendor_id": "045e",
                        "product_id": "02a1",
                        "device_type": "gamepad",
                        "device_types": ["gamepad"],
                        "grabbed_by_keymasq": False,
                    }
                ]
            },
        )
    )

    devices = await session_recording_module.get_devices_for_recording(
        manager,
        ["gamepad"],
        include_grabbed=True,
    )

    assert devices[0]["grabbed_by_keymasq"] is False
    assert devices[0]["source_hardware_id"] == ""
    assert devices[0]["source_interface_id"] == ""


@pytest.mark.asyncio
async def test_start_recording_defaults_to_recommended_sources_only() -> None:
    manager = SessionManager()
    sent_payloads: list[dict[str, object]] = []

    async def send_command(command):
        sent_payloads.append(dict(command.data or {}))
        return Response(status="ok", data={"status": "ok"})

    manager.client = SimpleNamespace(send_command=send_command)  # type: ignore[assignment]
    manager.recording_state.settings = {
        "include_mouse_movement": False,
        "include_mouse_clicks": False,
        "record_start_position": False,
        "device_overrides": {},
    }
    manager.recording_state.devices_cache = [
        {
            "path": "/dev/input/event20",
            "recording_id": "keymasq:output:keyboard",
            "recording_kind": "keymasq_output",
            "device_type": "keyboard",
            "device_types": ["keyboard"],
        },
        {
            "path": "/dev/input/event21",
            "recording_id": "keymasq:passthrough:1234:5678:mouse",
            "recording_kind": "keymasq_passthrough",
            "device_type": "mouse",
            "device_types": ["mouse"],
        },
        {
            "path": "/dev/input/event0",
            "stable_path": "/dev/input/by-id/usb-raw-event-kbd",
            "recording_id": "physical:/dev/input/by-id/usb-raw-event-kbd",
            "recording_kind": "physical",
            "device_type": "keyboard",
            "device_types": ["keyboard"],
        },
    ]
    session_recording_module.update_selected_recording_devices_cache(manager)

    result = await session_recording_module.start_recording(manager)

    assert result == {"status": "ok", "recording_slot": 1}
    sent_devices = cast(list[dict[str, object]], sent_payloads[0]["devices"])
    assert [device["recording_id"] for device in sent_devices] == [
        "keymasq:output:keyboard",
        "keymasq:passthrough:1234:5678:mouse",
    ]


@pytest.mark.asyncio
async def test_update_recording_settings_recomputes_selected_devices_cache() -> None:
    manager = SessionManager()
    manager.recording_state.settings = {
        "include_mouse_movement": False,
        "include_mouse_clicks": False,
        "record_start_position": False,
        "device_overrides": {},
    }
    manager.recording_state.devices_cache = [
        {
            "path": "/dev/input/event20",
            "recording_id": "keymasq:output:keyboard",
            "recording_kind": "keymasq_output",
            "device_type": "keyboard",
            "device_types": ["keyboard"],
        },
        {
            "path": "/dev/input/event21",
            "recording_id": "physical:/dev/input/by-id/usb-Test_Mouse-event-mouse",
            "recording_kind": "physical",
            "device_type": "mouse",
            "device_types": ["mouse"],
        },
    ]
    session_recording_module.update_selected_recording_devices_cache(manager)
    assert [
        device["recording_id"] for device in manager.recording_state.selected_devices_cache
    ] == ["keymasq:output:keyboard"]

    session_recording_module.update_recording_settings(
        manager,
        {
            "device_overrides": {
                "physical:/dev/input/by-id/usb-Test_Mouse-event-mouse": True
            }
        },
    )

    assert [
        device["recording_id"] for device in manager.recording_state.selected_devices_cache
    ] == ["keymasq:output:keyboard", "physical:/dev/input/by-id/usb-Test_Mouse-event-mouse"]

    save_task = manager.recording_state.settings_save_task
    if save_task is not None:
        await save_task


@pytest.mark.asyncio
async def test_update_recording_settings_prunes_stale_device_overrides() -> None:
    manager = SessionManager()
    manager.recording_state.settings = {
        "include_mouse_movement": False,
        "include_mouse_clicks": False,
        "record_start_position": False,
        "device_overrides": {},
    }
    manager.recording_state.devices_cache_ready = True
    manager.recording_state.devices_cache = [
        {
            "path": "/dev/input/event20",
            "recording_id": "keymasq:output:keyboard",
            "recording_kind": "keymasq_output",
            "device_type": "keyboard",
            "device_types": ["keyboard"],
        },
    ]

    session_recording_module.update_recording_settings(
        manager,
        {
            "device_overrides": {
                "keymasq:output:keyboard": False,
                "physical:/dev/input/by-id/stale-mouse": True,
            }
        },
    )

    assert manager.recording_state.settings["device_overrides"] == {
        "keymasq:output:keyboard": False
    }

    save_task = manager.recording_state.settings_save_task
    if save_task is not None:
        await save_task


@pytest.mark.asyncio
async def test_update_recording_settings_preserves_overrides_when_cache_is_empty() -> None:
    manager = SessionManager()
    manager.recording_state.settings = {
        "include_mouse_movement": False,
        "include_mouse_clicks": False,
        "record_start_position": False,
        "device_overrides": {
            "physical:/dev/input/by-id/stale-mouse": True,
        },
    }
    manager.recording_state.devices_cache_ready = True
    manager.recording_state.devices_cache = []

    session_recording_module.update_recording_settings(
        manager,
        {
            "device_overrides": {
                "physical:/dev/input/by-id/stale-mouse": True,
            }
        },
    )

    assert manager.recording_state.settings["device_overrides"] == {
        "physical:/dev/input/by-id/stale-mouse": True,
    }

    save_task = manager.recording_state.settings_save_task
    if save_task is not None:
        await save_task


def test_update_recording_settings_ignores_requests_without_settings_fields() -> None:
    manager = SessionManager()
    manager.recording_state.settings = {
        "include_mouse_movement": True,
        "include_mouse_clicks": True,
        "record_start_position": True,
        "device_overrides": {
            "physical:/dev/input/by-id/keep": True,
        },
    }
    manager.recording_state.devices_cache_ready = True
    manager.recording_state.devices_cache = []

    session_recording_module.update_recording_settings(
        manager,
        {"command": "start_recording", "recording_slot": 2},
    )

    assert manager.recording_state.settings == {
        "include_mouse_movement": True,
        "include_mouse_clicks": True,
        "record_start_position": True,
        "device_overrides": {
            "physical:/dev/input/by-id/keep": True,
        },
    }
    assert manager.recording_state.settings_save_task is None


@pytest.mark.asyncio
async def test_start_recording_replaces_pending_recording_in_selected_slot() -> None:
    manager = SessionManager()
    sent_commands: list[Command] = []

    async def send_command(command: Command) -> Response:
        sent_commands.append(command)
        return Response(status="ok", data={"status": "ok"})

    manager.client = SimpleNamespace(send_command=send_command)  # type: ignore[assignment]
    session_recording_module.begin_pending_macro_save(
        manager,
        {"pending_recording_id": "recording-old"},
        recording_slot=1,
    )

    result = await session_recording_module.start_recording(manager)

    assert result == {"status": "ok", "recording_slot": 1}
    assert [command.command for command in sent_commands] == [
        CommandType.START_RECORDING,
        CommandType.MACRO_DELETE_RECORDING,
    ]
    assert sent_commands[0].data["recording_slot"] == 1
    assert sent_commands[1].data == {"pending_recording_id": "recording-old"}
    assert manager.recording_state.pending_slots == {}
    assert manager.recording_state.pending_save is None


@pytest.mark.asyncio
async def test_start_recording_rejects_missing_recording_slot() -> None:
    manager = SessionManager()

    result = await session_recording_module.start_recording(manager, recording_slot=0)

    assert result["status"] == "error"
    assert result["error_code"] == "macro_recording_slot_required"
