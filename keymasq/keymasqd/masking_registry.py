"""Runtime path ownership; callers retain the device manager's locking."""

import os
from dataclasses import dataclass, field


@dataclass
class MaskRegistry:
    hardware_paths: dict[str, list[str]] = field(default_factory=dict)
    reservation_paths: dict[str, list[str]] = field(default_factory=dict)
    reservation_attachments: dict[str, str] = field(default_factory=dict)
    blocked_attachments: set[str] = field(default_factory=set)

    @property
    def has_runtime_state(self) -> bool:
        return bool(
            self.hardware_paths
            or self.reservation_paths
            or self.reservation_attachments
            or self.blocked_attachments
        )

    def register(self, reservation_id: str, paths: list[str], attachment: str) -> None:
        """Record the physical reservation independently of its logical owners."""
        self.hardware_paths[reservation_id] = paths
        self.reservation_paths[reservation_id] = paths.copy()
        self.reservation_attachments[reservation_id] = attachment

    def release(self, reservation_id: str, released: set[str]) -> None:
        """Remove a reservation's paths from every adopted hardware configuration."""
        paths = {os.path.realpath(path) for path in self.reservation_paths.pop(reservation_id, [])}
        paths.update(released)
        for hardware_id, reserved in list(self.hardware_paths.items()):
            remaining = [path for path in reserved if os.path.realpath(path) not in paths]
            if remaining:
                self.hardware_paths[hardware_id] = remaining
            else:
                self.hardware_paths.pop(hardware_id, None)
        self.reservation_attachments.pop(reservation_id, None)
