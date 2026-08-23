import evdev

from keymasq.gui.widgets.macro_editor.gaps import (
    build_timeline_gaps,
    build_track_gaps,
    set_timeline_gap_and_following,
    set_timeline_gap_next_action,
    set_timeline_gap_track_following,
)
from keymasq.gui.widgets.macro_editor.model import (
    EditableEvent,
    EditableMove,
    MacroEvent,
    parse_events,
)


def _key(code: int, start_us: int, end_us: int) -> EditableEvent:
    return EditableEvent(
        device_type="keyboard",
        ev_type=evdev.ecodes.EV_KEY,
        code=code,
        press_t_us=start_us,
        release_t_us=end_us,
    )


def _mouse(code: int, start_us: int, end_us: int) -> EditableEvent:
    return EditableEvent(
        device_type="mouse",
        ev_type=evdev.ecodes.EV_KEY,
        code=code,
        press_t_us=start_us,
        release_t_us=end_us,
    )


def _repeat(code: int, t_us: int) -> MacroEvent:
    return {
        "device_type": "keyboard",
        "type": evdev.ecodes.EV_KEY,
        "code": code,
        "value": 2,
        "t_us": t_us,
    }


def _rel(t_us: int) -> MacroEvent:
    return {
        "device_type": "mouse",
        "type": evdev.ecodes.EV_REL,
        "code": evdev.ecodes.REL_X,
        "value": 4,
        "t_us": t_us,
    }


def test_timeline_gaps_group_chords_and_use_latest_active_end() -> None:
    ctrl = _key(evdev.ecodes.KEY_LEFTCTRL, 0, 300_000)
    key_a = _key(evdev.ecodes.KEY_A, 0, 80_000)
    key_b = _key(evdev.ecodes.KEY_B, 100_000, 180_000)
    key_c = _key(evdev.ecodes.KEY_C, 500_000, 580_000)

    gaps = build_timeline_gaps([ctrl, key_a, key_b, key_c], [], [])

    assert len(gaps) == 2
    assert gaps[0].previous_items == (ctrl, key_a)
    assert gaps[0].duration_us == -200_000
    assert gaps[0].minimum_us == -300_000
    assert gaps[1].previous_items == (ctrl, key_a)
    assert gaps[1].previous_end_us == 300_000
    assert gaps[1].duration_us == 200_000
    assert gaps[1].minimum_us == -200_000


def test_track_gap_ignores_holds_in_other_tracks() -> None:
    key_a = _key(evdev.ecodes.KEY_A, 0, 100_000)
    key_d = _key(evdev.ecodes.KEY_D, 300_000, 400_000)
    mouse_hold = _mouse(evdev.ecodes.BTN_LEFT, 0, 1_000_000)

    track_gap = build_track_gaps(
        [key_a, key_d],
        track="keyboard",
    )[0]
    global_gaps = build_timeline_gaps([key_a, key_d, mouse_hold], [], [])

    assert track_gap.scope == "track"
    assert track_gap.duration_us == 200_000
    assert all(gap.duration_us <= 0 for gap in global_gaps)


def test_track_gaps_follow_chronology_instead_of_rendered_sublanes() -> None:
    long_hold = _key(evdev.ecodes.KEY_LEFTCTRL, 0, 1_000_000)
    first = _key(evdev.ecodes.KEY_A, 100_000, 250_000)
    parallel = _key(evdev.ecodes.KEY_S, 150_000, 180_000)
    next_action = _key(evdev.ecodes.KEY_D, 300_000, 350_000)

    gaps = build_track_gaps(
        [long_hold, first, parallel, next_action],
        track="keyboard",
    )

    gap_before_next = gaps[-1]
    assert gap_before_next.previous_items == (parallel,)
    assert gap_before_next.next_items == (next_action,)
    assert gap_before_next.duration_us == 120_000


def test_track_following_moves_only_later_events_from_that_track() -> None:
    key_a = _key(evdev.ecodes.KEY_A, 0, 100_000)
    key_d = _key(evdev.ecodes.KEY_D, 300_000, 400_000)
    key_w = _key(evdev.ecodes.KEY_W, 600_000, 700_000)
    overlapping_key = _key(evdev.ecodes.KEY_S, 50_000, 500_000)
    mouse_hold = _mouse(evdev.ecodes.BTN_LEFT, 0, 1_000_000)
    events = [key_a, overlapping_key, key_d, key_w, mouse_hold]
    gap = build_track_gaps(
        [key_a, key_d, key_w],
        track="keyboard",
    )[0]

    assert (
        set_timeline_gap_track_following(events, [], [], [], [], gap, 50_000)
        == -150_000
    )

    assert (key_d.press_t_us, key_d.release_t_us) == (150_000, 250_000)
    assert (key_w.press_t_us, key_w.release_t_us) == (450_000, 550_000)
    assert (overlapping_key.press_t_us, overlapping_key.release_t_us) == (50_000, 500_000)
    assert (mouse_hold.press_t_us, mouse_hold.release_t_us) == (0, 1_000_000)


def test_track_following_resolves_the_suffix_when_the_edit_is_applied() -> None:
    key_a = _key(evdev.ecodes.KEY_A, 0, 100_000)
    key_d = _key(evdev.ecodes.KEY_D, 300_000, 400_000)
    gap = build_track_gaps([key_a, key_d], track="keyboard")[0]
    later_key = _key(evdev.ecodes.KEY_W, 600_000, 700_000)

    set_timeline_gap_track_following(
        [key_a, key_d, later_key],
        [],
        [],
        [],
        [],
        gap,
        50_000,
    )

    assert (key_d.press_t_us, key_d.release_t_us) == (150_000, 250_000)
    assert (later_key.press_t_us, later_key.release_t_us) == (450_000, 550_000)


def test_track_following_moves_unowned_raw_events_from_the_same_track() -> None:
    key_a = _key(evdev.ecodes.KEY_A, 0, 400_000)
    key_d = _key(evdev.ecodes.KEY_D, 300_000, 350_000)
    gap = build_track_gaps([key_a, key_d], track="keyboard")[0]
    owned_repeat = _repeat(evdev.ecodes.KEY_A, 350_000)
    unowned_repeat = _repeat(evdev.ecodes.KEY_B, 320_000)
    mouse_raw: MacroEvent = {
        "device_type": "mouse",
        "type": evdev.ecodes.EV_KEY,
        "code": evdev.ecodes.BTN_RIGHT,
        "value": 2,
        "t_us": 330_000,
    }

    set_timeline_gap_track_following(
        [key_a, key_d],
        [],
        [owned_repeat, unowned_repeat, mouse_raw],
        [],
        [],
        gap,
        -50_000,
    )

    assert (key_d.press_t_us, key_d.release_t_us) == (350_000, 400_000)
    assert owned_repeat["t_us"] == 350_000
    assert unowned_repeat["t_us"] == 370_000
    assert mouse_raw["t_us"] == 330_000


def test_movement_track_following_moves_later_raw_samples() -> None:
    first = EditableMove(mode="rel", t_us=0, x=1, y=2)
    second = EditableMove(mode="rel", t_us=300_000, x=3, y=4)
    movement_sample = _rel(400_000)
    gap = build_track_gaps([first, second], track="movement")[0]

    set_timeline_gap_track_following(
        [],
        [movement_sample],
        [],
        [first, second],
        [],
        gap,
        100_000,
    )

    assert second.t_us == 100_000
    assert movement_sample["t_us"] == 200_000


def test_setting_one_gap_and_following_moves_the_suffix_and_owned_repeats() -> None:
    first = _key(evdev.ecodes.KEY_A, 0, 100_000)
    second = _key(evdev.ecodes.KEY_B, 300_000, 500_000)
    third = _key(evdev.ecodes.KEY_C, 800_000, 900_000)
    first_repeat = _repeat(evdev.ecodes.KEY_A, 50_000)
    second_repeat = _repeat(evdev.ecodes.KEY_B, 400_000)
    movement = _rel(700_000)
    passthrough = [first_repeat, second_repeat]
    gaps = build_timeline_gaps(
        [first, second, third],
        [],
        [],
    )
    gap_before_second = next(gap for gap in gaps if gap.next_start_us == 300_000)

    assert (
        set_timeline_gap_and_following(
            [first, second, third],
            [movement],
            passthrough,
            [],
            [],
            gap_before_second,
            50_000,
        )
        == -150_000
    )

    assert (first.press_t_us, first.release_t_us) == (0, 100_000)
    assert (second.press_t_us, second.release_t_us) == (150_000, 350_000)
    assert (third.press_t_us, third.release_t_us) == (650_000, 750_000)
    assert first_repeat["t_us"] == 50_000
    assert second_repeat["t_us"] == 250_000
    assert movement["t_us"] == 550_000

    updated = build_timeline_gaps(
        [first, second, third],
        [],
        [],
    )
    assert next(gap for gap in updated if gap.next_start_us == 150_000).duration_us == 50_000


def test_setting_overlap_is_clamped_before_step_order_can_reverse() -> None:
    first = _key(evdev.ecodes.KEY_A, 0, 100_000)
    second = _key(evdev.ecodes.KEY_B, 300_000, 400_000)
    gap = build_timeline_gaps([first, second], [], [])[0]

    assert (
        set_timeline_gap_next_action(
            [first, second], [], [], [], [], gap, -500_000
        )
        == -300_000
    )
    assert second.press_t_us == first.press_t_us
    assert second.release_t_us == 100_000


def test_setting_one_gap_next_action_leaves_later_times_unchanged() -> None:
    first = _key(evdev.ecodes.KEY_A, 0, 100_000)
    second = _key(evdev.ecodes.KEY_B, 300_000, 500_000)
    third = _key(evdev.ecodes.KEY_C, 800_000, 900_000)
    second_repeat = _repeat(evdev.ecodes.KEY_B, 400_000)
    movement = _rel(700_000)
    passthrough = [second_repeat]
    gaps = build_timeline_gaps(
        [first, second, third],
        [],
        [],
    )
    gap_before_second = next(gap for gap in gaps if gap.next_start_us == 300_000)

    assert (
        set_timeline_gap_next_action(
            [first, second, third],
            [movement],
            passthrough,
            [],
            [],
            gap_before_second,
            50_000,
        )
        == -150_000
    )

    assert (first.press_t_us, first.release_t_us) == (0, 100_000)
    assert (second.press_t_us, second.release_t_us) == (150_000, 350_000)
    assert second_repeat["t_us"] == 250_000
    assert movement["t_us"] == 700_000
    assert (third.press_t_us, third.release_t_us) == (800_000, 900_000)
    updated = build_timeline_gaps(
        [first, second, third],
        [],
        [],
    )
    assert next(gap for gap in updated if gap.next_start_us == 150_000).duration_us == 50_000
    assert next(gap for gap in updated if gap.next_start_us == 800_000).duration_us == 450_000


def test_repeat_ownership_uses_original_order_at_tied_boundaries() -> None:
    raw_events: list[MacroEvent] = [
        {
            "device_type": "keyboard",
            "type": evdev.ecodes.EV_KEY,
            "code": evdev.ecodes.KEY_B,
            "value": 1,
            "t_us": 0,
        },
        {
            "device_type": "keyboard",
            "type": evdev.ecodes.EV_KEY,
            "code": evdev.ecodes.KEY_B,
            "value": 0,
            "t_us": 100_000,
        },
        {
            "device_type": "keyboard",
            "type": evdev.ecodes.EV_KEY,
            "code": evdev.ecodes.KEY_A,
            "value": 1,
            "t_us": 300_000,
        },
        {
            "device_type": "keyboard",
            "type": evdev.ecodes.EV_KEY,
            "code": evdev.ecodes.KEY_A,
            "value": 2,
            "t_us": 400_000,
        },
        {
            "device_type": "keyboard",
            "type": evdev.ecodes.EV_KEY,
            "code": evdev.ecodes.KEY_A,
            "value": 0,
            "t_us": 400_000,
        },
        {
            "device_type": "keyboard",
            "type": evdev.ecodes.EV_KEY,
            "code": evdev.ecodes.KEY_A,
            "value": 1,
            "t_us": 400_000,
        },
        {
            "device_type": "keyboard",
            "type": evdev.ecodes.EV_KEY,
            "code": evdev.ecodes.KEY_A,
            "value": 2,
            "t_us": 400_000,
        },
        {
            "device_type": "keyboard",
            "type": evdev.ecodes.EV_KEY,
            "code": evdev.ecodes.KEY_A,
            "value": 0,
            "t_us": 500_000,
        },
    ]
    events, rel_events, repeats, moves, controls = parse_events(raw_events)
    gap = next(
        gap
        for gap in build_timeline_gaps(events, moves, controls)
        if gap.next_start_us == 300_000
    )

    set_timeline_gap_next_action(
        events,
        rel_events,
        repeats,
        moves,
        controls,
        gap,
        100_000,
    )

    assert [int(repeat["t_us"]) for repeat in repeats] == [300_000, 400_000]
