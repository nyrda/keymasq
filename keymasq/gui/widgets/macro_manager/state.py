"""Widget-independent macro catalog, filtering, and recording state."""

from dataclasses import dataclass, field
from typing import Any

from keymasq.gui.widgets.fuzzy_search import fuzzy_query_matches, macro_search_text

type MacroData = dict[str, Any]


class CatalogValidationError(ValueError):
    """Raised when a session response cannot replace the current catalog."""


@dataclass(frozen=True, slots=True)
class MacroRowState:
    name: str
    display_name: str
    metadata: str
    search_text: str
    is_temporary_slot: bool

    @classmethod
    def from_macro(cls, macro: MacroData) -> "MacroRowState":
        name = str(macro.get("name", "") or "")
        is_slot = str(macro.get("kind", "") or "") == "recording_slot"
        duration_ms = int(macro.get("duration_us", 0) or 0) // 1000
        device_types_value = macro.get("device_types", [])
        device_types = (
            [str(device_type) for device_type in device_types_value]
            if isinstance(device_types_value, list)
            else []
        )
        device_abbrevs = "+".join(
            {"keyboard": "kbd", "mouse": "mouse", "gamepad": "pad"}.get(
                device_type,
                device_type,
            )
            for device_type in device_types
        )
        metadata = f"{duration_ms}ms" if duration_ms < 1000 else f"{duration_ms / 1000.0:.1f}s"
        if device_abbrevs:
            metadata += f" · {device_abbrevs}"
        event_count = macro.get("event_count", 0)
        if event_count:
            metadata += f" · {event_count} events"
        if is_slot:
            metadata = f"temporary · {metadata}"
        return cls(
            name=name,
            display_name=str(macro.get("display_name", name) or ""),
            metadata=metadata,
            search_text=macro_search_text(macro),
            is_temporary_slot=is_slot,
        )


@dataclass(slots=True)
class CatalogState:
    macros: list[MacroData] = field(default_factory=list)
    query: str = ""

    @classmethod
    def from_response(cls, response: object) -> "CatalogState":
        if not isinstance(response, dict):
            raise CatalogValidationError("Failed to load macros")
        if response.get("status") not in (None, "ok"):
            raise CatalogValidationError(str(response.get("message") or "Failed to load macros"))
        macros = response.get("macros")
        if not isinstance(macros, list):
            raise CatalogValidationError(str(response.get("message") or "Failed to load macros"))
        validated: list[MacroData] = []
        for index, macro in enumerate(macros):
            if not isinstance(macro, dict) or not isinstance(macro.get("name"), str):
                raise CatalogValidationError(
                    f"Invalid macro entry at index {index}: expected an object with a name"
                )
            validated.append(macro)
        return cls(validated)

    @property
    def names(self) -> set[str]:
        return {str(macro.get("name", "")) for macro in self.macros if str(macro.get("name", ""))}

    def filtered_macros(self) -> list[MacroData]:
        return [
            macro
            for macro in self.macros
            if fuzzy_query_matches(self.query, macro_search_text(macro))
        ]


@dataclass(frozen=True, slots=True)
class RecordingRequest:
    command: str
    slot: int


@dataclass(slots=True)
class RecordingState:
    active: bool = False
    unlocked: bool = False
    enabled: bool = False
    selected_slot: int = 1
    active_slot: int = 0

    def select_index(self, index: int, *, max_slots: int) -> int:
        if self.active:
            active_slot = self.active_slot or self.selected_slot
            if 1 <= active_slot <= max_slots:
                return active_slot - 1
            return int(index)
        self.selected_slot = int(index) + 1
        return int(index)

    def next_request(self) -> RecordingRequest | None:
        if not self.active and not self.enabled:
            return None
        command = "stop_recording" if self.active else "start_recording"
        slot = self.active_slot or self.selected_slot if self.active else self.selected_slot
        if command == "start_recording":
            self.active_slot = slot
        return RecordingRequest(command, int(slot))

    def recording_started(self, slot: int, *, max_slots: int) -> None:
        self.active = True
        self.unlocked = True
        self.enabled = True
        if 1 <= slot <= max_slots:
            self.selected_slot = slot
            self.active_slot = slot

    def recording_stopped(self) -> None:
        self.active = False
        self.active_slot = 0


def suggest_unique_macro_name(existing_names: set[str]) -> str:
    base = "macro"
    name = base
    index = 1
    while name in existing_names:
        name = f"{base}_{index}"
        index += 1
    return name


def suggest_duplicate_macro_name(source_name: str, existing_names: set[str]) -> str:
    index = 1
    while True:
        candidate = f"{source_name}_{index}"
        if candidate not in existing_names:
            return candidate
        index += 1
