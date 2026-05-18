import evdev
from support import ScenarioContext

PROFILE = "Integration Analog Multi"


def run(ctx: ScenarioContext) -> None:
    try:
        ctx.set_profile_enabled(PROFILE, enabled=True)
        ctx.source_abs(evdev.ecodes.ABS_X, 22000)
        ctx.expect_gamepad_events([(evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X, 22000)])
        ctx.expect_keys([(evdev.ecodes.KEY_D, 1)])

        ctx.source_abs(evdev.ecodes.ABS_X, 0)
        ctx.expect_gamepad_events([(evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X, 0)])
        ctx.expect_keys([(evdev.ecodes.KEY_D, 0)])
    finally:
        ctx.source_abs(evdev.ecodes.ABS_X, 0)
        ctx.set_profile_enabled(PROFILE, enabled=False)
