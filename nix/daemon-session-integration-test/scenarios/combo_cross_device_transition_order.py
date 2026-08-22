import time

import evdev
from support import ScenarioContext


def run(ctx: ScenarioContext) -> None:
    for _ in range(10):
        ctx.source_key(evdev.ecodes.KEY_F24, 1)
        ctx.secondary_key(evdev.ecodes.KEY_H, 1)
        ctx.expect_keys([(evdev.ecodes.KEY_F11, 1)], timeout_s=1.0)

        # Release the binding owned by the other evdev task while the long tap
        # child is held, then require its release without waiting for timeout.
        ctx.source_key(evdev.ecodes.KEY_F24, 0)
        ctx.secondary_key(evdev.ecodes.KEY_H, 0)

        ctx.expect_keys([(evdev.ecodes.KEY_F11, 0)], timeout_s=1.0)
        time.sleep(0.01)
