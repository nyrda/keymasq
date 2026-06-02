from keymasq.common.models import ActionType, MappingAction
from keymasq.keymasqd.combo_engine import (
    ComboEngine,
    ComboInputEvent,
    RuntimeCombo,
    RuntimeComboBinding,
    RuntimeComboStep,
)


def binding(
    evdev: str,
    hardware_id: str = "1234:5678",
    source: str = "kbd",
) -> RuntimeComboBinding:
    return RuntimeComboBinding(hardware_id=hardware_id, evdev=evdev, source=source)


def combo(
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


def handle_combo_event(
    engine: ComboEngine,
    event_binding: RuntimeComboBinding,
    value: int,
    now: float,
):
    return engine.handle_event(ComboInputEvent(binding=event_binding, value=value), now)
