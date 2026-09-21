"""Restore independent access policy without guessing lost hardware undo data."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn, cast

from keymasq.common.types import JsonObject
from keymasq.masking.backend import finish_io
from keymasq.masking.paths import validate_identity

if TYPE_CHECKING:
    from keymasq.masking.backend import LinuxMaskBackend


def read_object(path: Path) -> JsonObject:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain an object")
    return cast(JsonObject, value)


def read_journal(path: Path) -> JsonObject:
    data = read_object(path)
    if (
        not isinstance(data.get("id"), str)
        or not isinstance(data.get("generation"), str)
        or not isinstance(data.get("bindings"), dict)
        or not isinstance(data.get("nodes"), dict)
        or data.get("mode", "") not in ("", "Y", "N")
        or not isinstance(data.get("selector", {}), dict)
    ):
        raise ValueError("Invalid hardware recovery journal fields")
    bindings = cast(JsonObject, data["bindings"])
    validate_identity(str(data["id"]))
    validate_baseline(data)
    if any(not isinstance(driver, str) for driver in bindings.values()):
        raise ValueError("Invalid recorded driver binding")
    port = data.get("usb_port")
    if port is not None and (
        not isinstance(port, dict)
        or not isinstance(port.get("path"), str)
        or not isinstance(port.get("disabled"), bool)
        or any(
            not isinstance(port.get(key), int) or isinstance(port.get(key), bool)
            for key in ("inode", "filesystem")
        )
    ):
        raise ValueError("Invalid recorded USB port")
    return data


def validated_selector(backend: LinuxMaskBackend, value: object) -> JsonObject:
    if not isinstance(value, dict) or any(
        not isinstance(value.get(key), str)
        for key in ("id", "vendor", "product", "transport", "path", "kernel_name")
    ):
        raise ValueError("Invalid independent attachment selector")
    selector = cast(JsonObject, value)
    identity = str(selector["id"])
    validate_identity(identity)
    if backend.reservation_id and identity != backend.reservation_id:
        raise ValueError("Attachment selector belongs to another reservation")
    return backend.inventory.selector(backend.inventory.from_selector(selector))


def validate_baseline(record: JsonObject) -> None:
    nodes = record.get("nodes")
    if not isinstance(nodes, dict):
        raise ValueError("Invalid independent permission baseline")
    for baseline in nodes.values():
        if (
            not isinstance(baseline, dict)
            or not isinstance(baseline.get("acl"), str)
            or not baseline["acl"].strip()
            or any(
                not isinstance(baseline.get(key), int)
                or isinstance(baseline.get(key), bool)
                or not 0 <= baseline[key] < 2**32 - 1
                for key in ("uid", "gid")
            )
        ):
            raise ValueError("Invalid independent node permissions")


def read_prearmed_selector(backend: LinuxMaskBackend) -> JsonObject | None:
    """Validate independent records before synthesizing a missing journal."""
    selectors: list[JsonObject] = []
    for path in (backend.armed_record, backend.permissions):
        try:
            record = read_object(path)
        except FileNotFoundError:
            continue
        selectors.append(
            validated_selector(
                backend, record.get("selector") if path == backend.permissions else record
            )
        )
        if path == backend.permissions:
            validate_baseline(record)
    if not selectors:
        # A saved preference alone does not mean there is runtime state to undo.
        return None
    try:
        saved = read_object(backend.state_dir / "selector.json")
    except FileNotFoundError:
        pass
    else:
        selectors.append(validated_selector(backend, saved))
    if any(selector != selectors[0] for selector in selectors):
        raise ValueError("Conflicting independent attachment selectors")
    return selectors[0]


async def recover_damaged_journal(
    backend: LinuxMaskBackend, cause: OSError | ValueError
) -> NoReturn:
    from keymasq.masking.permissions import restore

    problems: list[str] = []
    selectors: list[JsonObject] = []
    baseline: JsonObject | None = None
    # These root-owned records were saved separately, before access changes.
    # Keep them and the original journal in place across every retry.
    for path in (backend.permissions, backend.armed_record, backend.state_dir / "selector.json"):
        try:
            record = await finish_io(read_object, path)
            selector = await finish_io(
                validated_selector,
                backend,
                record.get("selector") if path == backend.permissions else record,
            )
            selectors.append(selector)
            if path == backend.permissions:
                validate_baseline(record)
                baseline = record
        except FileNotFoundError:
            continue
        except (OSError, ValueError, KeyError) as exc:
            problems.append(f"{path.name}: {exc}")

    async def attempt(label: str, operation: Callable[[], Awaitable[object]]) -> None:
        try:
            await operation()
        except (OSError, ValueError, KeyError) as exc:
            problems.append(f"{label}: {exc}")

    # Removing this reservation's fixed rule filenames requires no journal
    # data and must not depend on rediscovering its device or permission file.
    await attempt("removing rules", lambda: backend.remove_rules(preserve_record=True))
    if not selectors or any(selector != selectors[0] for selector in selectors):
        problems.append("no consistent independent attachment identity; nodes were not changed")
    else:
        try:
            attachment = next(
                (
                    item
                    for item in await finish_io(backend.inventory.scan)
                    if backend.inventory.selector(item) == selectors[0]
                ),
                None,
            )
            if attachment is None:
                problems.append(
                    "recorded attachment is absent; replacement devices were not changed"
                )
            else:
                await finish_io(backend.inventory.validate, attachment)
                await attempt(
                    "removing hidden markers", lambda: backend.clear_hidden_markers(attachment)
                )
                if baseline is not None:
                    await attempt("restoring permissions", lambda: restore(backend, attachment, {}))
                else:
                    problems.append("no valid independent permission baseline")
                await attempt(
                    "applying current device policy", lambda: backend.trigger(attachment, "add")
                )
        except (OSError, ValueError) as exc:
            problems.append(f"attachment lookup: {exc}")
    detail = "; ".join(problems) if problems else "independent device access policy restored"
    journal_status = (
        f"Damaged recovery journal retained at {backend.journal}"
        if backend.journal.exists()
        else f"Recovery journal missing at {backend.journal}; independent records retained"
    )
    raise OSError(
        f"{journal_status}: {cause}. {detail}. "
        "USB port and original driver state cannot be verified; full recovery remains incomplete"
    ) from cause
