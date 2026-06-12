import evdev
from support import ScenarioContext

PROFILE = "Integration Analog Gamepad Invert"


def run(ctx: ScenarioContext) -> None:
    try:
        ctx.set_profile_enabled(PROFILE, enabled=True)
        ctx.source_abs(evdev.ecodes.ABS_X, 8192)
        ctx.expect_gamepad_events([(evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X, -8192)])
        ctx.source_abs(evdev.ecodes.ABS_Y, -8192)
        ctx.expect_gamepad_events([(evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Y, 8192)])

        ctx.source_abs(evdev.ecodes.ABS_X, 0)
        ctx.expect_gamepad_events([(evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X, 0)])
        ctx.source_abs(evdev.ecodes.ABS_Y, 0)
        ctx.expect_gamepad_events([(evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Y, 0)])
    finally:
        ctx.source_abs(evdev.ecodes.ABS_X, 0)
        ctx.source_abs(evdev.ecodes.ABS_Y, 0)
        ctx.set_profile_enabled(PROFILE, enabled=False)
