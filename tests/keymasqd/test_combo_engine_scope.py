# ruff: noqa: F403, F405, I001
from tests.keymasqd.combo_engine_support import *

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


def test_wildcard_combo_release_from_other_device_does_not_stop_active_combo():
    engine = ComboEngine()
    expected = RuntimeComboBinding(hardware_id="", source="", evdev="key_f13")
    key_a = _binding("key_f13", hardware_id="1111:2222", source="kbd")
    key_b = _binding("key_f13", hardware_id="3333:4444", source="kbd")
    engine.set_combos([_combo("combo-any", (expected,))])

    press_a = _handle(engine, key_a, 1, 0.0)
    assert press_a.consume_current_event is True
    assert press_a.action_transition is not None
    assert press_a.action_transition.kind == "press"

    press_b = _handle(engine, key_b, 1, 0.1)
    assert press_b.consume_current_event is False
    assert press_b.action_transition is None

    release_b = _handle(engine, key_b, 0, 0.2)
    assert release_b.consume_current_event is False
    assert release_b.action_transition is None

    release_a = _handle(engine, key_a, 0, 0.3)
    assert release_a.consume_current_event is True
    assert release_a.action_transition is not None
    assert release_a.action_transition.combo_id == "combo-any"
    assert release_a.action_transition.kind == "release"
