import evdev
from support import ScenarioContext

PROFILE = "Integration Analog Mouse Area"


def run(ctx: ScenarioContext) -> None:
    try:
        ctx.set_profile_enabled(PROFILE, enabled=True)
        ctx.source_abs(evdev.ecodes.ABS_X, 32767)
        ctx.expect_mouse_events([(evdev.ecodes.EV_REL, evdev.ecodes.REL_X, 100)])

        ctx.source_abs(evdev.ecodes.ABS_X, 0)
        ctx.expect_mouse_events([(evdev.ecodes.EV_REL, evdev.ecodes.REL_X, -100)])
    finally:
        ctx.source_abs(evdev.ecodes.ABS_X, 0)
        ctx.source_abs(evdev.ecodes.ABS_Y, 0)
        ctx.set_profile_enabled(PROFILE, enabled=False)
