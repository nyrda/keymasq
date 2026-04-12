# pyright: reportUnusedImport=false, reportUnusedFunction=false, reportUnusedClass=false
# ruff: noqa: F401, I001
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

__all__ = [
    'ActionType',
    'MappingAction',
    'ComboEngine',
    'ComboInputEvent',
    'RuntimeCombo',
    'RuntimeComboBinding',
    'RuntimeComboStep',
    '_binding',
    '_combo',
    '_handle',
]
