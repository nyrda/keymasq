"""Pure-ish planning and capability matching for device grab transactions."""

import logging
from collections.abc import Sequence
from typing import Any, cast

import evdev

from keymasq.common.devices import resolve_evdev_code, resolve_evdev_event_type
from keymasq.keymasqd.runtime import device_path_resolver
from keymasq.keymasqd.runtime.grab.source_hiding import (
    interfaces_request_gamepad_source_hiding,
)
from keymasq.keymasqd.runtime.grab.state import (
    GrabDeviceDeps,
    GrabManager,
    GrabPlan,
    GrabRequest,
    ManagedGrabbedDevice,
    ResolveStablePathFn,
)

log = logging.getLogger("keymasqd.devices")


def build_grab_plan(
    manager: GrabManager,
    request: GrabRequest,
    deps: GrabDeviceDeps,
) -> GrabPlan:
    """Resolve configured interfaces and snapshot rollback state without mutating it."""

    raw_interfaces = (
        list(request.evdev_interfaces)
        if request.evdev_interfaces
        else device_path_resolver.interface_descriptors_from_paths(request.evdev_paths)
    )
    requests_gamepad_source_hiding = interfaces_request_gamepad_source_hiding(raw_interfaces)

    existing_devices = list(manager.grabbed_devices.get(request.hardware_id, []))
    existing_by_claim_path = grabbed_devices_by_claim_path(
        existing_devices,
        resolve_stable_path_fn=deps.resolve_stable_path_fn,
    )
    previous_desired_paths_raw = manager.grab_state.desired_paths.get(request.hardware_id)
    previous_desired_paths = (
        set(previous_desired_paths_raw) if previous_desired_paths_raw is not None else None
    )
    previous_desired_config = manager.grab_state.desired_grabs.get(request.hardware_id)
    excluded_paths = grabbed_paths_for_other_hardware(
        manager,
        request.hardware_id,
        resolve_stable_path_fn=deps.resolve_stable_path_fn,
    )
    resolved_interfaces = device_path_resolver.resolve_evdev_interfaces(
        raw_interfaces,
        deps=deps.device_path_resolver_deps,
        hardware_id=request.hardware_id,
        excluded_paths=excluded_paths,
        preferred_paths=grabbed_paths_for_hardware(
            manager,
            request.hardware_id,
            resolve_stable_path_fn=deps.resolve_stable_path_fn,
        ),
        match_model_gamepads=True,
    )
    requested_interface_paths = [
        deps.resolve_stable_path_fn(interface.path) for interface in resolved_interfaces
    ]
    requested_paths = set(requested_interface_paths)
    requested_claim_paths: set[str] = set()
    resolved_by_claim_path: dict[str, device_path_resolver.ResolvedInterface] = {}
    for interface in resolved_interfaces:
        aliases = path_claim_aliases(
            interface.path,
            resolve_stable_path_fn=deps.resolve_stable_path_fn,
        )
        requested_claim_paths.update(aliases)
        for alias in aliases:
            resolved_by_claim_path.setdefault(alias, interface)
    raw_interface_paths = {
        path
        for descriptor in raw_interfaces
        if (path := str(descriptor.get("path", "") or "").strip())
    }
    desired_paths = requested_paths | raw_interface_paths
    mapped_evdev_names = {name.lower() for name in request.button_map.values()}
    resolved_button_codes = {
        button_id: int(code) for button_id, code in (request.button_codes or {}).items()
    }
    resolved_button_values = {
        button_id: int(value) for button_id, value in (request.button_values or {}).items()
    }
    button_mapped_bindings = {
        (int(event_type), int(code))
        for button_id, code in resolved_button_codes.items()
        if (event_type := resolve_evdev_event_type(request.button_map.get(button_id))) is not None
    }
    analog_inputs = dict(request.analog_inputs or {})
    mapped_bindings = button_mapped_bindings | analog_input_bindings(analog_inputs)

    return GrabPlan(
        hardware_id=request.hardware_id,
        raw_interfaces=raw_interfaces,
        evdev_interfaces_provided=request.evdev_interfaces is not None,
        resolved_interfaces=resolved_interfaces,
        requested_paths=requested_paths,
        requested_claim_paths=requested_claim_paths,
        resolved_by_claim_path=resolved_by_claim_path,
        desired_paths=desired_paths,
        mapped_evdev_names=mapped_evdev_names,
        resolved_button_codes=resolved_button_codes,
        resolved_button_values=resolved_button_values,
        button_mapped_bindings=button_mapped_bindings,
        mapped_bindings=mapped_bindings,
        analog_inputs=analog_inputs,
        existing_devices=existing_devices,
        existing_by_claim_path=existing_by_claim_path,
        previous_desired_paths=previous_desired_paths,
        previous_desired_config=previous_desired_config,
        requests_gamepad_source_hiding=requests_gamepad_source_hiding,
    )


def persist_desired_grab(
    manager: GrabManager,
    request: GrabRequest,
    plan: GrabPlan,
    deps: GrabDeviceDeps,
) -> None:
    if not request.update_desired:
        return
    manager.grab_state.desired_paths[request.hardware_id] = set(plan.desired_paths)
    manager.grab_state.desired_grabs[request.hardware_id] = deps.desired_grab_config_cls(
        paths=set(plan.desired_paths),
        button_map=dict(request.button_map),
        button_codes=dict(plan.resolved_button_codes),
        button_values=dict(plan.resolved_button_values),
        analog_inputs=dict(plan.analog_inputs),
        force_grab_unmapped=bool(request.force_grab_unmapped),
        evdev_interfaces=list(plan.raw_interfaces) if plan.evdev_interfaces_provided else [],
    )


def log_grab_request(plan: GrabPlan) -> None:
    log.info(
        "Grab request for %s: paths=%d mapped_evdev_names=%d mapped_bindings=%d",
        plan.hardware_id,
        len(plan.requested_paths),
        len(plan.mapped_evdev_names),
        len(plan.mapped_bindings),
    )


def update_existing_devices(
    plan: GrabPlan,
    request: GrabRequest,
    deps: GrabDeviceDeps,
) -> None:
    """Apply a newly planned mapping configuration to already grabbed interfaces."""

    for device in plan.existing_devices:
        device_claim_paths = grabbed_device_claim_paths(
            device,
            resolve_stable_path_fn=deps.resolve_stable_path_fn,
        )
        resolved_interface = next(
            (
                plan.resolved_by_claim_path[path]
                for path in device_claim_paths
                if path in plan.resolved_by_claim_path
            ),
            None,
        )
        interface_id = str(
            (resolved_interface.interface_id if resolved_interface is not None else "")
            or deps.get_interface_id_fn(str(getattr(device, "path", "") or ""))
            or ""
        ).lower()
        if interface_id:
            device.interface_id = interface_id
        device.update_button_map(
            request.button_map,
            plan.resolved_button_codes,
            plan.resolved_button_values,
        )
        update_analog_inputs = getattr(device, "update_analog_inputs", None)
        if callable(update_analog_inputs):
            update_analog_inputs(dict(plan.analog_inputs))


def grabbed_paths_for_other_hardware(
    manager: GrabManager,
    hardware_id: str,
    *,
    resolve_stable_path_fn: ResolveStablePathFn | None = None,
) -> set[str]:
    requested_hardware_id = str(hardware_id or "").strip().lower()
    paths: set[str] = set()
    for grabbed_hardware_id, devices in manager.grabbed_devices.items():
        if str(grabbed_hardware_id or "").strip().lower() == requested_hardware_id:
            continue
        for device in devices:
            paths.update(
                grabbed_device_claim_paths(
                    device,
                    resolve_stable_path_fn=resolve_stable_path_fn,
                )
            )
    return paths


def grabbed_paths_for_hardware(
    manager: GrabManager,
    hardware_id: str,
    *,
    resolve_stable_path_fn: ResolveStablePathFn | None = None,
) -> set[str]:
    paths: set[str] = set()
    for device in manager.grabbed_devices.get(hardware_id, []):
        paths.update(
            grabbed_device_claim_paths(
                device,
                resolve_stable_path_fn=resolve_stable_path_fn,
            )
        )
    return paths


def grabbed_devices_by_claim_path(
    devices: Sequence[ManagedGrabbedDevice],
    *,
    resolve_stable_path_fn: ResolveStablePathFn | None = None,
) -> dict[str, ManagedGrabbedDevice]:
    by_path: dict[str, ManagedGrabbedDevice] = {}
    for device in devices:
        for path in grabbed_device_claim_paths(
            device,
            resolve_stable_path_fn=resolve_stable_path_fn,
        ):
            by_path.setdefault(path, device)
    return by_path


def grabbed_device_claim_paths(
    device: ManagedGrabbedDevice,
    *,
    resolve_stable_path_fn: ResolveStablePathFn | None = None,
) -> set[str]:
    paths: set[str] = set()
    for attr in ("path", "stable_path", "resolved_event_path"):
        paths.update(
            path_claim_aliases(
                getattr(device, attr, ""),
                resolve_stable_path_fn=resolve_stable_path_fn,
            )
        )
    return paths


def path_claim_aliases(
    path: object,
    *,
    resolve_stable_path_fn: ResolveStablePathFn | None = None,
) -> set[str]:
    path_text = str(path or "").strip()
    if not path_text:
        return set()
    paths = {path_text}
    if resolve_stable_path_fn is None:
        return paths
    try:
        stable_path = resolve_stable_path_fn(path_text)
    except OSError as exc:
        log.debug("Unable to resolve current stable path for grabbed %s: %s", path_text, exc)
        return paths
    except Exception:
        log.exception("Unexpected failure resolving current stable path for grabbed %s", path_text)
        return paths
    if stable_path:
        paths.add(stable_path)
    return paths


def restore_desired_grab_state(
    manager: GrabManager,
    hardware_id: str,
    previous_desired_paths: set[str] | None,
    previous_desired_config: object | None,
) -> None:
    if previous_desired_paths is None:
        manager.grab_state.desired_paths.pop(hardware_id, None)
    else:
        manager.grab_state.desired_paths[hardware_id] = set(previous_desired_paths)

    if previous_desired_config is None:
        manager.grab_state.desired_grabs.pop(hardware_id, None)
    else:
        manager.grab_state.desired_grabs[hardware_id] = previous_desired_config


def store_grabbed_devices(
    manager: GrabManager,
    hardware_id: str,
    devices: Sequence[ManagedGrabbedDevice],
) -> None:
    if devices:
        manager.grabbed_devices[hardware_id] = list(devices)
    else:
        manager.grabbed_devices.pop(hardware_id, None)


def device_has_mapped_buttons(
    caps: dict[int, Sequence[object]],
    mapped_evdev_names: set[str],
    mapped_bindings: set[tuple[int, int]] | None,
    *,
    evdev_mod: Any,
) -> bool:
    mapped_binding_set = {
        (int(event_type), int(code)) for event_type, code in (mapped_bindings or set())
    }
    for ev_type, codes in caps.items():
        if ev_type == evdev_mod.ecodes.EV_SYN:
            continue
        for code in codes:
            code_val = _capability_code(code)
            if code_val is None:
                continue

            if (int(ev_type), int(code_val)) in mapped_binding_set:
                return True

            try:
                code_name = _normalize_evdev_name(
                    evdev_mod.ecodes.bytype[ev_type].get(code_val, str(code_val)),
                    str(code_val),
                )
                if code_name.lower() in mapped_evdev_names:
                    return True
            except (KeyError, TypeError):
                log.debug("Unable to resolve evdev capability name", exc_info=True)
    return False


def _capability_code(code: object) -> int | None:
    if isinstance(code, tuple):
        if not code or not isinstance(code[0], int):
            return None
        return code[0]
    if isinstance(code, int):
        return code
    return None


def _normalize_evdev_name(value: object, default: str) -> str:
    if isinstance(value, (tuple, list)):
        items = cast(Sequence[object], value)
        return default if not items else str(items[0])
    return str(value)


def analog_input_bindings(
    analog_inputs: dict[str, object],
    *,
    source: str | None = None,
) -> set[tuple[int, int]]:
    bindings: set[tuple[int, int]] = set()
    normalized_source = str(source or "").strip().lower()
    for raw_input in analog_inputs.values():
        if not isinstance(raw_input, dict):
            continue
        input_data = cast(dict[str, object], raw_input)
        input_source = str(input_data.get("source", "") or "").strip().lower()
        if normalized_source and input_source and input_source != normalized_source:
            continue
        raw_axes = input_data.get("axes")
        if not isinstance(raw_axes, list):
            continue
        for raw_axis in cast(list[object], raw_axes):
            if not isinstance(raw_axis, dict):
                continue
            axis_data = cast(dict[str, object], raw_axis)
            code = _axis_code(axis_data)
            if code is not None:
                bindings.add((int(evdev.ecodes.EV_ABS), int(code)))
    return bindings


def _axis_code(axis: dict[str, object]) -> int | None:
    evdev_code = axis.get("evdev_code")
    if isinstance(evdev_code, int):
        return evdev_code
    if isinstance(evdev_code, str):
        try:
            return int(evdev_code, 0)
        except ValueError:
            return None
    return resolve_evdev_code(str(axis.get("evdev", "") or ""))
