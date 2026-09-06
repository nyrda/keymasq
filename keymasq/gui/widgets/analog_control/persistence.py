"""Persistence boundary for analog controls and their saved references."""

from typing import Protocol

from keymasq.common.model.analog import AnalogControlConfig


class AnalogControlStore(Protocol):
    def save_analog_control(
        self,
        config: AnalogControlConfig,
        *,
        replacing_name: str | None = None,
    ) -> None: ...

    def delete_analog_control(self, name: str) -> bool: ...


class ProfileReferences(Protocol):
    def rename_analog_control_references(self, old_name: str, new_name: str) -> object: ...

    def replace_analog_control_with_suppress(self, analog_control_name: str) -> object: ...


class MotionControlReferences(Protocol):
    def rename_analog_control_references(self, old_name: str, new_name: str) -> object: ...

    def clear_analog_control_references(self, analog_control_name: str) -> object: ...


class AnalogControlPersistence:
    """Coordinates storage with profile and Motion Control reference updates."""

    def __init__(
        self,
        store: AnalogControlStore,
        motion_controls: MotionControlReferences | None = None,
    ) -> None:
        self._store = store
        self._motion_controls = motion_controls

    def save(
        self,
        config: AnalogControlConfig,
        *,
        replacing_name: str | None,
        profiles: ProfileReferences | None,
    ) -> None:
        self._store.save_analog_control(config, replacing_name=replacing_name)
        if replacing_name and replacing_name != config.name and profiles is not None:
            profiles.rename_analog_control_references(replacing_name, config.name)
        if replacing_name and replacing_name != config.name and self._motion_controls is not None:
            self._motion_controls.rename_analog_control_references(replacing_name, config.name)

    def delete(self, name: str, *, profiles: ProfileReferences | None) -> bool:
        if not self._store.delete_analog_control(name):
            return False
        if profiles is not None:
            profiles.replace_analog_control_with_suppress(name)
        if self._motion_controls is not None:
            self._motion_controls.clear_analog_control_references(name)
        return True
