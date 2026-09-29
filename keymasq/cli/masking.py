"""Hardware masking commands: list, inspect, enable, confirm, and disable device masks."""

import select
import sys
import time
from typing import Literal, NoReturn, cast

from keymasq.cli.commands import _message, _print_json, _session_request
from keymasq.common.masking import (
    HARDWARE_GUI_TIMEOUT,
    MaskPhase,
    is_active,
    last_seen_text,
    mask_enabled,
    mask_status,
)
from keymasq.common.types import JsonObject

POLL_INTERVAL_S = 0.5
SHORT_ID_LENGTH = 12
MIN_ID_PREFIX_LENGTH = 4
SELECTOR_EXIT_CODE = 2
INTERRUPTED_EXIT_CODE = 130
DEVICE_FIELDS = (
    "id",
    "name",
    "vendor",
    "product",
    "transport",
    "connection",
    "supported",
    "unsupported_reason",
    "scope",
    "warning",
)
MASK_FIELDS = (
    "lifecycle",
    "persist",
    "has_saved_mask",
    "automatic",
    "reason",
    "remaining_seconds",
    "next_retry_seconds",
    "last_seen",
    "error",
    "error_code",
    "blocking_application",
)

Entry = tuple[JsonObject, JsonObject]


class MaskingError(Exception):
    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _fail(error: MaskingError, *, json_output: bool) -> NoReturn:
    if json_output:
        _print_json({"status": "error", "message": str(error)})
    else:
        print(f"Error: {error}")
    sys.exit(error.exit_code)


def _request(payload: JsonObject) -> JsonObject:
    result = _session_request(payload, timeout=HARDWARE_GUI_TIMEOUT)
    if result is None:
        raise MaskingError("Session unavailable")
    if result.get("status") != "ok":
        raise MaskingError(_message(result, "Hardware masking failed"))
    return result


def _inventory() -> JsonObject:
    return _request({"command": "hardware_inventory"})


def _entries(inventory: JsonObject) -> list[Entry]:
    raw_masks = inventory.get("masks")
    raw_devices = inventory.get("devices")
    masks = {
        str(item["id"]): cast(JsonObject, item)
        for item in (raw_masks if isinstance(raw_masks, list) else [])
        if isinstance(item, dict) and item.get("id")
    }
    return [
        (cast(JsonObject, device), masks.get(str(device["id"]), {}))
        for device in (raw_devices if isinstance(raw_devices, list) else [])
        if isinstance(device, dict) and device.get("id")
    ]


def _paused(inventory: JsonObject) -> bool:
    return bool(inventory.get("remapping_suspended"))


def _short_id(device: JsonObject) -> str:
    return str(device.get("id", ""))[:SHORT_ID_LENGTH]


def _name(device: JsonObject) -> str:
    return str(device.get("name") or "Input device")


def _selector_matches(device: JsonObject, selector: str) -> bool:
    identity = str(device.get("id", "")).lower()
    model = f"{device.get('vendor', '')}:{device.get('product', '')}".lower()
    return (
        (len(selector) >= MIN_ID_PREFIX_LENGTH and identity.startswith(selector))
        or selector == str(device.get("connection") or "").lower()
        or selector == model
        or selector == _name(device).strip().lower()
    )


def resolve_device(entries: list[Entry], selector: str) -> Entry:
    wanted = selector.strip().lower()
    if not wanted:
        raise MaskingError("Name a device", SELECTOR_EXIT_CODE)
    exact = [entry for entry in entries if str(entry[0].get("id", "")).lower() == wanted]
    matches = exact or [entry for entry in entries if _selector_matches(entry[0], wanted)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise MaskingError(
            f"No device matches {selector!r}. Run 'keymasq masking list' to see devices.",
            SELECTOR_EXIT_CODE,
        )
    candidates = "\n".join(
        f"  {_short_id(device)}  {_name(device)}  {device.get('connection', '')}"
        for device, _mask in matches
    )
    raise MaskingError(
        f"{selector!r} matches several devices. Use a device ID:\n{candidates}",
        SELECTOR_EXIT_CODE,
    )


def _find(identity: str) -> tuple[JsonObject, JsonObject, bool]:
    inventory = _inventory()
    for device, mask in _entries(inventory):
        if str(device.get("id")) == identity:
            return device, mask, _paused(inventory)
    raise MaskingError("The device is no longer available")


def device_json(device: JsonObject, mask: JsonObject, *, paused: bool) -> JsonObject:
    status, failure = mask_status(device, mask, paused=paused)
    return {
        **{field: device[field] for field in DEVICE_FIELDS if field in device},
        "summary": status,
        "mask": {
            "state": str(mask.get("state", MaskPhase.UNMASKED)),
            "enabled": mask_enabled(mask) and not paused,
            "failure": failure,
            **{field: mask[field] for field in MASK_FIELDS if field in mask},
        },
    }


def _print_device_result(
    device: JsonObject, mask: JsonObject, *, paused: bool, json_output: bool
) -> None:
    if json_output:
        _print_json({"status": "ok", "device": device_json(device, mask, paused=paused)})
        return
    status, failure = mask_status(device, mask, paused=paused)
    print(f"{_name(device)}: {status}")
    if failure:
        print(f"  {failure}")


def list_cli(*, json_output: bool = False) -> None:
    try:
        inventory = _inventory()
    except MaskingError as error:
        _fail(error, json_output=json_output)
    paused = _paused(inventory)
    entries = _entries(inventory)
    message = str(inventory.get("message") or "")
    if json_output:
        _print_json(
            {
                "status": "ok",
                "available": bool(inventory.get("available", True)),
                "remapping_suspended": paused,
                "message": message,
                "devices": [device_json(device, mask, paused=paused) for device, mask in entries],
            }
        )
        return

    if not entries:
        print("No maskable devices found")
    else:
        rows = [
            (
                _short_id(device),
                _name(device),
                str(device.get("transport") or ""),
                str(device.get("connection") or ""),
                *mask_status(device, mask, paused=paused),
            )
            for device, mask in entries
        ]
        headers = ("ID", "NAME", "TRANSPORT", "CONNECTION")
        widths = [max(len(row[index]) for row in [headers, *rows]) for index in range(4)]
        print(
            "  ".join(header.ljust(width) for header, width in zip(headers, widths, strict=True))
            + "  STATUS"
        )
        for *columns, status, failure in rows:
            print(
                "  ".join(value.ljust(width) for value, width in zip(columns, widths, strict=True))
                + f"  {status}"
            )
            if failure:
                print(f"  {failure}")
    if message:
        print(message)
    if paused:
        print("Remapping was stopped by recovery. Run 'keymasq masking resume' to enable it.")


def show_cli(selector: str, *, json_output: bool = False) -> None:
    try:
        inventory = _inventory()
        device, mask = resolve_device(_entries(inventory), selector)
    except MaskingError as error:
        _fail(error, json_output=json_output)
    paused = _paused(inventory)
    if json_output:
        _print_json({"status": "ok", "device": device_json(device, mask, paused=paused)})
        return
    status, failure = mask_status(device, mask, paused=paused)
    connected = bool(device.get("supported"))
    enabled = mask_enabled(mask) and not paused
    details = [
        ("id", str(device.get("id", ""))),
        ("status", status),
        ("problem", failure),
        ("transport", str(device.get("transport") or "")),
        ("connection", str(device.get("connection") or "")),
        ("vendor:product", f"{device.get('vendor', '')}:{device.get('product', '')}"),
        ("scope", str(device.get("scope") or device.get("unsupported_reason") or "")),
        ("warning", str(device.get("warning") or "") if connected and not enabled else ""),
        ("saved", "yes" if mask.get("has_saved_mask") and mask.get("persist") else "no"),
        ("error", str(mask.get("error") or "")),
        ("last seen", "" if connected else last_seen_text(mask).removeprefix("Last seen ")),
    ]
    print(_name(device))
    for label, value in details:
        if value:
            print(f"  {label}: {value}")


def _awaiting_confirmation(mask: JsonObject) -> bool:
    return mask.get("state") == MaskPhase.TRIAL and not mask.get("automatic")


def _settling(device: JsonObject, mask: JsonObject, *, paused: bool) -> bool:
    state = mask.get("state", MaskPhase.UNMASKED)
    if state in {MaskPhase.APPLYING, MaskPhase.ACQUIRING, MaskPhase.RESTORING}:
        return True
    # A saved mask that is on for a connected device is about to start.
    _status, failure = mask_status(device, mask, paused=paused)
    return (
        bool(device.get("supported"))
        and mask_enabled(mask)
        and not paused
        and not is_active(state)
        and not failure
    )


def _wait(identity: str, *, progress: bool) -> tuple[JsonObject, JsonObject, bool]:
    deadline = time.monotonic() + HARDWARE_GUI_TIMEOUT
    shown = ""
    while True:
        device, mask, paused = _find(identity)
        status, _failure = mask_status(device, mask, paused=paused)
        if progress and status != shown:
            print(f"{_name(device)}: {status}")
            shown = status
        if not _settling(device, mask, paused=paused):
            return device, mask, paused
        if time.monotonic() >= deadline:
            raise MaskingError("Timed out waiting for the device")
        time.sleep(POLL_INTERVAL_S)


def _keep(identity: str, mask: JsonObject) -> None:
    _request(
        {
            "command": "keep_hardware_mask",
            "id": identity,
            "token": mask.get("token"),
            "persist": True,
        }
    )


def _undo(identity: str, mask: JsonObject) -> None:
    _request(
        {
            "command": "restore_hardware",
            "id": identity,
            "token": mask.get("token"),
            "persist": False,
        }
    )


def _ask_to_keep(mask: JsonObject) -> bool:
    remaining = int(mask.get("remaining_seconds") or 0)
    print(
        f"Does your input still work? Type y and press Enter within {remaining}s to keep "
        "masking. Anything else restores access."
    )
    readable, _writable, _errors = select.select([sys.stdin], [], [], max(0, remaining - 1))
    return bool(readable) and sys.stdin.readline().strip().lower() in {"y", "yes"}


def _require_masked(device: JsonObject, mask: JsonObject, *, paused: bool) -> None:
    _status, failure = mask_status(device, mask, paused=paused)
    if failure or not mask_enabled(mask):
        raise MaskingError(failure or f"{_name(device)} was not masked")


def _undo_unconfirmed(identity: str) -> bool:
    # An interface refresh during the trial replaces the token, so read it again.
    _device, mask, _paused = _find(identity)
    if mask.get("automatic") or mask.get("state") not in {
        MaskPhase.APPLYING,
        MaskPhase.ACQUIRING,
        MaskPhase.TRIAL,
    }:
        return False
    _undo(identity, mask)
    return True


type Confirmation = Literal["ask", "later", "yes"]


def enable_cli(
    selector: str, *, confirmation: Confirmation = "ask", json_output: bool = False
) -> None:
    identity = ""
    try:
        inventory = _inventory()
        device, mask = resolve_device(_entries(inventory), selector)
        identity = str(device["id"])
        if not is_active(mask.get("state")):
            request: JsonObject = {"command": "mask_hardware", "id": identity, "persist": True}
            if device.get("supported"):
                request["generation"] = device.get("generation")
            _request(request)
        device, mask, paused = _wait(identity, progress=not json_output)
        if _awaiting_confirmation(mask):
            if confirmation == "yes":
                _keep(identity, mask)
                device, mask, paused = _wait(identity, progress=not json_output)
            elif not json_output:
                if confirmation == "later" or not sys.stdin.isatty():
                    print(
                        f"Run 'keymasq masking confirm {_short_id(device)}' within "
                        f"{int(mask.get('remaining_seconds') or 0)}s to keep masking."
                    )
                elif _ask_to_keep(mask):
                    device, mask, paused = _wait(identity, progress=False)
                    if _awaiting_confirmation(mask):
                        _keep(identity, mask)
                        device, mask, paused = _wait(identity, progress=True)
                else:
                    _undo_unconfirmed(identity)
                    _wait(identity, progress=True)
                    raise MaskingError("Masking was undone")
        _require_masked(device, mask, paused=paused)
        if json_output:
            _print_json({"status": "ok", "device": device_json(device, mask, paused=paused)})
    except KeyboardInterrupt:
        if identity:
            try:
                if _undo_unconfirmed(identity):
                    print("\nMasking was undone")
            except MaskingError as error:
                print(f"\nError: {error}")
        sys.exit(INTERRUPTED_EXIT_CODE)
    except MaskingError as error:
        _fail(error, json_output=json_output)


def confirm_cli(selector: str, *, json_output: bool = False) -> None:
    try:
        device, mask = resolve_device(_entries(_inventory()), selector)
        identity = str(device["id"])
        if not _awaiting_confirmation(mask):
            raise MaskingError(f"{_name(device)} is not waiting for confirmation")
        _keep(identity, mask)
        device, mask, paused = _wait(identity, progress=False)
        _require_masked(device, mask, paused=paused)
    except MaskingError as error:
        _fail(error, json_output=json_output)
    _print_device_result(device, mask, paused=paused, json_output=json_output)


def disable_cli(selector: str | None, *, wait: bool = True, json_output: bool = False) -> None:
    try:
        if selector is None:
            _request({"command": "restore_hardware", "persist": False})
            if json_output:
                _print_json({"status": "ok"})
            else:
                print("Masking turned off for all devices")
            return
        inventory = _inventory()
        device, mask = resolve_device(_entries(inventory), selector)
        paused = _paused(inventory)
        identity = str(device["id"])
        if mask_enabled(mask) or is_active(mask.get("state")):
            _undo(identity, mask)
            device, mask, paused = _wait(identity, progress=False) if wait else _find(identity)
        _status, failure = mask_status(device, mask, paused=paused)
        if mask.get("state") == MaskPhase.RECOVERY_FAILED:
            raise MaskingError(failure)
    except MaskingError as error:
        _fail(error, json_output=json_output)
    _print_device_result(device, mask, paused=paused, json_output=json_output)


def resume_cli(*, json_output: bool = False) -> None:
    try:
        _request({"command": "resume_hardware"})
    except MaskingError as error:
        _fail(error, json_output=json_output)
    if json_output:
        _print_json({"status": "ok"})
    else:
        print("Remapping resumed")
