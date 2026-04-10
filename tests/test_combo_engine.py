from keyforge.common.models import ActionType, MappingAction
from keyforge.keyforged.combo_engine import (
    ComboEngine,
    ComboInputEvent,
    RuntimeCombo,
    RuntimeComboBinding,
    RuntimeComboStep,
)


def _binding(
    evdev: str,
    hardware_id: str = "1234:5678",
    source: str = "kbd",
) -> RuntimeComboBinding:
    return RuntimeComboBinding(hardware_id=hardware_id, evdev=evdev, source=source)


def _combo(
    combo_id: str,
    *steps: tuple[RuntimeComboBinding, ...],
    action: MappingAction | None = None,
) -> RuntimeCombo:
    return RuntimeCombo(
        id=combo_id,
        name=combo_id,
        steps=[RuntimeComboStep(bindings=step) for step in steps],
        action=action or MappingAction(action_type=ActionType.KEYBOARD, target="key_f5"),
    )


def _handle(engine: ComboEngine, binding: RuntimeComboBinding, value: int, now: float):
    return engine.handle_event(ComboInputEvent(binding=binding, value=value), now)


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


def test_wrong_key_between_steps_cancels_combo_and_passes_through():
    engine = ComboEngine()
    ctrl = _binding("key_leftctrl")
    key_x = _binding("key_x")
    key_1 = _binding("key_1")
    key_h = _binding("key_h")
    engine.set_combos([_combo("combo-1", (ctrl, key_x), (key_1,))])

    _handle(engine, ctrl, 1, 0.0)
    _handle(engine, key_x, 1, 0.1)
    _handle(engine, key_x, 0, 0.2)
    _handle(engine, ctrl, 0, 0.3)

    wrong = _handle(engine, key_h, 1, 0.4)
    assert wrong.passthrough_current_event is True
    assert wrong.reset_candidates is True


def test_timeout_between_steps_cancels_silently():
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
                    RuntimeComboStep(bindings=(key_1,), timeout_ms=100),
                ],
                action=MappingAction(action_type=ActionType.KEYBOARD, target="key_f5"),
            )
        ]
    )

    _handle(engine, ctrl, 1, 0.0)
    _handle(engine, key_x, 1, 0.1)
    _handle(engine, key_x, 0, 0.2)
    _handle(engine, ctrl, 0, 0.3)

    assert engine.expire_timeouts(0.41) is True
    assert engine.next_deadline() is None


def test_double_tap_consumes_first_press_and_uses_second_press_for_action():
    engine = ComboEngine()
    key_a = _binding("key_a")
    engine.set_combos([_combo("combo-1", (key_a,), (key_a,))])

    first_down = _handle(engine, key_a, 1, 0.0)
    assert first_down.consume_current_event is True

    first_up = _handle(engine, key_a, 0, 0.1)
    assert first_up.consume_current_event is True

    second_down = _handle(engine, key_a, 1, 0.2)
    assert second_down.consume_current_event is True
    assert second_down.action_transition is not None
    assert second_down.action_transition.kind == "press"


def test_overlapping_first_step_combos_activate_independently():
    engine = ComboEngine()
    alt = _binding("key_leftalt")
    key_c = _binding("key_c")
    key_v = _binding("key_v")
    engine.set_combos(
        [
            _combo("alt-c", (alt, key_c)),
            _combo("alt-v", (alt, key_v)),
            _combo("alt-c-v", (alt, key_c, key_v)),
        ]
    )

    _handle(engine, alt, 1, 0.0)
    press_c = _handle(engine, key_c, 1, 0.1)
    assert press_c.action_transition is not None
    assert press_c.action_transition.combo_id == "alt-c"

    press_v = _handle(engine, key_v, 1, 0.2)
    transitions = []
    if press_v.action_transition is not None:
        transitions.append(press_v.action_transition.combo_id)
    transitions.extend(transition.combo_id for transition in press_v.extra_action_transitions)
    assert set(transitions) == {"alt-v", "alt-c-v"}
    assert press_v.consume_current_event is True


def test_multiple_concurrent_candidates_resolve_to_matching_combo():
    engine = ComboEngine()
    ctrl = _binding("key_leftctrl")
    key_a = _binding("key_a")
    key_b = _binding("key_b")
    engine.set_combos(
        [
            _combo("combo-a", (ctrl, key_a)),
            _combo("combo-b", (ctrl, key_b)),
        ]
    )

    _handle(engine, ctrl, 1, 0.0)
    press_a = _handle(engine, key_a, 1, 0.1)

    assert press_a.action_transition is not None
    assert press_a.action_transition.combo_id == "combo-a"
    assert press_a.recall_events == []


def test_exact_binding_matching_requires_hardware_and_source_match():
    engine = ComboEngine()
    expected = _binding("key_a", hardware_id="1111:2222", source="kbd")
    engine.set_combos([_combo("combo-1", (expected,))])

    wrong_hardware = _handle(
        engine,
        _binding("key_a", hardware_id="3333:4444", source="kbd"),
        1,
        0.0,
    )
    assert wrong_hardware.consume_current_event is False
    assert wrong_hardware.passthrough_current_event is False

    wrong_source = _handle(
        engine,
        _binding("key_a", hardware_id="1111:2222", source="mouse"),
        1,
        0.1,
    )
    assert wrong_source.consume_current_event is False
    assert wrong_source.passthrough_current_event is False


def test_modifier_side_is_ignored_for_matching():
    engine = ComboEngine()
    generic_ctrl = _binding("ctrl")
    key_x = _binding("key_x")
    engine.set_combos([_combo("combo-1", (generic_ctrl, key_x))])

    press_right_ctrl = _handle(engine, _binding("key_rightctrl"), 1, 0.0)
    assert press_right_ctrl.passthrough_current_event is True

    press_x = _handle(engine, key_x, 1, 0.1)
    assert press_x.consume_current_event is True
    assert press_x.action_transition is not None


def test_held_bindings_for_step_respects_source_specific_and_wildcard_matching():
    engine = ComboEngine()
    held_aux = _binding("key_a", hardware_id="1111:2222", source="aux")
    held_kbd = _binding("key_a", hardware_id="1111:2222", source="kbd")
    engine.prime_held_bindings({held_aux})

    exact_step = RuntimeComboStep(
        bindings=(RuntimeComboBinding("1111:2222", "key_a", "kbd"),)
    )
    wildcard_step = RuntimeComboStep(
        bindings=(RuntimeComboBinding("1111:2222", "key_a", ""),)
    )

    assert engine._held_bindings_for_step(exact_step) is None

    wildcard_match = engine._held_bindings_for_step(wildcard_step)
    assert wildcard_match == {held_aux}

    engine.prime_held_bindings({held_kbd})
    assert engine._held_bindings_for_step(exact_step) == {held_kbd}

    wildcard_match = engine._held_bindings_for_step(wildcard_step)
    assert wildcard_match is not None
    assert len(wildcard_match) == 1
    assert next(iter(wildcard_match)) in {held_aux, held_kbd}


def test_repeat_events_are_ignored():
    engine = ComboEngine()
    ctrl = _binding("key_leftctrl")
    key_x = _binding("key_x")
    engine.set_combos([_combo("combo-1", (ctrl, key_x))])

    _handle(engine, ctrl, 1, 0.0)
    repeat = _handle(engine, ctrl, 2, 0.1)

    assert repeat.consume_current_event is False
    assert repeat.passthrough_current_event is False
    assert repeat.action_transition is None


def test_single_step_combo_rearms_when_modifier_stays_held():
    engine = ComboEngine()
    alt = _binding("key_leftalt")
    key_1 = _binding("key_1")
    engine.set_combos([_combo("combo-1", (alt, key_1))])

    first_alt = _handle(engine, alt, 1, 0.0)
    assert first_alt.passthrough_current_event is True

    first_press = _handle(engine, key_1, 1, 0.1)
    assert first_press.consume_current_event is True
    assert first_press.action_transition is not None
    assert first_press.action_transition.kind == "press"

    first_release = _handle(engine, key_1, 0, 0.2)
    assert first_release.consume_current_event is True
    assert first_release.action_transition is not None
    assert first_release.action_transition.kind == "release"

    second_press = _handle(engine, key_1, 1, 0.3)
    assert second_press.consume_current_event is True
    assert second_press.action_transition is not None
    assert second_press.action_transition.kind == "press"
    assert second_press.recall_events == []

    alt_release = _handle(engine, alt, 0, 0.4)
    assert alt_release.consume_current_event is True


def test_single_step_rearm_keeps_other_matching_modifier_combos_available():
    engine = ComboEngine()
    alt = _binding("key_leftalt")
    key_1 = _binding("key_1")
    key_2 = _binding("key_2")
    engine.set_combos(
        [
            _combo("combo-1", (alt, key_1)),
            _combo(
                "combo-2",
                (alt, key_2),
                action=MappingAction(action_type=ActionType.KEYBOARD, target="key_f6"),
            ),
        ]
    )

    _handle(engine, alt, 1, 0.0)
    _handle(engine, key_1, 1, 0.1)
    _handle(engine, key_1, 0, 0.2)

    second_combo_press = _handle(engine, key_2, 1, 0.3)
    assert second_combo_press.consume_current_event is True
    assert second_combo_press.action_transition is not None
    assert second_combo_press.action_transition.combo_id == "combo-2"


def test_single_step_held_completing_key_does_not_block_sibling_combo():
    engine = ComboEngine()
    alt = _binding("key_leftalt")
    key_1 = _binding("key_1")
    key_2 = _binding("key_2")
    engine.set_combos(
        [
            _combo("combo-1", (alt, key_1)),
            _combo(
                "combo-2",
                (alt, key_2),
                action=MappingAction(action_type=ActionType.KEYBOARD, target="key_f6"),
            ),
        ]
    )

    _handle(engine, alt, 1, 0.0)
    first_press = _handle(engine, key_1, 1, 0.1)
    assert first_press.consume_current_event is True
    assert first_press.action_transition is not None
    assert first_press.action_transition.combo_id == "combo-1"

    second_press = _handle(engine, key_2, 1, 0.2)
    assert second_press.consume_current_event is True
    assert second_press.action_transition is not None
    assert second_press.action_transition.combo_id == "combo-2"
    assert second_press.recall_events == []


def test_releasing_one_active_sibling_combo_does_not_drop_other_combo():
    engine = ComboEngine()
    alt = _binding("key_leftalt")
    key_1 = _binding("key_1")
    key_2 = _binding("key_2")
    engine.set_combos(
        [
            _combo("combo-1", (alt, key_1)),
            _combo(
                "combo-2",
                (alt, key_2),
                action=MappingAction(action_type=ActionType.KEYBOARD, target="key_f6"),
            ),
        ]
    )

    _handle(engine, alt, 1, 0.0)
    first_press = _handle(engine, key_1, 1, 0.1)
    assert first_press.action_transition is not None
    assert first_press.action_transition.combo_id == "combo-1"

    second_press = _handle(engine, key_2, 1, 0.2)
    assert second_press.action_transition is not None
    assert second_press.action_transition.combo_id == "combo-2"

    release_1 = _handle(engine, key_1, 0, 0.3)
    assert release_1.consume_current_event is True
    assert release_1.action_transition is not None
    assert release_1.action_transition.combo_id == "combo-1"
    assert release_1.action_transition.kind == "release"

    release_2 = _handle(engine, key_2, 0, 0.4)
    assert release_2.consume_current_event is True
    assert release_2.action_transition is not None
    assert release_2.action_transition.combo_id == "combo-2"
    assert release_2.action_transition.kind == "release"


def test_drop_candidates_for_binding_scope_preserves_other_hardware_actions():
    engine = ComboEngine()
    hw_a = _binding("key_a", hardware_id="1111:2222", source="kbd")
    hw_b = _binding("key_b", hardware_id="3333:4444", source="kbd")
    engine.set_combos(
        [
            _combo("combo-a", (hw_a,)),
            _combo("combo-b", (hw_b,)),
        ]
    )

    _handle(engine, hw_a, 1, 0.0)
    _handle(engine, hw_b, 1, 0.1)

    removed = engine.drop_candidates_for_binding_scope("1111:2222")
    assert removed == {"combo-a"}

    release_b = _handle(engine, hw_b, 0, 0.2)
    assert release_b.consume_current_event is True
    assert release_b.action_transition is not None
    assert release_b.action_transition.combo_id == "combo-b"
    assert release_b.action_transition.kind == "release"

    release_a = _handle(engine, hw_a, 0, 0.3)
    assert release_a.consume_current_event is False
    assert release_a.action_transition is None


def test_drop_candidates_for_binding_scope_can_target_specific_source():
    engine = ComboEngine()
    key_kbd = _binding("key_f13", hardware_id="1111:2222", source="kbd")
    key_mouse = _binding("btn_side", hardware_id="1111:2222", source="mouse")
    engine.set_combos(
        [
            _combo("combo-kbd", (key_kbd,)),
            _combo("combo-mouse", (key_mouse,)),
        ]
    )

    _handle(engine, key_kbd, 1, 0.0)
    _handle(engine, key_mouse, 1, 0.1)

    removed = engine.drop_candidates_for_binding_scope("1111:2222", "kbd")
    assert removed == {"combo-kbd"}

    release_mouse = _handle(engine, key_mouse, 0, 0.2)
    assert release_mouse.consume_current_event is True
    assert release_mouse.action_transition is not None
    assert release_mouse.action_transition.combo_id == "combo-mouse"
    assert release_mouse.action_transition.kind == "release"
