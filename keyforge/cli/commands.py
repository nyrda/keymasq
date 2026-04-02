import asyncio
import json
import socket
import sys
from typing import Any, cast

import evdev

from keyforge.common.paths import SESSION_SOCKET_PATH

JsonObject = dict[str, Any]


async def list_devices(verbose: bool = False) -> None:
    print("Available input devices:\n")

    devices = evdev.list_devices()

    for path in sorted(devices):
        try:
            device = evdev.InputDevice(path)
            info = device.info

            print(f"{path}")
            print(f"  Name: {device.name}")
            print(f"  VID:PID: {info.vendor:04x}:{info.product:04x}")

            if verbose:
                caps = device.capabilities()
                print("  Capabilities:")
                for ev_type, codes in caps.items():
                    type_name = evdev.ecodes.EV.get(ev_type, f"UNKNOWN({ev_type})")
                    code_names = []
                    for code in codes:
                        if isinstance(code, tuple):
                            code_name = evdev.ecodes.bytype[ev_type].get(code[0], str(code[0]))
                            code_names.append(f"{code_name}[{code[1]}]")
                        else:
                            code_name = evdev.ecodes.bytype[ev_type].get(code, str(code))
                            code_names.append(code_name)
                    print(f"    {type_name}: {', '.join(str(c) for c in code_names[:10])}")
                    if len(code_names) > 10:
                        print(f"      ... and {len(code_names) - 10} more")

            print()

        except Exception as e:
            if verbose:
                print(f"{path}: Error - {e}\n")


def create_hardware(vid: str, pid: str) -> None:
    print(f"Creating hardware config for {vid}:{pid}")
    print("This feature requires the GUI for interactive setup.")
    print("Run: keyforge")


async def test_device(device_path: str) -> None:
    try:
        device = evdev.InputDevice(device_path)
        print(f"Listening on: {device.name}")
        print(f"Path: {device_path}")
        print("Press Ctrl+C to stop\n")

        loop = asyncio.get_event_loop()

        def _read_events() -> list[evdev.InputEvent]:
            return list(device.read())

        while True:
            events = await loop.run_in_executor(None, _read_events)

            for event in events:
                if event.type == evdev.ecodes.EV_SYN:
                    continue

                type_name = evdev.ecodes.EV.get(event.type, f"UNKNOWN({event.type})")
                code_name = evdev.ecodes.bytype[event.type].get(event.code, str(event.code))

                print(f"{type_name:12} {code_name:20} value={event.value}")

    except FileNotFoundError:
        print(f"Error: Device not found: {device_path}")
        sys.exit(1)
    except PermissionError:
        print("Error: Permission denied. Try with sudo or add user to 'input' group.")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nStopped.")


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


def list_macros_cli() -> None:
    result = _session_request({"command": "list_macros"}) or {}
    if result.get("status") != "ok":
        print(f"Error: {result.get('message', 'Session unavailable')}")
        sys.exit(1)

    macros = result.get("macros", [])
    if not macros:
        print("No macros found")
        return

    for macro in macros:
        name = str(macro.get("name", ""))
        duration_ms = int(macro.get("duration_ms", 0) or 0)
        event_count = int(macro.get("event_count", 0) or 0)
        print(f"{name}\t{duration_ms}ms\t{event_count} events")


def play_macro_cli(name: str, speed: float = 1.0) -> None:
    result = _session_request({"command": "play_macro", "name": name, "speed": float(speed)}) or {}
    if result.get("status") != "ok":
        print(f"Error: {result.get('message', 'Session unavailable')}")
        sys.exit(1)
    print(f"Played macro: {name}")


def cancel_macro_cli() -> None:
    result = _session_request({"command": "cancel_macro_playback"}) or {}
    if result.get("status") != "ok":
        print(f"Error: {result.get('message', 'Session unavailable')}")
        sys.exit(1)
    cancelled = bool(result.get("cancelled", True))
    if cancelled:
        print("Cancelled running macro playback")
    else:
        print("No macro playback was running")


def set_diagnostics_cli(enabled: bool, interval: float = 5.0) -> None:
    result = (
        _session_request(
            {"command": "set_diagnostics", "enabled": bool(enabled), "interval": float(interval)}
        )
        or {}
    )
    if result.get("status") != "ok":
        print(f"Error: {result.get('message', 'Session unavailable')}")
        sys.exit(1)

    data = result.get("data") or {}
    state = "enabled" if data.get("enabled", enabled) else "disabled"
    print(f"Diagnostics {state} (interval={float(data.get('interval', interval)):.2f}s)")


def _profile_kind(profile: JsonObject) -> str:
    if bool(profile.get("is_permanent", False)):
        return "permanent"
    if int(profile.get("window_rule_count", 0) or 0) > 0:
        return "conditional"
    return "standard"


def list_profiles_cli() -> None:
    result = _session_request({"command": "list_profiles"}) or {}
    if result.get("status") != "ok":
        print(f"Error: {result.get('message', 'Session unavailable')}")
        sys.exit(1)

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

def set_profile_state_cli(command: str, profile_name: str) -> None:
    result = (
        _session_request(
            {
                "command": command,
                "profile_name": profile_name,
            }
        )
        or {}
    )

    if result.get("status") != "ok":
        print(f"Error: {result.get('message', 'Session unavailable')}")
        sys.exit(1)

    enabled = bool(result.get("enabled", False))
    active_profiles = ", ".join(str(name) for name in result.get("active_profiles", []))
    state = "enabled" if enabled else "disabled"
    print(f"{profile_name} is now {state}")
    print(f"Active profiles: {active_profiles or 'passthrough'}")
