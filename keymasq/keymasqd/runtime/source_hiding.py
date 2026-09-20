import asyncio
import logging
from collections.abc import Sequence
from pathlib import Path

from keymasq.common.devices import hardware_model_id_key
from keymasq.common.paths import RUN_DIR
from keymasq.keymasqd.permission_hints import source_hiding_job_message
from keymasq.masking import client as hardware_jobs
from keymasq.masking.operations import INPUT_NODE_NAME

# Hiding flags live in the daemon's own runtime directory. Making udev act on
# them means writing root-owned sysfs uevent files, so every trigger runs as a
# bounded keymasq-hardware@ job through keymasq-record; keymasqd itself holds
# no capabilities (see keymasqd.service and docs/security.md).

log = logging.getLogger("keymasqd.source_hiding")

HIDDEN_DIR = RUN_DIR / "hidden"
HIDDEN_HARDWARE_DIR = RUN_DIR / "hidden-hardware"
SYS_CLASS_INPUT = Path("/sys/class/input")
# Each job pays for a systemd unit start plus helper start-up before udevadm
# settles, so these budgets are wider than a bare udevadm call would need and
# must stay above the helper's own udevadm timeouts in masking/operations.py.
TRIGGER_TIMEOUT_S = 15.0
RECONCILE_TIMEOUT_S = 30.0


def node_kernel_names(resolved_event_path: str) -> list[str]:
    event_name = Path(resolved_event_path).name
    if not event_name.startswith("event"):
        log.warning("Cannot hide non-event input source: %s", resolved_event_path)
        return []

    names = [event_name]
    device_dir = SYS_CLASS_INPUT / event_name / "device"
    try:
        for js_path in sorted(device_dir.glob("js*")):
            js_name = js_path.name
            if js_name.startswith("js") and js_name not in names:
                names.append(js_name)
    except OSError as exc:
        log.warning("Failed to discover js sibling for %s: %s", event_name, exc)

    return names


async def hide_source(resolved_event_path: str) -> list[str]:
    names = node_kernel_names(resolved_event_path)
    if not names:
        return []

    write_task = asyncio.create_task(asyncio.to_thread(_write_flags, names))
    written_names: list[str] = []
    try:
        written_names = await asyncio.shield(write_task)
        log.info("Hiding input source %s as %s", resolved_event_path, written_names)
        await _trigger_input_nodes(written_names, timeout_s=TRIGGER_TIMEOUT_S)
    except asyncio.CancelledError:
        if not written_names:
            written_names = await _written_names_after_hide_cancel(
                write_task,
                fallback_names=names,
            )
        await _restore_source_after_hide_cancel(written_names)
        raise
    return written_names


async def restore_source_by_kernel_names(names: Sequence[str]) -> None:
    kernel_names = _validated_kernel_names(names)
    if not kernel_names:
        return

    await asyncio.to_thread(_remove_flags, kernel_names)
    log.info("Restoring hidden input source %s", kernel_names)
    await _trigger_input_nodes(kernel_names, timeout_s=TRIGGER_TIMEOUT_S)


async def _restore_source_after_hide_cancel(names: Sequence[str]) -> None:
    kernel_names = _validated_kernel_names(names)
    if not kernel_names:
        return

    await asyncio.to_thread(_remove_flags, kernel_names)
    try:
        await _trigger_input_nodes(kernel_names, timeout_s=TRIGGER_TIMEOUT_S)
    except asyncio.CancelledError:
        log.warning("Interrupted udev trigger while restoring hidden sources %s", kernel_names)
    except Exception:
        log.exception("Unexpected failure triggering source restore for %s", kernel_names)


async def _written_names_after_hide_cancel(
    write_task: asyncio.Task[list[str]],
    *,
    fallback_names: Sequence[str],
) -> list[str]:
    try:
        return await asyncio.shield(write_task)
    except asyncio.CancelledError:
        log.warning("Interrupted while waiting for hidden source flag write cancellation")
        return list(fallback_names)
    except Exception:
        log.exception("Unexpected failure writing hidden source flags after cancellation")
        return []


async def enable_hardware_hotplug_hiding(hardware_id: str) -> bool:
    flag_name = hardware_flag_name(hardware_id)
    if flag_name is None:
        return False
    return await asyncio.to_thread(_write_hardware_flag, flag_name)


async def disable_hardware_hotplug_hiding(hardware_id: str) -> bool:
    flag_name = hardware_flag_name(hardware_id)
    if flag_name is None:
        return False
    removed = await asyncio.to_thread(_remove_hardware_flag, flag_name)
    if removed:
        await _trigger_input_subsystem(
            timeout_s=RECONCILE_TIMEOUT_S,
            context=f"hardware {flag_name} restore",
        )
    return removed


async def reconcile_all() -> None:
    await asyncio.to_thread(_clear_flag_dirs)
    log.info("Cleared hidden source flags; re-evaluating input udev rules")
    await _trigger_input_subsystem(timeout_s=RECONCILE_TIMEOUT_S, context="input reconcile")


def hardware_flag_name(hardware_id: object) -> str | None:
    flag_name = hardware_model_id_key(hardware_id)
    if flag_name is None:
        log.warning("Ignoring invalid hidden source hardware id: %s", hardware_id)
    return flag_name


def _validated_kernel_names(names: Sequence[str]) -> list[str]:
    validated: list[str] = []
    for raw_name in names:
        name = str(raw_name or "").strip()
        # The root job rejects a whole batch over one bad name, so apply its
        # exact contract here and drop only the offender.
        if not INPUT_NODE_NAME.fullmatch(name):
            log.warning("Ignoring invalid hidden source kernel name: %s", raw_name)
            continue
        if name not in validated:
            validated.append(name)
    return validated


def _write_flags(names: Sequence[str]) -> list[str]:
    try:
        HIDDEN_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("Failed to create hidden source directory %s: %s", HIDDEN_DIR, exc)
        return []

    written_names: list[str] = []
    for name in _validated_kernel_names(names):
        try:
            (HIDDEN_DIR / name).write_text("1\n", encoding="utf-8")
        except OSError as exc:
            log.warning("Failed to hide input source %s: %s", name, exc)
            continue
        written_names.append(name)
    return written_names


def _write_hardware_flag(flag_name: str) -> bool:
    try:
        HIDDEN_HARDWARE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning(
            "Failed to create hidden hardware directory %s: %s",
            HIDDEN_HARDWARE_DIR,
            exc,
        )
        return False

    try:
        (HIDDEN_HARDWARE_DIR / flag_name).write_text("1\n", encoding="utf-8")
    except OSError as exc:
        log.warning("Failed to enable hotplug hiding for %s: %s", flag_name, exc)
        return False
    return True


def _remove_flags(names: Sequence[str]) -> None:
    for name in _validated_kernel_names(names):
        try:
            (HIDDEN_DIR / name).unlink(missing_ok=True)
        except OSError as exc:
            log.warning("Failed to restore input source %s: %s", name, exc)


def _remove_hardware_flag(flag_name: str) -> bool:
    try:
        (HIDDEN_HARDWARE_DIR / flag_name).unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        log.warning("Failed to disable hotplug hiding for %s: %s", flag_name, exc)
        return False


def _clear_flag_dirs() -> None:
    _clear_flag_dir(HIDDEN_DIR, "hidden source")
    _clear_flag_dir(HIDDEN_HARDWARE_DIR, "hidden hardware")


def _clear_flag_dir(flag_dir: Path, label: str) -> None:
    try:
        flag_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("Failed to create %s directory %s: %s", label, flag_dir, exc)
        return

    try:
        entries = list(flag_dir.iterdir())
    except OSError as exc:
        log.warning("Failed to list %s flags in %s: %s", label, flag_dir, exc)
        return

    for entry in entries:
        try:
            if entry.is_dir() and not entry.is_symlink():
                log.warning("Ignoring unexpected %s directory %s", label, entry)
                continue
            entry.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("Failed to remove stale %s flag %s: %s", label, entry, exc)


async def _trigger_input_nodes(names: Sequence[str], *, timeout_s: float) -> bool:
    kernel_names = _validated_kernel_names(names)
    if not kernel_names:
        return True
    return await _request_trigger(
        {"names": kernel_names},
        timeout_s=timeout_s,
        context=", ".join(kernel_names),
    )


async def _trigger_input_subsystem(*, timeout_s: float, context: str) -> bool:
    return await _request_trigger({}, timeout_s=timeout_s, context=context)


async def _request_trigger(data: dict[str, object], *, timeout_s: float, context: str) -> bool:
    """Run one bounded root job that re-evaluates udev rules for the flagged nodes."""
    try:
        await hardware_jobs.request("trigger", "", timeout=timeout_s, **data)
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        log.warning("Timed out triggering udev for %s after %.1fs", context, timeout_s)
        return False
    except (OSError, ValueError) as exc:
        message = source_hiding_job_message(f"udev trigger job failed for {context}: {exc}")
        log.warning("%s", message)
        return False
    except Exception:
        log.exception("Unexpected failure triggering udev for %s", context)
        return False
    return True
