"""USB/IP HID gadgets and input reports, confined to the exporter test VM."""

import errno
import os
import select
import time
from pathlib import Path

from uhid import DESCRIPTOR, REPORT

NAMES = ("mask-usb-target", "mask-usb-bystander", "mask-usb-replacement", "mask-usb-composite")
KEYBOARD_DESCRIPTOR = bytes.fromhex(
    "05010906a1018502050719e029e71500250175019508810295017508810195067508150025650507190029658100c0"
)


def configure(index, name):
    gadget = Path("/sys/kernel/config/usb_gadget") / name
    gadget.mkdir()
    product = "0x0006" if index == 3 else "0x0005"
    for key, value in {"idVendor": "0xcafe", "idProduct": product, "bcdUSB": "0x0200"}.items():
        (gadget / key).write_text(value)
    strings = gadget / "strings/0x409"
    strings.mkdir()
    (strings / "product").write_text(name)
    (strings / "serialnumber").write_text(name)
    function = gadget / "functions/hid.usb0"
    function.mkdir()
    for key, value in {"protocol": "0", "subclass": "0", "report_length": str(len(REPORT))}.items():
        (function / key).write_text(value)
    # Separate application collections expose two evdev interfaces without
    # exceeding the kernel HID gadget driver's four-function limit.
    descriptor = DESCRIPTOR + KEYBOARD_DESCRIPTOR if index == 3 else DESCRIPTOR
    (function / "report_desc").write_bytes(descriptor)
    config = gadget / "configs/c.1"
    config.mkdir()
    (config / "hid.usb0").symlink_to(function)
    (gadget / "UDC").write_text(f"usbip-vudc.{index}")
    return os.open(f"/dev/hidg{index}", os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)


def serve():
    descriptors = [configure(index, name) for index, name in enumerate(NAMES)]
    report = bytearray(REPORT)
    keyboard_report = bytearray([2] + [0] * 8)
    phase = 0
    Path("/run/keymasq-usb-gadgets-ready").touch()
    while True:
        pressed = (phase // 2) % 2
        report[8] = (report[8] & ~1) | pressed
        keyboard_report[3] = 0x65 if pressed else 0  # Keyboard application key.
        for index, fd in enumerate(descriptors):
            payload = keyboard_report if index == 3 and phase % 2 else report
            try:
                if select.select([], [fd], [], 0)[1]:
                    os.write(fd, payload)
                while select.select([fd], [], [], 0)[0]:
                    if not os.read(fd, 4096):
                        break
            except OSError as exc:
                if exc.errno not in {errno.EAGAIN, errno.ENODEV, errno.ESHUTDOWN}:
                    raise
        phase += 1
        time.sleep(0.05)


if __name__ == "__main__":
    serve()
