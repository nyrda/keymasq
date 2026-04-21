# ruff: noqa: F403, F405, I001
import tomllib

from tests.session.profile_support import *

@pytest.mark.asyncio
async def test_recording_settings_persistence_applies_latest_snapshot_last() -> None:
    manager = SessionManager()
    manager.recording_state.settings = {
        "include_mouse_movement": False,
        "include_mouse_clicks": False,
        "record_start_position": False,
    }
    persisted: dict[str, bool] = {}
    writes: list[dict[str, bool]] = []

    def fake_save(_manager, settings: dict | None = None) -> None:
        state = dict(settings or {})
        if state.get("include_mouse_movement", False):
            time.sleep(0.05)
        else:
            time.sleep(0.005)
        persisted.clear()
        persisted.update(state)
        writes.append(state)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(session_recording_module, "save_recording_settings_to_disk", fake_save)

    session_recording_module.update_recording_settings(manager, {"include_mouse_movement": True})
    session_recording_module.update_recording_settings(
        manager,
        {"include_mouse_movement": False, "include_mouse_clicks": True},
    )

    for _ in range(100):
        save_task = manager.recording_state.settings_save_task
        if save_task is None or save_task.done():
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("recording settings save task did not complete")

    assert writes
    assert persisted == {
        "include_mouse_movement": False,
        "include_mouse_clicks": True,
        "record_start_position": False,
    }
    monkeypatch.undo()


def test_recording_settings_save_load_toml_uses_recording_ids(tmp_path) -> None:
    manager = SessionManager()
    manager.RECORDING_SETTINGS_PATH = tmp_path / "recording_settings.toml"
    manager.recording_state.settings = {
        "include_mouse_movement": True,
        "include_mouse_clicks": True,
        "record_start_position": True,
        "record_keyboard": False,
        "record_mouse": True,
        "record_gamepad": False,
        "device_overrides": {
            "keymasq:passthrough:1234:5678:mouse": True,
            "physical:/dev/input/by-id/usb-test-event-mouse": False,
        },
    }

    session_recording_module.save_recording_settings_to_disk(manager)

    with manager.RECORDING_SETTINGS_PATH.open("rb") as f:
        written = tomllib.load(f)
    assert written == manager.recording_state.settings

    loaded_manager = SessionManager()
    loaded_manager.RECORDING_SETTINGS_PATH = manager.RECORDING_SETTINGS_PATH
    session_recording_module.load_recording_settings_from_disk(loaded_manager)

    assert loaded_manager.recording_state.settings == manager.recording_state.settings


@pytest.mark.asyncio
async def test_start_recording_sends_recording_ids_from_cache() -> None:
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
        "record_keyboard": True,
        "record_mouse": False,
        "record_gamepad": False,
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

    result = await session_recording_module.start_recording(manager)

    assert result == {"status": "ok"}
    assert sent_commands == [CommandType.START_RECORDING]
    assert sent_payloads[0]["recording_ids"] == ["keymasq:passthrough:1234:5678:kbd"]


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

    result = await session_recording_module.start_recording(manager)

    assert result == {"status": "ok"}
    assert sent_payloads[0]["recording_ids"] == [
        "keymasq:output:keyboard",
        "keymasq:passthrough:1234:5678:mouse",
    ]
