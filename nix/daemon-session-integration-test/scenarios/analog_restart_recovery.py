import evdev
from support import ScenarioContext

PROFILE = "Integration Analog Gamepad"


def run(ctx: ScenarioContext) -> None:
    try:
        ctx.set_profile_enabled(PROFILE, enabled=True)
        ctx.source_abs(evdev.ecodes.ABS_X, 16384)
        ctx.expect_gamepad_events([(evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X, 16384)])

        ctx.restart_keymasqd()
        ctx.set_profile_enabled(PROFILE, enabled=True)
        ctx.source_abs(evdev.ecodes.ABS_X, 0)
        ctx.drain_outputs()
        ctx.source_abs(evdev.ecodes.ABS_X, 16384)
        ctx.expect_gamepad_events([(evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X, 16384)])
    finally:
        ctx.source_abs(evdev.ecodes.ABS_X, 0)
        ctx.set_profile_enabled(PROFILE, enabled=False)
