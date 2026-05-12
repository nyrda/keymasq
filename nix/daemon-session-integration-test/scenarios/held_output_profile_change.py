import evdev
from support import SECOND_PROFILE_NAME, ScenarioContext


def run(ctx: ScenarioContext) -> None:
    try:
        ctx.source_key(evdev.ecodes.KEY_A, 1)
        ctx.expect_keys([(evdev.ecodes.KEY_Q, 1)])

        ctx.tap_source(evdev.ecodes.KEY_N)
        ctx.wait_for_active_profile(SECOND_PROFILE_NAME, enabled=True)

        ctx.source_key(evdev.ecodes.KEY_A, 0)
        ctx.expect_keys([(evdev.ecodes.KEY_Q, 0)])

        ctx.tap_source(evdev.ecodes.KEY_A)
        ctx.expect_keys([(evdev.ecodes.KEY_P, 1), (evdev.ecodes.KEY_P, 0)])
    finally:
        ctx.source_key(evdev.ecodes.KEY_A, 0)
        ctx.set_profile_enabled(SECOND_PROFILE_NAME, enabled=False)
