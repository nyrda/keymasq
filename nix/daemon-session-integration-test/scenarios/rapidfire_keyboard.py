import time

import evdev
from support import ScenarioContext


def run(ctx: ScenarioContext) -> None:
    ctx.source_key(evdev.ecodes.KEY_R, 1)
    time.sleep(0.12)
    ctx.source_key(evdev.ecodes.KEY_R, 0)
    ctx.expect_keys(
        [
            (evdev.ecodes.KEY_I, 1),
            (evdev.ecodes.KEY_I, 0),
            (evdev.ecodes.KEY_I, 1),
            (evdev.ecodes.KEY_I, 0),
        ]
    )
