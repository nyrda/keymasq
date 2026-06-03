import concurrent.futures
import time

import evdev
from support import (
    HARDWARE_ID,
    MACRO_SLOT_PROFILE_NAME,
    PROFILE_NAME,
    SECOND_HARDWARE_ID,
    ScenarioContext,
)

RECORDING_SLOT = 1


def run(ctx: ScenarioContext) -> None:
    capture = ctx.request({"command": "begin_capture", "hardware_id": HARDWARE_ID})
    if capture.get("status") != "ok":
        raise AssertionError(f"begin_capture failed: {capture}")
    try:
        ctx.source_key(evdev.ecodes.KEY_Q, 1)
        captured = wait_for_capture(ctx, HARDWARE_ID)
        ctx.source_key(evdev.ecodes.KEY_Q, 0)
        if (
            captured.get("evdev") != "key_q"
            or captured.get("code") != evdev.ecodes.KEY_Q
            or captured.get("device_path") != ctx.source.device.path
        ):
            raise AssertionError(f"unexpected captured event: {captured}")
    finally:
        ctx.request({"command": "end_capture", "hardware_id": HARDWARE_ID}, ok=False)

    ctx.request({"command": "reevaluate_hardware"})
    ctx.reopen_outputs()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            lambda: ctx.request(
                {"command": "capture_combo", "profile_name": PROFILE_NAME, "timeout_s": 3.0},
                timeout=5.0,
            )
        )
        time.sleep(0.2)
        ctx.source_key(evdev.ecodes.KEY_Q, 1)
        ctx.secondary_key(evdev.ecodes.KEY_F, 1)
        time.sleep(0.05)
        ctx.secondary_key(evdev.ecodes.KEY_F, 0)
        ctx.source_key(evdev.ecodes.KEY_Q, 0)
        combo = future.result(timeout=6)

    events = {
        (str(event.get("hardware_id")), str(event.get("evdev")))
        for event in combo.get("events", [])
        if isinstance(event, dict)
    }
    expected = {(HARDWARE_ID, "key_q"), (SECOND_HARDWARE_ID, "key_f")}
    if events != expected:
        raise AssertionError(f"unexpected captured combo events: {combo}")

    status = ctx.request({"command": "get_status"})
    if status.get("macro_recording_enabled") is not True:
        raise AssertionError(f"macro recording opt-in was not enabled: {status}")

    ctx.request({"command": "list_devices_for_recording"})
    start = ctx.request({"command": "start_recording", "recording_slot": RECORDING_SLOT})
    if start.get("status") != "ok":
        raise AssertionError(f"start_recording failed: {start}")
    if start.get("recording_slot") != RECORDING_SLOT:
        raise AssertionError(f"recording started in unexpected slot: {start}")
    ctx.tap_source(evdev.ecodes.KEY_Q)
    stopped = ctx.request({"command": "stop_recording", "recording_slot": RECORDING_SLOT})
    if stopped.get("status") != "ok":
        raise AssertionError(f"stop_recording failed: {stopped}")
    if stopped.get("recording_slot") != RECORDING_SLOT:
        raise AssertionError(f"recording stopped in unexpected slot: {stopped}")
    token = str(stopped.get("pending_save_token", ""))
    if not token:
        raise AssertionError(f"stop_recording did not return a pending save token: {stopped}")
    assert_recording_slot_listed(ctx, RECORDING_SLOT)

    saved_name = "integration-recorded-macro"
    ctx.request({"command": "delete_macro", "name": saved_name}, ok=False)
    try:
        ctx.request(
            {
                "command": "save_recording",
                "name": saved_name,
                "recording_slot": RECORDING_SLOT,
                "pending_save_token": token,
            }
        )
        assert_recording_slot_listed(ctx, RECORDING_SLOT)
        ctx.request({"command": "play_macro", "name": saved_name})
        ctx.expect_keys([(evdev.ecodes.KEY_Q, 1), (evdev.ecodes.KEY_Q, 0)])
    finally:
        ctx.request({"command": "delete_macro", "name": saved_name}, ok=False)


def run_mapped_slot_actions(ctx: ScenarioContext) -> None:
    try:
        ctx.set_profile_enabled(MACRO_SLOT_PROFILE_NAME, enabled=True)
        assert_macro_recording_enabled(ctx)

        ctx.tap_source(evdev.ecodes.KEY_F23)
        wait_for_recording_state(ctx, active=True, recording_slot=RECORDING_SLOT)

        ctx.tap_source(evdev.ecodes.KEY_Q)

        ctx.tap_source(evdev.ecodes.KEY_F23)
        wait_for_recording_state(ctx, active=False, recording_slot=0)
        assert_recording_slot_listed(ctx, RECORDING_SLOT)

        ctx.drain_outputs()
        ctx.tap_source(evdev.ecodes.KEY_F24)
        ctx.expect_keys([(evdev.ecodes.KEY_Q, 1), (evdev.ecodes.KEY_Q, 0)])
    finally:
        ctx.request({"command": "stop_recording", "recording_slot": RECORDING_SLOT}, ok=False)
        ctx.set_profile_enabled(MACRO_SLOT_PROFILE_NAME, enabled=False)


def run_mapped_slot_playback_without_unlock(ctx: ScenarioContext) -> None:
    try:
        ctx.enable_macro_recording_opt_in()
        assert_macro_recording_enabled(ctx)
        assert_recording_locked_if_required(ctx)

        ctx.set_profile_enabled(MACRO_SLOT_PROFILE_NAME, enabled=True)
        assert_macro_recording_enabled(ctx)

        ctx.tap_source(evdev.ecodes.KEY_F23)
        wait_for_recording_state(ctx, active=True, recording_slot=RECORDING_SLOT)

        ctx.tap_source(evdev.ecodes.KEY_Q)

        ctx.tap_source(evdev.ecodes.KEY_F23)
        wait_for_recording_state(ctx, active=False, recording_slot=0)
        assert_recording_slot_listed(ctx, RECORDING_SLOT)

        assert_recording_locked_if_required(ctx)

        ctx.drain_outputs()
        ctx.tap_source(evdev.ecodes.KEY_F24)
        ctx.expect_keys([(evdev.ecodes.KEY_Q, 1), (evdev.ecodes.KEY_Q, 0)])
    finally:
        ctx.request({"command": "stop_recording", "recording_slot": RECORDING_SLOT}, ok=False)
        ctx.set_profile_enabled(MACRO_SLOT_PROFILE_NAME, enabled=False)


def assert_macro_recording_enabled(ctx: ScenarioContext) -> None:
    status = ctx.request({"command": "get_status"})
    if status.get("macro_recording_enabled") is not True:
        raise AssertionError(f"macro recording opt-in was not enabled: {status}")


def wait_for_recording_state(
    ctx: ScenarioContext,
    *,
    active: bool,
    recording_slot: int,
) -> None:
    def matches() -> bool:
        status = ctx.request({"command": "get_status"}, ok=False)
        if status.get("recording_active") is not active:
            return False
        return int(status.get("recording_slot", 0) or 0) == recording_slot

    label = f"recording active={active} slot={recording_slot}"
    ctx.wait_until(label, matches, timeout_s=5)


def assert_recording_locked_if_required(ctx: ScenarioContext) -> None:
    status = ctx.request({"command": "get_status"})
    if status.get("recording_unlock_required") is True:
        if status.get("recording_unlocked") is not False:
            raise AssertionError(f"capture unlock was unexpectedly active: {status}")


def assert_recording_slot_listed(ctx: ScenarioContext, recording_slot: int) -> None:
    result = ctx.request({"command": "list_macros", "include_slots": True})
    macros = result.get("macros", [])
    if not isinstance(macros, list):
        raise AssertionError(f"list_macros did not return a macro list: {result}")

    for macro in macros:
        if not isinstance(macro, dict):
            continue
        if (
            macro.get("kind") == "recording_slot"
            and macro.get("recording_slot") == recording_slot
        ):
            return

    raise AssertionError(f"recording slot {recording_slot} was not listed: {result}")


def wait_for_capture(ctx: ScenarioContext, hardware_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 3
    last: dict[str, object] | None = None
    while time.monotonic() < deadline:
        result = ctx.request({"command": "capture_read", "hardware_id": hardware_id}, ok=False)
        captured = result.get("captured")
        if isinstance(captured, dict):
            return captured
        last = result
        time.sleep(0.1)
    raise AssertionError(f"capture_read did not return an event: {last}")
