import evdev
from support import (
    LOWER_PROFILE_NAME,
    PASSTHROUGH_PROFILE_NAME,
    SECOND_HARDWARE_ID,
    ScenarioContext,
)


def run(ctx: ScenarioContext) -> None:
    passthrough = ctx.open_passthrough_output(SECOND_HARDWARE_ID)
    try:
        ctx.set_profile_enabled(LOWER_PROFILE_NAME, enabled=True)
        ctx.tap_secondary_source(evdev.ecodes.KEY_H)
        ctx.expect_keys([(evdev.ecodes.KEY_6, 1), (evdev.ecodes.KEY_6, 0)])

        ctx.set_profile_enabled(PASSTHROUGH_PROFILE_NAME, enabled=True)
        ctx.tap_secondary_source(evdev.ecodes.KEY_H)
        ctx.expect_no_keyboard_events()
        ctx.expect_events(
            passthrough,
            [
                (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_H, 1),
                (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_H, 0),
            ],
            label="secondary passthrough",
        )
    finally:
        passthrough.close()
        ctx.set_profile_enabled(PASSTHROUGH_PROFILE_NAME, enabled=False)
        ctx.set_profile_enabled(LOWER_PROFILE_NAME, enabled=False)
