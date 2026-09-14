"""Track command references owned by runtime actions and queued triggers."""

from dataclasses import dataclass
from weakref import WeakValueDictionary


@dataclass
class ExecReferenceLease:
    reference: int


class ExecReferenceRegistry:
    def __init__(self) -> None:
        self._leases: WeakValueDictionary[int, ExecReferenceLease] = WeakValueDictionary()

    def acquire(self, reference: int) -> ExecReferenceLease:
        lease = self._leases.get(reference)
        if lease is None:
            lease = ExecReferenceLease(reference)
            self._leases[reference] = lease
        return lease

    def unused(self, references: list[int]) -> list[int]:
        return [reference for reference in references if reference not in self._leases]


def acquire_exec_reference(manager: object, reference: int | None) -> object | None:
    registry = getattr(manager, "exec_references", None)
    if reference is None or not isinstance(registry, ExecReferenceRegistry):
        return None
    return registry.acquire(reference)
