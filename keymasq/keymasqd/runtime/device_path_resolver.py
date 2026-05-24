import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, cast

from keymasq.common.devices import (
    canonical_gamepad_button_name,
    capability_names_from_capabilities,
    is_gamepad_button_name,
    is_keymasq_device_path,
    parse_keymasq_device_path,
)
from keymasq.common.models import DeviceType

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
class _Candidate:
    path: str
    phys: str
    device_type: DeviceType
    capabilities: set[str]
    score: tuple[int, int, int]


def interface_descriptors_from_paths(paths: list[str]) -> list[JsonObject]:
    return [
        {"path": str(path), "id": "", "type": "", "capabilities": []}
        for path in paths
        if str(path or "").strip()
    ]


def resolve_evdev_interfaces(
    interfaces: list[JsonObject],
    *,
    device_paths_fn: Callable[[], list[str]],
    device_input_fn: Callable[[str], InputDeviceLike],
    detect_input_classes_fn: Callable[[InputDeviceLike], list[str]],
    primary_input_class_fn: Callable[[Iterable[str | DeviceType] | None], DeviceType],
) -> list[ResolvedInterface]:
    resolved: list[ResolvedInterface] = []
    selected_paths: set[str] = set()

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
            device_paths_fn=device_paths_fn,
            device_input_fn=device_input_fn,
            detect_input_classes_fn=detect_input_classes_fn,
            primary_input_class_fn=primary_input_class_fn,
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
    device_paths_fn: Callable[[], list[str]],
    device_input_fn: Callable[[str], InputDeviceLike],
    detect_input_classes_fn: Callable[[InputDeviceLike], list[str]],
    primary_input_class_fn: Callable[[Iterable[str | DeviceType] | None], DeviceType],
) -> _Candidate | None:
    parsed = parse_keymasq_device_path(configured_path)
    if parsed is None:
        return None
    vendor_id, product_id = parsed
    candidates: list[_Candidate] = []
    for path in sorted(device_paths_fn()):
        if path in selected_paths:
            continue
        device: InputDeviceLike | None = None
        try:
            device = device_input_fn(path)
            if _is_keymasq_virtual_device(device):
                continue
            info = device.info
            if f"{info.vendor:04x}" != vendor_id or f"{info.product:04x}" != product_id:
                continue
            caps = device.capabilities()
            capability_names = _normalize_capability_names(capability_names_from_capabilities(caps))
            detected_type = primary_input_class_fn(detect_input_classes_fn(device))
            phys = str(getattr(device, "phys", "") or "").strip()
            type_match = configured_type != DeviceType.OTHER and detected_type == configured_type
            type_score = int(configured_type == DeviceType.OTHER or type_match)
            phys_score = int(bool(configured_phys) and phys == configured_phys)
            cap_score = len(configured_caps & capability_names)
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
                    phys=phys,
                    device_type=detected_type,
                    capabilities=capability_names,
                    score=(type_score, phys_score, cap_score),
                )
            )
        except Exception:
            continue
        finally:
            if device is not None:
                _close_device(device)

    if not candidates:
        return None
    candidates.sort(
        key=lambda candidate: (
            -candidate.score[0],
            -candidate.score[1],
            -candidate.score[2],
            candidate.path,
        )
    )
    best = candidates[0]
    if len(candidates) > 1 and candidates[1].score == best.score:
        log.warning(
            "Ambiguous %s match; using %s from candidates %s",
            configured_path,
            best.path,
            [candidate.path for candidate in candidates],
        )
    return best


def _close_device(device: object) -> None:
    close = getattr(device, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception:
        pass


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
