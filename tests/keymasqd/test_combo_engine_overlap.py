from keymasq.common.models import ActionType, MappingAction
from keymasq.keymasqd.combo_engine import (
    ComboEngine,
    RuntimeCombo,
    RuntimeComboBinding,
    RuntimeComboStep,
)
from tests.keymasqd.combo_engine_support import binding, combo, handle_combo_event


def test_wrong_key_between_steps_cancels_combo_and_passes_through():
    engine = ComboEngine()
    ctrl = binding("key_leftctrl")
    key_x = binding("key_x")
    key_1 = binding("key_1")
    key_h = binding("key_h")
    engine.set_combos([combo("combo-1", (ctrl, key_x), (key_1,))])

    handle_combo_event(engine, ctrl, 1, 0.0)
    handle_combo_event(engine, key_x, 1, 0.1)
    handle_combo_event(engine, key_x, 0, 0.2)
    handle_combo_event(engine, ctrl, 0, 0.3)

    wrong = handle_combo_event(engine, key_h, 1, 0.4)
    assert wrong.passthrough_current_event is True
    assert wrong.reset_candidates is True


def test_timeout_between_steps_cancels_silently():
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
                    RuntimeComboStep(bindings=(key_1,), timeout_ms=100),
                ],
                action=MappingAction(action_type=ActionType.KEYBOARD, target="key_f5"),
            )
        ]
    )

    handle_combo_event(engine, ctrl, 1, 0.0)
    handle_combo_event(engine, key_x, 1, 0.1)
    handle_combo_event(engine, key_x, 0, 0.2)
    handle_combo_event(engine, ctrl, 0, 0.3)

    assert engine.expire_timeouts(0.41) is True
    assert engine.next_deadline() is None


def test_double_tap_consumes_first_press_and_uses_second_press_for_action():
    engine = ComboEngine()
    key_a = binding("key_a")
    engine.set_combos([combo("combo-1", (key_a,), (key_a,))])

    first_down = handle_combo_event(engine, key_a, 1, 0.0)
    assert first_down.consume_current_event is True

    first_up = handle_combo_event(engine, key_a, 0, 0.1)
    assert first_up.consume_current_event is True

    second_down = handle_combo_event(engine, key_a, 1, 0.2)
    assert second_down.consume_current_event is True
    assert second_down.action_transition is not None
    assert second_down.action_transition.kind == "press"


def test_overlapping_first_step_combos_activate_independently():
    engine = ComboEngine()
    alt = binding("key_leftalt")
    key_c = binding("key_c")
    key_v = binding("key_v")
    engine.set_combos(
        [
            combo("alt-c", (alt, key_c)),
            combo("alt-v", (alt, key_v)),
            combo("alt-c-v", (alt, key_c, key_v)),
        ]
    )

    handle_combo_event(engine, alt, 1, 0.0)
    press_c = handle_combo_event(engine, key_c, 1, 0.1)
    assert press_c.action_transition is not None
    assert press_c.action_transition.combo_id == "alt-c"

    press_v = handle_combo_event(engine, key_v, 1, 0.2)
    transitions = []
    if press_v.action_transition is not None:
        transitions.append(press_v.action_transition.combo_id)
    transitions.extend(transition.combo_id for transition in press_v.extra_action_transitions)
    assert set(transitions) == {"alt-v", "alt-c-v"}
    assert press_v.consume_current_event is True


def test_multiple_concurrent_candidates_resolve_to_matching_combo():
    engine = ComboEngine()
    ctrl = binding("key_leftctrl")
    key_a = binding("key_a")
    key_b = binding("key_b")
    engine.set_combos(
        [
            combo("combo-a", (ctrl, key_a)),
            combo("combo-b", (ctrl, key_b)),
        ]
    )

    handle_combo_event(engine, ctrl, 1, 0.0)
    press_a = handle_combo_event(engine, key_a, 1, 0.1)

    assert press_a.action_transition is not None
    assert press_a.action_transition.combo_id == "combo-a"
    assert press_a.recall_events == []


def test_exact_binding_matching_requires_hardware_and_source_match():
    engine = ComboEngine()
    expected = binding("key_a", hardware_id="1111:2222", source="kbd")
    engine.set_combos([combo("combo-1", (expected,))])

    wrong_hardware = handle_combo_event(
        engine,
        binding("key_a", hardware_id="3333:4444", source="kbd"),
        1,
        0.0,
    )
    assert wrong_hardware.consume_current_event is False
    assert wrong_hardware.passthrough_current_event is False

    wrong_source = handle_combo_event(
        engine,
        binding("key_a", hardware_id="1111:2222", source="mouse"),
        1,
        0.1,
    )
    assert wrong_source.consume_current_event is False
    assert wrong_source.passthrough_current_event is False


def test_blank_hardware_id_matches_any_hardware_without_source_guessing():
    engine = ComboEngine()
    expected = binding("key_a", hardware_id="", source="kbd")
    engine.set_combos([combo("combo-1", (expected,))])

    first_hardware = handle_combo_event(
        engine,
        binding("key_a", hardware_id="1111:2222", source="kbd"),
        1,
        0.0,
    )
    assert first_hardware.consume_current_event is True
    assert first_hardware.action_transition is not None
    assert first_hardware.action_transition.combo_id == "combo-1"
    handle_combo_event(
        engine,
        binding("key_a", hardware_id="1111:2222", source="kbd"),
        0,
        0.05,
    )

    second_hardware = handle_combo_event(
        engine,
        binding("key_a", hardware_id="5555:6666", source="kbd"),
        1,
        0.1,
    )
    assert second_hardware.consume_current_event is True
    assert second_hardware.action_transition is not None
    assert second_hardware.action_transition.combo_id == "combo-1"
    handle_combo_event(
        engine,
        binding("key_a", hardware_id="5555:6666", source="kbd"),
        0,
        0.15,
    )

    wrong_source = handle_combo_event(
        engine,
        binding("key_a", hardware_id="3333:4444", source="mouse"),
        1,
        0.2,
    )
    assert wrong_source.consume_current_event is False
    assert wrong_source.passthrough_current_event is False


def test_modifier_side_is_ignored_for_matching():
    engine = ComboEngine()
    generic_ctrl = binding("ctrl")
    key_x = binding("key_x")
    engine.set_combos([combo("combo-1", (generic_ctrl, key_x))])

    press_right_ctrl = handle_combo_event(engine, binding("key_rightctrl"), 1, 0.0)
    assert press_right_ctrl.passthrough_current_event is True

    press_x = handle_combo_event(engine, key_x, 1, 0.1)
    assert press_x.consume_current_event is True
    assert press_x.action_transition is not None


def test_held_bindings_for_step_respects_source_specific_and_wildcard_matching():
    held_aux = binding("key_a", hardware_id="1111:2222", source="aux")
    held_kbd = binding("key_a", hardware_id="1111:2222", source="kbd")
    trigger = binding("key_b", hardware_id="1111:2222", source="kbd")

    exact_binding = RuntimeComboBinding("1111:2222", "key_a", "kbd")
    wildcard_binding = RuntimeComboBinding("1111:2222", "key_a", "")

    engine = ComboEngine()
    engine.set_combos([combo("exact-source", (exact_binding, trigger))])
    engine.prime_held_bindings({held_aux})
    exact_aux = handle_combo_event(engine, trigger, 1, 0.0)
    assert exact_aux.consume_current_event is False
    assert exact_aux.action_transition is None

    engine = ComboEngine()
    engine.set_combos([combo("wildcard-source", (wildcard_binding, trigger))])
    engine.prime_held_bindings({held_aux})
    wildcard_aux = handle_combo_event(engine, trigger, 1, 0.0)
    assert wildcard_aux.consume_current_event is True
    assert wildcard_aux.action_transition is not None
    assert wildcard_aux.action_transition.combo_id == "wildcard-source"

    engine = ComboEngine()
    engine.set_combos([combo("exact-source", (exact_binding, trigger))])
    engine.prime_held_bindings({held_kbd})
    exact_kbd = handle_combo_event(engine, trigger, 1, 0.0)
    assert exact_kbd.consume_current_event is True
    assert exact_kbd.action_transition is not None
    assert exact_kbd.action_transition.combo_id == "exact-source"

    engine = ComboEngine()
    engine.set_combos([combo("wildcard-source", (wildcard_binding, trigger))])
    engine.prime_held_bindings({held_kbd})
    wildcard_kbd = handle_combo_event(engine, trigger, 1, 0.0)
    assert wildcard_kbd.consume_current_event is True
    assert wildcard_kbd.action_transition is not None
    assert wildcard_kbd.action_transition.combo_id == "wildcard-source"


def test_held_bindings_for_step_respects_hardware_wildcard_matching():
    held_first = binding("key_leftalt", hardware_id="1111:2222", source="kbd")
    held_second = binding("key_leftalt", hardware_id="3333:4444", source="kbd")
    trigger = binding("key_b", hardware_id="5555:6666", source="kbd")

    source_specific = RuntimeComboBinding("", "key_leftalt", "kbd")
    source_wildcard = RuntimeComboBinding("", "key_leftalt", "")

    engine = ComboEngine()
    engine.set_combos([combo("source-specific", (source_specific, trigger))])
    engine.prime_held_bindings({held_first, held_second})
    specific_press = handle_combo_event(engine, trigger, 1, 0.0)
    assert specific_press.consume_current_event is True
    assert specific_press.action_transition is not None
    assert specific_press.action_transition.combo_id == "source-specific"

    engine = ComboEngine()
    engine.set_combos([combo("source-wildcard", (source_wildcard, trigger))])
    engine.prime_held_bindings({held_first, held_second})
    wildcard_press = handle_combo_event(engine, trigger, 1, 0.0)
    assert wildcard_press.consume_current_event is True
    assert wildcard_press.action_transition is not None
    assert wildcard_press.action_transition.combo_id == "source-wildcard"
