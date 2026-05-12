import time

import evdev
from support import ScenarioContext


def run(ctx: ScenarioContext) -> None:
    ctx.secondary_key(evdev.ecodes.KEY_A, 1)
    ctx.secondary_key(evdev.ecodes.KEY_B, 1)
    ctx.expect_keys([(evdev.ecodes.KEY_F6, 1)])
    ctx.secondary_key(evdev.ecodes.KEY_C, 1)
    ctx.expect_keys([(evdev.ecodes.KEY_F7, 1)])
    ctx.secondary_key(evdev.ecodes.KEY_C, 0)
    ctx.secondary_key(evdev.ecodes.KEY_B, 0)
    ctx.secondary_key(evdev.ecodes.KEY_A, 0)
    ctx.expect_keys([(evdev.ecodes.KEY_F7, 0), (evdev.ecodes.KEY_F6, 0)])

    time.sleep(0.05)
    ctx.secondary_key(evdev.ecodes.KEY_A, 1)
    ctx.secondary_key(evdev.ecodes.KEY_D, 1)
    ctx.expect_keys([(evdev.ecodes.KEY_F8, 1)])
    ctx.secondary_key(evdev.ecodes.KEY_D, 0)
    ctx.secondary_key(evdev.ecodes.KEY_A, 0)
    ctx.expect_keys([(evdev.ecodes.KEY_F8, 0)])

    ctx.secondary_key(evdev.ecodes.KEY_A, 1)
    ctx.secondary_key(evdev.ecodes.KEY_E, 1)
    ctx.expect_keys([(evdev.ecodes.KEY_F9, 1)])
    ctx.secondary_key(evdev.ecodes.KEY_E, 0)
    ctx.secondary_key(evdev.ecodes.KEY_A, 0)
    ctx.expect_keys([(evdev.ecodes.KEY_F9, 0)])
