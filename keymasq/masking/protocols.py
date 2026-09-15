"""Operations available to daemon-owned masking reservations."""

from pathlib import Path
from typing import Protocol

from keymasq.masking.inventory import Attachment, HardwareInventory


class MaskBackend(Protocol):
    inventory: HardwareInventory
    state_dir: Path
    journal: Path
    active_attachment: Attachment | None

    @property
    def armed(self) -> bool: ...

    def for_attachment(self, identity: str) -> "MaskBackend": ...

    def reservation_ids(self) -> list[str]: ...

    def prepare_directories(self) -> None: ...

    async def install_rules(self, attachment: Attachment) -> None: ...

    async def activate(self, attachment: Attachment) -> list[str]: ...

    async def refresh_interfaces(self, attachment: Attachment) -> list[str]: ...

    async def recover(self, *, keep_rules: bool = False) -> None: ...
