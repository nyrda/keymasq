import copy
from collections.abc import Mapping
from datetime import datetime

from keymasq.common.combos import normalize_combo_evdev, normalize_combo_restore_keys
from keymasq.common.model.core import ActionType
from keymasq.common.model.profiles import (
    ComboConfig,
    ComboStep,
    ProfileConfig,
)

from .rules import has_unsupported_rules, matches_window_rules
from .types import (
    ComboSignature,
    ComboStepSignature,
    ProfileInfo,
    ResolvedCombo,
    ResolvedDeviceProfile,
    ResolvedProfiles,
    TomlDict,
)


class ProfileResolver:
    """Resolve an immutable profile snapshot into the effective runtime layers."""

    def __init__(self, profiles: Mapping[str, ProfileInfo]) -> None:
        self._profiles = profiles

    def resolve(
        self,
        window_info: TomlDict | None = None,
        capabilities: list[str] | None = None,
        hardware_ids: list[str] | None = None,
        runtime_profile_names: list[str] | None = None,
    ) -> ResolvedProfiles:
        capabilities = capabilities or []
        known_hardware_ids = set(hardware_ids or [])
        active_profiles: list[ProfileConfig] = []
        runtime_names = [
            str(name).strip() for name in (runtime_profile_names or []) if str(name).strip()
        ]

        for info in self._profiles.values():
            profile = info.config
            if not profile.enabled or has_unsupported_rules(profile, capabilities):
                continue
            if profile.is_permanent or matches_window_rules(profile, window_info):
                active_profiles.append(profile)
                known_hardware_ids.update(profile.device_layers.keys())

        active_profiles.sort(
            key=lambda profile: (
                0 if profile.is_permanent else 1,
                profile.priority,
                profile.created_at or datetime.min,
                profile.name.casefold(),
            )
        )

        runtime_profiles: list[ProfileConfig] = []
        seen_runtime_names: set[str] = set()
        for name in runtime_names:
            if name in seen_runtime_names:
                continue
            seen_runtime_names.add(name)
            info = self._profiles.get(name)
            if info is not None:
                runtime_profiles.append(info.config)
        if runtime_profiles:
            runtime_name_set = {profile.name for profile in runtime_profiles}
            active_profiles = [
                profile for profile in active_profiles if profile.name not in runtime_name_set
            ]
            active_profiles.extend(runtime_profiles)

        for profile in active_profiles:
            known_hardware_ids.update(profile.device_layers.keys())

        devices = self._resolve_devices(active_profiles, known_hardware_ids)
        combos = resolve_combos(active_profiles, devices)
        return ResolvedProfiles(
            active_profiles=active_profiles,
            devices=devices,
            combos=combos,
        )

    @staticmethod
    def _resolve_devices(
        active_profiles: list[ProfileConfig],
        known_hardware_ids: set[str],
    ) -> dict[str, ResolvedDeviceProfile]:
        devices: dict[str, ResolvedDeviceProfile] = {}
        for hardware_id in sorted(known_hardware_ids):
            resolved = ResolvedDeviceProfile(hardware_id=hardware_id)
            for profile in active_profiles:
                layer = profile.get_layer(hardware_id)
                if layer is None:
                    continue
                resolved.active_profile_names.append(profile.name)
                resolved.always_grab_all = resolved.always_grab_all or layer.always_grab_all
                if profile.notify_on_activation:
                    resolved.notify_profiles.append(profile.name)
                for button_id, action in layer.mappings.items():
                    if action.action_type == ActionType.PASSTHROUGH:
                        resolved.mappings.pop(button_id, None)
                        resolved.mapping_profile_names.pop(button_id, None)
                    else:
                        copied_action = copy.deepcopy(action)
                        copied_action.source_profile_name = profile.name
                        resolved.mappings[button_id] = copied_action
                        resolved.mapping_profile_names[button_id] = profile.name
            devices[hardware_id] = resolved
        return devices


def resolve_combos(
    active_profiles: list[ProfileConfig],
    devices: dict[str, ResolvedDeviceProfile],
) -> list[ResolvedCombo]:
    combos_by_signature: dict[ComboSignature, ResolvedCombo] = {}
    for profile in active_profiles:
        for combo in profile.combos:
            action = combo.action
            if action is None or not combo.steps:
                continue
            normalized = normalize_combo_steps(combo)
            if normalized is None:
                continue
            signature, combo_steps = normalized
            mark_combo_devices(devices, profile, signature)
            if signature in combos_by_signature:
                combos_by_signature.pop(signature, None)
            combo_action = copy.deepcopy(action)
            combo_action.source_profile_name = profile.name
            combos_by_signature[signature] = ResolvedCombo(
                id=combo.id,
                name=combo.name,
                steps=combo_steps,
                action=combo_action,
                profile_name=profile.name,
                recall_trigger_keys=bool(combo.recall_trigger_keys),
                restore_trigger_keys=normalize_combo_restore_keys(
                    copy.deepcopy(combo.restore_trigger_keys)
                ),
                match_across_devices=bool(combo.match_across_devices),
            )
    return list(combos_by_signature.values())


def normalize_combo_steps(
    combo: ComboConfig,
) -> tuple[ComboSignature, list[ComboStep]] | None:
    normalized_steps: list[ComboStepSignature] = []
    combo_steps: list[ComboStep] = []
    for step in combo.steps:
        if not step.events:
            return None
        effective_step = copy.deepcopy(step)
        if combo.match_across_devices:
            for event in effective_step.events:
                event.hardware_id = ""
                event.source = None
        normalized_events: ComboStepSignature = tuple(
            sorted(
                (
                    event.hardware_id or "",
                    event.source or "",
                    normalize_combo_evdev(event.evdev),
                )
                for event in effective_step.events
                if event.evdev
            )
        )
        if not normalized_events:
            return None
        normalized_steps.append(normalized_events)
        combo_steps.append(effective_step)
    return tuple(normalized_steps), combo_steps


def mark_combo_devices(
    devices: dict[str, ResolvedDeviceProfile],
    profile: ProfileConfig,
    signature: ComboSignature,
) -> None:
    for normalized_events in signature:
        for hardware_id, source, _evdev in normalized_events:
            if not hardware_id:
                continue
            resolved = devices.setdefault(
                hardware_id,
                ResolvedDeviceProfile(hardware_id=hardware_id),
            )
            if profile.name not in resolved.active_profile_names:
                resolved.active_profile_names.append(profile.name)
            resolved.combo_event_count += 1
            if profile.notify_on_activation and profile.name not in resolved.notify_profiles:
                resolved.notify_profiles.append(profile.name)
            if source:
                resolved.combo_sources.add(source)
