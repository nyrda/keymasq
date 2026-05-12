import evdev
from support import ScenarioContext


def run(ctx: ScenarioContext) -> None:
    ctx.source_key(evdev.ecodes.KEY_Y, 1)
    try:
        ctx.expect_keys([(evdev.ecodes.KEY_Y, 1)])
        ctx.tap_source(evdev.ecodes.KEY_W)
        ctx.expect_keys([(evdev.ecodes.KEY_Y, 0)])
    finally:
        ctx.source_key(evdev.ecodes.KEY_Y, 0)
