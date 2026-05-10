import json
import socket
import sys
from typing import Any, cast

from keymasq.common.paths import SESSION_SOCKET_PATH

JsonObject = dict[str, Any]
IntLike = int | float | str | bytes
DIAGNOSTICS_CATEGORIES = ("mainline", "combo", "internal")


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
        duration_ms = int(macro.get("duration_us", 0) or 0) // 1000
        event_count = int(macro.get("event_count", 0) or 0)
        print(f"{name}\t{duration_ms}ms\t{event_count} events")


def play_macro_cli(name: str, speed: float = 1.0, *, json_output: bool = False) -> None:
    result = _request_or_error({"command": "play_macro", "name": name, "speed": float(speed)})
    if _handled_json_or_error(result, json_output):
        return
    print(f"Played macro: {name}")


def create_macro_cli(
    name: str,
    json_parts: list[str],
    *,
    force: bool = False,
    json_output: bool = False,
) -> None:
    try:
        macro = _macro_definition_from_json_input(name, json_parts)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    command = "update_macro" if force else "create_macro"
    request: JsonObject = {"command": command, "macro": macro}
    if force:
        request["name"] = name

    result = _request_or_error(request)
    if _handled_json_or_error(result, json_output):
        return
    action = "Updated" if force else "Created"
    print(f"{action} macro: {name}")


def delete_macro_cli(name: str, *, json_output: bool = False) -> None:
    result = _request_or_error({"command": "delete_macro", "name": name})
    if _handled_json_or_error(result, json_output):
        return
    print(f"Deleted macro: {name}")


def type_cli(
    text_parts: list[str],
    *,
    down_ms: int = 10,
    pause_ms: int = 20,
    speed: float = 1.0,
    use_unicode_input: bool = True,
    print_json: bool = False,
    json_output: bool = False,
) -> None:
    text = " ".join(text_parts) if text_parts else _read_stdin_or_exit("No text provided")
    if print_json:
        try:
            from keymasq.common.macro_compile import (
                build_type_macro_events,
                macro_definition_from_events,
            )

            events = build_type_macro_events(
                text,
                max(0, int(down_ms)),
                max(0, int(pause_ms)),
                use_unicode_input=use_unicode_input,
            )
        except ValueError as exc:
            print(f"Error: {exc}")
            sys.exit(1)
        _print_json(macro_definition_from_events(events, device_types=["keyboard"]))
        return

    result = _request_or_error(
        {
            "command": "type_text",
            "text": text,
            "down_ms": max(0, int(down_ms)),
            "pause_ms": max(0, int(pause_ms)),
            "use_unicode_input": bool(use_unicode_input),
            "speed": float(speed),
        }
    )
    if _handled_json_or_error(result, json_output):
        return
    print(f"Played type macro: {len(text)} chars")


def play_adhoc_cli(
    tokens: list[str],
    *,
    input_json: bool = False,
    speed: float = 1.0,
    print_json: bool = False,
    json_output: bool = False,
) -> None:
    try:
        if input_json:
            from keymasq.common.macro_compile import build_macro_payload, parse_macro_json

            macro_data = parse_macro_json(_json_input_from_args_or_stdin(tokens))
            raw_events = macro_data.get("events", [])
            if not isinstance(raw_events, list):
                raise ValueError("macro JSON events must be a list")
            events = [cast(JsonObject, event) for event in raw_events if isinstance(event, dict)]
            payload = build_macro_payload(
                events,
                name=str(macro_data.get("name", "") or ""),
                speed=float(speed),
                loop_mode=str(macro_data.get("loop_mode", "none") or "none"),
                loop_count=int(cast(IntLike, macro_data.get("loop_count", 1) or 1)),
                loop_stop_behavior=str(
                    macro_data.get("loop_stop_behavior", "finish_run") or "finish_run"
                ),
                move_to_start=bool(macro_data.get("move_to_start", False)),
                start_x=int(cast(IntLike, macro_data.get("start_x", 0) or 0)),
                start_y=int(cast(IntLike, macro_data.get("start_y", 0) or 0)),
                block_mouse_movement=bool(macro_data.get("block_mouse_movement", False)),
            )
        else:
            event_tokens = tokens or _read_stdin_tokens_or_exit("No macro events provided")
            if print_json:
                from keymasq.common.macro_compile import (
                    build_compact_macro_events,
                    build_macro_payload,
                )

                events = build_compact_macro_events(event_tokens)
                payload = build_macro_payload(events, speed=float(speed))
            else:
                result = _request_or_error(
                    {
                        "command": "play_compact_macro",
                        "tokens": event_tokens,
                        "speed": float(speed),
                    }
                )
                if _handled_json_or_error(result, json_output):
                    return
                event_count = int(result.get("event_count", 0) or 0)
                print(f"Played ad-hoc macro: {event_count} events")
                return
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    if print_json:
        from keymasq.common.macro_compile import macro_definition_from_events

        _print_json(macro_definition_from_events(cast(list[JsonObject], payload["macro_events"])))
        return

    result = _request_or_error({"command": "play_macro_payload", **payload})
    if _handled_json_or_error(result, json_output):
        return
    event_count = len(cast(list[JsonObject], payload["macro_events"]))
    print(f"Played ad-hoc macro: {event_count} events")


def cancel_macro_cli(*, json_output: bool = False) -> None:
    result = _request_or_error({"command": "cancel_macro_playback"})
    if _handled_json_or_error(result, json_output):
        return
    cancelled = bool(result.get("cancelled", True))
    if cancelled:
        print("Cancelled running macro playback")
    else:
        print("No macro playback was running")


def _read_stdin_or_exit(message: str) -> str:
    if sys.stdin.isatty():
        print(f"Error: {message}")
        sys.exit(1)
    return sys.stdin.read()


def _read_stdin_tokens_or_exit(message: str) -> list[str]:
    text = _read_stdin_or_exit(message)
    tokens = text.split()
    if not tokens:
        print(f"Error: {message}")
        sys.exit(1)
    return tokens


def _json_input_from_args_or_stdin(parts: list[str]) -> str:
    if parts:
        return " ".join(parts)
    return _read_stdin_or_exit("No macro JSON provided")


def _macro_definition_from_json_input(name: str, json_parts: list[str]) -> JsonObject:
    from keymasq.common.macro_compile import macro_definition_from_events, parse_macro_json

    macro_data = parse_macro_json(_json_input_from_args_or_stdin(json_parts))
    raw_events = macro_data.get("events", [])
    if not isinstance(raw_events, list):
        raise ValueError("macro JSON events must be a list")
    events = [cast(JsonObject, event) for event in raw_events if isinstance(event, dict)]
    macro = macro_definition_from_events(
        events,
        name=name,
        device_types=_macro_device_types(macro_data),
    )
    for key in (
        "created_at",
        "loop_mode",
        "loop_count",
        "loop_stop_behavior",
        "move_to_start",
        "start_x",
        "start_y",
        "block_mouse_movement",
    ):
        if key in macro_data:
            macro[key] = macro_data[key]
    return macro


def _macro_device_types(macro_data: JsonObject) -> list[str] | None:
    raw_device_types = macro_data.get("device_types")
    if not isinstance(raw_device_types, list):
        return None
    return [str(device_type) for device_type in raw_device_types if str(device_type)]


def set_diagnostics_cli(
    enabled: bool,
    interval: float = 5.0,
    *,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    json_output: bool = False,
) -> None:
    categories = _diagnostics_categories(include, exclude)
    result = _request_or_error(
        {
            "command": "set_diagnostics",
            "enabled": bool(enabled),
            "interval": float(interval),
            "categories": categories,
        }
    )
    if _handled_json_or_error(result, json_output):
        return

    raw_data = result.get("data")
    data = cast(JsonObject, raw_data) if isinstance(raw_data, dict) else {}
    state = "enabled" if bool(data.get("enabled", enabled)) else "disabled"
    raw_categories = data.get("categories", categories)
    shown_categories = (
        ", ".join(str(category) for category in raw_categories)
        if isinstance(raw_categories, list)
        else ", ".join(categories)
    )
    print(
        f"Diagnostics {state} "
        f"(interval={float(data.get('interval', interval)):.2f}s, categories={shown_categories})"
    )


def _diagnostics_categories(
    include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> list[str]:
    selected = {"mainline"}
    include_set = {str(category or "").lower() for category in include or []}
    if "all" in include_set:
        selected = set(DIAGNOSTICS_CATEGORIES)
    else:
        selected.update(category for category in include_set if category in DIAGNOSTICS_CATEGORIES)
    selected.difference_update(
        str(category or "").lower()
        for category in exclude or []
        if str(category or "").lower() in DIAGNOSTICS_CATEGORIES
    )
    if not selected:
        selected = {"mainline"}
    return [category for category in DIAGNOSTICS_CATEGORIES if category in selected]


def _profile_kind(profile: JsonObject) -> str:
    if int(profile.get("window_rule_count", 0) or 0) > 0:
        return "conditional"
    if bool(profile.get("is_permanent", False)):
        return "permanent"
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
