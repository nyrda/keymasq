import time

import evdev
from support import REPEAT_PROFILE_NAME, ScenarioContext


def run(ctx: ScenarioContext) -> None:
    try:
        ctx.set_profile_enabled(REPEAT_PROFILE_NAME, enabled=True)
        ctx.subtest(
            "repeat replays combo output from a mapped repeat key",
            lambda: _mapped_repeat_replays_combo_action(ctx),
        )
        ctx.subtest(
            "combo repeat action replays mapped keyboard output",
            lambda: _combo_repeat_replays_keyboard_action(ctx),
        )
        ctx.subtest(
            "repeat replays pattern superkey path",
            lambda: _repeat_replays_pattern_superkey(ctx),
        )
        ctx.subtest(
            "combo repeat action replays pattern superkey path",
            lambda: _combo_repeat_replays_pattern_superkey(ctx),
        )
        ctx.subtest(
            "repeat replays overload superkey path",
            lambda: _repeat_replays_overload_superkey(ctx),
        )
        ctx.subtest(
            "combo repeat action replays overload superkey path",
            lambda: _combo_repeat_replays_overload_superkey(ctx),
        )
    finally:
        ctx.set_profile_enabled(REPEAT_PROFILE_NAME, enabled=False)


def _mapped_repeat_replays_combo_action(ctx: ScenarioContext) -> None:
    _trigger_primary_combo(ctx, evdev.ecodes.KEY_C, evdev.ecodes.KEY_D)
    ctx.expect_keys([(evdev.ecodes.KEY_E, 1), (evdev.ecodes.KEY_E, 0)])

    ctx.tap_source(evdev.ecodes.KEY_F13)
    ctx.expect_keys([(evdev.ecodes.KEY_E, 1), (evdev.ecodes.KEY_E, 0)])


def _combo_repeat_replays_keyboard_action(ctx: ScenarioContext) -> None:
    ctx.tap_source(evdev.ecodes.KEY_F19)
    ctx.expect_keys([(evdev.ecodes.KEY_A, 1), (evdev.ecodes.KEY_A, 0)])

    _trigger_repeat_combo(ctx)
    ctx.expect_keys([(evdev.ecodes.KEY_A, 1), (evdev.ecodes.KEY_A, 0)])


def _repeat_replays_pattern_superkey(ctx: ScenarioContext) -> None:
    ctx.tap_source(evdev.ecodes.KEY_B)
    ctx.expect_keys([(evdev.ecodes.KEY_W, 1), (evdev.ecodes.KEY_W, 0)])

    ctx.tap_source(evdev.ecodes.KEY_F13)
    ctx.expect_keys([(evdev.ecodes.KEY_W, 1), (evdev.ecodes.KEY_W, 0)])


def _combo_repeat_replays_pattern_superkey(ctx: ScenarioContext) -> None:
    ctx.tap_source(evdev.ecodes.KEY_B)
    ctx.expect_keys([(evdev.ecodes.KEY_W, 1), (evdev.ecodes.KEY_W, 0)])

    _trigger_repeat_combo(ctx)
    ctx.expect_keys([(evdev.ecodes.KEY_W, 1), (evdev.ecodes.KEY_W, 0)])


def _repeat_replays_overload_superkey(ctx: ScenarioContext) -> None:
    _press_overload_superkey(ctx)
    _release_overload_superkey(ctx)

    ctx.tap_source(evdev.ecodes.KEY_F13)
    _expect_overload_full_cycle(ctx)


def _combo_repeat_replays_overload_superkey(ctx: ScenarioContext) -> None:
    _press_overload_superkey(ctx)
    _release_overload_superkey(ctx)

    _trigger_repeat_combo(ctx)
    _expect_combo_overload_full_cycle(ctx)


def _press_overload_superkey(ctx: ScenarioContext) -> None:
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


def _release_overload_superkey(ctx: ScenarioContext) -> None:
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


def _expect_overload_full_cycle(ctx: ScenarioContext) -> None:
    ctx.expect_keys(
        [
            (evdev.ecodes.KEY_LEFTCTRL, 1),
            (evdev.ecodes.KEY_LEFTSHIFT, 1),
            (evdev.ecodes.KEY_1, 1),
            (evdev.ecodes.KEY_1, 0),
            (evdev.ecodes.KEY_2, 1),
            (evdev.ecodes.KEY_2, 0),
            (evdev.ecodes.KEY_3, 1),
            (evdev.ecodes.KEY_3, 0),
            (evdev.ecodes.KEY_4, 1),
            (evdev.ecodes.KEY_4, 0),
            (evdev.ecodes.KEY_LEFTCTRL, 0),
            (evdev.ecodes.KEY_LEFTSHIFT, 0),
        ]
    )


def _expect_combo_overload_full_cycle(ctx: ScenarioContext) -> None:
    ctx.expect_keys(
        [
            (evdev.ecodes.KEY_LEFTCTRL, 1),
            (evdev.ecodes.KEY_LEFTSHIFT, 1),
            (evdev.ecodes.KEY_1, 1),
            (evdev.ecodes.KEY_1, 0),
            (evdev.ecodes.KEY_2, 1),
            (evdev.ecodes.KEY_2, 0),
            (evdev.ecodes.KEY_3, 1),
            (evdev.ecodes.KEY_3, 0),
            (evdev.ecodes.KEY_4, 1),
            (evdev.ecodes.KEY_4, 0),
            (evdev.ecodes.KEY_LEFTSHIFT, 0),
            (evdev.ecodes.KEY_LEFTCTRL, 0),
        ]
    )


def _trigger_primary_combo(ctx: ScenarioContext, first_code: int, second_code: int) -> None:
    ctx.source_key(first_code, 1)
    ctx.source_key(second_code, 1)
    time.sleep(0.08)
    ctx.source_key(second_code, 0)
    ctx.source_key(first_code, 0)


def _trigger_repeat_combo(ctx: ScenarioContext) -> None:
    ctx.secondary_key(evdev.ecodes.KEY_F13, 1)
    ctx.secondary_key(evdev.ecodes.KEY_F14, 1)
    time.sleep(0.08)
    ctx.secondary_key(evdev.ecodes.KEY_F14, 0)
    ctx.secondary_key(evdev.ecodes.KEY_F13, 0)
