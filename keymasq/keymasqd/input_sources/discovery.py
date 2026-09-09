"""Read-only sysfs discovery and instance association for native inputs."""

import errno
from collections.abc import Iterable
from pathlib import Path

from .registry import DRIVERS
from .types import Binding, Endpoint, InputDriver

SOURCE_PREFIX = "/dev/keymasq-sources/"


def subsystem_parent(path: Path, subsystem: str) -> Path | None:
    current = path.resolve()
    for candidate in (current, *current.parents):
        if (candidate / "subsystem").resolve().name == subsystem:
            return candidate
    return None


def hid_parent(event_path: str, sysfs: Path = Path("/sys")) -> str:
    node = Path(event_path).resolve().name
    parent = subsystem_parent(sysfs / "class/input" / node / "device", "hid")
    return str(parent) if parent else ""


def usb_device_parent(path: Path) -> Path | None:
    for candidate in (path, *path.parents):
        if (candidate / "subsystem").resolve().name == "usb" and (candidate / "idVendor").exists():
            return candidate
    return None


def discover_bindings(
    *,
    sysfs: Path = Path("/sys"),
    drivers: Iterable[InputDriver] = DRIVERS,
) -> list[Binding]:
    bindings: list[Binding] = []
    drivers = tuple(drivers)
    for node in sorted((sysfs / "class/hidraw").glob("hidraw*")):
        try:
            parent = subsystem_parent(node / "device", "hid")
            if parent is None:
                continue
            fields = dict(
                line.split("=", 1)
                for line in (parent / "uevent").read_text().splitlines()
                if "=" in line
            )
            bus, vendor, product = (int(value, 16) for value in fields["HID_ID"].split(":"))
            usb = usb_device_parent(parent)
            endpoint = Endpoint(
                path=f"/dev/{node.name}",
                hid_parent=str(parent),
                usb_parent=str(usb) if usb else "",
                bus=bus,
                vendor=vendor,
                product=product,
                descriptor=(parent / "report_descriptor").read_bytes(),
                name=fields.get("HID_NAME", ""),
                phys=fields.get("HID_PHYS", ""),
            )
            companions = tuple(
                sorted(f"/dev/input/{event.name}" for event in parent.glob("input/input*/event*"))
            )
            matches = [driver for driver in drivers if driver.matches(endpoint)]
            if len(matches) == 1:
                if matches[0].association == "same_usb" and usb is not None:
                    companions = tuple(
                        sorted(
                            f"/dev/input/{event.name}"
                            for event in usb.glob("**/input/input*/event*")
                        )
                    )
                bindings.append(Binding(matches[0], endpoint, companions))
        except (OSError, ValueError, KeyError):
            continue
    return bindings


def binding_for_path(path: str) -> Binding:
    for binding in discover_bindings():
        if binding.path == path:
            return binding
    raise FileNotFoundError(errno.ENODEV, "Native input source disconnected or unsupported", path)


def companion_binding(
    bindings: Iterable[Binding], driver_id: str, event_path: str
) -> Binding | None:
    parent = hid_parent(event_path)
    usb = usb_device_parent(Path(parent)) if parent else None
    matches = [
        binding
        for binding in bindings
        if binding.driver.id == driver_id
        and (
            (binding.driver.association == "same_hid" and binding.endpoint.hid_parent == parent)
            or (
                binding.driver.association == "same_usb"
                and usb is not None
                and binding.endpoint.usb_parent == str(usb)
            )
        )
    ]
    return matches[0] if len(matches) == 1 else None
