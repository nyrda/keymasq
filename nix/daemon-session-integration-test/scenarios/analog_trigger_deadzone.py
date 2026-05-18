import evdev
from support import ScenarioContext

PROFILE = "Integration Analog Gamepad"


def run(ctx: ScenarioContext) -> None:
    try:
        ctx.set_profile_enabled(PROFILE, enabled=True)
        ctx.source_abs(evdev.ecodes.ABS_Z, 40)
        ctx.expect_no_gamepad_events()

        ctx.source_abs(evdev.ecodes.ABS_Z, 128)
        ctx.expect_gamepad_events([(evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 86)])

        ctx.source_abs(evdev.ecodes.ABS_Z, 0)
        ctx.expect_gamepad_events([(evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 0)])
    finally:
        ctx.source_abs(evdev.ecodes.ABS_Z, 0)
        ctx.set_profile_enabled(PROFILE, enabled=False)
