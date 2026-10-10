"""Steam Deck controller interface emulated with UHID, bound by the kernel's hid-steam driver."""

import os
import select
import struct
import threading
import time

# Interface 2 of a Steam Deck: one vendor-defined 64-byte input and feature report.
DESCRIPTOR = bytes.fromhex("06ffff0901a101150026ff0075089540090181020901b102c0")
NAME = "Valve Software Steam Deck Controller"
PHYS = "integration-deck/input2"
SERIAL = "INTEGRATIONDECK"
BUS_USB = 3
VENDOR = 0x28DE
PRODUCT = 0x1205
REPORT_PERIOD_S = 0.01

UHID_CREATE2 = 11
UHID_GET_REPORT = 9
UHID_GET_REPORT_REPLY = 10
UHID_INPUT2 = 12
UHID_SET_REPORT = 13
UHID_SET_REPORT_REPLY = 14

GET_ATTRIBUTES_VALUES = 0x83
GET_STRING_ATTRIBUTE = 0xAE
ATTRIB_PRODUCT_ID = 1
ATTRIB_CONNECTION_INTERVAL_IN_US = 11
ATTRIB_STR_UNIT_SERIAL = 1


def _reply(command: bytes) -> bytes:
    if command[:1] == bytes([GET_STRING_ATTRIBUTE]):
        return bytes([GET_STRING_ATTRIBUTE, 0x15, ATTRIB_STR_UNIT_SERIAL]) + SERIAL.encode()
    if command[:1] == bytes([GET_ATTRIBUTES_VALUES]):
        return bytes([GET_ATTRIBUTES_VALUES, 10]) + struct.pack(
            "<BIBI", ATTRIB_PRODUCT_ID, PRODUCT, ATTRIB_CONNECTION_INTERVAL_IN_US, 4000
        )
    return command


class SteamDeck:
    def __init__(self) -> None:
        self.fd = os.open("/dev/uhid", os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
        self.stop = threading.Event()
        self.error: OSError | None = None
        self.condition = threading.Condition()
        self.state = bytes(64)
        self.revision = 0
        self.sent_revision = 0
        self.command = b""
        create = struct.pack(
            "<I128s64s64sHHIIII",
            UHID_CREATE2,
            NAME.encode(),
            PHYS.encode(),
            SERIAL.encode(),
            len(DESCRIPTOR),
            BUS_USB,
            VENDOR,
            PRODUCT,
            0x0200,
            0,
        )
        try:
            os.write(self.fd, create + DESCRIPTOR)
        except BaseException:
            os.close(self.fd)
            raise
        self.thread = threading.Thread(target=self.feed, daemon=True)
        self.thread.start()

    def answer(self, event: bytes) -> None:
        kind, request = struct.unpack_from("<II", event)
        if kind == UHID_SET_REPORT:
            size = struct.unpack_from("<H", event, 10)[0]
            self.command = bytes(event[13 : 12 + size])
            os.write(self.fd, struct.pack("<IIH", UHID_SET_REPORT_REPLY, request, 0))
        elif kind == UHID_GET_REPORT:
            data = b"\x00" + _reply(self.command).ljust(64, b"\x00")
            os.write(
                self.fd, struct.pack("<IIHH", UHID_GET_REPORT_REPLY, request, 0, len(data)) + data
            )

    def feed(self) -> None:
        sequence = 0
        deadline = time.monotonic()
        try:
            while not self.stop.is_set():
                if select.select([self.fd], [], [], max(0.0, deadline - time.monotonic()))[0]:
                    self.answer(os.read(self.fd, 4380))
                    continue
                deadline = max(deadline, time.monotonic()) + REPORT_PERIOD_S
                sequence += 1
                with self.condition:
                    report = bytearray(self.state)
                    report[0:8] = struct.pack("<BBBBI", 0x01, 0x00, 0x09, 0x40, sequence)
                    os.write(self.fd, struct.pack("<IH", UHID_INPUT2, len(report)) + report)
                    self.sent_revision = self.revision
                    self.condition.notify_all()
        except OSError as exc:
            self.error = exc

    def report(self, *bits: tuple[int, int], axes: dict[int, int] | None = None) -> None:
        state = bytearray(64)
        for offset, mask in bits:
            state[offset] |= mask
        for offset, value in (axes or {}).items():
            struct.pack_into("<h", state, offset, value)
        with self.condition:
            self.state = bytes(state)
            self.revision += 1
            assert self.condition.wait_for(
                lambda: self.sent_revision == self.revision, timeout=2
            ), "UHID did not emit the requested Steam Deck report"

    def close(self) -> None:
        self.stop.set()
        self.thread.join(timeout=2)
        if self.thread.is_alive():
            raise AssertionError("Steam Deck feeder did not stop")
        os.close(self.fd)
        if self.error is not None:
            raise AssertionError("Steam Deck feeder failed") from self.error
