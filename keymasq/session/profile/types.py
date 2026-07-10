from dataclasses import dataclass, field
from pathlib import Path

from keymasq.common.model.actions import MappingAction
from keymasq.common.model.profiles import (
    ComboStep,
    ProfileConfig,
)

type TomlDict = dict[str, object]
type ComboEventSignature = tuple[str, str, str]
type ComboStepSignature = tuple[ComboEventSignature, ...]
type ComboSignature = tuple[ComboStepSignature, ...]


@dataclass
class ProfileInfo:
    path: Path
    config: ProfileConfig


@dataclass
class ResolvedDeviceProfile:
    hardware_id: str
    active_profile_names: list[str] = field(default_factory=list)
    mappings: dict[str, MappingAction] = field(default_factory=dict)
    mapping_profile_names: dict[str, str] = field(default_factory=dict)
    always_grab_all: bool = False
    notify_profiles: list[str] = field(default_factory=list)
    combo_event_count: int = 0
    combo_sources: set[str] = field(default_factory=set)

    @property
    def mapping_count(self) -> int:
        return len(self.mappings)

    @property
    def has_effective_mapping(self) -> bool:
        return self.always_grab_all or bool(self.mappings) or self.combo_event_count > 0


@dataclass
class ResolvedCombo:
    id: str
    name: str
    steps: list[ComboStep] = field(default_factory=list)
    action: MappingAction | None = None
    profile_name: str = ""
    recall_trigger_keys: bool = False
    restore_trigger_keys: list[str] = field(default_factory=list)
    match_across_devices: bool = False


@dataclass
class ResolvedProfiles:
    active_profiles: list[ProfileConfig] = field(default_factory=list)
    devices: dict[str, ResolvedDeviceProfile] = field(default_factory=dict)
    combos: list[ResolvedCombo] = field(default_factory=list)
