import time

import evdev
from support import ScenarioContext

PROFILE = "Integration Analog Mouse Velocity"


def _wait_for_rel(ctx: ScenarioContext, code: int, sign: int) -> None:
    if ctx.mouse_output is None:
        raise AssertionError("output mouse is not available")
    deadline = time.monotonic() + 3.0
    observed: list[tuple[int, int, int]] = []
    while time.monotonic() < deadline:
        for event in ctx.read_output_events(ctx.mouse_output):
            if event.type != evdev.ecodes.EV_REL:
                continue
            event_tuple = (int(event.type), int(event.code), int(event.value))
            observed.append(event_tuple)
            if event.code == code and event.value * sign > 0:
                return
        time.sleep(0.01)
    observed_names = [ctx.event_label(event) for event in observed]
    raise AssertionError(f"missing mouse relative event code={code} sign={sign}: {observed_names}")


def run(ctx: ScenarioContext) -> None:
    try:
        ctx.set_profile_enabled(PROFILE, enabled=True)
        ctx.source_abs(evdev.ecodes.ABS_X, 2000)
        ctx.expect_no_mouse_events()

        ctx.source_abs(evdev.ecodes.ABS_X, 32767)
        _wait_for_rel(ctx, evdev.ecodes.REL_X, 1)

        ctx.source_abs(evdev.ecodes.ABS_X, 0)
        ctx.drain_outputs()
        ctx.expect_no_mouse_events()

        ctx.source_abs(evdev.ecodes.ABS_Y, -32768)
        _wait_for_rel(ctx, evdev.ecodes.REL_Y, 1)
    finally:
        ctx.source_abs(evdev.ecodes.ABS_X, 0)
        ctx.source_abs(evdev.ecodes.ABS_Y, 0)
        ctx.set_profile_enabled(PROFILE, enabled=False)
