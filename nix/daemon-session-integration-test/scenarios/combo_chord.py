import time

import evdev
from support import ScenarioContext


def run(ctx: ScenarioContext) -> None:
    ctx.source_key(evdev.ecodes.KEY_C, 1)
    ctx.source_key(evdev.ecodes.KEY_D, 1)
    time.sleep(0.08)
    ctx.source_key(evdev.ecodes.KEY_D, 0)
    ctx.source_key(evdev.ecodes.KEY_C, 0)
    ctx.expect_keys([(evdev.ecodes.KEY_E, 1), (evdev.ecodes.KEY_E, 0)])
