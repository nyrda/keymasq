# ruff: noqa: F403, F405, I001
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
