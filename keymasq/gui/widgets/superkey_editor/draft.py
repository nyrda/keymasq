"""Serializable Super Key editor draft state."""

from dataclasses import dataclass

from keymasq.common.model.actions import MappingAction
from keymasq.common.model.core import SuperkeyMode
from keymasq.common.model.superkeys import SuperkeyAction, SuperkeyConfig


@dataclass(frozen=True, slots=True)
class SuperkeyDraft:
    name: str
    description: str
    mode: SuperkeyMode
    tap_actions: tuple[SuperkeyAction, ...] = ()
    double_tap_actions: tuple[SuperkeyAction, ...] = ()
    hold_actions: tuple[SuperkeyAction, ...] = ()
    tap_hold_actions: tuple[SuperkeyAction, ...] = ()
    overload_actions: tuple[MappingAction, ...] = ()
    overload_down_actions: tuple[MappingAction, ...] = ()
    overload_up_actions: tuple[MappingAction, ...] = ()
    tap_timeout_ms: int = 200
    double_tap_window_ms: int = 300
    hold_threshold_ms: int = 300

    @classmethod
    def from_config(cls, config: SuperkeyConfig) -> "SuperkeyDraft":
        return cls(
            name=config.name,
            description=config.description or "",
            mode=config.mode,
            tap_actions=tuple(config.tap_actions),
            double_tap_actions=tuple(config.double_tap_actions),
            hold_actions=tuple(config.hold_actions),
            tap_hold_actions=tuple(config.tap_hold_actions),
            overload_actions=tuple(config.overload_actions),
            overload_down_actions=tuple(config.overload_down_actions),
            overload_up_actions=tuple(config.overload_up_actions),
            tap_timeout_ms=config.tap_timeout_ms,
            double_tap_window_ms=config.double_tap_window_ms,
            hold_threshold_ms=config.hold_threshold_ms,
        )

    @classmethod
    def new(cls) -> "SuperkeyDraft":
        return cls.from_config(SuperkeyConfig(name="New Super Key"))

    def to_config(self) -> SuperkeyConfig:
        name = self.name.strip()
        if not name:
            raise ValueError("super key name is required")
        pattern_mode = self.mode == SuperkeyMode.PATTERN
        return SuperkeyConfig(
            name=name,
            description=self.description.strip() or None,
            mode=self.mode,
            tap_actions=list(self.tap_actions) if pattern_mode else [],
            double_tap_actions=list(self.double_tap_actions) if pattern_mode else [],
            hold_actions=list(self.hold_actions) if pattern_mode else [],
            tap_hold_actions=list(self.tap_hold_actions) if pattern_mode else [],
            overload_actions=list(self.overload_actions) if not pattern_mode else [],
            overload_down_actions=(list(self.overload_down_actions) if not pattern_mode else []),
            overload_up_actions=list(self.overload_up_actions) if not pattern_mode else [],
            tap_timeout_ms=self.tap_timeout_ms,
            double_tap_window_ms=self.double_tap_window_ms,
            hold_threshold_ms=self.hold_threshold_ms,
        )

    def is_pristine_new_draft(self) -> bool:
        return self == self.new()
