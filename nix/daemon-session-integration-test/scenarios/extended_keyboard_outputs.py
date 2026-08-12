import evdev
from support import (
    EXTENDED_KEYBOARD_OUTPUT_PROFILE_NAME,
    ScenarioContext,
)

OUTPUT_MAPPINGS = [
    *(
        (getattr(evdev.ecodes, f"KEY_F{number}"), getattr(evdev.ecodes, f"KEY_F{number}"))
        for number in range(13, 25)
    ),
    (evdev.ecodes.KEY_A, evdev.ecodes.KEY_PREVIOUSSONG),
    (evdev.ecodes.KEY_B, evdev.ecodes.KEY_PLAYPAUSE),
    (evdev.ecodes.KEY_E, evdev.ecodes.KEY_NEXTSONG),
    (evdev.ecodes.KEY_K, evdev.ecodes.KEY_STOP),
    (evdev.ecodes.KEY_L, evdev.ecodes.KEY_PLAY),
    (evdev.ecodes.KEY_M, evdev.ecodes.KEY_MICMUTE),
    (evdev.ecodes.KEY_N, evdev.ecodes.KEY_BRIGHTNESSDOWN),
    (evdev.ecodes.KEY_O, evdev.ecodes.KEY_BRIGHTNESSUP),
]


def run(ctx: ScenarioContext) -> None:
    try:
        ctx.set_profile_enabled(EXTENDED_KEYBOARD_OUTPUT_PROFILE_NAME, enabled=True)

        if ctx.keyboard_output is None:
            raise AssertionError("keyboard output is not available")
        advertised = set(ctx.keyboard_output.capabilities().get(evdev.ecodes.EV_KEY, []))
        missing = [target for _source, target in OUTPUT_MAPPINGS if target not in advertised]
        if missing:
            names = [evdev.ecodes.KEY.get(code, str(code)) for code in missing]
            raise AssertionError(f"keyboard output does not advertise {names}")

        for source, target in OUTPUT_MAPPINGS:
            ctx.tap_source(source)
            ctx.expect_keys([(target, 1), (target, 0)])
    finally:
        ctx.set_profile_enabled(EXTENDED_KEYBOARD_OUTPUT_PROFILE_NAME, enabled=False)
