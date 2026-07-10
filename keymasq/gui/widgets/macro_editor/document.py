"""Widget-independent macro editor document state."""

import copy
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

from keymasq.common.model.actions import normalize_macro_loop_stop_behavior
from keymasq.gui.widgets.macro_editor.model import (
    EditableControl,
    EditableEvent,
    EditableMove,
    MacroEvent,
    _get_editor_order,
    parse_events,
    reconstruct_events,
)


@dataclass(slots=True)
class MacroDocument:
    """Editable macro data without any GTK widget dependencies."""

    source: dict[str, Any]
    events: list[EditableEvent]
    relative_events: list[MacroEvent]
    passthrough_events: list[MacroEvent]
    moves: list[EditableMove]
    controls: list[EditableControl]
    duration_us: int
    has_move_to_start_setting: bool
    move_to_start: bool
    start_x: int
    start_y: int
    block_mouse_movement: bool
    loop_mode: str
    loop_count: int
    loop_stop_behavior: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "MacroDocument":
        source = copy.deepcopy(payload)
        raw_events = cast(list[MacroEvent], source.get("events", []))
        events, relative, passthrough, moves, controls = parse_events(raw_events)
        duration_us = _content_duration_us(
            int(source.get("duration_us", 0) or 0),
            events,
            relative,
            passthrough,
            moves,
            controls,
        )
        return cls(
            source=source,
            events=events,
            relative_events=relative,
            passthrough_events=passthrough,
            moves=moves,
            controls=controls,
            duration_us=duration_us,
            has_move_to_start_setting="move_to_start" in source,
            move_to_start=bool(source.get("move_to_start", False)),
            start_x=int(source.get("start_x", 0) or 0),
            start_y=int(source.get("start_y", 0) or 0),
            block_mouse_movement=bool(source.get("block_mouse_movement", False)),
            loop_mode=str(source.get("loop_mode", "none") or "none"),
            loop_count=max(1, int(source.get("loop_count", 1) or 1)),
            loop_stop_behavior=normalize_macro_loop_stop_behavior(source.get("loop_stop_behavior")),
        )

    def to_payload(
        self,
        name: str,
        *,
        loop_mode: str,
        loop_count: int,
        loop_stop_behavior: str,
        move_to_start: bool,
        start_x: int,
        start_y: int,
        block_mouse_movement: bool,
    ) -> dict[str, Any]:
        raw_events = reconstruct_events(
            self.events,
            self.relative_events,
            self.passthrough_events,
            self.moves,
            self.controls,
        )
        duration_us = self.duration_us
        if raw_events:
            duration_us = max(duration_us, max(int(event["t_us"]) for event in raw_events))

        device_types = list({event.device_type for event in self.events})
        if (self.relative_events or self.moves) and "mouse" not in device_types:
            device_types.append("mouse")

        data = dict(self.source)
        data.update(
            {
                "name": name,
                "events": raw_events,
                "duration_us": duration_us,
                "device_types": device_types,
                "loop_mode": loop_mode,
                "loop_count": max(1, int(loop_count)),
                "loop_stop_behavior": loop_stop_behavior,
                "block_mouse_movement": bool(block_mouse_movement),
            }
        )
        if self.has_move_to_start_setting:
            data["move_to_start"] = bool(move_to_start)
            data["start_x"] = int(start_x)
            data["start_y"] = int(start_y)
        else:
            data.pop("move_to_start", None)
            data.pop("start_x", None)
            data.pop("start_y", None)

        if raw_events != self.source.get("events", []):
            for key in (
                "type_text",
                "type_down_ms",
                "type_pause_ms",
                "type_use_unicode_input",
            ):
                data.pop(key, None)
            data["type_binding"] = False
        return data


def normalized_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize order-insensitive fields for dirty-state comparisons."""
    data = copy.deepcopy(payload)
    device_types = data.get("device_types")
    if isinstance(device_types, list):
        data["device_types"] = sorted(str(item) for item in device_types)
    return data


def has_pending_changes(
    *,
    initial_state_loaded: bool,
    initial_payload: dict[str, Any],
    current_payload: dict[str, Any],
) -> bool:
    if not initial_state_loaded or not initial_payload:
        return False
    return normalized_payload(current_payload) != normalized_payload(initial_payload)


def selection_order(item: object) -> tuple[int, int, int]:
    if isinstance(item, EditableEvent):
        original_order = item.original_press_order
        if original_order is None:
            original_order = item.original_release_order
        return _selection_order_tuple(original_order, item.press_t_us, 0)
    if isinstance(item, EditableMove):
        return _selection_order_tuple(item.original_order, item.t_us, 1)
    if isinstance(item, EditableControl):
        return _selection_order_tuple(item.original_order, item.t_us, 2)
    if isinstance(item, dict):
        return _selection_order_tuple(
            _get_editor_order(item),
            int(item.get("t_us", 0) or 0),
            3,
        )
    return (1, 2**63 - 1, 4)


class CloseAction(StrEnum):
    CLOSE = "close"
    PROMPT = "prompt"
    SAVE = "save"
    DISCARD = "discard"
    CANCEL = "cancel"


def close_action(has_changes: bool) -> CloseAction:
    return CloseAction.PROMPT if has_changes else CloseAction.CLOSE


def close_response_action(response: str) -> CloseAction:
    if response == "save":
        return CloseAction.SAVE
    if response == "discard":
        return CloseAction.DISCARD
    return CloseAction.CANCEL


def is_valid_macro_name(name: str) -> bool:
    return bool(name) and re.match(r"^[a-zA-Z0-9_\-]+$", name) is not None


class SaveMode(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    RENAME = "rename"


@dataclass(frozen=True, slots=True)
class SaveTarget:
    mode: SaveMode
    current_name: str
    requested_name: str
    revision: int


def resolve_save_target(
    *,
    macro_exists: bool,
    current_name: str,
    requested_name: str,
    revision: int,
) -> SaveTarget:
    if not macro_exists:
        mode = SaveMode.CREATE
    elif requested_name != current_name:
        mode = SaveMode.RENAME
    else:
        mode = SaveMode.UPDATE
    return SaveTarget(mode, current_name, requested_name, int(revision))


def _selection_order_tuple(
    original_order: int | None,
    t_us: int,
    priority: int,
) -> tuple[int, int, int]:
    if original_order is not None:
        return (0, int(original_order), int(priority))
    return (1, int(t_us), int(priority))


def _content_duration_us(
    duration_us: int,
    events: list[EditableEvent],
    relative_events: list[MacroEvent],
    passthrough_events: list[MacroEvent],
    moves: list[EditableMove],
    controls: list[EditableControl],
) -> int:
    candidates = [int(duration_us)]
    candidates.extend(event.release_t_us for event in events)
    candidates.extend(int(event.get("t_us", 0)) for event in relative_events)
    candidates.extend(int(event.get("t_us", 0)) for event in passthrough_events)
    candidates.extend(move.t_us for move in moves)
    candidates.extend(control.t_us for control in controls)
    return max(candidates, default=0)
