import json
import socket
import sys
from typing import Any, cast

from keymasq.common.paths import SESSION_SOCKET_PATH

JsonObject = dict[str, Any]


def _session_unavailable() -> JsonObject:
    return {"status": "error", "message": "Session unavailable"}


def _session_request(payload: JsonObject, timeout: float = 5.0) -> JsonObject | None:
    if not SESSION_SOCKET_PATH.exists():
        return None

    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(str(SESSION_SOCKET_PATH))
        sock.send((json.dumps(payload) + "\n").encode())

        buffer = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buffer += chunk
            if b"\n" in buffer:
                break

        sock.close()
        if not buffer:
            return None
        line = buffer.split(b"\n", 1)[0]
        decoded = json.loads(line.decode())
        if isinstance(decoded, dict):
            return cast(JsonObject, decoded)
        return None
    except Exception:
        return None


def _request_or_error(payload: JsonObject) -> JsonObject:
    return _session_request(payload) or _session_unavailable()


def _message(result: JsonObject, default: str) -> str:
    return str(result.get("message") or result.get("error") or default)


def _print_json(result: JsonObject) -> None:
    print(json.dumps(result, indent=2, sort_keys=True))


def _handled_json_or_error(result: JsonObject, json_output: bool) -> bool:
    if json_output:
        _print_json(result)
        if result.get("status") != "ok":
            sys.exit(1)
        return True

    if result.get("status") != "ok":
        print(f"Error: {_message(result, 'Session unavailable')}")
        sys.exit(1)
    return False


def _bool_status(value: object, true_text: str, false_text: str) -> str:
    return true_text if bool(value) else false_text


def _names(value: object) -> str:
    if not isinstance(value, list):
        return "passthrough"
    names = [str(name) for name in value if str(name)]
    return ", ".join(names) if names else "passthrough"


def _device_label(hardware_id: str, device: JsonObject) -> str:
    device_name = str(device.get("device_name") or "").strip()
    if not device_name or device_name == hardware_id:
        return hardware_id
    return f"{device_name} ({hardware_id})"


def status_cli(*, json_output: bool = False) -> None:
    result = _request_or_error({"command": "get_status"})
    if _handled_json_or_error(result, json_output):
        return

    keymasqd_state = _bool_status(
        result.get("keymasqd_connected"), "connected", "disconnected"
    )
    print(f"keymasqd: {keymasqd_state}")

    compositor_name = str(result.get("compositor_name") or result.get("compositor_id") or "unknown")
    compositor_id = str(result.get("compositor_id") or "")
    compositor = (
        f"{compositor_name} ({compositor_id})"
        if compositor_id and compositor_id != compositor_name
        else compositor_name
    )
    if not bool(result.get("compositor_supported", False)):
        compositor = f"{compositor} [unsupported]"
    print(f"compositor: {compositor}")

    listener_name = str(result.get("listener_name") or "none")
    listener_state = _bool_status(result.get("listener_active"), "active", "inactive")
    print(f"listener: {listener_state} ({listener_name})")

    recording_state = _bool_status(result.get("recording_active"), "active", "idle")
    print(f"recording: {recording_state}")

    unlock_required = bool(result.get("recording_unlock_required", True))
    raw_unlocked = bool(result.get("recording_unlocked", False))
    unlocked = raw_unlocked or not unlock_required
    if not unlock_required:
        unlock_state = "not required"
    elif unlocked:
        source = str(result.get("recording_unlock_source") or "unknown")
        unlock_state = f"unlocked ({source})"
    else:
        unlock_state = "locked"
    print(f"recording unlock: {unlock_state}")

    if "active_profiles" in result:
        print(f"active profiles: {_names(result.get('active_profiles'))}")

    raw_devices = result.get("devices")
    if isinstance(raw_devices, dict) and raw_devices:
        print("devices:")
        for hardware_id in sorted(str(key) for key in raw_devices):
            raw_device = raw_devices.get(hardware_id)
            device = cast(JsonObject, raw_device) if isinstance(raw_device, dict) else {}
            print(f"  {_device_label(hardware_id, device)}")
            print(f"    active: {_names(device.get('profiles'))}")
            print(f"    mappings: {int(device.get('mapping_count', 0) or 0)}")

    raw_window = result.get("window")
    window = cast(JsonObject, raw_window) if isinstance(raw_window, dict) else {}
    title = str(window.get("title") or "")
    app_id = str(window.get("app_id") or window.get("class") or "")
    if title or app_id:
        label = f"{app_id} - {title}" if app_id and title else app_id or title
        print(f"window: {label}")


def list_macros_cli(*, json_output: bool = False) -> None:
    result = _request_or_error({"command": "list_macros"})
    if _handled_json_or_error(result, json_output):
        return

    macros = result.get("macros", [])
    if not macros:
        print("No macros found")
        return

    for macro in macros:
        name = str(macro.get("name", ""))
        duration_ms = int(macro.get("duration_ms", 0) or 0)
        event_count = int(macro.get("event_count", 0) or 0)
        print(f"{name}\t{duration_ms}ms\t{event_count} events")


def play_macro_cli(name: str, speed: float = 1.0, *, json_output: bool = False) -> None:
    result = _request_or_error({"command": "play_macro", "name": name, "speed": float(speed)})
    if _handled_json_or_error(result, json_output):
        return
    print(f"Played macro: {name}")


def cancel_macro_cli(*, json_output: bool = False) -> None:
    result = _request_or_error({"command": "cancel_macro_playback"})
    if _handled_json_or_error(result, json_output):
        return
    cancelled = bool(result.get("cancelled", True))
    if cancelled:
        print("Cancelled running macro playback")
    else:
        print("No macro playback was running")


def set_diagnostics_cli(
    enabled: bool, interval: float = 5.0, *, json_output: bool = False
) -> None:
    result = _request_or_error(
        {"command": "set_diagnostics", "enabled": bool(enabled), "interval": float(interval)}
    )
    if _handled_json_or_error(result, json_output):
        return

    raw_data = result.get("data")
    data = cast(JsonObject, raw_data) if isinstance(raw_data, dict) else {}
    state = "enabled" if bool(data.get("enabled", enabled)) else "disabled"
    print(f"Diagnostics {state} (interval={float(data.get('interval', interval)):.2f}s)")


def _profile_kind(profile: JsonObject) -> str:
    if bool(profile.get("is_permanent", False)):
        return "permanent"
    if int(profile.get("window_rule_count", 0) or 0) > 0:
        return "conditional"
    return "standard"


def list_profiles_cli(*, json_output: bool = False) -> None:
    result = _request_or_error({"command": "list_profiles"})
    if _handled_json_or_error(result, json_output):
        return

    profiles = result.get("profiles", [])
    devices = result.get("devices", [])
    if not profiles:
        print("No profiles found")
        return

    print("Profiles:")
    for profile in profiles:
        name = str(profile.get("name", ""))
        enabled = bool(profile.get("enabled", False))
        active = bool(profile.get("active", False))
        marker = "*" if active else " "
        kind = _profile_kind(profile)
        priority = int(profile.get("priority", 0) or 0)
        window_rule_count = int(profile.get("window_rule_count", 0) or 0)
        device_names = ", ".join(str(device) for device in profile.get("devices", []))
        details = [kind, f"priority={priority}"]
        if window_rule_count > 0:
            details.append(f"rules={window_rule_count}")
        if device_names:
            details.append(f"devices={device_names}")
        state = "on" if enabled else "off"
        print(f"  [{marker}] [{state:3}] {name} ({', '.join(details)})")

    if devices:
        print()
        print("Devices:")
        for device in devices:
            hardware_id = str(device.get("hardware_id", ""))
            device_name = str(device.get("device_name", hardware_id) or hardware_id)
            active_profiles = ", ".join(str(name) for name in device.get("active_profiles", []))
            mapping_count = int(device.get("mapping_count", 0) or 0)
            print(f"  {hardware_id}  {device_name}")
            print(f"    active: {active_profiles or 'passthrough'}")
            print(f"    mappings: {mapping_count}")


def set_profile_state_cli(
    command: str, profile_name: str, *, json_output: bool = False
) -> None:
    result = _request_or_error(
        {
            "command": command,
            "profile_name": profile_name,
        }
    )
    if _handled_json_or_error(result, json_output):
        return

    enabled = bool(result.get("enabled", False))
    active_profiles = ", ".join(str(name) for name in result.get("active_profiles", []))
    state = "enabled" if enabled else "disabled"
    print(f"{profile_name} is now {state}")
    print(f"Active profiles: {active_profiles or 'passthrough'}")
