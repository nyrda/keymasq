import time

import evdev
from support import ScenarioContext


def run(ctx: ScenarioContext) -> None:
    ctx.source_key(evdev.ecodes.KEY_F, 1)
    ctx.source_key(evdev.ecodes.KEY_G, 1)
    time.sleep(0.08)
    ctx.source_key(evdev.ecodes.KEY_G, 0)
    ctx.source_key(evdev.ecodes.KEY_F, 0)
    ctx.expect_keys([(evdev.ecodes.KEY_R, 1), (evdev.ecodes.KEY_R, 0)])
