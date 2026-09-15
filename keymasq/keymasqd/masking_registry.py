"""Runtime path ownership; callers retain the device manager's locking.

Reader inspection, adoption, and release hold DeviceManager._op_lock. Under
that lock, registry operations and release_interface_unlocked() are safe;
grab_device(), release_device(), release_interface(), and release_all_devices()
acquire the lock themselves and must be called outside it. Cancel and await
acquisition/update tasks before taking the lock, since those tasks can need it.

Acquisition registers paths synchronously on the event loop before calling
grab_device(); it does not hold _op_lock across that public call. Path snapshots
used while examining readers last only for the current locked operation, so a
reconnected device's symlinks are resolved again on the next operation.
"""

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
        reservation_paths = self.reservation_paths.pop(reservation_id, [])
        all_paths = set(reservation_paths)
        for reserved in self.hardware_paths.values():
            all_paths.update(reserved)
        resolved = {path: os.path.realpath(path) for path in all_paths}
        paths = {resolved[path] for path in reservation_paths}
        paths.update(released)
        for hardware_id, reserved in list(self.hardware_paths.items()):
            remaining = [path for path in reserved if resolved[path] not in paths]
            if remaining:
                self.hardware_paths[hardware_id] = remaining
            else:
                self.hardware_paths.pop(hardware_id, None)
        self.reservation_attachments.pop(reservation_id, None)
