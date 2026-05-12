import time

import evdev
from support import ScenarioContext


def run(ctx: ScenarioContext) -> None:
    ctx.source_key(evdev.ecodes.KEY_Q, 1)
    ctx.secondary_key(evdev.ecodes.KEY_F, 1)
    ctx.expect_keys([(evdev.ecodes.KEY_F10, 1)])
    time.sleep(0.05)
    ctx.secondary_key(evdev.ecodes.KEY_F, 0)
    ctx.source_key(evdev.ecodes.KEY_Q, 0)
    ctx.expect_keys([(evdev.ecodes.KEY_F10, 0)])
