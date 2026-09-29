"""Read-only sysfs discovery and instance association for native inputs."""

import errno
import re
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
    hidraw_drivers = tuple(driver for driver in drivers if driver.transport == "hidraw")
    bpf_drivers = tuple(driver for driver in drivers if driver.transport == "hid-bpf")
    for node in sorted((sysfs / "class/hidraw").glob("hidraw*")):
        try:
            parent = subsystem_parent(node / "device", "hid")
            if parent is None:
                continue
            _bind(bindings, hidraw_drivers, parent, f"/dev/{node.name}")
        except (OSError, ValueError, KeyError):
            continue
    if bpf_drivers:
        # In-kernel drivers use the HID device itself, which may have no hidraw node.
        for device in sorted((sysfs / "bus/hid/devices").glob("*")):
            try:
                parent = device.resolve()
                _bind(bindings, bpf_drivers, parent, str(parent))
            except (OSError, ValueError, KeyError):
                continue
    return bindings


def hid_endpoint(
    parent: Path, path: str, models: frozenset[tuple[int, int]] | None = None
) -> Endpoint | None:
    fields = dict(
        line.split("=", 1) for line in (parent / "uevent").read_text().splitlines() if "=" in line
    )
    bus, vendor, product = (int(value, 16) for value in fields["HID_ID"].split(":"))
    if models is not None and (vendor, product) not in models:
        return None
    group = re.search(r"g([0-9A-Fa-f]{4})", fields.get("MODALIAS", ""))
    usb = usb_device_parent(parent)
    return Endpoint(
        path=path,
        hid_parent=str(parent),
        usb_parent=str(usb) if usb else "",
        bus=bus,
        vendor=vendor,
        product=product,
        descriptor=(parent / "report_descriptor").read_bytes(),
        name=fields.get("HID_NAME", ""),
        phys=fields.get("HID_PHYS", ""),
        group=int(group.group(1), 16) if group else 0,
    )


def _bind(
    bindings: list[Binding], drivers: tuple[InputDriver, ...], parent: Path, path: str
) -> None:
    models = frozenset(model for driver in drivers for model in driver.models)
    endpoint = hid_endpoint(parent, path, models)
    if endpoint is None:
        return
    matches = [driver for driver in drivers if driver.matches(endpoint)]
    if len(matches) != 1:
        return
    parent = Path(endpoint.hid_parent)
    usb = Path(endpoint.usb_parent) if endpoint.usb_parent else None
    events = (
        usb.glob("**/input/input*/event*")
        if matches[0].association == "same_usb" and usb is not None
        else parent.glob("input/input*/event*")
    )
    companions = tuple(sorted(f"/dev/input/{event.name}" for event in events))
    bindings.append(Binding(matches[0], endpoint, companions))


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
