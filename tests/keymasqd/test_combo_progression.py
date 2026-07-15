from keymasq.keymasqd.runtime.combo.progression import ComboProgressionMachine
from tests.keymasqd.combo_engine_support import binding, combo


def test_progression_machine_seeds_held_modifier_without_daemon_manager() -> None:
    meta = binding("key_leftmeta")
    key_1 = binding("key_1")
    machine = ComboProgressionMachine(clock=lambda: 12.5)
    machine.engine.set_combos([combo("combo-1", (meta, key_1))])

    decision = machine.handle(
        key_1,
        1,
        held_bindings={meta, key_1},
    )

    assert decision.consume_current_event is True
    assert decision.action_transition is not None
    assert decision.action_transition.combo_id == "combo-1"
    assert decision.action_transition.kind == "press"
