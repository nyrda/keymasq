"""Pure profile-tab presentation and editor state."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from keymasq.common.model.core import ProfileState
from keymasq.common.model.profiles import ProfileConfig

PROFILE_TYPE_ICONS = {
    "permanent": "⭐",
    "conditional": "🪟",
}


@dataclass(frozen=True, slots=True)
class ActiveProfiles:
    """Normalized active-profile ordering received from the session service."""

    names: tuple[str, ...] = ()

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> ActiveProfiles:
        value = payload.get("active_profiles", ())
        if not isinstance(value, list):
            return cls()
        return cls(tuple(str(name) for name in value))

    def summary(self, visible_limit: int = 3) -> str:
        if not self.names:
            return "None"
        visible = self.names[:visible_limit]
        result = ", ".join(visible)
        if len(self.names) > len(visible):
            result += f", +{len(self.names) - len(visible)}"
        return result

    def layer_tooltip(self) -> str:
        return "Layer order: " + " -> ".join(self.names)


def profile_state(
    config: ProfileConfig,
    active_names: tuple[str, ...],
) -> ProfileState:
    """Resolve the visible state for a supported profile configuration."""

    if config.name in active_names:
        return ProfileState.ACTIVE
    if not config.enabled:
        return ProfileState.INACTIVE
    if config.is_permanent:
        return ProfileState.STANDBY
    if config.window_rules:
        return ProfileState.WAITING
    return ProfileState.INACTIVE


def profile_state_icon(
    config: ProfileConfig,
    active_names: tuple[str, ...],
    *,
    unsupported_rules: bool,
) -> str:
    """Return the dropdown icon for a profile's current runtime state."""

    if unsupported_rules:
        return "❗"
    return {
        ProfileState.ACTIVE: "🟢",
        ProfileState.WAITING: "🟡",
        ProfileState.INACTIVE: "🔴" if not config.enabled else "⚪",
        ProfileState.STANDBY: "⚪",
    }[profile_state(config, active_names)]


def profile_type_icon(config: ProfileConfig) -> str:
    return PROFILE_TYPE_ICONS["permanent" if config.is_permanent else "conditional"]


@dataclass(frozen=True, slots=True)
class LifecycleMacroOptions:
    """Stable macro choices and dropdown index/name conversion."""

    available: tuple[str, ...] = ()

    @classmethod
    def from_payload(cls, payload: Mapping[str, object] | None) -> LifecycleMacroOptions:
        macros = (payload or {}).get("macros", ())
        names: set[str] = set()
        if isinstance(macros, list):
            for macro in macros:
                if not isinstance(macro, dict):
                    continue
                name = str(macro.get("name", "") or "").strip()
                if name:
                    names.add(name)
        return cls(tuple(sorted(names, key=str.casefold)))

    def choices(self, *selected: str | None) -> tuple[str, ...]:
        choices = [""]
        for name in (*self.available, *(name or "" for name in selected)):
            if name and name not in choices:
                choices.append(name)
        return tuple(choices)

    @staticmethod
    def index(choices: tuple[str, ...], name: str | None) -> int:
        try:
            return choices.index(name or "")
        except ValueError:
            return 0

    @staticmethod
    def selected_name(choices: tuple[str, ...], index: int) -> str | None:
        if index < 0 or index >= len(choices):
            return None
        return choices[index] or None


def next_copy_name(name: str, existing_names: set[str]) -> str:
    """Return the first available incremented copy name."""

    match = re.match(r"^(.+?)_(\d+)$", name)
    if match:
        base = match.group(1)
        number = int(match.group(2)) + 1
    else:
        base = name
        number = 1

    candidate = f"{base}_{number}"
    while candidate in existing_names:
        number += 1
        candidate = f"{base}_{number}"
    return candidate
