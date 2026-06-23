import asyncio
import errno
import logging
import os
import queue
import select
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, cast

import evdev

from keymasq.common.devices import (
    clear_device_path_cache,
    get_interface_id,
    normalize_wheel_value,
    resolve_stable_path,
    wheel_button_id,
)
from keymasq.common.types import JsonObject
from keymasq.keymasqd.permission_hints import (
    input_device_permission_message,
    is_permission_error,
)
from keymasq.keymasqd.runtime import device_path_resolver
from keymasq.keymasqd.runtime.adapters import DeviceInfo

log = logging.getLogger("keymasq.keymasqd.capture_manager")


class _CaptureInputDevice(Protocol):
    path: str
    name: str
    fd: int
    info: DeviceInfo

    def grab(self) -> None: ...

    def ungrab(self) -> None: ...

    def close(self) -> None: ...

    def read_one(self) -> evdev.InputEvent | None: ...

    def read(self) -> Iterable[evdev.InputEvent]: ...

    def capabilities(self) -> Mapping[int, Sequence[object]]: ...

    def input_props(self) -> Iterable[int]: ...

    def absinfo(self, axis: int) -> object: ...


def _device_path_resolver_deps() -> device_path_resolver.DevicePathResolverDeps:
    return device_path_resolver.evdev_device_path_resolver_deps(
        lambda path: cast(device_path_resolver.InputDeviceLike, evdev.InputDevice(path)),
    )


def _event_code_name(event_type: int, code: int) -> str:
    bytype = cast(dict[int, dict[int, object]], evdev.ecodes.bytype)
    code_name = bytype.get(event_type, {}).get(code, str(code))
    if isinstance(code_name, tuple):
        tuple_name = cast(tuple[object, ...], code_name)
        first = tuple_name[0] if tuple_name else str(code)
        return first.lower() if isinstance(first, str) else str(code)
    return code_name.lower() if isinstance(code_name, str) else str(code)


@dataclass
class CaptureSession:
    token: str
    hardware_id: str
    devices: list[_CaptureInputDevice]
    started_at: float
    event_queue: queue.SimpleQueue[JsonObject] | None = None
    stop_event: threading.Event | None = None
    reader_thread: threading.Thread | None = None
    notify_loop: asyncio.AbstractEventLoop | None = None
    notify_event: asyncio.Event | None = None
    path_hardware_ids: dict[str, str] = field(default_factory=dict)
    path_sources: dict[str, str] = field(default_factory=dict)
    mode: str = "button"


@dataclass(frozen=True)
class _ComboCaptureAuthorization:
    token: str


class CaptureManager:
    def __init__(self) -> None:
        self._sessions: dict[str, CaptureSession] = {}
        self._combo_capture_authorizations: set[str] = set()

    def begin(
        self,
        hardware_id: str,
        evdev_paths: list[str] | None = None,
        evdev_interfaces: list[JsonObject] | None = None,
        mode: str = "button",
    ) -> JsonObject:
        mode = _capture_mode(mode)
        path_sources: dict[str, str] = {}
        if evdev_interfaces:
            matched, path_sources = self._find_devices_by_interfaces(
                hardware_id,
                evdev_interfaces,
            )
        elif evdev_paths:
            matched = self._find_devices_by_paths(evdev_paths)
        else:
            matched = self._find_devices(*self._parse_hardware_id(hardware_id))
        if not matched:
            raise ValueError(
                input_device_permission_message(f"No devices found for {hardware_id}")
            )

        grabbed: list[_CaptureInputDevice] = []
        warnings: list[str] = []
        permission_error_seen = False
        for device in matched:
            try:
                device.grab()
                grabbed.append(device)
            except OSError as e:
                if e.errno == errno.EBUSY:
                    warnings.append(f"{device.path}: busy")
                elif is_permission_error(e):
                    permission_error_seen = True
                    warnings.append(f"{device.path}: permission denied")
                else:
                    warnings.append(f"{device.path}: {e}")
                _close_device(device)

        if not grabbed:
            message = "No readable/grabbable interfaces found"
            if warnings:
                message = f"{message}: {', '.join(warnings)}"
            if permission_error_seen:
                message = input_device_permission_message(message)
            raise RuntimeError(message)

        token = str(uuid.uuid4())
        self._sessions[token] = CaptureSession(
            token=token,
            hardware_id=hardware_id,
            devices=grabbed,
            started_at=time.time(),
            path_sources=path_sources,
            mode=mode,
        )

        return {
            "token": token,
            "hardware_id": hardware_id,
            "warnings": warnings,
        }

    def read(self, token: str) -> JsonObject:
        session = self._sessions.get(token)
        if session is None:
            raise ValueError("Invalid capture token")

        for device in session.devices:
            event = _read_one_device_event(device)

            if event is None:
                continue

            parsed = self._parse_event(device, event, session.mode, session.path_sources)
            if parsed is not None:
                return {"captured": parsed}

        return {"captured": None}

    def begin_combo(
        self,
        token: str | None = None,
        exclude_paths: set[str] | None = None,
        allow_empty: bool = False,
        hardware_ids: set[str] | None = None,
        hardware_paths: Mapping[str, Sequence[str]] | None = None,
        hardware_interfaces: Mapping[str, Sequence[JsonObject]] | None = None,
        authorization: _ComboCaptureAuthorization | None = None,
    ) -> JsonObject:
        if not self._consume_combo_capture_authorization(authorization):
            raise PermissionError("combo_capture_denied: missing authorization")

        path_sources: dict[str, str] = {}
        if hardware_interfaces:
            path_hardware_ids, path_sources = self._hardware_interface_lookup(
                hardware_interfaces
            )
        else:
            path_hardware_ids = _hardware_path_lookup(hardware_paths or {})
        matched = self._find_combo_devices(
            exclude_paths=exclude_paths or set(),
            hardware_ids=hardware_ids or set(),
            path_hardware_ids=path_hardware_ids,
        )
        if not matched and not allow_empty:
            raise ValueError(
                input_device_permission_message(
                    "No keyboard devices found for combo capture"
                )
            )

        devices = list(matched)
        warnings: list[str] = []

        if not devices and not allow_empty:
            raise RuntimeError("No readable keyboard interfaces found")

        token = token or str(uuid.uuid4())
        if token in self._sessions:
            for device in devices:
                _close_device(device)
            raise ValueError("Capture token already active")

        session = CaptureSession(
            token=token,
            hardware_id="__combo__",
            devices=devices,
            started_at=time.time(),
            event_queue=queue.SimpleQueue(),
            stop_event=threading.Event(),
            path_hardware_ids=path_hardware_ids,
            path_sources=path_sources,
        )
        self._sessions[token] = session
        self._start_combo_reader(session)

        log.info(
            "Started combo capture %s on %d devices",
            token,
            len(devices),
        )
        for device in devices:
            log.debug("Combo capture device %s name=%s", device.path, device.name)

        return {
            "token": token,
            "warnings": warnings,
        }

    def authorize_combo_capture(self) -> _ComboCaptureAuthorization:
        token = str(uuid.uuid4())
        self._combo_capture_authorizations.add(token)
        return _ComboCaptureAuthorization(token=token)

    def _consume_combo_capture_authorization(
        self,
        authorization: _ComboCaptureAuthorization | None,
    ) -> bool:
        if authorization is None:
            return False
        token = str(authorization.token or "").strip()
        if not token:
            return False
        if token not in self._combo_capture_authorizations:
            return False
        self._combo_capture_authorizations.remove(token)
        return True

    def read_combo(self, token: str) -> JsonObject:
        session = self._sessions.get(token)
        if session is None:
            raise ValueError("Invalid capture token")

        queued = self._read_combo_nowait(session)
        if queued is not None:
            return {"event": queued}
        if session.event_queue is not None:
            return {"event": None}

        for device in session.devices:
            event = _read_one_device_event(device)

            if event is None:
                continue

            parsed = self._parse_combo_event(device, event, path_sources=session.path_sources)
            if parsed is not None:
                return {"event": parsed}

        return {"event": None}

    def read_combo_nowait(self, token: str) -> JsonObject:
        session = self._sessions.get(token)
        if session is None:
            raise ValueError("Invalid capture token")
        return {"event": self._read_combo_nowait(session)}

    def register_combo_notifier(
        self,
        token: str,
        loop: asyncio.AbstractEventLoop,
        notify_event: asyncio.Event,
    ) -> None:
        session = self._sessions.get(token)
        if session is None:
            raise ValueError("Invalid capture token")
        session.notify_loop = loop
        session.notify_event = notify_event

    def _read_combo_nowait(self, session: CaptureSession) -> JsonObject | None:
        if session.event_queue is None:
            return None
        try:
            return session.event_queue.get_nowait()
        except queue.Empty:
            return None

    def end(self, token: str) -> JsonObject:
        session = self._sessions.pop(token, None)
        if session is None:
            return {"status": "ok", "ended": False}

        if session.stop_event is not None:
            session.stop_event.set()
        if session.reader_thread is not None and session.reader_thread.is_alive():
            session.reader_thread.join(timeout=0.2)

        for device in session.devices:
            if session.hardware_id != "__combo__":
                _ungrab_device(device)
            _close_device(device, context="capture device during capture end")

        log.info("Ended capture %s hardware_id=%s", token, session.hardware_id)
        return {"status": "ok", "ended": True}

    def _start_combo_reader(self, session: CaptureSession) -> None:
        if session.stop_event is None or session.event_queue is None:
            return

        reader = threading.Thread(
            target=self._combo_reader_loop,
            args=(session,),
            daemon=True,
            name=f"combo-capture-{session.token[:8]}",
        )
        session.reader_thread = reader
        reader.start()

    def _combo_reader_loop(self, session: CaptureSession) -> None:
        if session.stop_event is None or session.event_queue is None:
            return

        fd_map = {device.fd: device for device in session.devices}
        while not session.stop_event.is_set():
            try:
                ready, _, _ = select.select(list(fd_map), [], [], 0.1)
            except (OSError, ValueError) as e:
                log.debug("Combo capture select failed token=%s error=%s", session.token, e)
                return

            for fd in ready:
                device = fd_map.get(fd)
                if device is None:
                    continue
                try:
                    for event in device.read():
                        parsed = self._parse_combo_event(
                            device,
                            event,
                            session.path_hardware_ids,
                            session.path_sources,
                        )
                        if parsed is None:
                            continue
                        session.event_queue.put(parsed)
                        if session.notify_loop is not None and session.notify_event is not None:
                            session.notify_loop.call_soon_threadsafe(session.notify_event.set)
                        log.debug(
                            "Combo capture token=%s event=%s value=%s source=%s",
                            session.token,
                            parsed.get("evdev"),
                            parsed.get("value"),
                            parsed.get("source"),
                        )
                except BlockingIOError:
                    continue
                except OSError as e:
                    log.debug(
                        "Combo capture read failed token=%s path=%s error=%s",
                        session.token,
                        device.path,
                        e,
                    )
                except Exception:
                    log.exception(
                        "Combo capture unexpected read failure token=%s path=%s",
                        session.token,
                        device.path,
                    )

    def _parse_hardware_id(self, hardware_id: str) -> tuple[str, str]:
        if ":" not in hardware_id:
            raise ValueError("Invalid hardware_id")
        vendor_id, product_id = hardware_id.split(":", 1)
        product_id = product_id.split("@", 1)[0]
        return vendor_id.lower(), product_id.lower()

    def _find_devices(self, vendor_id: str, product_id: str) -> list[_CaptureInputDevice]:
        clear_device_path_cache()
        devices: list[_CaptureInputDevice] = []
        list_devices = cast(Callable[[], list[str]], evdev.list_devices)
        for path in list_devices():
            try:
                device = cast(_CaptureInputDevice, evdev.InputDevice(path))
            except OSError:
                continue
            if (
                f"{device.info.vendor:04x}" == vendor_id
                and f"{device.info.product:04x}" == product_id
            ):
                devices.append(device)
            else:
                _close_device(device)
        return devices

    def _find_devices_by_paths(self, evdev_paths: list[str]) -> list[_CaptureInputDevice]:
        clear_device_path_cache()
        devices: list[_CaptureInputDevice] = []
        seen: set[str] = set()
        for path in evdev_paths:
            normalized = str(path or "").strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            try:
                devices.append(cast(_CaptureInputDevice, evdev.InputDevice(normalized)))
            except OSError:
                continue
        return devices

    def _find_devices_by_interfaces(
        self,
        hardware_id: str,
        evdev_interfaces: list[JsonObject],
    ) -> tuple[list[_CaptureInputDevice], dict[str, str]]:
        clear_device_path_cache()
        resolved = device_path_resolver.resolve_evdev_interfaces(
            evdev_interfaces,
            deps=_device_path_resolver_deps(),
            hardware_id=hardware_id,
        )
        devices: list[_CaptureInputDevice] = []
        path_sources: dict[str, str] = {}
        for interface in resolved:
            try:
                device = cast(_CaptureInputDevice, evdev.InputDevice(interface.path))
                devices.append(device)
                if interface.interface_id:
                    path_sources[device.path] = interface.interface_id
            except OSError:
                continue
        return devices, path_sources

    def _hardware_interface_lookup(
        self,
        hardware_interfaces: Mapping[str, Sequence[JsonObject]],
    ) -> tuple[dict[str, str], dict[str, str]]:
        path_hardware_ids: dict[str, str] = {}
        path_sources: dict[str, str] = {}
        clear_device_path_cache()
        deps = _device_path_resolver_deps()
        claimed_paths: set[str] = set()
        for hardware_id, interfaces in hardware_interfaces.items():
            normalized_hardware_id = str(hardware_id or "").lower()
            if not normalized_hardware_id:
                continue
            resolved = device_path_resolver.resolve_evdev_interfaces(
                list(interfaces),
                deps=deps,
                hardware_id=normalized_hardware_id,
                excluded_paths=claimed_paths,
            )
            for interface in resolved:
                aliases = _path_aliases(interface.path)
                claimed_paths.update(aliases)
                for alias in aliases:
                    if alias in path_hardware_ids:
                        continue
                    path_hardware_ids[alias] = normalized_hardware_id
                    if interface.interface_id:
                        path_sources[alias] = interface.interface_id
        return path_hardware_ids, path_sources

    def _find_combo_devices(
        self,
        exclude_paths: set[str],
        hardware_ids: set[str],
        path_hardware_ids: Mapping[str, str] | None = None,
    ) -> list[_CaptureInputDevice]:
        clear_device_path_cache()
        devices: list[_CaptureInputDevice] = []
        path_hardware_ids = path_hardware_ids or {}
        list_devices = cast(Callable[[], list[str]], evdev.list_devices)
        for path in list_devices():
            if path in exclude_paths:
                continue
            keep_device = False
            try:
                device = cast(_CaptureInputDevice, evdev.InputDevice(path))
            except OSError:
                continue

            try:
                if device.name.startswith("keymasq-"):
                    continue
                if path_hardware_ids:
                    if not _hardware_id_for_path(device.path, path_hardware_ids):
                        continue
                elif hardware_ids:
                    device_hardware_id = (
                        f"{device.info.vendor:04x}:{device.info.product:04x}"
                    ).lower()
                    if device_hardware_id not in hardware_ids:
                        continue

                key_codes = _device_key_codes(device)

                has_supported_key = False
                for code in cast(list[object], key_codes):
                    if not isinstance(code, int):
                        continue
                    code_name = _event_code_name(evdev.ecodes.EV_KEY, int(code)).upper()
                    if code_name.startswith(("KEY_", "BTN_")):
                        has_supported_key = True
                        break

                if has_supported_key:
                    devices.append(device)
                    keep_device = True
            finally:
                if not keep_device:
                    _close_device(device)
        return devices

    def _parse_event(
        self,
        device: _CaptureInputDevice,
        event: evdev.InputEvent,
        mode: str = "button",
        path_sources: Mapping[str, str] | None = None,
    ) -> JsonObject | None:
        if mode == "analog":
            if event.type != evdev.ecodes.EV_ABS:
                return None
            evdev_name = _event_code_name(event.type, int(event.code))
            payload: JsonObject = {
                "evdev": evdev_name,
                "code": int(event.code),
                "value": int(event.value),
                "source": self._source_for_device(device, path_sources),
                "stable_path": resolve_stable_path(device.path),
                "device_path": device.path,
            }
            abs_info = _abs_info_payload(device, int(event.code))
            if abs_info:
                payload["absinfo"] = abs_info
            return payload

        if event.type == evdev.ecodes.EV_KEY and event.value == 1:
            evdev_name = _event_code_name(event.type, int(event.code))
            return {
                "evdev": evdev_name,
                "code": int(event.code),
                "source": self._source_for_device(device, path_sources),
                "stable_path": resolve_stable_path(device.path),
                "device_path": device.path,
            }

        if event.type == evdev.ecodes.EV_REL:
            if event.code == evdev.ecodes.REL_WHEEL:
                value = normalize_wheel_value(int(event.value))
                if value is None:
                    return None
                direction = "up" if value > 0 else "down"
                return {
                    "evdev": "rel_wheel",
                    "code": int(event.code),
                    "direction": direction,
                    "value": value,
                    "source": self._source_for_device(device, path_sources),
                    "stable_path": resolve_stable_path(device.path),
                    "device_path": device.path,
                }
            if event.code == evdev.ecodes.REL_HWHEEL:
                value = normalize_wheel_value(int(event.value))
                if value is None:
                    return None
                direction = "right" if value > 0 else "left"
                return {
                    "evdev": "rel_hwheel",
                    "code": int(event.code),
                    "direction": direction,
                    "value": value,
                    "source": self._source_for_device(device, path_sources),
                    "stable_path": resolve_stable_path(device.path),
                    "device_path": device.path,
                }

        return None

    def _parse_combo_event(
        self,
        device: _CaptureInputDevice,
        event: evdev.InputEvent,
        path_hardware_ids: Mapping[str, str] | None = None,
        path_sources: Mapping[str, str] | None = None,
    ) -> JsonObject | None:
        hardware_id = _hardware_id_for_device(device, path_hardware_ids or {})
        if event.type == evdev.ecodes.EV_REL:
            if event.code == evdev.ecodes.REL_WHEEL:
                normalized_value = normalize_wheel_value(int(event.value))
                evdev_name = wheel_button_id("rel_wheel", normalized_value)
            elif event.code == evdev.ecodes.REL_HWHEEL:
                normalized_value = normalize_wheel_value(int(event.value))
                evdev_name = wheel_button_id("rel_hwheel", normalized_value)
            else:
                return None
            if evdev_name is None:
                return None
            return {
                "evdev": evdev_name,
                "code": int(event.code),
                "value": 1,
                "hardware_id": hardware_id,
                "source": self._source_for_device(device, path_sources),
                "stable_path": resolve_stable_path(device.path),
                "device_path": device.path,
            }

        if event.type != evdev.ecodes.EV_KEY or event.value not in {0, 1}:
            return None

        evdev_name = _event_code_name(event.type, int(event.code))
        if not evdev_name.startswith(("key_", "btn_")):
            return None
        return {
            "evdev": evdev_name,
            "code": int(event.code),
            "value": int(event.value),
            "hardware_id": hardware_id,
            "source": self._source_for_device(device, path_sources),
            "stable_path": resolve_stable_path(device.path),
            "device_path": device.path,
        }

    def _source_for_device(
        self,
        device: _CaptureInputDevice,
        path_sources: Mapping[str, str] | None,
    ) -> str:
        configured = str((path_sources or {}).get(device.path, "") or "")
        if configured:
            return configured
        return self._source_for_path(device.path)

    def _source_for_path(self, device_path: str) -> str:
        stable_path = resolve_stable_path(device_path)
        return get_interface_id(stable_path)


def _path_aliases(path: str) -> set[str]:
    aliases = {str(path)}
    try:
        aliases.add(resolve_stable_path(path))
    except OSError as exc:
        log.debug("Unable to resolve stable path alias for %s: %s", path, exc)
    except Exception:
        log.exception("Unexpected failure resolving stable path alias for %s", path)
    try:
        aliases.add(os.path.realpath(path))
    except OSError as exc:
        log.debug("Unable to resolve real path alias for %s: %s", path, exc)
    except Exception:
        log.exception("Unexpected failure resolving real path alias for %s", path)
    return {alias for alias in aliases if alias}


def _hardware_path_lookup(hardware_paths: Mapping[str, Sequence[str]]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for hardware_id, paths in hardware_paths.items():
        normalized_hardware_id = str(hardware_id or "").lower()
        if not normalized_hardware_id:
            continue
        for path in paths:
            for alias in _path_aliases(str(path or "")):
                lookup[alias] = normalized_hardware_id
    return lookup


def _hardware_id_for_path(path: str, path_hardware_ids: Mapping[str, str]) -> str:
    for alias in _path_aliases(path):
        hardware_id = path_hardware_ids.get(alias)
        if hardware_id:
            return hardware_id
    return ""


def _hardware_id_for_device(
    device: _CaptureInputDevice,
    path_hardware_ids: Mapping[str, str],
) -> str:
    return _hardware_id_for_path(
        device.path,
        path_hardware_ids,
    ) or f"{device.info.vendor:04x}:{device.info.product:04x}"


def _capture_mode(mode: str) -> str:
    normalized = str(mode or "button").strip().lower()
    if normalized not in {"button", "analog"}:
        raise ValueError(f"unsupported capture mode: {mode}")
    return normalized


def _read_one_device_event(device: _CaptureInputDevice) -> evdev.InputEvent | None:
    try:
        return device.read_one()
    except BlockingIOError:
        return None
    except OSError as exc:
        log.debug("Failed to read capture device %s: %s", device.path, exc)
        return None
    except Exception:
        log.exception("Unexpected failure reading capture device %s", device.path)
        return None


def _ungrab_device(device: _CaptureInputDevice) -> None:
    try:
        device.ungrab()
    except OSError as exc:
        log.debug("Failed to ungrab capture device %s during capture end: %s", device.path, exc)
    except Exception:
        log.exception(
            "Unexpected failure ungrabbing capture device %s during capture end",
            device.path,
        )


def _close_device(device: _CaptureInputDevice, *, context: str = "capture device") -> None:
    try:
        device.close()
    except OSError as exc:
        log.debug("Failed to close %s %s: %s", context, device.path, exc)
    except Exception:
        log.exception("Unexpected failure closing %s %s", context, device.path)


def _device_key_codes(device: _CaptureInputDevice) -> Sequence[object]:
    try:
        return device.capabilities().get(evdev.ecodes.EV_KEY, [])
    except OSError as exc:
        log.debug("Unable to read combo capture capabilities from %s: %s", device.path, exc)
        return []
    except Exception:
        log.exception("Unexpected failure reading combo capture capabilities from %s", device.path)
        return []


def _abs_info_payload(device: _CaptureInputDevice, code: int) -> JsonObject:
    try:
        info = device.absinfo(code)
    except KeyError:
        return {}
    except OSError as exc:
        log.debug(
            "Unable to read absinfo from capture device %s code=%s: %s",
            device.path,
            code,
            exc,
        )
        return {}
    except Exception:
        log.exception(
            "Unexpected failure reading absinfo from capture device %s code=%s",
            device.path,
            code,
        )
        return {}
    fields = {
        "value": getattr(info, "value", None),
        "minimum": getattr(info, "min", None),
        "maximum": getattr(info, "max", None),
        "fuzz": getattr(info, "fuzz", None),
        "flat": getattr(info, "flat", None),
        "resolution": getattr(info, "resolution", None),
    }
    return {key: int(value) for key, value in fields.items() if isinstance(value, int)}
