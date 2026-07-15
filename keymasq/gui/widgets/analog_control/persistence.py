"""Persistence transaction boundary for analog controls and profile references."""

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


class AnalogControlPersistence:
    """Coordinates analog-control storage with profile-reference updates."""

    def __init__(self, store: AnalogControlStore) -> None:
        self._store = store

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

    def delete(self, name: str, *, profiles: ProfileReferences | None) -> bool:
        if not self._store.delete_analog_control(name):
            return False
        if profiles is not None:
            profiles.replace_analog_control_with_suppress(name)
        return True
