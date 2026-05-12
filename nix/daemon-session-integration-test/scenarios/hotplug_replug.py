import evdev
from support import ScenarioContext


def run(ctx: ScenarioContext) -> None:
    ctx.tap_secondary_source(evdev.ecodes.KEY_G)
    ctx.expect_keys([(evdev.ecodes.KEY_5, 1), (evdev.ecodes.KEY_5, 0)])

    ctx.recreate_secondary_source()
    ctx.tap_secondary_source(evdev.ecodes.KEY_G)
    ctx.expect_keys([(evdev.ecodes.KEY_5, 1), (evdev.ecodes.KEY_5, 0)])
