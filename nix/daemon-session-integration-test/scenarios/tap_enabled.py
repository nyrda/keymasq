import evdev
from support import ScenarioContext


def run(ctx: ScenarioContext) -> None:
    ctx.source_key(evdev.ecodes.KEY_M, 1)
    ctx.expect_keys([(evdev.ecodes.KEY_O, 1), (evdev.ecodes.KEY_O, 0)])
    ctx.source_key(evdev.ecodes.KEY_M, 0)
