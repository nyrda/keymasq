"""Ultimate 2 Wireless DInput reports, following SDL's 8BitDo protocol layout.

Reference: SDL commit 6083940ef432b0c7b077083e5459da0f6a386f40,
src/joystick/hidapi/SDL_hidapi_8bitdo.c. No device initialization writes are needed.
"""

import math
import struct
from typing import Literal

from ..types import Channel, Endpoint


class Ultimate2Driver:
    id = "8bitdo-ultimate2"
    label = "Ultimate 2 motion"
    association: Literal["same_hid", "same_usb"] = "same_hid"
    channels = (
        Channel("accel_x", "accelerometer", "y", 9.80665 / 4096),
        Channel("accel_y", "accelerometer", "x", 9.80665 / 4096, True),
        Channel("accel_z", "accelerometer", "z", 9.80665 / 4096),
        Channel("gyro_x", "gyro", "roll", math.radians(2000) / 32767, True),
        Channel("gyro_y", "gyro", "pitch", math.radians(2000) / 32767, True),
        Channel("gyro_z", "gyro", "yaw", math.radians(2000) / 32767),
    )

    def matches(self, endpoint: Endpoint) -> bool:
        # The tested interface is a Generic Desktop / Gamepad application
        # collection, with report 1 and a vendor-defined tail. Report decoding
        # below verifies the firmware's extended format before exposing samples.
        return (
            (endpoint.bus, endpoint.vendor, endpoint.product) == (3, 0x2DC8, 0x6012)
            and endpoint.descriptor.startswith(bytes.fromhex("05010905a101"))
            and bytes.fromhex("8501") in endpoint.descriptor
            and bytes.fromhex("0600ff") in endpoint.descriptor
        )

    def decode(self, report: bytes) -> dict[str, int] | None:
        if len(report) != 34 or report[0] != 0x01:
            return None
        return dict(
            zip(
                (channel.name for channel in self.channels),
                struct.unpack_from("<6h", report, 15),
                strict=True,
            )
        )
