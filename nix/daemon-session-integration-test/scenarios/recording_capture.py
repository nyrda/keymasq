import concurrent.futures
import time

import evdev
from support import HARDWARE_ID, PROFILE_NAME, SECOND_HARDWARE_ID, ScenarioContext


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

    ctx.request({"command": "list_devices_for_recording"})
    start = ctx.request({"command": "start_recording"})
    if start.get("status") != "ok":
        raise AssertionError(f"start_recording failed: {start}")
    ctx.tap_source(evdev.ecodes.KEY_Q)
    stopped = ctx.request({"command": "stop_recording"})
    if stopped.get("status") != "ok":
        raise AssertionError(f"stop_recording failed: {stopped}")
    token = str(stopped.get("pending_save_token", ""))
    saved_name = "integration-recorded-macro"
    ctx.request(
        {
            "command": "save_recording",
            "name": saved_name,
            "pending_save_token": token,
        }
    )
    ctx.request({"command": "play_macro", "name": saved_name})
    ctx.expect_keys([(evdev.ecodes.KEY_Q, 1), (evdev.ecodes.KEY_Q, 0)])


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
