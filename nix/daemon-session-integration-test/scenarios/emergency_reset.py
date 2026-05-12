import time

import evdev
from support import PROFILE_NAME, ScenarioContext


def run(ctx: ScenarioContext) -> None:
    try:
        ctx.source_key(evdev.ecodes.KEY_A, 1)
        ctx.expect_keys([(evdev.ecodes.KEY_Q, 1)])

        ctx.source_key(evdev.ecodes.KEY_O, 1)
        ctx.expect_mouse_events([(evdev.ecodes.EV_KEY, evdev.ecodes.BTN_LEFT, 1)])

        ctx.source_key(evdev.ecodes.KEY_S, 1)
        ctx.expect_gamepad_events([(evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SOUTH, 1)])

        ctx.tap_source(evdev.ecodes.KEY_T)
        ctx.expect_keys([(evdev.ecodes.KEY_Q, 0)])
        ctx.expect_mouse_events([(evdev.ecodes.EV_KEY, evdev.ecodes.BTN_LEFT, 0)])
        ctx.expect_gamepad_events([(evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SOUTH, 0)])
    finally:
        for code in (evdev.ecodes.KEY_A, evdev.ecodes.KEY_O, evdev.ecodes.KEY_S):
            ctx.source_key(code, 0)

    time.sleep(0.5)
    ctx.wait_for_active_profile(PROFILE_NAME, enabled=True)
    ctx.request({"command": "reevaluate_hardware"})
    ctx.reopen_outputs()
    ctx.tap_source(evdev.ecodes.KEY_A)
    ctx.expect_keys([(evdev.ecodes.KEY_Q, 1), (evdev.ecodes.KEY_Q, 0)])
