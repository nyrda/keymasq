import evdev
from support import ScenarioContext


def run(ctx: ScenarioContext) -> None:
    ctx.restart_session()
    ctx.tap_source(evdev.ecodes.KEY_A)
    ctx.expect_keys([(evdev.ecodes.KEY_Q, 1), (evdev.ecodes.KEY_Q, 0)])

    ctx.restart_keymasqd()
    ctx.tap_source(evdev.ecodes.KEY_A)
    ctx.expect_keys([(evdev.ecodes.KEY_Q, 1), (evdev.ecodes.KEY_Q, 0)])
