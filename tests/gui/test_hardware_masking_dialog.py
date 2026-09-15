from types import SimpleNamespace

import pytest

from keymasq.common.masking import HARDWARE_COMMAND_TIMEOUT
from keymasq.gui.widgets import hardware_masking_dialog as module


@pytest.fixture
def dialog(monkeypatch):
    monkeypatch.setattr(module, "session_request_async", lambda *_args, **_kwargs: None)
    dialog = module.HardwareMaskingPanel()
    yield dialog
    dialog.close()


def response(*masks) -> dict:
    return {
        "status": "ok",
        "available": True,
        "devices": [
            {"id": name, "generation": "gen", "name": name, "supported": True}
            for name in ("first", "second", "third")
        ],
        "masks": list(masks),
    }


@pytest.mark.parametrize("command", ["mask_hardware", "restore_hardware"])
def test_changes_and_followup_inventory_outwait_the_session(dialog, monkeypatch, command):
    requests = []
    monkeypatch.setattr(
        module,
        "session_request_async",
        lambda payload, callback, *, timeout: requests.append((payload, callback, timeout)),
    )
    dialog._render(response())
    dialog._change(command, {"id": "first"})
    payload, changed, timeout = requests.pop()
    assert payload["command"] == command
    assert timeout > HARDWARE_COMMAND_TIMEOUT
    assert "first" in dialog._pending
    assert not dialog._errors

    enabled = command == "mask_hardware"
    result = response(
        {"id": "first", "state": "masked" if enabled else "restored", "enabled": enabled}
    )
    changed(result)
    assert "first" not in dialog._pending
    assert not dialog._errors
    payload, loaded, timeout = requests.pop()
    assert payload["command"] == "hardware_inventory"
    assert timeout > HARDWARE_COMMAND_TIMEOUT
    loaded(result)
    assert dialog._rows["first"].switch.get_active() is enabled


def test_switches_control_only_their_device_and_countdowns_preserve_controls(dialog, monkeypatch):
    calls = []
    monkeypatch.setattr(
        dialog, "_authorized_change", lambda command, data: calls.append((command, data))
    )
    state = response(
        {"id": "first", "state": "masked", "token": "one", "enabled": True},
        {"id": "second", "state": "trial", "token": "two", "remaining_seconds": 20},
    )
    dialog._render(state)
    first, second, third = (dialog._rows[name] for name in ("first", "second", "third"))
    third.switch.set_active(True)
    assert calls.pop() == ("mask_hardware", {"id": "third", "generation": "gen", "persist": True})
    # Pending authentication cannot leave an unmasked device's switch on.
    assert not third.switch.get_active()
    second.expand.emit("clicked")
    assert not calls
    state["masks"][1]["remaining_seconds"] = 19
    dialog._render(state)
    assert dialog._rows["second"] is second
    assert second.details.get_visible()
    assert second.undo.get_label() == "Undo · 19s"
    second.keep.emit("clicked")
    assert calls.pop() == ("keep_hardware_mask", {"id": "second", "token": "two", "persist": True})
    first.switch.set_active(False)
    assert calls.pop() == ("restore_hardware", {"id": "first", "token": "one", "persist": False})


def test_saved_disconnected_device_stays_on_and_can_be_turned_off(dialog, monkeypatch):
    calls = []
    monkeypatch.setattr(
        dialog, "_authorized_change", lambda command, data: calls.append((command, data))
    )
    state = response(
        {
            "id": "first",
            "state": "restored",
            "token": "one",
            "enabled": True,
            "persist": True,
            "has_saved_mask": True,
            "reason": "hardware_disconnected",
        }
    )
    state["devices"][0]["supported"] = False
    dialog._render(state)
    row = dialog._rows["first"]
    assert row.switch.get_active()
    assert row.switch.get_sensitive()
    assert row.status.get_text() == "Waiting for device"
    assert not row.confirmation.get_visible()
    row.switch.set_active(False)
    assert calls.pop()[1] == {"id": "first", "token": "one", "persist": False}
    state["masks"][0].update(enabled=False, persist=False)
    dialog._render(state)
    row.switch.set_active(True)
    assert calls.pop() == ("mask_hardware", {"id": "first", "persist": True})


@pytest.mark.parametrize("globally_paused", [False, True])
def test_saved_mask_stopped_after_restart_cannot_look_active(dialog, globally_paused):
    state = response(
        {
            "id": "first",
            "state": "restored",
            "enabled": True,
            "remapping_suspended": True,
            "reason": "service_stopped",
        }
    )
    state["remapping_suspended"] = globally_paused
    dialog._render(state)
    row = dialog._rows["first"]
    assert row.switch.get_active() is not globally_paused
    assert row.status.get_visible() is not globally_paused
    assert row.status.get_text() == (
        "Off · remapping stopped by recovery" if globally_paused else "Not masked"
    )
    assert not row.failure.get_visible()


@pytest.mark.parametrize(
    ("process", "display"),
    [
        ("steam", "Steam"),
        ("steamwebhelper", "Steam"),
        ("winedevice.exe", "Wine"),
        ("wine64-preloader", "Wine"),
        ("wine-preloader", "Wine"),
        ("custom-controller-app", "custom-controller-app"),
        (None, "Another app"),
        ("", "Another app"),
    ],
)
def test_blocked_app_has_readable_error_and_diagnostics_in_details(
    dialog, monkeypatch, process, display
):
    calls = []
    monkeypatch.setattr(
        dialog, "_authorized_change", lambda command, data: calls.append((command, data))
    )
    detail = (
        "Process 1234 has a direct USB or auxiliary HID connection. "
        "Close that application and retry masking."
    )
    dialog._render(
        response(
            {
                "id": "first",
                "state": "restored",
                "enabled": False,
                "error": detail,
                "error_code": "device_in_use",
                "blocking_application": process,
            }
        )
    )
    row = dialog._rows["first"]
    assert not row.switch.get_active()
    assert row.status.get_text() == "Couldn’t mask"
    assert row.error.get_text() == f"{display} is using this device directly. Close it, then retry."
    assert "1234" not in row.error.get_text()
    assert detail not in row.technical.get_text()
    assert not row.technical.get_selectable()
    copied = []
    monkeypatch.setattr(row, "get_clipboard", lambda: SimpleNamespace(set=copied.append))
    row.expand.emit("clicked")
    row.copy.emit("clicked")
    assert detail in copied[0]
    row.expand.emit("clicked")
    assert not row.details.get_visible()
    row.retry.emit("clicked")
    assert calls.pop()[0] == "mask_hardware"


@pytest.mark.parametrize("automatic", [False, True])
@pytest.mark.parametrize("phase", ["applying", "acquiring", "trial", "masked"])
def test_confirmation_waits_for_manual_trial(dialog, automatic, phase):
    dialog._render(
        response(
            {
                "id": "first",
                "state": phase,
                "automatic": automatic,
                "enabled": True,
                "remaining_seconds": 25,
            }
        )
    )
    row = dialog._rows["first"]
    assert row.switch.get_active()
    assert row.confirmation.get_visible() is (phase == "trial" and not automatic)
    if phase in {"applying", "acquiring"}:
        assert row.status.get_text() == "Reconnecting device…"


def test_cancelled_unlock_does_not_change_switch_or_send_mask_request(dialog, monkeypatch):
    prompts = []
    changes = []
    dialog._parent = SimpleNamespace(
        _recording_unlocked=False, present_unlock_dialog=lambda **kwargs: prompts.append(kwargs)
    )
    monkeypatch.setattr(dialog, "_change", lambda *args: changes.append(args))
    dialog._render(response())
    dialog._rows["first"].switch.set_active(True)
    assert len(prompts) == 1
    assert not changes
    assert not dialog._rows["first"].switch.get_active()
