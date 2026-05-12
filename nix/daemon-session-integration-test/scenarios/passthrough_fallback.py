import evdev
from support import LOWER_PROFILE_NAME, PASSTHROUGH_PROFILE_NAME, ScenarioContext


def run(ctx: ScenarioContext) -> None:
    try:
        ctx.set_profile_enabled(LOWER_PROFILE_NAME, enabled=True)
        ctx.tap_secondary_source(evdev.ecodes.KEY_H)
        ctx.expect_keys([(evdev.ecodes.KEY_6, 1), (evdev.ecodes.KEY_6, 0)])

        ctx.set_profile_enabled(PASSTHROUGH_PROFILE_NAME, enabled=True)
        ctx.tap_secondary_source(evdev.ecodes.KEY_H)
        ctx.expect_keys([(evdev.ecodes.KEY_6, 1), (evdev.ecodes.KEY_6, 0)])
    finally:
        ctx.set_profile_enabled(PASSTHROUGH_PROFILE_NAME, enabled=False)
        ctx.set_profile_enabled(LOWER_PROFILE_NAME, enabled=False)
