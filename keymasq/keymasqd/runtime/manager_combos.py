"""Combo parsing, lifecycle coordination, and emergency bindings for DeviceManager."""

import asyncio
import logging
from collections.abc import Sequence
from typing import Any, cast

from keymasq.common.coercion import coerce_int, coerce_str, json_list, json_object
from keymasq.common.combos import (
    EMERGENCY_CANCEL_COMBO_EVDEVS,
    is_emergency_cancel_combo_evdevs,
    normalize_combo_restore_keys,
)
from keymasq.common.model.actions import MappingAction
from keymasq.common.model.core import ActionType, DeviceType, SuperkeyMode
from keymasq.common.types import JsonObject
from keymasq.keymasqd.combo_engine import RuntimeCombo, RuntimeComboBinding, RuntimeComboStep
from keymasq.keymasqd.output_helpers import resolve_output_code
from keymasq.keymasqd.runtime import action_parser, adapters, repeat
from keymasq.keymasqd.runtime.combo import events, lifecycle, recall
from keymasq.keymasqd.runtime.combo.state import (
    ComboRuntimeDeps,
    ComboRuntimeState,
    FireAndObserve,
    ResolveCodeFn,
)
from keymasq.keymasqd.superkey_state import SuperkeyActionData, SuperkeyConfig
from keymasq.keymasqd.task_helpers import fire_and_observe

log = logging.getLogger("keymasqd.devices")

EMERGENCY_CANCEL_COMBO_ID_PREFIX = "__keymasq_emergency_cancel:"
EMERGENCY_CANCEL_COMBO_NAME = "Keymasq Emergency Cancel"
EMERGENCY_CANCEL_COMBO_PROFILE = "__keymasq_internal"
EMERGENCY_CANCEL_DOUBLE_TAP_WINDOW_MS = 200


def combo_runtime_deps(
    *,
    resolve_code_fn: ResolveCodeFn = resolve_output_code,
    fire_and_observe_fn: FireAndObserve = fire_and_observe,
) -> ComboRuntimeDeps:
    return ComboRuntimeDeps(
        asyncio_mod=adapters.ASYNCIO_RUNTIME,
        evdev_mod=adapters.COMBO_EVDEV_RUNTIME,
        uinput_writer=adapters.identity_uinput_writer,
        resolve_code_fn=resolve_code_fn,
        fire_and_observe_fn=fire_and_observe_fn,
    )


def combo_runtime_signature(combo: RuntimeCombo) -> tuple[object, ...]:
    steps = tuple(
        (
            tuple(
                sorted(
                    (
                        str(binding.hardware_id or "").lower(),
                        str(binding.source or "").lower(),
                        str(binding.evdev or "").lower(),
                    )
                    for binding in step.bindings
                )
            ),
            step.timeout_ms,
        )
        for step in combo.steps
    )
    return (
        str(combo.id or ""),
        str(combo.profile_name or ""),
        steps,
        combo.action,
        bool(combo.recall_trigger_keys),
        tuple(combo.restore_trigger_keys),
        bool(combo.match_across_devices),
    )


def combo_runtime_signatures(
    combos: Sequence[RuntimeCombo],
) -> dict[str, tuple[object, ...]]:
    return {combo.id: combo_runtime_signature(combo) for combo in combos if combo.id}


def unchanged_combo_ids(
    old_signatures: dict[str, tuple[object, ...]],
    new_combos: Sequence[RuntimeCombo],
) -> set[str]:
    preserved: set[str] = set()
    seen: set[str] = set()
    for combo in new_combos:
        if not combo.id or combo.id in seen:
            continue
        seen.add(combo.id)
        if old_signatures.get(combo.id) == combo_runtime_signature(combo):
            preserved.add(combo.id)
    return preserved


def parse_runtime_combos(manager: object, payloads: Sequence[object]) -> list[RuntimeCombo]:
    """Normalize session payloads into runtime combo definitions."""
    parsed: list[RuntimeCombo] = []
    for combo_data in payloads:
        combo_dict = json_object(combo_data)
        if combo_dict is None:
            continue
        action_data = combo_dict.get("action")
        action_dict = json_object(action_data)
        if isinstance(action_data, str):
            parsed_action_data: JsonObject | str = action_data
        elif action_dict is not None:
            parsed_action_data = action_dict
        else:
            continue

        steps_data = json_list(combo_dict.get("steps"))
        if not steps_data:
            continue
        match_across_devices = bool(combo_dict.get("match_across_devices", False))
        steps: list[RuntimeComboStep] = []
        for step_data in steps_data:
            step_dict = json_object(step_data)
            if step_dict is None:
                continue
            events_data = json_list(step_dict.get("events"))
            if not events_data:
                continue
            bindings: list[RuntimeComboBinding] = []
            for event_data in events_data:
                event_dict = json_object(event_data)
                if event_dict is None:
                    continue
                hardware_id = coerce_str(event_dict.get("hardware_id"), "").lower()
                evdev_name = coerce_str(event_dict.get("evdev"), "").lower()
                source = coerce_str(event_dict.get("source"), "").lower()
                if not evdev_name:
                    continue
                if match_across_devices:
                    hardware_id = ""
                    source = ""
                bindings.append(
                    RuntimeComboBinding(
                        hardware_id=hardware_id,
                        evdev=evdev_name,
                        source=source,
                    )
                )
            if bindings:
                timeout_raw = step_dict.get("timeout_ms")
                timeout_ms = coerce_int(timeout_raw) if timeout_raw is not None else None
                steps.append(RuntimeComboStep(bindings=tuple(bindings), timeout_ms=timeout_ms))

        if not steps:
            continue
        parsed.append(
            RuntimeCombo(
                id=coerce_str(combo_dict.get("id"), ""),
                name=coerce_str(combo_dict.get("name"), ""),
                steps=steps,
                action=action_parser.parse_action(manager, parsed_action_data),
                profile_name=coerce_str(combo_dict.get("profile_name"), ""),
                recall_trigger_keys=bool(combo_dict.get("recall_trigger_keys", False)),
                restore_trigger_keys=normalize_combo_restore_keys(
                    json_list(combo_dict.get("restore_trigger_keys"))
                ),
                match_across_devices=match_across_devices,
            )
        )
    return parsed


class ComboManagerMixin:
    """Owns DeviceManager's configured/active combo coordination."""

    def _initialize_combo_runtime(self) -> None:
        self.emergency_cancel_combo_enabled = True
        self.combo_state = ComboRuntimeState()
        self._configured_combos: list[RuntimeCombo] = []

    async def set_combos(self: Any, combos: Sequence[object]) -> JsonObject:
        async with self._op_lock:
            parsed = parse_runtime_combos(self, combos)
            old_active_signatures = combo_runtime_signatures(self.combo_state.active_combos)
            new_active_combos = self._with_emergency_cancel_combos(parsed)
            unchanged_ids = unchanged_combo_ids(old_active_signatures, new_active_combos)
            preserve_combo_ids = unchanged_ids if unchanged_ids else None
            repeat.forget_exec_actions(
                self.repeat_state,
                source_button_prefix="combo:",
            )
            self._configured_combos = parsed
            active_combos = await self._refresh_combo_runtime_unlocked(
                preserve_combo_ids=preserve_combo_ids,
            )
            log.info(
                "Updated combos (%d active, %d configured)",
                len(active_combos),
                len(parsed),
            )
            return {"updated": True, "combo_count": len(active_combos)}

    async def _refresh_combo_runtime_unlocked(
        self: Any,
        *,
        preserve_combo_ids: set[str] | None = None,
    ) -> list[RuntimeCombo]:
        active_combos = self._with_emergency_cancel_combos(self._configured_combos)
        self.combo_state.active_combos = active_combos
        if preserve_combo_ids is None:
            await lifecycle.clear_combo_runtime(self, deps=combo_runtime_deps())
        else:
            await lifecycle.clear_combo_runtime_except(
                self,
                preserve_combo_ids,
                deps=combo_runtime_deps(),
            )
        async with self.combo_state.runtime_lock:
            if preserve_combo_ids is None:
                self.combo_state.progression.engine.set_combos(active_combos)
            else:
                self.combo_state.progression.engine.set_combos(
                    active_combos,
                    preserve_candidate_ids=preserve_combo_ids,
                )
            recall.prime_combo_engine_with_held_bindings(self)
            lifecycle.refresh_combo_timeout_watchdog(self, deps=combo_runtime_deps())
            return active_combos

    async def _refresh_combo_runtime_preserving_unchanged(
        self: Any,
    ) -> list[RuntimeCombo]:
        unchanged_ids = unchanged_combo_ids(
            combo_runtime_signatures(self.combo_state.active_combos),
            self._with_emergency_cancel_combos(self._configured_combos),
        )
        return await self._refresh_combo_runtime_unlocked(
            preserve_combo_ids=unchanged_ids if unchanged_ids else None,
        )

    def _with_emergency_cancel_combos(
        self: Any,
        combos: list[RuntimeCombo],
    ) -> list[RuntimeCombo]:
        if not self.emergency_cancel_combo_enabled:
            return combos
        hardware_ids = self._grabbed_keyboard_hardware_ids()
        if not hardware_ids:
            return combos

        hardware_id_set = set(hardware_ids)
        user_combos = [
            combo
            for combo in combos
            if not self._is_emergency_cancel_duplicate(combo, hardware_id_set)
        ]
        emergency_combos = [self._emergency_cancel_combo(item) for item in hardware_ids]
        return [*emergency_combos, *user_combos]

    def _grabbed_keyboard_hardware_ids(self: Any) -> list[str]:
        hardware_ids: list[str] = []
        for raw_hardware_id, devices in self.grabbed_devices.items():
            hardware_id = str(raw_hardware_id or "").lower()
            if hardware_id and any(self._grabbed_device_is_keyboard(device) for device in devices):
                hardware_ids.append(hardware_id)
        return sorted(set(hardware_ids))

    def _grabbed_device_is_keyboard(self, device: object) -> bool:
        device_type = getattr(device, "device_type", None)
        if device_type == DeviceType.KEYBOARD:
            return True
        if str(getattr(device_type, "value", device_type) or "").lower() == "keyboard":
            return True
        raw_types = getattr(device, "device_types", ())
        if not isinstance(raw_types, (list, tuple, set, frozenset)):
            return False
        items = cast(Sequence[object] | set[object] | frozenset[object], raw_types)
        return any(str(item or "").lower() == "keyboard" for item in items)

    def _emergency_cancel_combo(self, hardware_id: str) -> RuntimeCombo:
        bindings = tuple(
            RuntimeComboBinding(hardware_id=hardware_id, evdev=evdev_name, source="")
            for evdev_name in EMERGENCY_CANCEL_COMBO_EVDEVS
        )
        return RuntimeCombo(
            id=f"{EMERGENCY_CANCEL_COMBO_ID_PREFIX}{hardware_id}",
            name=EMERGENCY_CANCEL_COMBO_NAME,
            steps=[RuntimeComboStep(bindings=bindings)],
            action=MappingAction(
                action_type=ActionType.SUPERKEY,
                superkey_config=cast(
                    Any,
                    SuperkeyConfig(
                        name=EMERGENCY_CANCEL_COMBO_NAME,
                        mode=SuperkeyMode.PATTERN,
                        double_tap_window_ms=EMERGENCY_CANCEL_DOUBLE_TAP_WINDOW_MS,
                        tap_actions=[
                            SuperkeyActionData(action_type=ActionType.CANCEL_MACRO_PLAYBACK.value)
                        ],
                        double_tap_actions=[
                            SuperkeyActionData(action_type=ActionType.EMERGENCY_RESET.value),
                        ],
                    ),
                ),
            ),
            profile_name=EMERGENCY_CANCEL_COMBO_PROFILE,
            recall_trigger_keys=True,
            restore_trigger_keys=[],
        )

    def _is_emergency_cancel_duplicate(
        self,
        combo: RuntimeCombo,
        keyboard_hardware_ids: set[str],
    ) -> bool:
        if len(combo.steps) != 1:
            return False
        step = combo.steps[0]
        if not step.bindings:
            return False
        if not is_emergency_cancel_combo_evdevs(binding.evdev for binding in step.bindings):
            return False
        return all(
            not binding.hardware_id or binding.hardware_id in keyboard_hardware_ids
            for binding in step.bindings
        )

    def begin_combo_capture(
        self: Any,
        token: str,
        hardware_ids: set[str],
        notify_event: asyncio.Event | None = None,
    ) -> JsonObject:
        return events.begin_combo_capture(self, token, hardware_ids, notify_event)

    def read_combo_capture(self: Any, token: str) -> JsonObject:
        return events.read_combo_capture(self, token)

    def end_combo_capture(self: Any, token: str) -> JsonObject:
        return events.end_combo_capture(self, token)
