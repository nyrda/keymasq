import asyncio
import logging
import os
import shutil
from collections.abc import Sequence
from pathlib import Path

from keymasq.common.devices import hardware_model_id_key
from keymasq.common.paths import RUN_DIR
from keymasq.keymasqd.permission_hints import (
    capability_permission_message,
    is_capability_permission_failure,
)

# `udevadm trigger` writes root-owned sysfs uevent files, so hide/restore
# depends on the ambient CAP_DAC_OVERRIDE granted by keymasqd.service; without
# it every trigger fails with a sysfs permission error.

log = logging.getLogger("keymasqd.source_hiding")

HIDDEN_DIR = RUN_DIR / "hidden"
HIDDEN_HARDWARE_DIR = RUN_DIR / "hidden-hardware"
SYS_CLASS_INPUT = Path("/sys/class/input")
UDEVADM_FALLBACK_PATHS = (
    Path("/usr/bin/udevadm"),
    Path("/run/current-system/sw/bin/udevadm"),
)
TRIGGER_TIMEOUT_S = 2.0
RECONCILE_TIMEOUT_S = 5.0
HOST_TOOL_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"
HOST_TOOL_ENV_PASSTHROUGH = (
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LC_MESSAGES",
    "SYSTEMD_COLORS",
    "SYSTEMD_LOG_LEVEL",
    "SYSTEMD_LOG_TARGET",
    "TERM",
)


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
        for name in written_names:
            await _trigger_input_node(name, timeout_s=TRIGGER_TIMEOUT_S)
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
    for name in kernel_names:
        await _trigger_input_node(name, timeout_s=TRIGGER_TIMEOUT_S)


async def _restore_source_after_hide_cancel(names: Sequence[str]) -> None:
    kernel_names = _validated_kernel_names(names)
    if not kernel_names:
        return

    await asyncio.to_thread(_remove_flags, kernel_names)
    for name in kernel_names:
        try:
            await _trigger_input_node(name, timeout_s=TRIGGER_TIMEOUT_S)
        except asyncio.CancelledError:
            log.warning("Interrupted udev trigger while restoring hidden source %s", name)
            continue
        except Exception:
            log.exception("Unexpected failure triggering source restore for %s", name)


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
        await _run_udevadm(
            [
                "trigger",
                "--subsystem-match=input",
                "--action=change",
                "--settle",
            ],
            timeout_s=RECONCILE_TIMEOUT_S,
            context=f"hardware {flag_name} restore",
        )
    return removed


async def reconcile_all() -> None:
    await asyncio.to_thread(_clear_flag_dirs)
    await _run_udevadm(
        [
            "trigger",
            "--subsystem-match=input",
            "--action=change",
            "--settle",
        ],
        timeout_s=RECONCILE_TIMEOUT_S,
        context="input reconcile",
    )


def resolve_udevadm_path() -> str | None:
    path = shutil.which("udevadm")
    if path:
        return path

    for candidate in UDEVADM_FALLBACK_PATHS:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def host_tool_environment() -> dict[str, str]:
    env = {
        key: value
        for key in HOST_TOOL_ENV_PASSTHROUGH
        if (value := os.environ.get(key))
    }
    env["PATH"] = HOST_TOOL_PATH
    return env


def hardware_flag_name(hardware_id: object) -> str | None:
    flag_name = hardware_model_id_key(hardware_id)
    if flag_name is None:
        log.warning("Ignoring invalid hidden source hardware id: %s", hardware_id)
    return flag_name


def _validated_kernel_names(names: Sequence[str]) -> list[str]:
    validated: list[str] = []
    for raw_name in names:
        name = str(raw_name or "").strip()
        if not name or Path(name).name != name:
            log.warning("Ignoring invalid hidden source kernel name: %s", raw_name)
            continue
        if not (name.startswith("event") or name.startswith("js")):
            log.warning("Ignoring unexpected hidden source kernel name: %s", name)
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


async def _trigger_input_node(name: str, *, timeout_s: float) -> bool:
    return await _run_udevadm(
        [
            "trigger",
            "--subsystem-match=input",
            "--action=change",
            f"--sysname-match={name}",
            "--settle",
        ],
        timeout_s=timeout_s,
        context=name,
    )


async def _run_udevadm(args: Sequence[str], *, timeout_s: float, context: str) -> bool:
    udevadm_path = resolve_udevadm_path()
    if udevadm_path is None:
        log.warning("Cannot trigger udev for %s: udevadm not found", context)
        return False

    process: asyncio.subprocess.Process | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            udevadm_path,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=host_tool_environment(),
        )
        _stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=timeout_s,
        )
    except TimeoutError:
        log.warning("Timed out triggering udev for %s after %.1fs", context, timeout_s)
        if process is not None:
            await _terminate_process(process)
        return False
    except asyncio.CancelledError:
        if process is not None:
            await _terminate_process(process)
        raise
    except OSError as exc:
        log.warning("Failed to trigger udev for %s: %s", context, exc)
        return False
    except Exception:
        log.exception("Unexpected failure triggering udev for %s", context)
        return False

    if process.returncode != 0:
        stderr_text = stderr.decode(errors="replace").strip() if stderr else ""
        message = (
            f"udevadm trigger failed for {context}: "
            f"returncode={process.returncode} stderr={stderr_text}"
        )
        if is_capability_permission_failure(stderr_text):
            message = capability_permission_message(message)
        log.warning("%s", message)
        return False
    return True


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    try:
        process.terminate()
        await asyncio.wait_for(process.wait(), timeout=1.0)
        return
    except TimeoutError:
        pass
    except OSError as exc:
        log.debug("Failed to terminate udevadm process: %s", exc)
        return
    except Exception:
        log.exception("Unexpected failure terminating udevadm process")
        return

    try:
        process.kill()
        await asyncio.wait_for(process.wait(), timeout=1.0)
    except TimeoutError:
        log.warning("udevadm process did not exit after kill")
    except OSError as exc:
        log.debug("Failed to kill udevadm process: %s", exc)
    except Exception:
        log.exception("Unexpected failure killing udevadm process")
