"""Storage coordination for Super Key edits."""

from typing import Protocol

from keymasq.common.model.superkeys import SuperkeyConfig


class SuperkeyStore(Protocol):
    def save_superkey(
        self,
        config: SuperkeyConfig,
        *,
        replacing_name: str | None = None,
    ) -> None: ...

    def delete_superkey(self, name: str) -> bool: ...


class ProfileReferences(Protocol):
    def rename_superkey_references(self, old_name: str, new_name: str) -> int: ...

    def replace_superkey_with_suppress(self, superkey_name: str) -> int: ...


class SuperkeyPersistence:
    """Coordinates rename/save/delete operations and profile references."""

    def __init__(self, store: SuperkeyStore) -> None:
        self._store = store

    def save(
        self,
        config: SuperkeyConfig,
        *,
        replacing_name: str | None,
        profiles: ProfileReferences | None,
    ) -> None:
        self._store.save_superkey(config, replacing_name=replacing_name)
        if replacing_name and replacing_name != config.name and profiles is not None:
            profiles.rename_superkey_references(replacing_name, config.name)

    def delete(self, name: str, *, profiles: ProfileReferences | None) -> bool:
        if not self._store.delete_superkey(name):
            return False
        if profiles is not None:
            profiles.replace_superkey_with_suppress(name)
        return True
