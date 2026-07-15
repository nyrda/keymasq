"""Manager-independent progression state machine for runtime combo input."""

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from keymasq.common.combos import is_combo_pulse_evdev
from keymasq.keymasqd.combo_engine import (
    ComboDecision,
    ComboEngine,
    ComboInputEvent,
    RuntimeComboBinding,
)


def _monotonic() -> float:
    return time.monotonic()


@dataclass(slots=True)
class ComboProgressionMachine:
    """Advance a ``ComboEngine`` while accounting for already-held modifiers.

    The daemon-facing runtime gathers held bindings from grabbed devices; this
    class owns the deterministic matching transition and can be tested without a
    device manager or event loop.
    """

    engine: ComboEngine = field(default_factory=ComboEngine)
    clock: Callable[[], float] = _monotonic

    def handle(
        self,
        binding: RuntimeComboBinding,
        value: int,
        *,
        held_bindings: Iterable[RuntimeComboBinding] = (),
    ) -> ComboDecision:
        if value == 1 and not is_combo_pulse_evdev(binding.evdev):
            held = set(held_bindings)
            held.discard(binding)
            self.engine.prime_held_bindings(held)
        return self.engine.handle_event(
            ComboInputEvent(binding=binding, value=value),
            self.clock(),
        )
