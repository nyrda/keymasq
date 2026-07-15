"""Superkey expansion and serialization for daemon runtime payloads."""

from typing import TYPE_CHECKING

from keymasq.common.model.actions import MappingAction
from keymasq.common.model.core import SuperkeyMode
from keymasq.common.model.superkeys import (
    SuperkeyAction,
    SuperkeyConfig,
    combo_effective_superkey_config,
)

from ..common import JsonObject

if TYPE_CHECKING:
    from ..core import SessionManager


def resolve_combo(
    manager: "SessionManager",
    action: MappingAction,
    *,
    step_count: int,
) -> SuperkeyConfig | None:
    """Resolve a named superkey and adapt it to a combo's step count."""
    if not action.superkey_name:
        return None
    config = manager.superkeys.get_superkey(action.superkey_name)
    if config is None:
        return None
    return combo_effective_superkey_config(config, step_count=step_count)


def serialize(
    manager: "SessionManager",
    config: SuperkeyConfig,
    hardware_id: str,
    *,
    track_combo_refs: bool = False,
) -> JsonObject:
    return _serialize(
        manager,
        config,
        hardware_id,
        signature=False,
        track_combo_refs=track_combo_refs,
    )


def serialize_signature(
    manager: "SessionManager",
    config: SuperkeyConfig,
    hardware_id: str,
) -> JsonObject:
    return _serialize(manager, config, hardware_id, signature=True)


def _serialize(
    manager: "SessionManager",
    config: SuperkeyConfig,
    hardware_id: str,
    *,
    signature: bool,
    track_combo_refs: bool = False,
) -> JsonObject:
    data: JsonObject = {
        "name": config.name,
        "mode": config.mode.value,
        "tap_timeout_ms": int(config.tap_timeout_ms) if signature else config.tap_timeout_ms,
        "double_tap_window_ms": (
            int(config.double_tap_window_ms) if signature else config.double_tap_window_ms
        ),
        "hold_threshold_ms": (
            int(config.hold_threshold_ms) if signature else config.hold_threshold_ms
        ),
    }

    if config.mode == SuperkeyMode.PATTERN:
        slots = (
            ("tap_actions", config.tap_actions),
            ("double_tap_actions", config.double_tap_actions),
            ("hold_actions", config.hold_actions),
            ("tap_hold_actions", config.tap_hold_actions),
        )
        for key, actions in slots:
            if actions:
                data[key] = _serialize_pattern_actions(
                    manager,
                    actions,
                    hardware_id,
                    signature=signature,
                    track_combo_refs=track_combo_refs,
                )
    elif config.overload_actions:
        data["overload_actions"] = _serialize_overload_actions(
            manager,
            config.overload_actions,
            hardware_id,
            signature=signature,
            track_combo_refs=track_combo_refs,
        )

    if config.mode == SuperkeyMode.OVERLOAD:
        slots = (
            ("overload_down_actions", config.overload_down_actions),
            ("overload_up_actions", config.overload_up_actions),
        )
        for key, actions in slots:
            if actions:
                data[key] = _serialize_overload_actions(
                    manager,
                    actions,
                    hardware_id,
                    signature=signature,
                    track_combo_refs=track_combo_refs,
                )

    return data


def _serialize_pattern_actions(
    manager: "SessionManager",
    actions: list[SuperkeyAction],
    hardware_id: str,
    *,
    signature: bool,
    track_combo_refs: bool,
) -> list[JsonObject]:
    from .action import serialize_superkey_action, serialize_superkey_action_signature

    if signature:
        return [
            serialize_superkey_action_signature(manager, action, hardware_id) for action in actions
        ]
    return [
        serialize_superkey_action(
            manager,
            action,
            hardware_id,
            track_combo_refs=track_combo_refs,
        )
        for action in actions
    ]


def _serialize_overload_actions(
    manager: "SessionManager",
    actions: list[MappingAction],
    hardware_id: str,
    *,
    signature: bool,
    track_combo_refs: bool,
) -> list[JsonObject]:
    from .action import action_signature_payload, serialize_superkey_overload_action

    if signature:
        return [action_signature_payload(manager, action, hardware_id) for action in actions]
    return [
        serialize_superkey_overload_action(
            manager,
            action,
            hardware_id,
            track_combo_refs=track_combo_refs,
        )
        for action in actions
    ]
