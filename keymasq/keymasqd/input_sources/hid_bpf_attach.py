"""Root side of a HID-BPF attachment: the program always comes from a bundled driver
that matches the requested HID device.
"""

import pwd
import re
from pathlib import Path
from typing import cast

from keymasq.common.paths import HANDOFF_SOCKET_PATH, KEYMASQ_USER
from keymasq.common.types import JsonObject
from keymasq.keymasqd import fd_handoff

from . import bpf
from .discovery import hid_endpoint
from .registry import DRIVERS

HID_DEVICE = re.compile(r"[0-9A-F]{4}:[0-9A-F]{4}:[0-9A-F]{4}\.[0-9A-F]{4,}\Z")


def attach_request(
    message: JsonObject,
    *,
    sysfs: Path = Path("/sys"),
    handoff_path: Path = HANDOFF_SOCKET_PATH,
) -> JsonObject:
    driver_id = message.get("driver")
    device = message.get("device")
    token = message.get("token")
    if not isinstance(device, str) or not HID_DEVICE.fullmatch(device):
        raise ValueError("Invalid HID device name")
    if not isinstance(token, str) or not fd_handoff.TOKEN.fullmatch(token):
        raise ValueError("Invalid handoff token")
    driver = next(
        (item for item in DRIVERS if item.id == driver_id and item.transport == "hid-bpf"),
        None,
    )
    if driver is None:
        raise ValueError("Unknown HID-BPF driver")
    parent = (sysfs / "bus/hid/devices" / device).resolve(strict=True)
    endpoint = hid_endpoint(parent, str(parent), driver.models)
    if endpoint is None or not driver.matches(endpoint):
        raise ValueError("The HID device does not match this driver")
    attached = bpf.attach(cast(bpf.HidBpfDriver, driver), int(device.rsplit(".", 1)[1], 16))
    try:
        fd_handoff.send(
            handoff_path,
            token,
            list(attached.fds),
            expected_uid=pwd.getpwnam(KEYMASQ_USER).pw_uid,
        )
    finally:
        attached.close()
    return {"device": device}
