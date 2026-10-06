from collections.abc import Iterator
from contextlib import contextmanager

import evdev
from support import HARDWARE_ID, ScenarioContext

PROFILE_NAME = "Integration Macro Keyboard Block"
BLOCKING_NAME = "integration-keyboard-block"
OTHER_NAME = "integration-keyboard-block-other"
TRIGGER = evdev.ecodes.KEY_F13
OTHER_TRIGGER = evdev.ecodes.KEY_F14
REMAPPED = evdev.ecodes.KEY_F15
REMAP_TARGET = evdev.ecodes.KEY_9
HELD_BEFORE = evdev.ecodes.KEY_SPACE
STRAY = evdev.ecodes.KEY_F16
HELD_ACROSS = evdev.ecodes.KEY_F17
MACRO_START = evdev.ecodes.KEY_1
MACRO_END = evdev.ecodes.KEY_2
OTHER_MACRO_KEY = evdev.ecodes.KEY_3
EV_KEY = evdev.ecodes.EV_KEY


def _key(code: int, value: int, t_us: int = 0) -> dict[str, object]:
    return {"device_type": "keyboard", "type": EV_KEY, "code": code, "value": value, "t_us": t_us}


def _keys(*pairs: tuple[int, int]) -> list[tuple[int, int, int]]:
    return [(EV_KEY, code, value) for code, value in pairs]


@contextmanager
def _blocking_profile(ctx: ScenarioContext) -> Iterator[evdev.InputDevice]:
    profile_path = ctx.config_dir / "profiles" / "integration-macro-keyboard-block.toml"
    passthrough: evdev.InputDevice | None = None
    try:
        ctx.request(
            {
                "command": "create_macro",
                "macro": {
                    "name": BLOCKING_NAME,
                    "block_keyboard": True,
                    "events": [
                        _key(MACRO_START, 1),
                        _key(MACRO_START, 0),
                        _key(MACRO_END, 1, 2_500_000),
                        _key(MACRO_END, 0, 2_500_000),
                    ],
                },
            }
        )
        ctx.request(
            {
                "command": "create_macro",
                "macro": {
                    "name": OTHER_NAME,
                    "events": [_key(OTHER_MACRO_KEY, 1), _key(OTHER_MACRO_KEY, 0)],
                },
            }
        )
        ctx.write_fixture(
            profile_path,
            "profiles/macro-keyboard-block.toml",
            {
                "HARDWARE_ID": HARDWARE_ID,
                "BLOCK_PROFILE_NAME": PROFILE_NAME,
                "BLOCKING_MACRO_NAME": BLOCKING_NAME,
                "OTHER_MACRO_NAME": OTHER_NAME,
            },
        )
        ctx.request({"command": "reload"})
        ctx.set_profile_enabled(PROFILE_NAME, enabled=True)
        ctx.reopen_outputs()
        passthrough = ctx.open_passthrough_output(HARDWARE_ID)
        ctx.drain_device(passthrough)
        yield passthrough
    finally:
        for code in (HELD_BEFORE, HELD_ACROSS, TRIGGER):
            ctx.source_key(code, 0)
        ctx.request({"command": "cancel_macro_playback"}, ok=False)
        ctx.request({"command": "disable_profile", "profile_name": PROFILE_NAME}, ok=False)
        profile_path.unlink(missing_ok=True)
        ctx.request({"command": "reload"})
        for name in (BLOCKING_NAME, OTHER_NAME):
            ctx.request({"command": "delete_macro", "name": name}, ok=False)
        if passthrough is not None:
            passthrough.close()
        ctx.drain_outputs()


def run(ctx: ScenarioContext) -> None:
    with _blocking_profile(ctx) as passthrough:
        ctx.source_key(HELD_BEFORE, 1)
        ctx.expect_events(passthrough, _keys((HELD_BEFORE, 1)), label="passthrough")

        ctx.tap_source(TRIGGER)
        ctx.expect_exact_keys([(MACRO_START, 1), (MACRO_START, 0)])

        ctx.source_key(HELD_BEFORE, 0)
        ctx.expect_events(passthrough, _keys((HELD_BEFORE, 0)), label="passthrough")
        ctx.tap_source(STRAY)
        ctx.tap_source(REMAPPED)
        ctx.tap_source(OTHER_TRIGGER)
        ctx.source_key(HELD_ACROSS, 1)
        ctx.source_key(HELD_ACROSS, 2)

        ctx.expect_exact_keys(
            [(OTHER_MACRO_KEY, 1), (OTHER_MACRO_KEY, 0), (MACRO_END, 1), (MACRO_END, 0)]
        )

        ctx.source_key(HELD_ACROSS, 2)
        ctx.source_key(HELD_ACROSS, 0)
        ctx.expect_no_events(passthrough, label="passthrough", event_types={EV_KEY})

        ctx.tap_source(HELD_ACROSS)
        ctx.expect_exact_events(
            passthrough,
            _keys((HELD_ACROSS, 1), (HELD_ACROSS, 0)),
            label="passthrough",
            event_types={EV_KEY},
        )
        ctx.tap_source(REMAPPED)
        ctx.expect_exact_keys([(REMAP_TARGET, 1), (REMAP_TARGET, 0)])
