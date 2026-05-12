import evdev
from support import ScenarioContext


def run(ctx: ScenarioContext) -> None:
    ctx.tap_source(evdev.ecodes.KEY_L)
    ctx.expect_no_keyboard_events()
