# ruff: noqa: F403, F405, I001
from tests.keymasqd.combo_engine_support import *

def test_prime_held_bindings_rebuilds_modifier_seed_after_unrelated_press():
    engine = ComboEngine()
    meta = _binding("key_leftmeta")
    key_1 = _binding("key_1")
    key_4 = _binding("key_4")
    engine.set_combos([_combo("combo-1", (meta, key_1))])

    first_meta = _handle(engine, meta, 1, 0.0)
    assert first_meta.passthrough_current_event is True

    wrong = _handle(engine, key_4, 1, 0.1)
    assert wrong.consume_current_event is False
    assert wrong.passthrough_current_event is False
    assert wrong.reset_candidates is False

    engine.prime_held_bindings({meta})
    press_1 = _handle(engine, key_1, 1, 0.2)
    assert press_1.consume_current_event is True
    assert press_1.action_transition is not None
    assert press_1.action_transition.combo_id == "combo-1"


def test_single_step_combo_tracks_recalls_and_releases():
    engine = ComboEngine()
    key_a = _binding("key_a")
    key_x = _binding("key_x")
    engine.set_combos([_combo("combo-1", (key_a, key_x))])

    press_a = _handle(engine, key_a, 1, 0.0)
    assert press_a.passthrough_current_event is True

    press_x = _handle(engine, key_x, 1, 0.1)
    assert press_x.consume_current_event is True
    assert [event.binding.evdev for event in press_x.recall_events] == ["key_a"]
    assert press_x.action_transition is not None
    assert press_x.action_transition.kind == "press"

    release_x = _handle(engine, key_x, 0, 0.2)
    assert release_x.consume_current_event is True
    assert release_x.action_transition is not None
    assert release_x.action_transition.kind == "release"

    release_a = _handle(engine, key_a, 0, 0.3)
    assert release_a.consume_current_event is False
    assert release_a.action_transition is None
    assert release_a.reset_candidates is False


def test_single_step_combo_releases_action_when_any_step_key_is_released():
    engine = ComboEngine()
    alt = _binding("key_leftalt")
    key_2 = _binding("key_2")
    engine.set_combos([_combo("combo-1", (alt, key_2))])

    _handle(engine, alt, 1, 0.0)
    press_2 = _handle(engine, key_2, 1, 0.1)
    assert press_2.action_transition is not None
    assert press_2.action_transition.kind == "press"

    release_alt = _handle(engine, alt, 0, 0.2)
    assert release_alt.consume_current_event is True
    assert release_alt.action_transition is not None
    assert release_alt.action_transition.kind == "release"
    assert release_alt.action_transition.combo_id == "combo-1"


def test_three_key_step_recalls_in_reverse_press_order():
    engine = ComboEngine()
    key_a = _binding("key_a")
    key_b = _binding("key_b")
    key_c = _binding("key_c")
    engine.set_combos([_combo("combo-1", (key_a, key_b, key_c))])

    _handle(engine, key_a, 1, 0.0)
    _handle(engine, key_b, 1, 0.1)
    press_c = _handle(engine, key_c, 1, 0.2)

    assert [event.binding.evdev for event in press_c.recall_events] == [
        "key_b",
        "key_a",
    ]


def test_modifier_keys_are_not_recalled_on_combo_completion():
    engine = ComboEngine()
    alt = _binding("key_leftalt")
    key_1 = _binding("key_1")
    engine.set_combos([_combo("combo-1", (alt, key_1))])

    press_alt = _handle(engine, alt, 1, 0.0)
    assert press_alt.passthrough_current_event is True

    press_1 = _handle(engine, key_1, 1, 0.1)
    assert press_1.consume_current_event is True
    assert press_1.recall_events == []
    assert press_1.action_transition is not None
    assert press_1.action_transition.kind == "press"


def test_multi_step_release_phase_defers_timeout_until_all_keys_up():
    engine = ComboEngine()
    ctrl = _binding("key_leftctrl")
    key_x = _binding("key_x")
    key_1 = _binding("key_1")
    engine.set_combos(
        [
            RuntimeCombo(
                id="combo-1",
                name="combo-1",
                steps=[
                    RuntimeComboStep(bindings=(ctrl, key_x)),
                    RuntimeComboStep(bindings=(key_1,), timeout_ms=700),
                ],
                action=MappingAction(action_type=ActionType.KEYBOARD, target="key_f5"),
            )
        ]
    )

    _handle(engine, ctrl, 1, 0.0)
    _handle(engine, key_x, 1, 0.1)
    assert engine.next_deadline() is None

    _handle(engine, key_x, 0, 0.2)
    assert engine.next_deadline() is None

    _handle(engine, ctrl, 0, 0.3)
    assert engine.next_deadline() == 1.0

    press_1 = _handle(engine, key_1, 1, 0.5)
    assert press_1.consume_current_event is True
    assert press_1.action_transition is not None
    assert press_1.action_transition.kind == "press"


def test_wrong_key_before_completion_does_not_cancel_held_condition():
    engine = ComboEngine()
    ctrl = _binding("key_leftctrl")
    key_x = _binding("key_x")
    key_h = _binding("key_h")
    engine.set_combos([_combo("combo-1", (ctrl, key_x))])

    _handle(engine, ctrl, 1, 0.0)
    wrong = _handle(engine, key_h, 1, 0.1)

    assert wrong.consume_current_event is False
    assert wrong.passthrough_current_event is False
    assert wrong.reset_candidates is False

    press_x = _handle(engine, key_x, 1, 0.2)
    assert press_x.consume_current_event is True
    assert press_x.action_transition is not None
    assert press_x.action_transition.combo_id == "combo-1"


def test_unrelated_key_does_not_block_multi_step_first_step_activation():
    engine = ComboEngine()
    alt = _binding("key_leftalt")
    key_c = _binding("key_c")
    key_h = _binding("key_h")
    key_1 = _binding("key_1")
    engine.set_combos([_combo("combo-1", (alt, key_c), (key_1,))])

    _handle(engine, alt, 1, 0.0)
    wrong = _handle(engine, key_h, 1, 0.1)
    assert wrong.consume_current_event is False
    assert wrong.passthrough_current_event is False

    press_c = _handle(engine, key_c, 1, 0.2)
    assert press_c.consume_current_event is True
    assert press_c.action_transition is None

    release_c = _handle(engine, key_c, 0, 0.3)
    assert release_c.consume_current_event is True

    release_alt = _handle(engine, alt, 0, 0.4)
    assert release_alt.consume_current_event is True

    press_1 = _handle(engine, key_1, 1, 0.5)
    assert press_1.consume_current_event is True
    assert press_1.action_transition is not None
    assert press_1.action_transition.combo_id == "combo-1"


def test_overlapping_multi_step_combos_with_shared_first_step_progress_independently():
    engine = ComboEngine()
    meta = _binding("key_leftmeta")
    key_a = _binding("key_a")
    key_1 = _binding("key_1")
    key_2 = _binding("key_2")
    engine.set_combos(
        [
            _combo("combo-1", (meta, key_a), (key_1,)),
            _combo(
                "combo-2",
                (meta, key_a),
                (key_2,),
                action=MappingAction(action_type=ActionType.KEYBOARD, target="key_f6"),
            ),
        ]
    )

    _handle(engine, meta, 1, 0.0)
    first_step = _handle(engine, key_a, 1, 0.1)
    assert first_step.consume_current_event is True

    _handle(engine, key_a, 0, 0.2)
    _handle(engine, meta, 0, 0.3)

    press_1 = _handle(engine, key_1, 1, 0.4)
    assert press_1.consume_current_event is True
    assert press_1.action_transition is not None
    assert press_1.action_transition.combo_id == "combo-1"

    release_1 = _handle(engine, key_1, 0, 0.5)
    assert release_1.consume_current_event is True
    assert release_1.action_transition is not None
    assert release_1.action_transition.combo_id == "combo-1"
    assert release_1.action_transition.kind == "release"

    press_2 = _handle(engine, key_2, 1, 0.6)
    assert press_2.consume_current_event is True
    assert press_2.action_transition is not None
    assert press_2.action_transition.combo_id == "combo-2"

    release_2 = _handle(engine, key_2, 0, 0.7)
    assert release_2.consume_current_event is True
    assert release_2.action_transition is not None
    assert release_2.action_transition.combo_id == "combo-2"
    assert release_2.action_transition.kind == "release"
    assert release_2.reset_candidates is True
    assert engine._candidates == {}


def test_overlapping_multi_step_combos_with_shared_first_step_can_hold_outputs_together():
    engine = ComboEngine()
    meta = _binding("key_leftmeta")
    key_a = _binding("key_a")
    key_1 = _binding("key_1")
    key_2 = _binding("key_2")
    engine.set_combos(
        [
            _combo("combo-1", (meta, key_a), (key_1,)),
            _combo(
                "combo-2",
                (meta, key_a),
                (key_2,),
                action=MappingAction(action_type=ActionType.KEYBOARD, target="key_f6"),
            ),
        ]
    )

    _handle(engine, meta, 1, 0.0)
    _handle(engine, key_a, 1, 0.1)
    _handle(engine, meta, 0, 0.2)
    _handle(engine, key_a, 0, 0.3)

    press_1 = _handle(engine, key_1, 1, 0.4)
    assert press_1.action_transition is not None
    assert press_1.action_transition.combo_id == "combo-1"

    press_2 = _handle(engine, key_2, 1, 0.5)
    assert press_2.consume_current_event is True
    transitions = []
    if press_2.action_transition is not None:
        transitions.append(press_2.action_transition.combo_id)
    transitions.extend(transition.combo_id for transition in press_2.extra_action_transitions)
    assert transitions == ["combo-2"]

    release_1 = _handle(engine, key_1, 0, 0.6)
    assert release_1.consume_current_event is True
    assert release_1.action_transition is not None
    assert release_1.action_transition.combo_id == "combo-1"
    assert release_1.action_transition.kind == "release"

    release_2 = _handle(engine, key_2, 0, 0.7)
    assert release_2.consume_current_event is True
    assert release_2.action_transition is not None
    assert release_2.action_transition.combo_id == "combo-2"
    assert release_2.action_transition.kind == "release"
    assert engine._candidates == {}


