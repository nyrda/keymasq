import evdev
from support import ScenarioContext


def run(ctx: ScenarioContext) -> None:
    ctx.tap_source(evdev.ecodes.KEY_S)
    ctx.expect_gamepad_events(
        [
            (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SOUTH, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SOUTH, 0),
        ]
    )

    ctx.tap_source(evdev.ecodes.KEY_U)
    ctx.expect_gamepad_events(
        [
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 255),
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 0),
        ]
    )

    ctx.tap_source(evdev.ecodes.KEY_I)
    ctx.expect_gamepad_events(
        [
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_RZ, 255),
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_RZ, 0),
        ]
    )
