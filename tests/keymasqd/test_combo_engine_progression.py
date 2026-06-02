from keymasq.common.models import ActionType, MappingAction
from keymasq.keymasqd.combo_engine import ComboEngine, RuntimeCombo, RuntimeComboStep
from tests.keymasqd.combo_engine_support import binding, combo, handle_combo_event


def test_prime_held_bindings_rebuilds_modifier_seed_after_unrelated_press():
    engine = ComboEngine()
    meta = binding("key_leftmeta")
    key_1 = binding("key_1")
    key_4 = binding("key_4")
    engine.set_combos([combo("combo-1", (meta, key_1))])

    first_meta = handle_combo_event(engine, meta, 1, 0.0)
    assert first_meta.passthrough_current_event is True

    wrong = handle_combo_event(engine, key_4, 1, 0.1)
    assert wrong.consume_current_event is False
    assert wrong.passthrough_current_event is False
    assert wrong.reset_candidates is False

    engine.prime_held_bindings({meta})
    press_1 = handle_combo_event(engine, key_1, 1, 0.2)
    assert press_1.consume_current_event is True
    assert press_1.action_transition is not None
    assert press_1.action_transition.combo_id == "combo-1"


def test_single_step_combo_tracks_recalls_and_releases():
    engine = ComboEngine()
    key_a = binding("key_a")
    key_x = binding("key_x")
    engine.set_combos([combo("combo-1", (key_a, key_x))])

    press_a = handle_combo_event(engine, key_a, 1, 0.0)
    assert press_a.passthrough_current_event is True

    press_x = handle_combo_event(engine, key_x, 1, 0.1)
    assert press_x.consume_current_event is True
    assert [event.binding.evdev for event in press_x.recall_events] == ["key_a"]
    assert press_x.action_transition is not None
    assert press_x.action_transition.kind == "press"

    release_x = handle_combo_event(engine, key_x, 0, 0.2)
    assert release_x.consume_current_event is True
    assert release_x.action_transition is not None
    assert release_x.action_transition.kind == "release"

    release_a = handle_combo_event(engine, key_a, 0, 0.3)
    assert release_a.consume_current_event is False
    assert release_a.action_transition is None
    assert release_a.reset_candidates is False


def test_single_step_combo_releases_action_when_any_step_key_is_released():
    engine = ComboEngine()
    alt = binding("key_leftalt")
    key_2 = binding("key_2")
    engine.set_combos([combo("combo-1", (alt, key_2))])

    handle_combo_event(engine, alt, 1, 0.0)
    press_2 = handle_combo_event(engine, key_2, 1, 0.1)
    assert press_2.action_transition is not None
    assert press_2.action_transition.kind == "press"

    release_alt = handle_combo_event(engine, alt, 0, 0.2)
    assert release_alt.consume_current_event is True
    assert release_alt.action_transition is not None
    assert release_alt.action_transition.kind == "release"
    assert release_alt.action_transition.combo_id == "combo-1"


def test_three_key_step_recalls_in_reverse_press_order():
    engine = ComboEngine()
    key_a = binding("key_a")
    key_b = binding("key_b")
    key_c = binding("key_c")
    engine.set_combos([combo("combo-1", (key_a, key_b, key_c))])

    handle_combo_event(engine, key_a, 1, 0.0)
    handle_combo_event(engine, key_b, 1, 0.1)
    press_c = handle_combo_event(engine, key_c, 1, 0.2)

    assert [event.binding.evdev for event in press_c.recall_events] == [
        "key_b",
        "key_a",
    ]


def test_modifier_keys_are_not_recalled_on_combo_completion():
    engine = ComboEngine()
    alt = binding("key_leftalt")
    key_1 = binding("key_1")
    engine.set_combos([combo("combo-1", (alt, key_1))])

    press_alt = handle_combo_event(engine, alt, 1, 0.0)
    assert press_alt.passthrough_current_event is True

    press_1 = handle_combo_event(engine, key_1, 1, 0.1)
    assert press_1.consume_current_event is True
    assert press_1.recall_events == []
    assert press_1.action_transition is not None
    assert press_1.action_transition.kind == "press"


def test_multi_step_release_phase_defers_timeout_until_all_keys_up():
    engine = ComboEngine()
    ctrl = binding("key_leftctrl")
    key_x = binding("key_x")
    key_1 = binding("key_1")
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

    handle_combo_event(engine, ctrl, 1, 0.0)
    handle_combo_event(engine, key_x, 1, 0.1)
    assert engine.next_deadline() is None

    handle_combo_event(engine, key_x, 0, 0.2)
    assert engine.next_deadline() is None

    handle_combo_event(engine, ctrl, 0, 0.3)
    assert engine.next_deadline() == 1.0

    press_1 = handle_combo_event(engine, key_1, 1, 0.5)
    assert press_1.consume_current_event is True
    assert press_1.action_transition is not None
    assert press_1.action_transition.kind == "press"


def test_wrong_key_before_completion_does_not_cancel_held_condition():
    engine = ComboEngine()
    ctrl = binding("key_leftctrl")
    key_x = binding("key_x")
    key_h = binding("key_h")
    engine.set_combos([combo("combo-1", (ctrl, key_x))])

    handle_combo_event(engine, ctrl, 1, 0.0)
    wrong = handle_combo_event(engine, key_h, 1, 0.1)

    assert wrong.consume_current_event is False
    assert wrong.passthrough_current_event is False
    assert wrong.reset_candidates is False

    press_x = handle_combo_event(engine, key_x, 1, 0.2)
    assert press_x.consume_current_event is True
    assert press_x.action_transition is not None
    assert press_x.action_transition.combo_id == "combo-1"


def test_unrelated_key_does_not_block_multi_step_first_step_activation():
    engine = ComboEngine()
    alt = binding("key_leftalt")
    key_c = binding("key_c")
    key_h = binding("key_h")
    key_1 = binding("key_1")
    engine.set_combos([combo("combo-1", (alt, key_c), (key_1,))])

    handle_combo_event(engine, alt, 1, 0.0)
    wrong = handle_combo_event(engine, key_h, 1, 0.1)
    assert wrong.consume_current_event is False
    assert wrong.passthrough_current_event is False

    press_c = handle_combo_event(engine, key_c, 1, 0.2)
    assert press_c.consume_current_event is True
    assert press_c.action_transition is None

    release_c = handle_combo_event(engine, key_c, 0, 0.3)
    assert release_c.consume_current_event is True

    release_alt = handle_combo_event(engine, alt, 0, 0.4)
    assert release_alt.consume_current_event is True

    press_1 = handle_combo_event(engine, key_1, 1, 0.5)
    assert press_1.consume_current_event is True
    assert press_1.action_transition is not None
    assert press_1.action_transition.combo_id == "combo-1"


def test_overlapping_multi_step_combos_with_shared_first_step_progress_independently():
    engine = ComboEngine()
    meta = binding("key_leftmeta")
    key_a = binding("key_a")
    key_1 = binding("key_1")
    key_2 = binding("key_2")
    engine.set_combos(
        [
            combo("combo-1", (meta, key_a), (key_1,)),
            combo(
                "combo-2",
                (meta, key_a),
                (key_2,),
                action=MappingAction(action_type=ActionType.KEYBOARD, target="key_f6"),
            ),
        ]
    )

    handle_combo_event(engine, meta, 1, 0.0)
    first_step = handle_combo_event(engine, key_a, 1, 0.1)
    assert first_step.consume_current_event is True

    handle_combo_event(engine, key_a, 0, 0.2)
    handle_combo_event(engine, meta, 0, 0.3)

    press_1 = handle_combo_event(engine, key_1, 1, 0.4)
    assert press_1.consume_current_event is True
    assert press_1.action_transition is not None
    assert press_1.action_transition.combo_id == "combo-1"
    assert set(engine._candidates) == {"combo-1"}

    press_2 = handle_combo_event(engine, key_2, 1, 0.5)
    assert press_2.consume_current_event is False
    assert press_2.action_transition is None

    release_1 = handle_combo_event(engine, key_1, 0, 0.6)
    assert release_1.consume_current_event is True
    assert release_1.action_transition is not None
    assert release_1.action_transition.combo_id == "combo-1"
    assert release_1.action_transition.kind == "release"
    assert engine._candidates == {}


def test_sibling_sequence_candidate_drops_after_other_branch_matches():
    engine = ComboEngine()
    key_a = binding("key_a")
    key_b = binding("key_b")
    key_c = binding("key_c")
    engine.set_combos(
        [
            combo("combo-a-b", (key_a,), (key_b,)),
            combo(
                "combo-a-c",
                (key_a,),
                (key_c,),
                action=MappingAction(action_type=ActionType.KEYBOARD, target="key_f6"),
            ),
        ]
    )

    handle_combo_event(engine, key_a, 1, 0.0)
    handle_combo_event(engine, key_a, 0, 0.1)

    press_b = handle_combo_event(engine, key_b, 1, 0.2)
    assert press_b.consume_current_event is True
    assert press_b.action_transition is not None
    assert press_b.action_transition.combo_id == "combo-a-b"
    assert set(engine._candidates) == {"combo-a-b"}

    press_c = handle_combo_event(engine, key_c, 1, 0.3)
    assert press_c.consume_current_event is False
    assert press_c.action_transition is None


def test_overlapping_multi_step_combos_with_shared_first_step_can_hold_outputs_together():
    engine = ComboEngine()
    meta = binding("key_leftmeta")
    key_a = binding("key_a")
    key_1 = binding("key_1")
    key_2 = binding("key_2", source="mouse")
    engine.set_combos(
        [
            combo("combo-1", (meta, key_a), (key_1,)),
            combo(
                "combo-2",
                (meta, key_a),
                (key_2,),
                action=MappingAction(action_type=ActionType.KEYBOARD, target="key_f6"),
            ),
        ]
    )

    handle_combo_event(engine, meta, 1, 0.0)
    handle_combo_event(engine, key_a, 1, 0.1)
    handle_combo_event(engine, meta, 0, 0.2)
    handle_combo_event(engine, key_a, 0, 0.3)

    press_1 = handle_combo_event(engine, key_1, 1, 0.4)
    assert press_1.action_transition is not None
    assert press_1.action_transition.combo_id == "combo-1"

    press_2 = handle_combo_event(engine, key_2, 1, 0.5)
    assert press_2.consume_current_event is True
    transitions = []
    if press_2.action_transition is not None:
        transitions.append(press_2.action_transition.combo_id)
    transitions.extend(transition.combo_id for transition in press_2.extra_action_transitions)
    assert transitions == ["combo-2"]

    release_1 = handle_combo_event(engine, key_1, 0, 0.6)
    assert release_1.consume_current_event is True
    assert release_1.action_transition is not None
    assert release_1.action_transition.combo_id == "combo-1"
    assert release_1.action_transition.kind == "release"

    release_2 = handle_combo_event(engine, key_2, 0, 0.7)
    assert release_2.consume_current_event is True
    assert release_2.action_transition is not None
    assert release_2.action_transition.combo_id == "combo-2"
    assert release_2.action_transition.kind == "release"
    assert engine._candidates == {}


def test_mixed_releasing_release_drops_unfinished_candidate():
    engine = ComboEngine()
    alt = binding("key_leftalt")
    key_1 = binding("key_1")
    leader = binding("key_f")
    key_a = binding("key_a")
    key_b = binding("key_b")
    engine.set_combos(
        [
            combo("held-combo", (alt, key_1)),
            combo(
                "sequence-combo",
                (leader,),
                (key_a, key_b),
                action=MappingAction(action_type=ActionType.KEYBOARD, target="key_f6"),
            ),
        ]
    )

    handle_combo_event(engine, alt, 1, 0.0)
    handle_combo_event(engine, key_1, 1, 0.1)
    handle_combo_event(engine, leader, 1, 0.2)
    handle_combo_event(engine, leader, 0, 0.3)

    partial_press = handle_combo_event(engine, key_a, 1, 0.4)
    assert partial_press.passthrough_current_event is True

    partial_release = handle_combo_event(engine, key_a, 0, 0.5)
    assert partial_release.passthrough_current_event is True
    assert partial_release.reset_candidates is False
    assert set(engine._candidates) == {"held-combo"}

    final_press = handle_combo_event(engine, key_b, 1, 0.6)
    assert final_press.action_transition is None
    assert final_press.consume_current_event is False


def test_single_step_combo_with_wheel_pulse_fires_without_sticking():
    engine = ComboEngine()
    meta = binding("key_leftmeta")
    wheel_up = binding("wheel_up", source="mouse")
    engine.set_combos([combo("combo-1", (meta, wheel_up))])

    handle_combo_event(engine, meta, 1, 0.0)
    first_tick = handle_combo_event(engine, wheel_up, 1, 0.1)
    second_tick = handle_combo_event(engine, wheel_up, 1, 0.2)

    assert first_tick.consume_current_event is True
    assert first_tick.action_transition is not None
    assert first_tick.action_transition.kind == "pulse"
    assert first_tick.action_transition.combo_id == "combo-1"
    assert second_tick.action_transition is not None
    assert second_tick.action_transition.kind == "pulse"
    assert engine._candidates == {}


def test_multistep_combo_accepts_wheel_pulse_as_final_step():
    engine = ComboEngine()
    meta = binding("key_leftmeta")
    key_a = binding("key_a")
    wheel_down = binding("wheel_down", source="mouse")
    engine.set_combos([combo("combo-1", (meta, key_a), (wheel_down,))])

    handle_combo_event(engine, meta, 1, 0.0)
    handle_combo_event(engine, key_a, 1, 0.1)
    handle_combo_event(engine, key_a, 0, 0.2)
    handle_combo_event(engine, meta, 0, 0.3)
    decision = handle_combo_event(engine, wheel_down, 1, 0.4)

    assert decision.consume_current_event is True
    assert decision.action_transition is not None
    assert decision.action_transition.combo_id == "combo-1"
    assert decision.action_transition.kind == "pulse"
    assert engine._candidates == {}


def test_multistep_combo_rejects_wheel_only_first_step():
    engine = ComboEngine()
    wheel_up = binding("wheel_up", source="mouse")
    key_a = binding("key_a")
    engine.set_combos([combo("combo-1", (wheel_up,), (key_a,))])

    first_step = handle_combo_event(engine, wheel_up, 1, 0.0)
    decision = handle_combo_event(engine, key_a, 1, 0.1)

    assert first_step.consume_current_event is False
    assert first_step.passthrough_current_event is True
    assert first_step.action_transition is None
    assert decision.action_transition is None
    assert engine._candidates == {}


def test_multistep_combo_accepts_leader_plus_wheel_as_first_step():
    engine = ComboEngine()
    meta = binding("key_leftmeta")
    wheel_up = binding("wheel_up", source="mouse")
    key_a = binding("key_a")
    engine.set_combos([combo("combo-1", (meta, wheel_up), (key_a,))])

    handle_combo_event(engine, meta, 1, 0.0)
    first_step = handle_combo_event(engine, wheel_up, 1, 0.1)
    handle_combo_event(engine, meta, 0, 0.2)
    decision = handle_combo_event(engine, key_a, 1, 0.3)

    assert first_step.consume_current_event is True
    assert first_step.action_transition is None
    assert decision.consume_current_event is True
    assert decision.action_transition is not None
    assert decision.action_transition.combo_id == "combo-1"
