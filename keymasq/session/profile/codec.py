import logging
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

from keymasq.common.coercion import coerce_int, coerce_str
from keymasq.common.combos import normalize_combo_evdev, normalize_combo_restore_keys
from keymasq.common.model.actions import MappingAction
from keymasq.common.model.core import ActionType
from keymasq.common.model.profiles import (
    ComboConfig,
    ComboEvent,
    ComboStep,
    DeviceProfileLayer,
    ProfileConfig,
    WindowRule,
)
from keymasq.session.action_toml import (
    mapping_action_from_toml,
    mapping_action_to_toml,
    mapping_action_type_from_toml,
)

from .rules import SUPPORTED_WINDOW_RULE_FIELDS, normalize_window_rule_field
from .types import TomlDict

log = logging.getLogger("keymasq-session.profiles")


def as_toml_dict(value: object) -> TomlDict | None:
    return cast(TomlDict, value) if isinstance(value, dict) else None


def as_toml_list(value: object) -> list[object]:
    return cast(list[object], value) if isinstance(value, list) else []


@dataclass(frozen=True)
class DecodedProfile:
    config: ProfileConfig
    created_at_repair_reason: str | None = None


class ProfileCodec:
    """Translate between TOML-shaped data and profile domain models."""

    def __init__(
        self,
        *,
        superkey_exists: Callable[[str], bool] | None = None,
        analog_control_exists: Callable[[str], bool] | None = None,
        motion_control_exists: Callable[[str], bool] | None = None,
    ) -> None:
        self._superkey_exists = superkey_exists
        self._analog_control_exists = analog_control_exists
        self._motion_control_exists = motion_control_exists

    def load(self, path: Path) -> DecodedProfile:
        with open(path, "rb") as profile_file:
            data = cast(TomlDict, tomllib.load(profile_file))
        return self.decode(data, default_name=path.stem)

    def decode(
        self,
        data: TomlDict,
        *,
        default_name: str,
        now: datetime | None = None,
    ) -> DecodedProfile:
        profile = as_toml_dict(data.get("profile")) or {}
        window_rules: list[WindowRule] = []
        for rule_data in as_toml_list(profile.get("window_rules", [])):
            rule_dict = as_toml_dict(rule_data)
            if rule_dict is None:
                continue
            field = normalize_window_rule_field(rule_dict.get("field", "class"))
            if field not in SUPPORTED_WINDOW_RULE_FIELDS:
                log.warning("Unknown window rule field '%s'; rule will not match", field)
            window_rules.append(
                WindowRule(
                    field=field,
                    pattern=str(rule_dict.get("pattern", "")),
                )
            )

        created_at = now or datetime.now()
        created_at_repair_reason: str | None = None
        created_at_raw = profile.get("created_at")
        if isinstance(created_at_raw, str):
            try:
                parsed_created_at = datetime.fromisoformat(created_at_raw)
            except ValueError:
                created_at_repair_reason = f"malformed created_at '{created_at_raw}'"
            else:
                if parsed_created_at.utcoffset() is None:
                    created_at = parsed_created_at
                else:
                    created_at_repair_reason = (
                        f"timezone-aware created_at '{created_at_raw}'"
                    )
        elif created_at_raw is None:
            created_at_repair_reason = "missing created_at"
        else:
            created_at_repair_reason = "noncanonical created_at"

        device_layers: dict[str, DeviceProfileLayer] = {}
        devices_data = data.get("devices", {})
        if isinstance(devices_data, dict):
            for hardware_id, layer_data in cast(dict[object, object], devices_data).items():
                layer_dict = as_toml_dict(layer_data)
                if layer_dict is None:
                    continue
                mappings: dict[str, MappingAction] = {}
                mapping_table = layer_dict.get("mapping", {})
                if isinstance(mapping_table, dict):
                    for button_id, action_data in cast(dict[object, object], mapping_table).items():
                        parsed_action_data = as_toml_dict(action_data)
                        if parsed_action_data is not None:
                            mappings[str(button_id)] = self.parse_action(parsed_action_data)
                        elif isinstance(action_data, str):
                            mappings[str(button_id)] = self.parse_action(action_data)
                device_layers[str(hardware_id)] = DeviceProfileLayer(
                    hardware_id=str(hardware_id),
                    always_grab_all=bool(layer_dict.get("always_grab_all", False)),
                    mappings=mappings,
                )

        activation_macro_name = coerce_str(profile.get("activation_macro"), None)
        if activation_macro_name is not None:
            activation_macro_name = activation_macro_name.strip() or None
        deactivation_macro_name = coerce_str(profile.get("deactivation_macro"), None)
        if deactivation_macro_name is not None:
            deactivation_macro_name = deactivation_macro_name.strip() or None

        config = ProfileConfig(
            name=str(profile.get("name", default_name)),
            enabled=bool(profile.get("enabled", True)),
            is_permanent=bool(profile.get("is_permanent", False)),
            priority=coerce_int(profile.get("priority"), 0),
            notify_on_activation=bool(profile.get("notify_on_activation", True)),
            activation_macro_name=activation_macro_name,
            deactivation_macro_name=deactivation_macro_name,
            window_rules=window_rules,
            device_layers=device_layers,
            combos=self._parse_combos(data.get("combos", [])),
            image=str(profile.get("image")) if profile.get("image") is not None else None,
            created_at=created_at,
        )
        return DecodedProfile(config, created_at_repair_reason)

    def encode(self, config: ProfileConfig) -> TomlDict:
        profile_data: dict[str, object] = {
            "name": config.name,
            "enabled": config.enabled,
            "is_permanent": config.is_permanent,
            "priority": config.priority,
            "notify_on_activation": config.notify_on_activation,
            "created_at": (config.created_at or datetime.now()).isoformat(),
        }
        if config.activation_macro_name:
            profile_data["activation_macro"] = config.activation_macro_name
        if config.deactivation_macro_name:
            profile_data["deactivation_macro"] = config.deactivation_macro_name
        if config.window_rules:
            profile_data["window_rules"] = [
                {"field": rule.field, "pattern": rule.pattern} for rule in config.window_rules
            ]
        if config.image:
            profile_data["image"] = config.image

        devices_data: dict[str, dict[str, object]] = {}
        for hardware_id in sorted(config.device_layers):
            layer = config.device_layers[hardware_id]
            devices_data[hardware_id] = {
                "always_grab_all": layer.always_grab_all,
                "mapping": {
                    button_id: self.serialize_action(action)
                    for button_id, action in layer.mappings.items()
                },
            }

        data: TomlDict = {"profile": profile_data, "devices": devices_data}
        if config.combos:
            data["combos"] = [self._serialize_combo(combo) for combo in config.combos]
        return data

    def parse_action(self, action_data: TomlDict | str) -> MappingAction:
        if isinstance(action_data, str):
            return MappingAction(action_type=ActionType.KEYBOARD, target=action_data)

        action_type, normalized_action_data = mapping_action_type_from_toml(
            action_data,
            unknown_action="passthrough",
            logger=log,
        )
        if action_type == ActionType.SUPERKEY:
            superkey_name_raw = normalized_action_data.get("superkey_name")
            superkey_name = str(superkey_name_raw) if superkey_name_raw is not None else None
            if superkey_name:
                if self._superkey_exists is not None and not self._superkey_exists(superkey_name):
                    log.warning("Unknown superkey '%s', replacing with suppress", superkey_name)
                    return MappingAction(action_type=ActionType.SUPPRESS)
                return MappingAction(
                    action_type=ActionType.SUPERKEY,
                    superkey_name=superkey_name,
                )
            log.warning("Superkey action missing superkey_name, replacing with suppress")
            return MappingAction(action_type=ActionType.SUPPRESS)

        if action_type == ActionType.ANALOG_CONTROL:
            raw_names = normalized_action_data.get("analog_control_names")
            if isinstance(raw_names, list):
                raw_analog_control_names = cast(list[object], raw_names)
            else:
                raw_name: object = normalized_action_data.get("analog_control_name")
                raw_analog_control_names = [raw_name] if raw_name is not None else []
            analog_control_names = [
                name for raw_name in raw_analog_control_names if (name := str(raw_name).strip())
            ]
            if analog_control_names:
                if self._analog_control_exists is not None:
                    missing = next(
                        (
                            name
                            for name in analog_control_names
                            if not self._analog_control_exists(name)
                        ),
                        None,
                    )
                    if missing is not None:
                        log.warning("Unknown analog control '%s', replacing with suppress", missing)
                        return MappingAction(action_type=ActionType.SUPPRESS)
                return MappingAction(
                    action_type=ActionType.ANALOG_CONTROL,
                    analog_control_names=analog_control_names,
                )
            log.warning(
                "Analog control action missing analog_control_names, replacing with suppress"
            )
            return MappingAction(action_type=ActionType.SUPPRESS)

        if action_type == ActionType.MOTION_CONTROL:
            raw_name = normalized_action_data.get("motion_control_name")
            name = str(raw_name).strip() if raw_name is not None else None
            if name and (
                self._motion_control_exists is None or self._motion_control_exists(name)
            ):
                return MappingAction(
                    action_type=ActionType.MOTION_CONTROL,
                    motion_control_name=name,
                )
            if name:
                log.warning("Unknown motion control '%s', replacing with suppress", name)
            else:
                log.warning("Motion control action missing a control name, replacing with suppress")
            return MappingAction(action_type=ActionType.SUPPRESS)

        return mapping_action_from_toml(
            normalized_action_data,
            action_type,
            logger=log,
            rapidfire_warning_context="profile config",
        )

    @staticmethod
    def serialize_action(action: MappingAction) -> dict[str, object]:
        return mapping_action_to_toml(
            action,
            include_profile_refs=True,
            logger=log,
            rapidfire_warning_context="profile config",
        )

    def _parse_combos(self, combos_data: object) -> list[ComboConfig]:
        if not isinstance(combos_data, list):
            return []
        combos: list[ComboConfig] = []
        for combo_data in cast(list[object], combos_data):
            combo = self._parse_combo(combo_data)
            if combo is not None:
                combos.append(combo)
        return combos

    def _parse_combo(self, combo_data: object) -> ComboConfig | None:
        combo_dict = as_toml_dict(combo_data)
        if combo_dict is None:
            return None
        steps = self._parse_combo_steps(combo_dict.get("steps", []))
        action = self._parse_combo_action(combo_dict.get("action"))
        combo_id = str(combo_dict.get("id", "") or "")
        if not combo_id:
            return None
        return ComboConfig(
            id=combo_id,
            name=str(combo_dict.get("name", "") or ""),
            steps=steps,
            action=action,
            recall_trigger_keys=bool(combo_dict.get("recall_trigger_keys", False)),
            restore_trigger_keys=normalize_combo_restore_keys(
                as_toml_list(combo_dict.get("restore_trigger_keys", []))
            ),
            match_across_devices=bool(combo_dict.get("match_across_devices", False)),
        )

    def _parse_combo_steps(self, steps_data: object) -> list[ComboStep]:
        if not isinstance(steps_data, list):
            return []
        steps: list[ComboStep] = []
        for step_data in cast(list[object], steps_data):
            step = self._parse_combo_step(step_data)
            if step is not None:
                steps.append(step)
        return steps

    def _parse_combo_step(self, step_data: object) -> ComboStep | None:
        step_dict = as_toml_dict(step_data)
        if step_dict is None:
            return None
        events = self._parse_combo_events(step_dict.get("events", []))
        if not events:
            return None
        timeout_raw = step_dict.get("timeout_ms")
        timeout_ms = coerce_int(timeout_raw, 0) if timeout_raw is not None else None
        return ComboStep(events=events, timeout_ms=timeout_ms)

    def _parse_combo_events(self, events_data: object) -> list[ComboEvent]:
        if not isinstance(events_data, list):
            return []
        events: list[ComboEvent] = []
        for event_data in cast(list[object], events_data):
            event = self._parse_combo_event(event_data)
            if event is not None:
                events.append(event)
        return events

    @staticmethod
    def _parse_combo_event(event_data: object) -> ComboEvent | None:
        event_dict = as_toml_dict(event_data)
        if event_dict is None:
            return None
        evdev = str(event_dict.get("evdev", "") or "")
        if not evdev:
            return None
        source_raw = event_dict.get("source")
        return ComboEvent(
            evdev=normalize_combo_evdev(evdev),
            hardware_id=str(event_dict.get("hardware_id", "") or ""),
            source=str(source_raw) if source_raw is not None else None,
        )

    def _parse_combo_action(self, action_data: object) -> MappingAction | None:
        action_dict = as_toml_dict(action_data)
        if isinstance(action_data, str):
            return self.parse_action(action_data)
        if action_dict is not None:
            return self.parse_action(action_dict)
        return None

    def _serialize_combo(self, combo: ComboConfig) -> dict[str, object]:
        return {
            "id": combo.id,
            "name": combo.name,
            "steps": [
                {
                    **({"timeout_ms": int(step.timeout_ms)} if step.timeout_ms is not None else {}),
                    "events": [
                        {
                            "evdev": event.evdev,
                            **({"hardware_id": event.hardware_id} if event.hardware_id else {}),
                            **({"source": event.source} if event.source else {}),
                        }
                        for event in step.events
                    ],
                }
                for step in combo.steps
            ],
            **({"action": self.serialize_action(combo.action)} if combo.action is not None else {}),
            **({"recall_trigger_keys": True} if combo.recall_trigger_keys else {}),
            **(
                {"restore_trigger_keys": normalize_combo_restore_keys(combo.restore_trigger_keys)}
                if combo.restore_trigger_keys
                else {}
            ),
            **({"match_across_devices": True} if combo.match_across_devices else {}),
        }
