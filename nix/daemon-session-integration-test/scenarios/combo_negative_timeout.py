import time

import evdev
from support import ScenarioContext


def run(ctx: ScenarioContext) -> None:
    ctx.tap_source(evdev.ecodes.KEY_H)
    ctx.expect_no_keyboard_events()

    ctx.source_key(evdev.ecodes.KEY_H, 1)
    time.sleep(0.05)
    ctx.source_key(evdev.ecodes.KEY_H, 0)
    time.sleep(0.65)
    ctx.tap_source(evdev.ecodes.KEY_J)
    ctx.expect_no_keyboard_events()
