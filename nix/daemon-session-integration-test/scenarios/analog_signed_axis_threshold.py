import evdev
from support import ScenarioContext

PROFILE = "Integration Analog Signed Axis Threshold"


def run(ctx: ScenarioContext) -> None:
    try:
        ctx.set_profile_enabled(PROFILE, enabled=True)

        ctx.source_abs(evdev.ecodes.ABS_RX, 16000)
        ctx.expect_no_keyboard_events()
        ctx.source_abs(evdev.ecodes.ABS_RX, 22000)
        ctx.expect_keys([(evdev.ecodes.KEY_D, 1)])
        ctx.source_abs(evdev.ecodes.ABS_RX, 14000)
        ctx.expect_keys([(evdev.ecodes.KEY_D, 0)])

        ctx.source_abs(evdev.ecodes.ABS_RX, -16000)
        ctx.expect_no_keyboard_events()
        ctx.source_abs(evdev.ecodes.ABS_RX, -22000)
        ctx.expect_keys([(evdev.ecodes.KEY_A, 1)])
        ctx.source_abs(evdev.ecodes.ABS_RX, -14000)
        ctx.expect_keys([(evdev.ecodes.KEY_A, 0)])
    finally:
        ctx.source_abs(evdev.ecodes.ABS_RX, 0)
        ctx.set_profile_enabled(PROFILE, enabled=False)
