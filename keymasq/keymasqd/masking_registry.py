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
Filesystem resolution awaits a worker while the caller retains _op_lock;
workers receive copied paths and never inspect or mutate the registry.
"""

import asyncio
import os
from dataclasses import dataclass, field


async def resolve_paths(paths: set[str]) -> dict[str, str]:
    """Resolve a copied path set without reading runtime state in the worker."""
    snapshot = tuple(paths)
    return await asyncio.to_thread(lambda: {path: os.path.realpath(path) for path in snapshot})


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

    async def release(self, reservation_id: str, released: set[str]) -> None:
        """Remove a reservation's paths from every adopted hardware configuration."""
        resolved: dict[str, str] = {}
        while True:
            all_paths = set(self.reservation_paths.get(reservation_id, []))
            for reserved in self.hardware_paths.values():
                all_paths.update(reserved)
            missing = all_paths - resolved.keys()
            if not missing:
                break
            # Acquisition can register paths before taking the manager lock.
            # Include any additions made while filesystem resolution yields.
            resolved.update(await resolve_paths(missing))
        reservation_paths = self.reservation_paths.pop(reservation_id, [])
        paths = {resolved[path] for path in reservation_paths}
        paths.update(released)
        for hardware_id, reserved in list(self.hardware_paths.items()):
            remaining = [path for path in reserved if resolved[path] not in paths]
            if remaining:
                self.hardware_paths[hardware_id] = remaining
            else:
                self.hardware_paths.pop(hardware_id, None)
        self.reservation_attachments.pop(reservation_id, None)
