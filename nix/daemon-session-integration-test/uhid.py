"""Synthetic Bluetooth controller using the kernel UHID interface."""

import errno
import os
import select
import struct
import threading

# Ultimate 2's captured descriptor and report, also used by the decoder tests.
DESCRIPTOR = bytes.fromhex(
    "05010905a1018501050115002507463b0195017504651409398142750195048101150026ff00"
    "09300931093209359504750881020502150026ff0009c409c5950275088102050919012918"
    "150025017501951881020600ff0920750895178102050f0970850515002564750895049102c0"
)
REPORT = bytes.fromhex("010f7f807f7f00000000000000006443fe04000f1003001700020000000000000000")
NAME = "integration-native-ultimate2"


class NativeController:
    def __init__(self, name: str = NAME, *, unique: str = "", toggle_button: bool = False) -> None:
        self.fd = os.open("/dev/uhid", os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
        self.stop = threading.Event()
        self.error: OSError | None = None
        self.toggle_button = toggle_button
        self.button_value: int | None = None
        self.button_condition = threading.Condition()
        self.button_revision = 0
        self.sent_revision = 0
        # linux/uhid.h: UHID_CREATE2, packed name/phys/uniq, descriptor size,
        # Bluetooth bus, vendor/product/version/country, then descriptor bytes.
        create = struct.pack(
            "<I128s64s64sHHIIII",
            11,
            name.encode(),
            b"integration/imu",
            unique.encode(),
            len(DESCRIPTOR),
            5,
            0x2DC8,
            0x6012,
            1,
            0,
        )
        try:
            os.write(self.fd, create + DESCRIPTOR)
        except BaseException:
            os.close(self.fd)
            raise
        self.thread = threading.Thread(target=self.feed, daemon=True)
        self.thread.start()

    def feed(self) -> None:
        report = bytearray(REPORT)
        try:
            while not self.stop.is_set():
                if select.select([self.fd], [], [], 0.02)[0]:
                    event = os.read(self.fd, 4380)
                    kind = struct.unpack_from("<I", event)[0]
                    if kind in (9, 13):  # GET_REPORT and SET_REPORT
                        request = struct.unpack_from("<I", event, 4)[0]
                        reply = struct.pack("<IIH", 10 if kind == 9 else 14, request, errno.EIO)
                        os.write(self.fd, reply)
                with self.button_condition:
                    if self.button_value is not None:
                        report[8] = (report[8] & ~1) | self.button_value
                    elif self.toggle_button:
                        report[8] ^= 1
                    os.write(self.fd, struct.pack("<IH", 12, len(report)) + report)
                    self.sent_revision = self.button_revision
                    self.button_condition.notify_all()
        except OSError as exc:
            self.error = exc

    def set_button(self, value: int | None) -> None:
        with self.button_condition:
            self.button_value = value
            self.button_revision += 1
            assert self.button_condition.wait_for(
                lambda: self.sent_revision == self.button_revision, timeout=2
            ), "UHID did not emit the requested button state"

    def close(self) -> None:
        self.stop.set()
        self.thread.join(timeout=2)
        if self.thread.is_alive():
            raise AssertionError("UHID feeder did not stop")
        os.close(self.fd)
        if self.error is not None:
            raise AssertionError("UHID feeder failed") from self.error
