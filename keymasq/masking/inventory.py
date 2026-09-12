"""Discover physical attachments without opening input or raw HID devices."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from keymasq.common.types import JsonObject

USB_NAME = re.compile(r"[0-9]+-[0-9]+(?:\.[0-9]+)*\Z")
HID_NAME = re.compile(r"[0-9A-Fa-f]{4}:[0-9A-Fa-f]{4}:[0-9A-Fa-f]{4}\.[0-9A-Fa-f]+\Z")


def read_attribute(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


@dataclass(frozen=True)
class Attachment:
    identity: str
    generation: str
    name: str
    vendor: str
    product: str
    transport: str
    syspath: Path
    kernel_name: str
    main_hid: str = ""

    @property
    def supported(self) -> bool:
        return (
            self.transport == "usb"
            and self.vendor == "28de"
            and self.product == "1205"
            and bool(self.main_hid)
        )

    def as_json(self) -> JsonObject:
        return {
            "id": self.identity,
            "generation": self.generation,
            "name": self.name,
            "vendor": self.vendor,
            "product": self.product,
            "transport": self.transport,
            "connection": self.kernel_name,
            "supported": self.supported,
            "unsupported_reason": "" if self.supported else "Takeover is not supported yet",
            "scope": "Controller HID, USB and input interfaces; touchscreen excluded"
            if self.supported
            else "",
        }


class HardwareInventory:
    def __init__(self, sys_root: Path = Path("/sys"), dev_root: Path = Path("/dev")) -> None:
        self.sys_root = sys_root
        self.dev_root = dev_root

    def scan(self) -> list[Attachment]:
        devices: list[Attachment] = []
        hid_paths = list((self.sys_root / "bus/hid/devices").glob("*"))
        for path in sorted((self.sys_root / "bus/usb/devices").glob("*")):
            if not USB_NAME.fullmatch(path.name):
                continue
            vendor = read_attribute(path / "idVendor").lower()
            product = read_attribute(path / "idProduct").lower()
            interfaces = list(path.glob(f"{path.name}:*"))
            if not any(read_attribute(item / "bInterfaceClass") == "03" for item in interfaces):
                continue
            real_path = path.resolve()
            main_hid = next(
                (
                    item.name
                    for item in hid_paths
                    if item.resolve().parent == real_path / f"{path.name}:1.2"
                    and HID_NAME.fullmatch(item.name)
                    # The raw proxy is a sibling with HID_GROUP_STEAM.
                    and not re.match(
                        r"hid:b[0-9a-f]{4}g0103", read_attribute(item / "modalias").lower()
                    )
                    and (item / "driver").resolve().name == "hid-steam"
                ),
                "",
            )
            serial = read_attribute(path / "serial")
            identity = hashlib.sha256(
                f"usb:{real_path}:{vendor}:{product}:{serial}".encode()
            ).hexdigest()[:24]
            generation = read_attribute(path / "devnum")
            try:
                generation += f":{real_path.stat().st_ino}"
            except OSError:
                continue
            devices.append(
                Attachment(
                    identity=identity,
                    generation=generation,
                    name=read_attribute(path / "product") or f"USB input device {vendor}:{product}",
                    vendor=vendor,
                    product=product,
                    transport="usb",
                    syspath=real_path,
                    kernel_name=path.name,
                    main_hid=main_hid,
                )
            )
        for path in hid_paths:
            if not path.name.startswith("0005:") or not HID_NAME.fullmatch(path.name):
                continue
            properties = dict(
                line.split("=", 1)
                for line in read_attribute(path / "uevent").splitlines()
                if "=" in line
            )
            real_path = path.resolve()
            _, vendor, product = path.name.split(".", 1)[0].split(":")
            devices.append(
                Attachment(
                    identity=hashlib.sha256(str(real_path).encode()).hexdigest()[:24],
                    generation=path.name,
                    name=properties.get("HID_NAME", "Bluetooth input device"),
                    vendor=vendor.lower(),
                    product=product.lower(),
                    transport="bluetooth",
                    syspath=real_path,
                    kernel_name=path.name,
                )
            )
        return devices

    def resolve(self, identity: str, generation: str) -> Attachment:
        for attachment in self.scan():
            if attachment.identity == identity and attachment.generation == generation:
                return attachment
        raise ValueError("Hardware disconnected or changed; refresh the hardware list")

    def nodes(self, attachment: Attachment) -> list[Path]:
        nodes: list[Path] = []
        for subsystem, patterns in (("hidraw", ("hidraw*",)), ("input", ("event*", "js*"))):
            for pattern in patterns:
                for path in sorted((self.sys_root / "class" / subsystem).glob(pattern)):
                    if (path / "device").resolve().is_relative_to(attachment.syspath):
                        node = self.dev_root / ("input" if subsystem == "input" else "") / path.name
                        if node.exists():
                            nodes.append(node)
        busnum = read_attribute(attachment.syspath / "busnum")
        devnum = read_attribute(attachment.syspath / "devnum")
        if busnum.isdecimal() and devnum.isdecimal():
            node = self.dev_root / "bus/usb" / f"{int(busnum):03d}" / f"{int(devnum):03d}"
            if node.exists():
                nodes.append(node)
        return nodes

    def event_nodes(self, attachment: Attachment) -> list[str]:
        return [str(node) for node in self.nodes(attachment) if node.name.startswith("event")]

    def node_role(self, attachment: Attachment, node: Path) -> str:
        """Match recreated nodes by physical interface and function, not minor number."""
        if "bus/usb" in str(node):
            return "usb"
        subsystem = "hidraw" if node.name.startswith("hidraw") else "input"
        device = (self.sys_root / "class" / subsystem / node.name / "device").resolve()
        relative = device.relative_to(attachment.syspath)
        interface = relative.parts[0]
        if subsystem == "hidraw":
            return f"{interface}:hidraw"
        kind = "event" if node.name.startswith("event") else "js"
        return f"{interface}:{kind}:{read_attribute(device / 'name')}"
