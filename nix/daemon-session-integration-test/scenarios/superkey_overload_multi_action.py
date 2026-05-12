import evdev
from support import ScenarioContext


def run(ctx: ScenarioContext) -> None:
    ctx.source_key(evdev.ecodes.KEY_E, 1)
    ctx.expect_keys(
        [
            (evdev.ecodes.KEY_LEFTCTRL, 1),
            (evdev.ecodes.KEY_LEFTSHIFT, 1),
            (evdev.ecodes.KEY_1, 1),
            (evdev.ecodes.KEY_1, 0),
            (evdev.ecodes.KEY_2, 1),
            (evdev.ecodes.KEY_2, 0),
        ]
    )

    ctx.source_key(evdev.ecodes.KEY_E, 0)
    ctx.expect_keys(
        [
            (evdev.ecodes.KEY_3, 1),
            (evdev.ecodes.KEY_3, 0),
            (evdev.ecodes.KEY_4, 1),
            (evdev.ecodes.KEY_4, 0),
            (evdev.ecodes.KEY_LEFTCTRL, 0),
            (evdev.ecodes.KEY_LEFTSHIFT, 0),
        ]
    )
