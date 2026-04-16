# ruff: noqa: F403, F405, I001
from tests.keymasqd.combo_engine_support import *

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


