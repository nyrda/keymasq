import evdev
from support import REPEAT_PROFILE_NAME, SECOND_PROFILE_NAME, ScenarioContext


def run(ctx: ScenarioContext) -> None:
    try:
        ctx.set_profile_enabled(REPEAT_PROFILE_NAME, enabled=True)
        ctx.subtest("repeat has no output before history", lambda: _repeat_without_history(ctx))
        ctx.subtest(
            "repeat follows keyboard press/repeat/release",
            lambda: _keyboard_lifecycle(ctx),
        )
        ctx.subtest(
            "repeat category filters select matching remembered actions",
            lambda: _category_filters(ctx),
        )
        ctx.subtest(
            "repeat ignores suppress and profile actions",
            lambda: _ignored_actions_do_not_replace_history(ctx),
        )
    finally:
        ctx.set_profile_enabled(SECOND_PROFILE_NAME, enabled=False)
        ctx.set_profile_enabled(REPEAT_PROFILE_NAME, enabled=False)


def _repeat_without_history(ctx: ScenarioContext) -> None:
    ctx.tap_source(evdev.ecodes.KEY_F13)
    ctx.expect_no_keyboard_events()
    ctx.expect_no_mouse_events()
    ctx.expect_no_gamepad_events()


def _keyboard_lifecycle(ctx: ScenarioContext) -> None:
    ctx.tap_source(evdev.ecodes.KEY_F19)
    ctx.expect_keys([(evdev.ecodes.KEY_A, 1), (evdev.ecodes.KEY_A, 0)])

    ctx.source_key(evdev.ecodes.KEY_F13, 1)
    ctx.source_key(evdev.ecodes.KEY_F13, 2)
    ctx.source_key(evdev.ecodes.KEY_F13, 0)
    ctx.expect_keys(
        [
            (evdev.ecodes.KEY_A, 1),
            (evdev.ecodes.KEY_A, 2),
            (evdev.ecodes.KEY_A, 0),
        ]
    )

    ctx.tap_source(evdev.ecodes.KEY_F13)
    ctx.expect_keys([(evdev.ecodes.KEY_A, 1), (evdev.ecodes.KEY_A, 0)])


def _category_filters(ctx: ScenarioContext) -> None:
    ctx.tap_source(evdev.ecodes.KEY_F19)
    ctx.expect_keys([(evdev.ecodes.KEY_A, 1), (evdev.ecodes.KEY_A, 0)])

    ctx.tap_source(evdev.ecodes.KEY_F20)
    ctx.expect_mouse_events(
        [
            (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_MIDDLE, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_MIDDLE, 0),
        ]
    )

    ctx.tap_source(evdev.ecodes.KEY_F21)
    ctx.expect_gamepad_events(
        [
            (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_NORTH, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_NORTH, 0),
        ]
    )

    ctx.tap_source(evdev.ecodes.KEY_F22)
    ctx.expect_keys([(evdev.ecodes.KEY_Y, 1), (evdev.ecodes.KEY_Y, 0)])

    ctx.tap_source(evdev.ecodes.KEY_F23)
    ctx.expect_mouse_events(
        [
            (evdev.ecodes.EV_REL, evdev.ecodes.REL_X, 9),
            (evdev.ecodes.EV_REL, evdev.ecodes.REL_Y, -4),
        ]
    )

    ctx.tap_source(evdev.ecodes.KEY_F14)
    ctx.expect_keys([(evdev.ecodes.KEY_A, 1), (evdev.ecodes.KEY_A, 0)])

    ctx.tap_source(evdev.ecodes.KEY_F15)
    ctx.expect_mouse_events(
        [
            (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_MIDDLE, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_MIDDLE, 0),
        ]
    )

    ctx.tap_source(evdev.ecodes.KEY_F16)
    ctx.expect_gamepad_events(
        [
            (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_NORTH, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_NORTH, 0),
        ]
    )

    ctx.tap_source(evdev.ecodes.KEY_F17)
    ctx.expect_keys([(evdev.ecodes.KEY_Y, 1), (evdev.ecodes.KEY_Y, 0)])

    ctx.tap_source(evdev.ecodes.KEY_F18)
    ctx.expect_mouse_events(
        [
            (evdev.ecodes.EV_REL, evdev.ecodes.REL_X, 9),
            (evdev.ecodes.EV_REL, evdev.ecodes.REL_Y, -4),
        ]
    )


def _ignored_actions_do_not_replace_history(ctx: ScenarioContext) -> None:
    ctx.tap_source(evdev.ecodes.KEY_F19)
    ctx.expect_keys([(evdev.ecodes.KEY_A, 1), (evdev.ecodes.KEY_A, 0)])

    ctx.tap_source(evdev.ecodes.KEY_L)
    ctx.expect_no_keyboard_events()

    ctx.tap_source(evdev.ecodes.KEY_F13)
    ctx.expect_keys([(evdev.ecodes.KEY_A, 1), (evdev.ecodes.KEY_A, 0)])

    ctx.tap_source(evdev.ecodes.KEY_N)
    ctx.wait_for_active_profile(SECOND_PROFILE_NAME, enabled=True)

    ctx.tap_source(evdev.ecodes.KEY_F13)
    ctx.expect_keys([(evdev.ecodes.KEY_A, 1), (evdev.ecodes.KEY_A, 0)])
