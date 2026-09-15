"""Discover physical attachments without opening input or raw HID devices."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from keymasq.common.types import JsonObject

USB_NAME = re.compile(r"[0-9]+-[0-9]+(?:\.[0-9]+)*\Z")
USB_INTERFACE_NAME = re.compile(r"[0-9]+-[0-9]+(?:\.[0-9]+)*:[0-9]+\.[0-9]+\Z")
HID_NAME = re.compile(r"[0-9A-Fa-f]{4}:[0-9A-Fa-f]{4}:[0-9A-Fa-f]{4}\.[0-9A-Fa-f]+\Z")
DRIVER_NAME = re.compile(r"[a-zA-Z0-9_-]+\Z")


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
    serial: str = ""

    @property
    def is_deck(self) -> bool:
        return (
            self.transport == "usb"
            and self.vendor == "28de"
            and self.product == "1205"
            and bool(self.main_hid)
        )

    @property
    def supported(self) -> bool:
        return self.transport in {"usb", "bluetooth", "hid"}

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
            "scope": "All HID and input interfaces on this USB device, including direct USB access"
            if self.transport == "usb"
            else "This device's HID and input interfaces; Bluetooth adapter remains available",
        }


class HardwareInventory:
    def __init__(self, sys_root: Path = Path("/sys"), dev_root: Path = Path("/dev")) -> None:
        self.sys_root = sys_root
        self.dev_root = dev_root

    def scan(self) -> list[Attachment]:
        devices: list[Attachment] = []
        hid_paths = list((self.sys_root / "bus/hid/devices").glob("*"))
        input_parents = [
            (path / "device").resolve() for path in (self.sys_root / "class/input").glob("event*")
        ]
        for path in sorted((self.sys_root / "bus/usb/devices").glob("*")):
            if not USB_NAME.fullmatch(path.name):
                continue
            vendor = read_attribute(path / "idVendor").lower()
            product = read_attribute(path / "idProduct").lower()
            interfaces = list(path.glob(f"{path.name}:*"))
            real_path = path.resolve()
            if (
                not any(read_attribute(item / "bInterfaceClass") == "03" for item in interfaces)
                and not any(item.resolve().parent.parent == real_path for item in hid_paths)
                and not any(
                    next(
                        (parent for parent in item.parents if USB_NAME.fullmatch(parent.name)), None
                    )
                    == real_path
                    for item in input_parents
                )
            ):
                continue
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
                    serial=serial,
                )
            )
        for path in hid_paths:
            if not HID_NAME.fullmatch(path.name):
                continue
            real_path = path.resolve()
            if any(real_path.is_relative_to(device.syspath) for device in devices):
                continue
            bluetooth = path.name.startswith("0005:")
            if not bluetooth and (self.sys_root / "devices/virtual") in real_path.parents:
                continue
            if not bluetooth and not any(real_path.glob("hidraw/hidraw*")):
                continue
            properties = dict(
                line.split("=", 1)
                for line in read_attribute(path / "uevent").splitlines()
                if "=" in line
            )
            _, vendor, product = path.name.split(".", 1)[0].split(":")
            # HID instance suffixes change on Bluetooth reconnect. Prefer the
            # remote identity and adapter/physical path when the driver supplies them.
            stable = (
                f"{properties['HID_UNIQ']}:{properties.get('HID_PHYS', '')}:{vendor}:{product}"
                if properties.get("HID_UNIQ")
                else str(real_path)
            )
            try:
                generation = f"{path.name}:{real_path.stat().st_ino}"
            except OSError:
                continue
            devices.append(
                Attachment(
                    identity=hashlib.sha256(stable.encode()).hexdigest()[:24],
                    generation=generation,
                    name=properties.get("HID_NAME", "Bluetooth input device"),
                    vendor=vendor.lower(),
                    product=product.lower(),
                    transport="bluetooth" if bluetooth else "hid",
                    syspath=real_path,
                    kernel_name=path.name,
                )
            )
        return devices

    def bindings(self, attachment: Attachment) -> dict[str, str]:
        """Record actual HID drivers, excluding driver-created Steam raw proxies."""
        result: dict[str, str] = {}
        for path in sorted((self.sys_root / "bus/hid/devices").glob("*")):
            if not HID_NAME.fullmatch(path.name):
                continue
            if not path.resolve().is_relative_to(attachment.syspath):
                continue
            if re.match(r"hid:b[0-9a-f]{4}g0103", read_attribute(path / "modalias").lower()):
                continue
            if attachment.is_deck and path.name != attachment.main_hid:
                continue
            driver = path / "driver"
            if not driver.is_symlink():
                raise ValueError(f"HID interface {path.name} has no bound driver")
            target = driver.resolve()
            if target.parent != (self.sys_root / "bus/hid/drivers").resolve():
                raise ValueError("HID driver is outside the HID bus")
            if not DRIVER_NAME.fullmatch(target.name):
                raise ValueError("Invalid HID driver name")
            result[path.name] = target.name
        if attachment.transport == "usb":
            # USB input drivers such as xpad need no HID layer. Only reconnect
            # interfaces that publish input, and never a parent hub or sibling
            # audio/storage interface. HID interfaces already use their child driver.
            for interface in attachment.syspath.glob(f"{attachment.kernel_name}:*"):
                if not USB_INTERFACE_NAME.fullmatch(interface.name):
                    continue
                if any(
                    path.resolve().is_relative_to(interface)
                    for path in (self.sys_root / "bus/hid/devices").glob("*")
                ):
                    continue
                if not any(
                    (path / "device").resolve().is_relative_to(interface)
                    for path in (self.sys_root / "class/input").glob("event*")
                ):
                    continue
                driver = interface / "driver"
                if not driver.is_symlink():
                    raise ValueError(f"USB input interface {interface.name} has no bound driver")
                target = driver.resolve()
                if target.parent != (
                    self.sys_root / "bus/usb/drivers"
                ).resolve() or not DRIVER_NAME.fullmatch(target.name):
                    raise ValueError("Invalid USB input driver")
                result[f"usb:{interface.name}"] = target.name
        if not result:
            raise ValueError("No bound input interfaces are available to reconnect")
        return result

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

    def endpoint_roles(self, attachment: Attachment) -> list[str]:
        return sorted(self.node_role(attachment, node) for node in self.nodes(attachment))

    def node_role(self, attachment: Attachment, node: Path) -> str:
        """Match recreated nodes by physical interface and function, not minor number."""
        if "bus/usb" in str(node):
            return "usb"
        subsystem = "hidraw" if node.name.startswith("hidraw") else "input"
        device = (self.sys_root / "class" / subsystem / node.name / "device").resolve()
        relative = device.relative_to(attachment.syspath)
        interface = relative.parts[0] if attachment.transport == "usb" else "hid"
        if subsystem == "hidraw":
            return f"{interface}:hidraw"
        kind = "event" if node.name.startswith("event") else "js"
        return f"{interface}:{kind}:{read_attribute(device / 'name')}"
