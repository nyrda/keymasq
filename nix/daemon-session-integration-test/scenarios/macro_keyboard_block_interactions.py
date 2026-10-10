import time
from collections.abc import Iterator
from contextlib import contextmanager

import evdev
from support import HARDWARE_ID, ScenarioContext

PROFILE_NAME = "Integration Macro Keyboard Block Interactions"
BLOCKING_NAME = "integration-keyboard-block-interactions"
TRIGGER = evdev.ecodes.KEY_F13
LEFT_MEMBER = evdev.ecodes.KEY_F15
RIGHT_MEMBER = evdev.ecodes.KEY_F16
RESET = evdev.ecodes.KEY_T
MACRO_START = [(evdev.ecodes.KEY_1, 1), (evdev.ecodes.KEY_1, 0)]
MACRO_END = [(evdev.ecodes.KEY_2, 1), (evdev.ecodes.KEY_2, 0)]
MACRO_TAIL_S = 4.0
LEFT = evdev.ecodes.KEY_LEFT
RIGHT = evdev.ecodes.KEY_RIGHT


def _key(code: int, value: int, t_us: int = 0) -> dict[str, object]:
    return {
        "device_type": "keyboard",
        "type": evdev.ecodes.EV_KEY,
        "code": code,
        "value": value,
        "t_us": t_us,
    }


@contextmanager
def _blocking_profile(ctx: ScenarioContext) -> Iterator[None]:
    profile_path = (
        ctx.config_dir / "profiles" / "integration-macro-keyboard-block-interactions.toml"
    )
    try:
        ctx.request(
            {
                "command": "create_macro",
                "macro": {
                    "name": BLOCKING_NAME,
                    "block_keyboard": True,
                    "events": [
                        _key(evdev.ecodes.KEY_1, 1),
                        _key(evdev.ecodes.KEY_1, 0),
                        _key(evdev.ecodes.KEY_2, 1, int(MACRO_TAIL_S * 1_000_000)),
                        _key(evdev.ecodes.KEY_2, 0, int(MACRO_TAIL_S * 1_000_000)),
                    ],
                },
            }
        )
        ctx.write_fixture(
            profile_path,
            "profiles/macro-keyboard-block-interactions.toml",
            {
                "HARDWARE_ID": HARDWARE_ID,
                "BLOCK_PROFILE_NAME": PROFILE_NAME,
                "BLOCKING_MACRO_NAME": BLOCKING_NAME,
            },
        )
        ctx.request({"command": "reload"})
        ctx.set_profile_enabled(PROFILE_NAME, enabled=True)
        ctx.wait_for_hardware_mapping(HARDWARE_ID)
        ctx.reopen_outputs()
        yield
    finally:
        for code in (LEFT_MEMBER, RIGHT_MEMBER, RESET, TRIGGER):
            ctx.source_key(code, 0)
        ctx.request({"command": "cancel_macro_playback"}, ok=False)
        ctx.request({"command": "disable_profile", "profile_name": PROFILE_NAME}, ok=False)
        profile_path.unlink(missing_ok=True)
        ctx.request({"command": "reload"})
        ctx.request({"command": "delete_macro", "name": BLOCKING_NAME}, ok=False)
        ctx.drain_outputs()


def run_rollover(ctx: ScenarioContext) -> None:
    with _blocking_profile(ctx):
        ctx.tap_source(TRIGGER)
        ctx.expect_exact_keys(MACRO_START)
        ctx.source_key(RIGHT_MEMBER, 1)
        ctx.expect_exact_keys(MACRO_END)

        ctx.source_key(LEFT_MEMBER, 1)
        ctx.expect_exact_keys([(LEFT, 1)])
        ctx.source_key(LEFT_MEMBER, 0)
        ctx.expect_exact_keys([(LEFT, 0), (RIGHT, 1)])
        ctx.source_key(RIGHT_MEMBER, 0)
        ctx.expect_exact_keys([(RIGHT, 0)])


def run_emergency_reset(ctx: ScenarioContext) -> None:
    with _blocking_profile(ctx):
        ctx.tap_source(TRIGGER)
        ctx.expect_exact_keys(MACRO_START)
        started = time.monotonic()
        ctx.tap_source(LEFT_MEMBER)
        ctx.tap_source(RESET)
        ctx.expect_no_keyboard_events(timeout_s=0.3)

        ctx.wait_for_active_profile(PROFILE_NAME, enabled=True)
        ctx.request({"command": "reevaluate_hardware"})
        ctx.wait_for_hardware_mapping(HARDWARE_ID)
        ctx.reopen_outputs()

        ctx.tap_source(LEFT_MEMBER)
        ctx.expect_exact_keys([(LEFT, 1), (LEFT, 0)])
        remaining = MACRO_TAIL_S - (time.monotonic() - started)
        if remaining <= 0:
            raise AssertionError("keyboard checked only after the canceled macro would have ended")
        ctx.expect_no_keyboard_events(timeout_s=remaining + 0.5)
        ctx.tap_source(TRIGGER)
        ctx.expect_exact_keys(MACRO_START)
        ctx.expect_exact_keys(MACRO_END)
