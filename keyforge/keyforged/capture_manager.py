import asyncio
import errno
import logging
import queue
import select
import threading
import time
import uuid
from dataclasses import dataclass

import evdev

from keyforge.common.devices import clear_device_path_cache, get_interface_id, resolve_stable_path

log = logging.getLogger("keyforge.keyforged.capture_manager")


@dataclass
class CaptureSession:
    token: str
    hardware_id: str
    devices: list[evdev.InputDevice]
    started_at: float
    event_queue: queue.SimpleQueue[dict] | None = None
    stop_event: threading.Event | None = None
    reader_thread: threading.Thread | None = None
    notify_loop: asyncio.AbstractEventLoop | None = None
    notify_event: asyncio.Event | None = None


@dataclass(frozen=True)
class _ComboCaptureAuthorization:
    token: str


class CaptureManager:
    def __init__(self) -> None:
        self._sessions: dict[str, CaptureSession] = {}
        self._combo_capture_authorizations: set[str] = set()

    def begin(self, hardware_id: str) -> dict:
        vendor_id, product_id = self._parse_hardware_id(hardware_id)
        matched = self._find_devices(vendor_id, product_id)
        if not matched:
            raise ValueError(f"No devices found for {hardware_id}")

        grabbed: list[evdev.InputDevice] = []
        warnings: list[str] = []
        for device in matched:
            try:
                device.grab()
                grabbed.append(device)
            except OSError as e:
                if e.errno == errno.EBUSY:
                    warnings.append(f"{device.path}: busy")
                elif e.errno == errno.EACCES:
                    warnings.append(f"{device.path}: permission denied")
                else:
                    warnings.append(f"{device.path}: {e}")

        if not grabbed:
            raise RuntimeError("No readable/grabbable interfaces found")

        token = str(uuid.uuid4())
        self._sessions[token] = CaptureSession(
            token=token,
            hardware_id=hardware_id,
            devices=grabbed,
            started_at=time.time(),
        )

        return {
            "token": token,
            "hardware_id": hardware_id,
            "warnings": warnings,
        }

    def read(self, token: str) -> dict:
        session = self._sessions.get(token)
        if session is None:
            raise ValueError("Invalid capture token")

        for device in session.devices:
            try:
                event = device.read_one()
            except Exception:
                continue

            if event is None:
                continue

            parsed = self._parse_event(device, event)
            if parsed is not None:
                return {"captured": parsed}

        return {"captured": None}

    def begin_combo(
        self,
        token: str | None = None,
        exclude_paths: set[str] | None = None,
        allow_empty: bool = False,
        hardware_ids: set[str] | None = None,
        authorization: _ComboCaptureAuthorization | None = None,
    ) -> dict:
        if not self._consume_combo_capture_authorization(authorization):
            raise PermissionError("combo_capture_denied: missing authorization")

        matched = self._find_combo_devices(
            exclude_paths=exclude_paths or set(),
            hardware_ids=hardware_ids or set(),
        )
        if not matched and not allow_empty:
            raise ValueError("No keyboard devices found for combo capture")

        devices: list[evdev.InputDevice] = []
        warnings: list[str] = []
        for device in matched:
            try:
                devices.append(device)
            except Exception as e:
                warnings.append(f"{device.path}: {e}")

        if not devices and not allow_empty:
            raise RuntimeError("No readable keyboard interfaces found")

        token = token or str(uuid.uuid4())
        session = CaptureSession(
            token=token,
            hardware_id="__combo__",
            devices=devices,
            started_at=time.time(),
            event_queue=queue.SimpleQueue(),
            stop_event=threading.Event(),
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

    def _authorize_combo_capture(self) -> _ComboCaptureAuthorization:
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

    def read_combo(self, token: str) -> dict:
        session = self._sessions.get(token)
        if session is None:
            raise ValueError("Invalid capture token")

        queued = self._read_combo_nowait(session)
        if queued is not None:
            return {"event": queued}

        for device in session.devices:
            try:
                event = device.read_one()
            except Exception:
                continue

            if event is None:
                continue

            parsed = self._parse_combo_event(device, event)
            if parsed is not None:
                return {"event": parsed}

        return {"event": None}

    def read_combo_nowait(self, token: str) -> dict:
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

    def _read_combo_nowait(self, session: CaptureSession) -> dict | None:
        if session.event_queue is None:
            return None
        try:
            return session.event_queue.get_nowait()
        except queue.Empty:
            return None

    def end(self, token: str) -> dict:
        session = self._sessions.pop(token, None)
        if session is None:
            return {"status": "ok", "ended": False}

        if session.stop_event is not None:
            session.stop_event.set()
        if session.reader_thread is not None and session.reader_thread.is_alive():
            session.reader_thread.join(timeout=0.2)

        for device in session.devices:
            if session.hardware_id != "__combo__":
                try:
                    device.ungrab()
                except Exception:
                    pass
            try:
                device.close()
            except Exception:
                pass

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
            except Exception as e:
                log.debug("Combo capture select failed token=%s error=%s", session.token, e)
                return

            for fd in ready:
                device = fd_map.get(fd)
                if device is None:
                    continue
                try:
                    for event in device.read():
                        parsed = self._parse_combo_event(device, event)
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
                except Exception as e:
                    log.debug(
                        "Combo capture unexpected read failure token=%s path=%s error=%s",
                        session.token,
                        device.path,
                        e,
                    )

    def _parse_hardware_id(self, hardware_id: str) -> tuple[str, str]:
        if ":" not in hardware_id:
            raise ValueError("Invalid hardware_id")
        vendor_id, product_id = hardware_id.split(":", 1)
        return vendor_id.lower(), product_id.lower()

    def _find_devices(self, vendor_id: str, product_id: str) -> list[evdev.InputDevice]:
        clear_device_path_cache()
        devices: list[evdev.InputDevice] = []
        for path in evdev.list_devices():
            try:
                device = evdev.InputDevice(path)
            except Exception:
                continue
            if (
                f"{device.info.vendor:04x}" == vendor_id
                and f"{device.info.product:04x}" == product_id
            ):
                devices.append(device)
        return devices

    def _find_combo_devices(
        self,
        exclude_paths: set[str],
        hardware_ids: set[str],
    ) -> list[evdev.InputDevice]:
        clear_device_path_cache()
        devices: list[evdev.InputDevice] = []
        for path in evdev.list_devices():
            if path in exclude_paths:
                continue
            try:
                device = evdev.InputDevice(path)
            except Exception:
                continue

            if device.name.startswith("keyforge-"):
                continue
            if hardware_ids:
                device_hardware_id = (
                    f"{device.info.vendor:04x}:{device.info.product:04x}"
                ).lower()
                if device_hardware_id not in hardware_ids:
                    continue

            try:
                key_codes = device.capabilities().get(evdev.ecodes.EV_KEY, [])
            except Exception:
                key_codes = []

            has_supported_key = False
            for code in key_codes:
                code_name = evdev.ecodes.bytype.get(evdev.ecodes.EV_KEY, {}).get(code)
                if isinstance(code_name, tuple):
                    code_name = code_name[0] if code_name else None
                if isinstance(code_name, str) and code_name.startswith(("KEY_", "BTN_")):
                    has_supported_key = True
                    break

            if has_supported_key:
                devices.append(device)
        return devices

    def _parse_event(self, device: evdev.InputDevice, event: evdev.InputEvent) -> dict | None:
        if event.type == evdev.ecodes.EV_KEY and event.value == 1:
            code_name = evdev.ecodes.bytype.get(event.type, {}).get(event.code, str(event.code))
            if isinstance(code_name, tuple):
                code_name = code_name[0] if code_name else str(event.code)
            evdev_name = code_name.lower()
            return {
                "evdev": evdev_name,
                "code": int(event.code),
                "source": self._source_for_path(device.path),
                "stable_path": resolve_stable_path(device.path),
                "device_path": device.path,
            }

        if event.type == evdev.ecodes.EV_REL:
            if event.code == evdev.ecodes.REL_WHEEL:
                direction = "up" if event.value > 0 else "down"
                return {
                    "evdev": "rel_wheel",
                    "direction": direction,
                    "value": int(event.value),
                    "source": self._source_for_path(device.path),
                    "stable_path": resolve_stable_path(device.path),
                    "device_path": device.path,
                }
            if event.code == evdev.ecodes.REL_HWHEEL:
                direction = "right" if event.value > 0 else "left"
                return {
                    "evdev": "rel_hwheel",
                    "direction": direction,
                    "value": int(event.value),
                    "source": self._source_for_path(device.path),
                    "stable_path": resolve_stable_path(device.path),
                    "device_path": device.path,
                }

        return None

    def _parse_combo_event(self, device: evdev.InputDevice, event: evdev.InputEvent) -> dict | None:
        if event.type != evdev.ecodes.EV_KEY or event.value not in {0, 1}:
            return None

        code_name = evdev.ecodes.bytype.get(event.type, {}).get(event.code, str(event.code))
        if isinstance(code_name, tuple):
            code_name = code_name[0] if code_name else str(event.code)
        evdev_name = code_name.lower()
        if not evdev_name.startswith(("key_", "btn_")):
            return None
        return {
            "evdev": evdev_name,
            "code": int(event.code),
            "value": int(event.value),
            "hardware_id": f"{device.info.vendor:04x}:{device.info.product:04x}",
            "source": self._source_for_path(device.path),
            "stable_path": resolve_stable_path(device.path),
            "device_path": device.path,
        }

    def _source_for_path(self, device_path: str) -> str:
        stable_path = resolve_stable_path(device_path)
        return get_interface_id(stable_path)
