import evdev
from support import HARDWARE_ID, REPEAT_PROFILE_NAME, ScenarioContext


def run(ctx: ScenarioContext) -> None:
    passthrough = ctx.open_passthrough_output(HARDWARE_ID)
    try:
        ctx.set_profile_enabled(REPEAT_PROFILE_NAME, enabled=True)

        ctx.tap_source(evdev.ecodes.KEY_SPACE)
        ctx.expect_events(
            passthrough,
            [
                (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_SPACE, 1),
                (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_SPACE, 0),
            ],
            label="primary passthrough",
        )

        ctx.tap_source(evdev.ecodes.KEY_F13)
        ctx.expect_keys([(evdev.ecodes.KEY_SPACE, 1), (evdev.ecodes.KEY_SPACE, 0)])
    finally:
        passthrough.close()
        ctx.set_profile_enabled(REPEAT_PROFILE_NAME, enabled=False)
