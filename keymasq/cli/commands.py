import asyncio
import json
import socket
import sys
from typing import cast

from keymasq.common.macro_compile import (
    DEFAULT_TYPE_MACRO_DOWN_MS,
    DEFAULT_TYPE_MACRO_PAUSE_MS,
)
from keymasq.common.model.actions import parse_mpris_command
from keymasq.common.paths import SESSION_SOCKET_PATH
from keymasq.common.types import JsonObject

DIAGNOSTICS_CATEGORIES = ("mainline", "combo", "macro", "internal")


def _session_unavailable() -> JsonObject:
    return {"status": "error", "message": "Session unavailable"}


def _session_request(payload: JsonObject, timeout: float = 5.0) -> JsonObject | None:
    if not SESSION_SOCKET_PATH.exists():
        return None

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(SESSION_SOCKET_PATH))
            sock.sendall((json.dumps(payload) + "\n").encode())

            buffer = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buffer += chunk
                if b"\n" in buffer:
                    break

        if not buffer:
            return None
        line = buffer.split(b"\n", 1)[0]
        decoded = json.loads(line.decode())
        if isinstance(decoded, dict):
            return cast(JsonObject, decoded)
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
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


def _cli_mpris_command(command: str) -> str:
    normalized = parse_mpris_command(command)
    if normalized is None:
        print(f"Error: unknown MPRIS command: {command}")
        sys.exit(1)
    return normalized


def _mpris_payload(result: JsonObject) -> JsonObject:
    raw_mpris = result.get("mpris")
    return cast(JsonObject, raw_mpris) if isinstance(raw_mpris, dict) else {}


def _capability_text(value: object) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "unknown"


def _mpris_service_label(service: object) -> str:
    raw = str(service or "").strip()
    prefix = "org.mpris.MediaPlayer2."
    if raw.startswith(prefix):
        raw = raw.removeprefix(prefix)
    raw = raw.split(".instance_", 1)[0].split(".", 1)[0]
    raw = raw.strip("_-. ")
    if not raw:
        return "Unknown"
    lower = raw.lower()
    special_labels = {
        "mpv": "mpv",
        "vlc": "VLC",
    }
    if lower in special_labels:
        return special_labels[lower]
    return raw.replace("_", " ").replace("-", " ").title()


def _mpris_track_text(player: JsonObject) -> str:
    raw_track = player.get("track")
    track = cast(JsonObject, raw_track) if isinstance(raw_track, dict) else {}
    title = str(track.get("title") or "").strip()
    album = str(track.get("album") or "").strip()
    raw_artists = track.get("artists")
    artists = (
        [str(artist).strip() for artist in cast(list[object], raw_artists)]
        if isinstance(raw_artists, list)
        else []
    )
    artists = [artist for artist in artists if artist]
    if title and artists:
        text = f"{', '.join(artists)} - {title}"
    elif title:
        text = title
    elif artists:
        text = ", ".join(artists)
    else:
        return "unknown"
    return f"{text} ({album})" if album else text


def _ordered_mpris_players(players: list[object], order: object) -> list[JsonObject]:
    player_objects = [cast(JsonObject, player) for player in players if isinstance(player, dict)]
    by_owner = {str(player.get("owner") or ""): player for player in player_objects}
    ordered: list[JsonObject] = []
    seen: set[str] = set()
    if isinstance(order, list):
        for raw_owner in order:
            owner = str(raw_owner or "")
            player = by_owner.get(owner)
            if player is not None:
                ordered.append(player)
                seen.add(owner)
    ordered.extend(
        sorted(
            (player for player in player_objects if str(player.get("owner") or "") not in seen),
            key=lambda player: _mpris_service_label(player.get("service")),
        )
    )
    return ordered


def _mpris_player_supports(player: JsonObject, capability: str) -> bool:
    return player.get(capability) is not False


def _latest_mpris_player_for(
    mpris: JsonObject,
    players: list[JsonObject],
    *,
    capability: str,
    prefer_started: bool = False,
    require_not_playing: bool = False,
) -> JsonObject | None:
    by_owner = {str(player.get("owner") or ""): player for player in players}
    orders = [mpris.get("player_order")]
    if prefer_started:
        orders.insert(0, mpris.get("started_order"))

    seen: set[str] = set()
    for order in orders:
        if not isinstance(order, list):
            continue
        for raw_owner in reversed(order):
            owner = str(raw_owner or "")
            if owner in seen:
                continue
            seen.add(owner)
            player = by_owner.get(owner)
            if player is None:
                continue
            if require_not_playing and bool(player.get("playing")):
                continue
            if not _mpris_player_supports(player, capability):
                continue
            return player
    return None


def _mpris_player_label(player: JsonObject | None, labels_by_owner: dict[str, str]) -> str:
    if player is None:
        return "none"
    owner = str(player.get("owner") or "")
    return labels_by_owner.get(owner) or _mpris_service_label(player.get("service"))


def _print_mpris_targets(
    mpris: JsonObject,
    players: list[JsonObject],
    labels_by_owner: dict[str, str],
) -> None:
    if not players:
        return

    playing_labels = [
        _mpris_player_label(player, labels_by_owner)
        for player in players
        if bool(player.get("playing"))
    ]
    play_target = _latest_mpris_player_for(
        mpris,
        players,
        capability="can_play",
        prefer_started=True,
    )
    play_pause_target = _latest_mpris_player_for(
        mpris,
        players,
        capability="can_play",
        prefer_started=True,
        require_not_playing=True,
    )
    next_target = _latest_mpris_player_for(mpris, players, capability="can_go_next")
    previous_target = _latest_mpris_player_for(mpris, players, capability="can_go_previous")

    print("targets:")
    print(f"  play: {_mpris_player_label(play_target, labels_by_owner)}")
    if playing_labels:
        print(f"  play-pause: pause {', '.join(playing_labels)}")
    else:
        print(f"  play-pause: play {_mpris_player_label(play_pause_target, labels_by_owner)}")
    print(f"  next: {_mpris_player_label(next_target, labels_by_owner)}")
    print(f"  previous: {_mpris_player_label(previous_target, labels_by_owner)}")


def _print_mpris_status(mpris: JsonObject) -> None:
    if not bool(mpris.get("started")):
        print("MPRIS: not started")

    players_raw = mpris.get("players")
    players = cast(list[object], players_raw) if isinstance(players_raw, list) else []
    ordered_players = _ordered_mpris_players(players, mpris.get("player_order"))
    labels_by_owner: dict[str, str] = {}

    if not ordered_players:
        print("players: none")
    else:
        print("players:")
        for index, player in enumerate(ordered_players, start=1):
            label = _mpris_service_label(player.get("service"))
            owner = str(player.get("owner") or "")
            if owner:
                labels_by_owner[owner] = f"{index}. {label}"
            playback = str(player.get("playback_status") or "Stopped")
            active = "yes" if bool(player.get("playing")) else "no"
            print(f"  {index}. {label}: {playback}, active={active}")
            track = _mpris_track_text(player)
            if track != "unknown" or bool(player.get("playing")):
                print(f"    current: {track}")
            print(
                "    can: "
                f"play={_capability_text(player.get('can_play'))}, "
                f"next={_capability_text(player.get('can_go_next'))}, "
                f"previous={_capability_text(player.get('can_go_previous'))}"
            )

    _print_mpris_targets(mpris, ordered_players, labels_by_owner)


def _device_label(hardware_id: str, device: JsonObject) -> str:
    device_name = str(device.get("device_name") or "").strip()
    if not device_name or device_name == hardware_id:
        return hardware_id
    return f"{device_name} ({hardware_id})"


def status_cli(*, json_output: bool = False) -> None:
    result = _request_or_error({"command": "get_status"})
    if _handled_json_or_error(result, json_output):
        return

    keymasqd_state = _bool_status(result.get("keymasqd_connected"), "connected", "disconnected")
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

    macro_recording_enabled = bool(result.get("macro_recording_enabled", False))
    if macro_recording_enabled:
        source = str(result.get("macro_recording_source") or "unknown")
        macro_recording_state = f"enabled ({source})"
    else:
        macro_recording_state = "disabled"
    print(f"macro recording: {macro_recording_state}")

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
    print(f"capture unlock: {unlock_state}")

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


def mpris_cli(command: str, *, json_output: bool = False) -> None:
    normalized = _cli_mpris_command(command)
    result = _request_or_error({"command": "mpris", "mpris_command": normalized})
    if _handled_json_or_error(result, json_output):
        return
    print(f"MPRIS: {normalized}")


def mpris_status_cli(*, json_output: bool = False) -> None:
    result = _request_or_error({"command": "mpris", "mpris_command": "status"})
    if json_output:
        mpris = _mpris_payload(result)
        payload = {
            "status": result.get("status", "error"),
            "mpris": mpris,
        }
        if result.get("status") != "ok":
            payload["message"] = _message(result, "Session unavailable")
        _print_json(payload)
        if result.get("status") != "ok":
            sys.exit(1)
        return

    if result.get("status") != "ok":
        print(f"Error: {_message(result, 'Session unavailable')}")
        sys.exit(1)

    _print_mpris_status(_mpris_payload(result))


def _playback_request(payload: JsonObject, wait: bool) -> JsonObject:
    if wait:
        from keymasq.cli.playback import wait_for_playback

        return asyncio.run(wait_for_playback(payload))
    return _request_or_error(payload)


def play_macro_cli(
    name: str, speed: float = 1.0, *, json_output: bool = False, wait: bool = False
) -> None:
    result = _playback_request({"command": "play_macro", "name": name, "speed": float(speed)}, wait)
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
    down_ms: int = DEFAULT_TYPE_MACRO_DOWN_MS,
    pause_ms: int = DEFAULT_TYPE_MACRO_PAUSE_MS,
    speed: float = 1.0,
    use_unicode_input: bool = True,
    print_json: bool = False,
    wait: bool = False,
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
        _print_json(macro_definition_from_events(events))
        return

    result = _playback_request(
        {
            "command": "type_text",
            "text": text,
            "down_ms": max(0, int(down_ms)),
            "pause_ms": max(0, int(pause_ms)),
            "use_unicode_input": bool(use_unicode_input),
            "speed": float(speed),
        },
        wait,
    )
    if _handled_json_or_error(result, json_output):
        return
    action = "Completed" if wait else "Queued"
    print(f"{action} type macro: {len(text)} chars")


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
    events = _macro_events_from_json(raw_events)
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


def _macro_events_from_json(raw_events: list[object]) -> list[JsonObject]:
    events: list[JsonObject] = []
    for index, event in enumerate(raw_events):
        if not isinstance(event, dict):
            raise ValueError(f"macro JSON events[{index}] must be an object")
        events.append(cast(JsonObject, event))
    return events


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
        if not devices:
            return
    else:
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


def set_profile_state_cli(command: str, profile_name: str, *, json_output: bool = False) -> None:
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
