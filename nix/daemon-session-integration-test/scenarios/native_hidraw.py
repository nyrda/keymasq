"""Read real UHID reports through the installed daemon's read-only hidraw ACL."""

import contextlib
from pathlib import Path

from support import ScenarioContext
from uhid import NAME, NativeController

HARDWARE_ID = "2dc8:6012"


def _capture(ctx: ScenarioContext) -> None:
    # Use the connected evdev interface, as the hardware setup wizard does.
    # A model selector can still be cached from before this new connection.
    events = [
        event
        for node in Path("/sys/class/hidraw").glob("hidraw*")
        if f"HID_NAME={NAME}\n" in (node / "device/uevent").read_text()
        for event in (node / "device").glob("input/input*/event*")
    ]
    assert len(events) == 1, events
    anchor = {"id": "pad", "path": f"/dev/input/{events[0].name}", "type": "gamepad"}
    ctx.request(
        {
            "command": "begin_capture",
            "hardware_id": HARDWARE_ID,
            "mode": "motion",
            "motion_axis_codes": [0, 1, 2, 3, 4, 5],
            "evdev_interfaces": [
                {
                    "id": "imu",
                    "path": "keymasq-source:imu",
                    "backend": "hidraw",
                    "driver": "8bitdo-ultimate2",
                    "anchor": anchor,
                    "companion_of": "pad",
                }
            ],
        }
    )
    try:
        result = ctx.request({"command": "capture_read", "hardware_id": HARDWARE_ID})
        frames = result.get("frames", [])
        expected = {"0": -445, "1": 4, "2": 4111, "3": 3, "4": 23, "5": 2}
        if not frames or any(frame.get("values") != expected for frame in frames):
            raise AssertionError(f"native hidraw samples missing or incorrect: {result}")
    finally:
        ctx.request({"command": "end_capture", "hardware_id": HARDWARE_ID})


def run(ctx: ScenarioContext) -> None:
    def connected() -> bool:
        return any(
            f"HID_NAME={NAME}\n" in (node / "device/uevent").read_text()
            for node in Path("/sys/class/hidraw").glob("hidraw*")
        )

    # The first connection follows daemon startup. Restart with it attached,
    # then reconnect it to exercise both startup and hotplug access grants.
    for restart in (True, False):
        with contextlib.closing(NativeController()):
            ctx.wait_until("native hidraw connection", connected)
            ctx.settle_udev()
            _capture(ctx)
            if restart:
                ctx.restart_keymasqd()
                _capture(ctx)
        ctx.wait_until("native hidraw disconnect", lambda: not connected())
