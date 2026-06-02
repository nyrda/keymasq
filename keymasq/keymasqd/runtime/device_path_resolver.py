import logging
import re
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, cast

import evdev

from keymasq.common.devices import (
    canonical_gamepad_button_name,
    capability_names_from_capabilities,
    detect_input_classes,
    is_gamepad_button_name,
    is_keymasq_device_path,
    parse_keymasq_device_path,
    primary_input_class,
    resolve_stable_path,
)
from keymasq.common.models import DeviceType
from keymasq.keymasqd.runtime.adapters import close_device

log = logging.getLogger("keymasqd.device_path_resolver")
type JsonObject = dict[str, object]


class _DeviceInfo(Protocol):
    vendor: int
    product: int


class InputDeviceLike(Protocol):
    path: str
    name: str | None

    @property
    def info(self) -> _DeviceInfo: ...

    def input_props(self) -> Iterable[int]: ...

    def capabilities(self) -> Mapping[int, Sequence[object]]: ...


@dataclass(frozen=True)
class ResolvedInterface:
    path: str
    configured_path: str
    interface_id: str
    device_type: DeviceType
    capabilities: list[str]


@dataclass(frozen=True)
class _MatchScore:
    type_match: int
    phys_match: int
    cap_overlap: int


@dataclass(frozen=True)
class _Candidate:
    path: str
    phys: str
    device_type: DeviceType
    capabilities: set[str]
    score: _MatchScore
    claimed: bool = False


@dataclass(frozen=True)
class CachedDeviceInfo:
    path: str
    vendor_id: str
    product_id: str
    phys: str
    device_type: DeviceType
    capabilities: set[str]
    is_virtual: bool


@dataclass
class DeviceCache:
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _devices: dict[str, CachedDeviceInfo] = field(default_factory=dict)

    def refresh_sync(
        self,
        *,
        device_paths_fn: Callable[[], list[str]],
        device_input_fn: Callable[[str], InputDeviceLike],
        detect_input_classes_fn: Callable[[InputDeviceLike], list[str]],
        primary_input_class_fn: Callable[
            [Iterable[str | DeviceType] | None], DeviceType
        ],
    ) -> dict[str, CachedDeviceInfo]:
        devices: dict[str, CachedDeviceInfo] = {}
        for path in sorted(device_paths_fn()):
            device: InputDeviceLike | None = None
            try:
                device = device_input_fn(path)
                info = device.info
                caps = device.capabilities()
                devices[path] = CachedDeviceInfo(
                    path=path,
                    vendor_id=f"{info.vendor:04x}",
                    product_id=f"{info.product:04x}",
                    phys=str(getattr(device, "phys", "") or "").strip(),
                    device_type=primary_input_class_fn(detect_input_classes_fn(device)),
                    capabilities=_normalize_capability_names(
                        capability_names_from_capabilities(caps)
                    ),
                    is_virtual=_is_keymasq_virtual_device(device),
                )
            except Exception:
                continue
            finally:
                if device is not None:
                    close_device(device)

        with self._lock:
            self._devices.clear()
            self._devices.update(devices)
        return devices

    def snapshot(self) -> dict[str, CachedDeviceInfo]:
        with self._lock:
            return dict(self._devices)

    def clear(self) -> None:
        with self._lock:
            self._devices.clear()


@dataclass(frozen=True)
class DevicePathResolverDeps:
    device_paths_fn: Callable[[], list[str]]
    device_input_fn: Callable[[str], InputDeviceLike]
    detect_input_classes_fn: Callable[[InputDeviceLike], list[str]]
    primary_input_class_fn: Callable[[Iterable[str | DeviceType] | None], DeviceType]
    resolve_stable_path_fn: Callable[[str], str] | None = None
    cache: DeviceCache | None = None


_DEFAULT_CACHE = DeviceCache()


def evdev_device_path_resolver_deps(
    device_input_fn: Callable[[str], InputDeviceLike],
) -> DevicePathResolverDeps:
    return DevicePathResolverDeps(
        device_paths_fn=cast(Callable[[], list[str]], evdev.list_devices),
        device_input_fn=device_input_fn,
        detect_input_classes_fn=cast(
            Callable[[InputDeviceLike], list[str]],
            detect_input_classes,
        ),
        primary_input_class_fn=primary_input_class,
        resolve_stable_path_fn=resolve_stable_path,
    )


def refresh_cached_devices_sync(
    *,
    device_paths_fn: Callable[[], list[str]],
    device_input_fn: Callable[[str], InputDeviceLike],
    detect_input_classes_fn: Callable[[InputDeviceLike], list[str]],
    primary_input_class_fn: Callable[[Iterable[str | DeviceType] | None], DeviceType],
) -> dict[str, CachedDeviceInfo]:
    return _DEFAULT_CACHE.refresh_sync(
        device_paths_fn=device_paths_fn,
        device_input_fn=device_input_fn,
        detect_input_classes_fn=detect_input_classes_fn,
        primary_input_class_fn=primary_input_class_fn,
    )


def cached_devices_snapshot() -> dict[str, CachedDeviceInfo]:
    return _DEFAULT_CACHE.snapshot()


def clear_cached_devices() -> None:
    _DEFAULT_CACHE.clear()


def interface_descriptors_from_paths(paths: list[str]) -> list[JsonObject]:
    return [
        {"path": str(path), "id": "", "type": "", "capabilities": []}
        for path in paths
        if str(path or "").strip()
    ]


def resolve_evdev_interfaces(
    interfaces: list[JsonObject],
    *,
    deps: DevicePathResolverDeps,
    hardware_id: str | None = None,
    excluded_paths: Iterable[str] | None = None,
) -> list[ResolvedInterface]:
    resolved: list[ResolvedInterface] = []
    selected_paths: set[str] = set()
    normalized_excluded_paths = {
        path for value in excluded_paths or [] if (path := str(value or "").strip())
    }

    for descriptor in interfaces:
        configured_path = str(descriptor.get("path", "") or "").strip()
        if not configured_path:
            continue
        interface_id = str(descriptor.get("id", "") or "").strip().lower()
        configured_type = _device_type(descriptor.get("type"))
        configured_phys = str(descriptor.get("phys", "") or "").strip()
        configured_caps = _capability_set(descriptor.get("capabilities"))

        if not is_keymasq_device_path(configured_path):
            resolved.append(
                ResolvedInterface(
                    path=configured_path,
                    configured_path=configured_path,
                    interface_id=interface_id,
                    device_type=configured_type,
                    capabilities=sorted(configured_caps),
                )
            )
            selected_paths.add(configured_path)
            continue

        candidate = _resolve_keymasq_path(
            configured_path,
            configured_type=configured_type,
            configured_phys=configured_phys,
            configured_caps=configured_caps,
            selected_paths=selected_paths,
            excluded_paths=normalized_excluded_paths,
            hardware_id=hardware_id,
            deps=deps,
        )
        if candidate is None:
            continue
        selected_paths.add(candidate.path)
        resolved.append(
            ResolvedInterface(
                path=candidate.path,
                configured_path=configured_path,
                interface_id=interface_id,
                device_type=configured_type,
                capabilities=sorted(configured_caps),
            )
        )

    return resolved


def _resolve_keymasq_path(
    configured_path: str,
    *,
    configured_type: DeviceType,
    configured_phys: str,
    configured_caps: set[str],
    selected_paths: set[str],
    excluded_paths: set[str],
    hardware_id: str | None,
    deps: DevicePathResolverDeps,
) -> _Candidate | None:
    parsed = parse_keymasq_device_path(configured_path)
    if parsed is None:
        return None
    vendor_id, product_id = parsed
    candidates: list[_Candidate] = []
    cached_devices = (deps.cache or _DEFAULT_CACHE).snapshot()
    for path in sorted(deps.device_paths_fn()):
        if path in selected_paths:
            continue
        cached = cached_devices.get(path)
        if cached is None and not cached_devices:
            cached = _probe_cached_device(path, deps)
        if cached is None or cached.is_virtual:
            continue
        if cached.vendor_id != vendor_id or cached.product_id != product_id:
            continue
        type_match = configured_type != DeviceType.OTHER and cached.device_type == configured_type
        type_score = int(configured_type == DeviceType.OTHER or type_match)
        phys_score = int(bool(configured_phys) and cached.phys == configured_phys)
        cap_score = len(configured_caps & cached.capabilities)
        has_selector = (
            configured_type != DeviceType.OTHER
            or bool(configured_phys)
            or bool(configured_caps)
        )
        if has_selector and not (type_match or phys_score or cap_score):
            continue
        candidates.append(
            _Candidate(
                path=path,
                phys=cached.phys,
                device_type=cached.device_type,
                capabilities=cached.capabilities,
                score=_MatchScore(
                    type_match=type_score,
                    phys_match=phys_score,
                    cap_overlap=cap_score,
                ),
                claimed=_is_excluded_path(
                    path,
                    excluded_paths,
                    resolve_stable_path_fn=deps.resolve_stable_path_fn,
                ),
            )
        )

    if not candidates:
        return None
    candidates.sort(
        key=lambda candidate: (
            -candidate.score.type_match,
            -candidate.score.phys_match,
            -candidate.score.cap_overlap,
            candidate.path,
        )
    )
    available_candidates = [
        candidate for candidate in candidates if not candidate.claimed
    ]
    if not available_candidates:
        log.info(
            "No unclaimed %s match from candidates %s",
            configured_path,
            [candidate.path for candidate in candidates],
        )
        return None
    instance_index = _numbered_hardware_instance_index(
        hardware_id,
        vendor_id=vendor_id,
        product_id=product_id,
    )
    if instance_index is not None:
        best_score = candidates[0].score
        matching_instances = [
            candidate for candidate in candidates if candidate.score == best_score
        ]
        if instance_index >= len(matching_instances):
            log.info(
                "No %s instance %d match from candidates %s; using best unclaimed match %s",
                configured_path,
                instance_index + 1,
                [candidate.path for candidate in matching_instances],
                available_candidates[0].path,
            )
            return available_candidates[0]
        for candidate in matching_instances[instance_index:]:
            if not candidate.claimed:
                return candidate
        log.info(
            "No unclaimed %s instance %d match from candidates %s; using best unclaimed match %s",
            configured_path,
            instance_index + 1,
            [candidate.path for candidate in matching_instances],
            available_candidates[0].path,
        )
        return available_candidates[0]

    best = available_candidates[0]
    if len(available_candidates) > 1 and available_candidates[1].score == best.score:
        log.warning(
            "Ambiguous %s match; using %s from candidates %s",
            configured_path,
            best.path,
            [candidate.path for candidate in available_candidates],
        )
    return best


def _probe_cached_device(
    path: str,
    deps: DevicePathResolverDeps,
) -> CachedDeviceInfo | None:
    device: InputDeviceLike | None = None
    try:
        device = deps.device_input_fn(path)
        info = device.info
        caps = device.capabilities()
        return CachedDeviceInfo(
            path=path,
            vendor_id=f"{info.vendor:04x}",
            product_id=f"{info.product:04x}",
            phys=str(getattr(device, "phys", "") or "").strip(),
            device_type=deps.primary_input_class_fn(deps.detect_input_classes_fn(device)),
            capabilities=_normalize_capability_names(
                capability_names_from_capabilities(caps)
            ),
            is_virtual=_is_keymasq_virtual_device(device),
        )
    except Exception:
        return None
    finally:
        if device is not None:
            close_device(device)


def _is_excluded_path(
    path: str,
    excluded_paths: set[str],
    *,
    resolve_stable_path_fn: Callable[[str], str] | None,
) -> bool:
    if path in excluded_paths:
        return True
    if resolve_stable_path_fn is None:
        return False
    try:
        stable_path = resolve_stable_path_fn(path)
    except Exception:
        return False
    return stable_path in excluded_paths


def _numbered_hardware_instance_index(
    hardware_id: str | None,
    *,
    vendor_id: str,
    product_id: str,
) -> int | None:
    normalized = str(hardware_id or "").strip().lower()
    match = re.fullmatch(
        r"([0-9a-f]{1,4}):([0-9a-f]{1,4})@([1-9][0-9]*)",
        normalized,
    )
    if match is None:
        return None

    hardware_vendor, hardware_product, instance_text = match.groups()
    if hardware_vendor.zfill(4) != vendor_id or hardware_product.zfill(4) != product_id:
        return None

    return int(instance_text) - 1


def _is_keymasq_virtual_device(device: object) -> bool:
    phys = str(getattr(device, "phys", "") or "").lower()
    name = str(getattr(device, "name", "") or "").lower()
    return phys == "py-evdev-uinput" or name.startswith("keymasq-")


def _device_type(value: object) -> DeviceType:
    if isinstance(value, DeviceType):
        return value
    try:
        return DeviceType(str(value or "other"))
    except ValueError:
        return DeviceType.OTHER


def _capability_set(value: object) -> set[str]:
    if not isinstance(value, list):
        return set()
    items = cast(list[object], value)
    return _normalize_capability_names(str(item) for item in items)


def _normalize_capability_names(names: Iterable[object]) -> set[str]:
    normalized: set[str] = set()
    for item in names:
        name = str(item).strip().lower()
        if not name:
            continue
        normalized.add(name)
        if is_gamepad_button_name(name):
            normalized.add(canonical_gamepad_button_name(name))
    return normalized
