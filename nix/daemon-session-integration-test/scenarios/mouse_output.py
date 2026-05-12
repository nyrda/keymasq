import time

import evdev
from support import ScenarioContext


def run(ctx: ScenarioContext) -> None:
    ctx.tap_source(evdev.ecodes.KEY_O)
    ctx.expect_mouse_events(
        [
            (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_LEFT, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_LEFT, 0),
        ]
    )

    ctx.tap_source(evdev.ecodes.KEY_P)
    ctx.expect_mouse_events(
        [
            (evdev.ecodes.EV_REL, evdev.ecodes.REL_X, 12),
            (evdev.ecodes.EV_REL, evdev.ecodes.REL_Y, -7),
        ]
    )

    ctx.tap_source(evdev.ecodes.KEY_V)
    ctx.expect_mouse_events([(evdev.ecodes.EV_REL, evdev.ecodes.REL_WHEEL, 1)])

    ctx.source_key(evdev.ecodes.KEY_X, 1)
    ctx.source_key(evdev.ecodes.KEY_Z, 1)
    time.sleep(0.08)
    ctx.source_key(evdev.ecodes.KEY_Z, 0)
    ctx.source_key(evdev.ecodes.KEY_X, 0)
    ctx.expect_mouse_events(
        [
            (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_RIGHT, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_RIGHT, 0),
        ]
    )
