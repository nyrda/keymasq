import logging
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
    make_keymasq_device_path,
    parse_hardware_model_id,
    parse_keymasq_device_path,
    primary_input_class,
    resolve_stable_path,
)
from keymasq.common.models import DeviceType
from keymasq.common.types import JsonObject
from keymasq.keymasqd.runtime.adapters import DeviceInfo, close_device

log = logging.getLogger("keymasqd.device_path_resolver")


class InputDeviceLike(Protocol):
    path: str
    name: str | None

    @property
    def info(self) -> DeviceInfo: ...

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
    order_path: str
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
            cached = _probe_cached_device_info(
                path,
                device_input_fn=device_input_fn,
                detect_input_classes_fn=detect_input_classes_fn,
                primary_input_class_fn=primary_input_class_fn,
                skip_log_message="Skipping device path resolver cache entry %s: %s",
                unexpected_log_message=(
                    "Unexpected failure caching device path resolver entry %s"
                ),
            )
            if cached is not None:
                devices[path] = cached

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


def _probe_cached_device_info(
    path: str,
    *,
    device_input_fn: Callable[[str], InputDeviceLike],
    detect_input_classes_fn: Callable[[InputDeviceLike], list[str]],
    primary_input_class_fn: Callable[[Iterable[str | DeviceType] | None], DeviceType],
    skip_log_message: str,
    unexpected_log_message: str,
) -> CachedDeviceInfo | None:
    device: InputDeviceLike | None = None
    try:
        device = device_input_fn(path)
        info = device.info
        caps = device.capabilities()
        return CachedDeviceInfo(
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
    except OSError as exc:
        log.debug(skip_log_message, path, exc)
        return None
    except Exception:
        log.exception(unexpected_log_message, path)
        return None
    finally:
        if device is not None:
            close_device(device)


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
    preferred_paths: Iterable[str] | None = None,
    match_model_gamepads: bool = False,
) -> list[ResolvedInterface]:
    resolved: list[ResolvedInterface] = []
    selected_paths: set[str] = set()
    normalized_excluded_paths = {
        path for value in excluded_paths or [] if (path := str(value or "").strip())
    }
    normalized_preferred_paths = {
        path for value in preferred_paths or [] if (path := str(value or "").strip())
    }

    for descriptor in interfaces:
        configured_path = str(descriptor.get("path", "") or "").strip()
        if not configured_path:
            continue
        interface_id = str(descriptor.get("id", "") or "").strip().lower()
        configured_type = _device_type(descriptor.get("type"))
        configured_phys = str(descriptor.get("phys", "") or "").strip()
        configured_caps = _capability_set(descriptor.get("capabilities"))

        resolution_path = configured_path
        resolution_phys = configured_phys
        model_path = _model_gamepad_path_for_hardware_id(
            hardware_id=hardware_id,
            configured_type=configured_type,
            enabled=match_model_gamepads,
        )
        if model_path is not None and _same_keymasq_model_path(configured_path, model_path):
            resolution_phys = ""
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

        candidate = _resolve_keymasq_paths(
            resolution_path,
            configured_path=configured_path,
            configured_type=configured_type,
            configured_phys=resolution_phys,
            configured_caps=configured_caps,
            selected_paths=selected_paths,
            excluded_paths=normalized_excluded_paths,
            preferred_paths=normalized_preferred_paths,
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


def _resolve_keymasq_paths(
    resolution_path: str,
    *,
    configured_path: str,
    configured_type: DeviceType,
    configured_phys: str,
    configured_caps: set[str],
    selected_paths: set[str],
    excluded_paths: set[str],
    preferred_paths: set[str],
    hardware_id: str | None,
    deps: DevicePathResolverDeps,
) -> _Candidate | None:
    parsed = parse_keymasq_device_path(resolution_path)
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
        order_path = _candidate_order_path(path, deps)
        candidates.append(
            _Candidate(
                path=path,
                order_path=order_path,
                phys=cached.phys,
                device_type=cached.device_type,
                capabilities=cached.capabilities,
                score=_MatchScore(
                    type_match=type_score,
                    phys_match=phys_score,
                    cap_overlap=cap_score,
                ),
                claimed=_path_matches_resolved(
                    path,
                    order_path,
                    excluded_paths,
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
            candidate.order_path,
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
    preferred_candidate = next(
        (
            candidate
            for candidate in available_candidates
            if _path_matches_resolved(
                candidate.path,
                candidate.order_path,
                preferred_paths,
            )
        ),
        None,
    )
    if preferred_candidate is not None:
        return preferred_candidate
    best = available_candidates[0]
    if len(available_candidates) > 1 and available_candidates[1].score == best.score:
        log.warning(
            "Ambiguous %s match; using %s from candidates %s",
            configured_path,
            best.path,
            [candidate.path for candidate in available_candidates],
        )
    return best


def _model_gamepad_path_for_hardware_id(
    *,
    hardware_id: str | None,
    configured_type: DeviceType,
    enabled: bool,
) -> str | None:
    if not enabled or configured_type != DeviceType.GAMEPAD:
        return None

    model_ids = parse_hardware_model_id(hardware_id)
    if model_ids is None:
        return None

    vendor_id, product_id = model_ids
    return make_keymasq_device_path(vendor_id, product_id)


def _same_keymasq_model_path(path: str, model_path: str) -> bool:
    parsed_path = parse_keymasq_device_path(path)
    parsed_model = parse_keymasq_device_path(model_path)
    return parsed_path is not None and parsed_path == parsed_model


def _probe_cached_device(
    path: str,
    deps: DevicePathResolverDeps,
) -> CachedDeviceInfo | None:
    return _probe_cached_device_info(
        path,
        device_input_fn=deps.device_input_fn,
        detect_input_classes_fn=deps.detect_input_classes_fn,
        primary_input_class_fn=deps.primary_input_class_fn,
        skip_log_message="Skipping device path resolver probe for %s: %s",
        unexpected_log_message=(
            "Unexpected failure probing device path resolver candidate %s"
        ),
    )


def _path_matches_resolved(
    path: str,
    resolved_path: str,
    paths: set[str],
) -> bool:
    if not paths:
        return False
    return path in paths or resolved_path in paths


def _candidate_order_path(path: str, deps: DevicePathResolverDeps) -> str:
    if deps.resolve_stable_path_fn is None:
        return path
    try:
        return deps.resolve_stable_path_fn(path)
    except OSError as exc:
        log.debug("Unable to resolve stable path for candidate %s: %s", path, exc)
        return path
    except Exception:
        log.exception("Unexpected failure resolving stable path for candidate %s", path)
        return path


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
