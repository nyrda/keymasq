import evdev
from support import SECOND_PROFILE_NAME, ScenarioContext


def run(ctx: ScenarioContext) -> None:
    ctx.tap_source(evdev.ecodes.KEY_N)
    ctx.wait_for_active_profile(SECOND_PROFILE_NAME, enabled=True)
    ctx.tap_source(evdev.ecodes.KEY_A)
    ctx.expect_keys([(evdev.ecodes.KEY_P, 1), (evdev.ecodes.KEY_P, 0)])

    ctx.tap_source(evdev.ecodes.KEY_N)
    ctx.wait_for_active_profile(SECOND_PROFILE_NAME, enabled=False)
    ctx.tap_source(evdev.ecodes.KEY_A)
    ctx.expect_keys([(evdev.ecodes.KEY_Q, 1), (evdev.ecodes.KEY_Q, 0)])
