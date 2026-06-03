import evdev
from support import SECOND_HARDWARE_ID, ScenarioContext


def run(ctx: ScenarioContext) -> None:
    ctx.wait_for_hardware_mapping(SECOND_HARDWARE_ID)
    ctx.tap_secondary_source_until_keys(
        evdev.ecodes.KEY_G,
        [(evdev.ecodes.KEY_5, 1), (evdev.ecodes.KEY_5, 0)],
    )

    ctx.recreate_secondary_source()
    ctx.wait_for_hardware_mapping(SECOND_HARDWARE_ID)
    ctx.tap_secondary_source_until_keys(
        evdev.ecodes.KEY_G,
        [(evdev.ecodes.KEY_5, 1), (evdev.ecodes.KEY_5, 0)],
    )
