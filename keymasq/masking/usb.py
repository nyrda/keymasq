"""Disconnect one USB leaf device, with a durable record for port recovery."""

from __future__ import annotations

import asyncio
import ctypes
import fcntl
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, cast

from keymasq.common.types import JsonObject
from keymasq.masking.backend import finish_io, run_host, save_json
from keymasq.masking.inventory import Attachment, read_attribute

if TYPE_CHECKING:
    from keymasq.masking.backend import LinuxMaskBackend

RECONNECT_TIMEOUT_S = 12.0


class _ControlTransfer(ctypes.Structure):
    _fields_ = [
        ("request_type", ctypes.c_uint8),
        ("request", ctypes.c_uint8),
        ("value", ctypes.c_uint16),
        ("index", ctypes.c_uint16),
        ("length", ctypes.c_uint16),
        ("timeout", ctypes.c_uint32),
        ("data", ctypes.c_void_p),
    ]


def check_hub_power(backend: LinuxMaskBackend, hub: Path) -> None:
    """Reject hubs whose port power switch also cuts power to their siblings."""
    bus, device = read_attribute(hub / "busnum"), read_attribute(hub / "devnum")
    if not bus.isdecimal() or not device.isdecimal():
        raise ValueError("Cannot identify the USB hub for a port reconnect")
    node = backend.inventory.dev_root / "bus/usb" / f"{int(bus):03d}/{int(device):03d}"
    descriptor = ctypes.create_string_buffer(12)
    descriptor_type = 0x2A if read_attribute(hub / "version").startswith("3.") else 0x29
    request = _ControlTransfer(
        0xA0, 6, descriptor_type << 8, 0, 12, 1000, ctypes.addressof(descriptor)
    )
    fd = os.open(node, os.O_RDWR | os.O_CLOEXEC)
    try:
        size = fcntl.ioctl(fd, 0xC0005500 | (ctypes.sizeof(request) << 16), bytearray(request))
    finally:
        os.close(fd)
    if size < 5 or descriptor.raw[1] != descriptor_type:
        raise OSError("The USB hub did not report its port power switching support")
    if int.from_bytes(descriptor.raw[3:5], "little") & 3 == 0:
        raise ValueError(
            "This hub switches power for multiple ports together; reconnect the device manually"
        )


def port_record(backend: LinuxMaskBackend, attachment: Attachment) -> JsonObject:
    live = backend.inventory.resolve(attachment.identity, attachment.generation)
    if live.transport != "usb" or read_attribute(live.syspath / "bDeviceClass") == "09":
        raise ValueError("Only individual USB devices can be reconnected")
    if any(read_attribute(item / "bInterfaceClass") == "09" for item in live.syspath.glob("*:*")):
        raise ValueError("Reconnecting a USB hub is not supported")
    port = (live.syspath / "port").resolve(strict=True)
    if (
        not port.is_relative_to(backend.inventory.sys_root / "devices")
        or port.parent.parent != live.syspath.parent
        or not re.fullmatch(r"(?:usb[0-9]+|[0-9]+-[0-9.]+)-port[0-9]+", port.name)
    ):
        raise ValueError("Cannot identify this device's individual USB port")
    number = live.kernel_name.rsplit(".", 1)[-1].split("-")[-1]
    if not port.name.endswith(f"-port{number}"):
        raise ValueError("USB port no longer belongs to the selected device")
    if read_attribute(port / "disable") != "0":
        raise ValueError("USB port is unavailable or already disabled")
    info = port.stat()
    return {"path": str(port), "inode": info.st_ino, "filesystem": info.st_dev, "disabled": True}


def enable_recorded_port(backend: LinuxMaskBackend, data: JsonObject) -> None:
    raw = data.get("usb_port")
    if not isinstance(raw, dict) or not raw.get("disabled"):
        return
    port = Path(str(raw["path"]))
    if not port.is_relative_to(backend.inventory.sys_root / "devices") or not re.fullmatch(
        r"(?:usb[0-9]+|[0-9]+-[0-9.]+)-port[0-9]+", port.name
    ):
        raise ValueError("Invalid recorded USB port")
    try:
        info = port.stat()
    except FileNotFoundError:
        return  # The parent hub itself was unplugged.
    if (info.st_dev, info.st_ino) != (raw.get("filesystem"), raw.get("inode")):
        return  # Never operate on a replacement hub at the same path.
    (port / "disable").write_text("0\n")
    raw["disabled"] = False
    save_json(backend.journal, data)


async def reconnect(
    backend: LinuxMaskBackend, attachment: Attachment, snapshot: JsonObject
) -> Attachment:
    await finish_io(check_hub_power, backend, attachment.syspath.parent)
    record = await finish_io(port_record, backend, attachment)
    snapshot.update({"usb_port": record, "usb_reconnect": True})
    await finish_io(save_json, backend.journal, snapshot)
    try:
        # Revalidate after journal I/O, immediately before the destructive write.
        def disable() -> None:
            current = port_record(backend, attachment)
            if current != record:
                raise ValueError("USB attachment changed before reconnect")
            (Path(str(record["path"])) / "disable").write_text("1\n")

        await finish_io(disable)
    finally:
        await finish_io(enable_recorded_port, backend, snapshot)

    deadline = asyncio.get_running_loop().time() + RECONNECT_TIMEOUT_S
    while asyncio.get_running_loop().time() < deadline:
        current = next(
            (
                item
                for item in await finish_io(backend.inventory.scan)
                if item.identity == attachment.identity and item.generation != attachment.generation
            ),
            None,
        )
        if current is not None:
            try:
                bindings = await finish_io(backend.inventory.bindings, current)
                roles = await finish_io(backend.inventory.endpoint_roles, current)
                if set(cast(dict[str, object], snapshot["nodes"])) <= set(roles):
                    await run_host("udevadm", "settle", "--timeout=8")
                    snapshot.update({"generation": current.generation, "bindings": bindings})
                    await finish_io(save_json, backend.journal, snapshot)
                    return current
            except (OSError, ValueError):
                pass  # The device is still being enumerated.
        await asyncio.sleep(0.1)
    raise OSError("The USB device did not return after reconnecting its port")
