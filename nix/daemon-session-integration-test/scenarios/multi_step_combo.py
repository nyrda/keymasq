import evdev
from support import ScenarioContext


def run(ctx: ScenarioContext) -> None:
    ctx.tap_source(evdev.ecodes.KEY_H, pause_s=0.05)
    ctx.tap_source(evdev.ecodes.KEY_J, pause_s=0.05)
    ctx.expect_keys([(evdev.ecodes.KEY_T, 1), (evdev.ecodes.KEY_T, 0)])
