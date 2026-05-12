import evdev
from support import ScenarioContext


def run(ctx: ScenarioContext) -> None:
    ctx.tap_source(evdev.ecodes.KEY_B)
    ctx.expect_keys([(evdev.ecodes.KEY_W, 1), (evdev.ecodes.KEY_W, 0)])
