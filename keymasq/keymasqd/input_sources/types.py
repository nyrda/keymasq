"""Transport-independent driver descriptions and complete input frames."""

from dataclasses import dataclass, field
from typing import Literal, Protocol


@dataclass(frozen=True)
class Endpoint:
    path: str
    hid_parent: str
    usb_parent: str
    bus: int
    vendor: int
    product: int
    descriptor: bytes
    name: str = ""
    phys: str = ""


@dataclass(frozen=True)
class Channel:
    name: str
    kind: Literal["gyro", "accelerometer", "button", "axis"]
    role: str
    scale: float = 1.0
    invert: bool = False


@dataclass(frozen=True)
class InputFrame:
    """Raw channel snapshot. Timestamps are monotonic; sample time may be estimated."""

    values: dict[str, int]
    arrival_ns: int
    sample_ns: int
    estimated_time: bool = True
    discontinuity: bool = False


class InputDriver(Protocol):
    id: str
    label: str
    channels: tuple[Channel, ...]
    association: Literal["same_hid", "same_usb"]

    def matches(self, endpoint: Endpoint) -> bool: ...

    def decode(self, report: bytes) -> dict[str, int] | None: ...


@dataclass(frozen=True)
class Binding:
    driver: InputDriver
    endpoint: Endpoint
    companions: tuple[str, ...] = field(default_factory=tuple)

    @property
    def path(self) -> str:
        # A connection-local source address, never a node opened by the kernel.
        hid_name = self.endpoint.hid_parent.rsplit("/", 1)[-1]
        node = self.endpoint.path.rsplit("/", 1)[-1]
        return f"/dev/keymasq-sources/{self.driver.id}/{hid_name}/{node}"
