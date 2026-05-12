import evdev
from support import ScenarioContext


def run(ctx: ScenarioContext) -> None:
    ctx.tap_source(evdev.ecodes.KEY_K)
    ctx.expect_keys([(evdev.ecodes.KEY_Y, 1), (evdev.ecodes.KEY_Y, 0)])
