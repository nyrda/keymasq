"""State and dependency contracts for device grab transactions."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from keymasq.common.types import JsonObject
from keymasq.keymasqd.runtime import adapters, device_path_resolver

type JsonObjectFn = Callable[[object], JsonObject | None]
type StrValueFn = Callable[..., str]
type IntValueFn = Callable[..., int]
type ResolveStablePathFn = Callable[[str], str]
type GetInterfaceIdFn = Callable[[str], str | None]
type FireAndObserve = Callable[[Awaitable[object], str], asyncio.Task[object]]
type DesiredGrabConfigFactory = Callable[..., object]
type GrabbedDeviceFactory = Callable[..., Any]
type ManagedGrabbedDevice = Any
type GrabManager = Any


@dataclass
class DesiredGrabConfig:
    """The persisted input configuration that topology recovery should restore."""

    paths: set[str]
    button_map: dict[str, str]
    button_codes: dict[str, int] = field(default_factory=dict)
    button_values: dict[str, int] = field(default_factory=dict)
    analog_inputs: dict[str, object] = field(default_factory=dict)
    force_grab_unmapped: bool = False
    evdev_interfaces: list[JsonObject] = field(default_factory=list)


@dataclass
class GrabRuntimeState:
    """Mutable release scheduling and desired-grab state owned by a manager."""

    release_grace_s: float
    held_release_retry_s: float
    desired_paths: dict[str, set[str]] = field(default_factory=dict)
    desired_grabs: dict[str, DesiredGrabConfig] = field(default_factory=dict)
    pending_interface_release: dict[tuple[str, str], asyncio.Task[None]] = field(
        default_factory=dict
    )
    pending_hardware_release: dict[str, asyncio.Task[None]] = field(default_factory=dict)


@dataclass(frozen=True)
class GrabDeviceDeps:
    """Injected operating-system and construction dependencies for a grab."""

    desired_grab_config_cls: DesiredGrabConfigFactory
    clear_device_path_cache_fn: Callable[[], None]
    resolve_stable_path_fn: ResolveStablePathFn
    device_path_resolver_deps: device_path_resolver.DevicePathResolverDeps
    grabbed_device_cls: GrabbedDeviceFactory
    get_interface_id_fn: GetInterfaceIdFn
    str_value_fn: StrValueFn
    int_value_fn: IntValueFn
    fire_and_observe_fn: FireAndObserve
    errno_mod: adapters.ErrnoModule


@dataclass(frozen=True)
class GrabRequest:
    """One requested reconciliation of a physical device's interfaces."""

    hardware_id: str
    evdev_paths: list[str]
    button_map: dict[str, str]
    button_codes: dict[str, int] | None = None
    button_values: dict[str, int] | None = None
    analog_inputs: dict[str, object] | None = None
    force_grab_unmapped: bool = False
    evdev_interfaces: list[JsonObject] | None = None
    update_desired: bool = True


@dataclass(frozen=True)
class GrabPlan:
    """Resolved, immutable inputs for a grab acquisition transaction."""

    hardware_id: str
    raw_interfaces: list[JsonObject]
    evdev_interfaces_provided: bool
    resolved_interfaces: list[device_path_resolver.ResolvedInterface]
    requested_paths: set[str]
    requested_claim_paths: set[str]
    resolved_by_claim_path: dict[str, device_path_resolver.ResolvedInterface]
    desired_paths: set[str]
    mapped_evdev_names: set[str]
    resolved_button_codes: dict[str, int]
    resolved_button_values: dict[str, int]
    button_mapped_bindings: set[tuple[int, int]]
    mapped_bindings: set[tuple[int, int]]
    analog_inputs: dict[str, object]
    existing_devices: list[ManagedGrabbedDevice]
    existing_by_claim_path: dict[str, ManagedGrabbedDevice]
    previous_desired_paths: set[str] | None
    previous_desired_config: object | None
    requests_gamepad_source_hiding: bool


@dataclass
class GrabAcquisitionState:
    """Explicit progress and resources owned by one acquisition transaction."""

    devices: list[ManagedGrabbedDevice]
    grabbed_count: int = 0
    skipped_count: int = 0
    available_count: int = 0
    created_global_uinputs: bool = False

    def is_waiting_for_device(self, plan: GrabPlan) -> bool:
        """Return whether configured interfaces exist but none are currently present."""

        return bool(
            (plan.requested_paths or plan.raw_interfaces)
            and self.available_count == 0
            and not self.devices
        )

    def owns_device(self, device: ManagedGrabbedDevice, plan: GrabPlan) -> bool:
        """Return whether this transaction created ``device`` and must roll it back."""

        return not any(device is existing for existing in plan.existing_devices)
