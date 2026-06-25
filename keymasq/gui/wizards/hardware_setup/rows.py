from collections.abc import Mapping, Sequence
from typing import Any

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.devices import INPUT_CLASS_ORDER, input_class_label
from keymasq.common.models import DeviceType
from keymasq.gui.wizards.hardware_setup.identity import device_search_text
from keymasq.gui.wizards.hardware_setup.templates import interface_device_types

InterfaceInfo = Mapping[str, Any]
DeviceInfo = Mapping[str, Any]


def should_show_interface_expander(
    show_raw_evdev_devices: bool,
    interfaces: Sequence[InterfaceInfo],
) -> bool:
    if show_raw_evdev_devices:
        return False
    return bool(interfaces)


def raw_device_summary(
    show_raw_evdev_devices: bool,
    interfaces: Sequence[InterfaceInfo],
) -> str:
    if not show_raw_evdev_devices or not interfaces:
        return ""
    iface = interfaces[0]
    parts = [str(iface.get("path", "") or "")]
    stable_path = str(iface.get("stable_path", "") or "")
    if stable_path and stable_path not in parts:
        parts.append(stable_path)
    phys = str(iface.get("phys", "") or "")
    if phys:
        parts.append(phys)
    return " · ".join(part for part in parts if part)


def device_in_use(dev_info: DeviceInfo) -> bool:
    return any(
        bool(iface.get("grabbed_by_keymasq", False))
        or bool(iface.get("configured_hardware_id", False))
        for iface in dev_info.get("interfaces", [])
        if isinstance(iface, dict)
    )


def device_in_use_summary(dev_info: DeviceInfo) -> str:
    for iface in dev_info.get("interfaces", []):
        if not isinstance(iface, dict):
            continue
        if not bool(iface.get("grabbed_by_keymasq", False)):
            configured_hardware_id = str(iface.get("configured_hardware_id", "") or "")
            if configured_hardware_id:
                return f"Configured as {configured_hardware_id}"
            continue
        hardware_id = str(iface.get("source_hardware_id", "") or "")
        interface_id = str(iface.get("source_interface_id", "") or "")
        if hardware_id and interface_id:
            return f"In use by {hardware_id} ({interface_id})"
        if hardware_id:
            return f"In use by {hardware_id}"
        return "In use by Keymasq"
    return ""


def interface_detail_lines(iface: InterfaceInfo) -> list[str]:
    lines = [f"- {iface.get('name', '') or iface.get('path', '')}"]
    path = str(iface.get("path", "") or "")
    stable_path = str(iface.get("stable_path", "") or "")
    phys = str(iface.get("phys", "") or "")
    if path:
        lines.append(f"  path: {path}")
    if stable_path and stable_path != path:
        lines.append(f"  stable: {stable_path}")
    if phys:
        lines.append(f"  phys: {phys}")
    in_use = device_in_use_summary({"interfaces": [iface]})
    if in_use:
        lines.append(f"  {in_use}")
    return lines


def device_type_label(device_type: str) -> str:
    return input_class_label(device_type)


def device_type_sort_order(device_type: DeviceType) -> int:
    order = {
        DeviceType.MOUSE: 0,
        DeviceType.KEYBOARD: 1,
        DeviceType.GAMEPAD: 2,
        DeviceType.OTHER: 3,
    }
    return order.get(device_type, 99)


def group_device_type(dev_info: DeviceInfo) -> DeviceType:
    interfaces = dev_info.get("interfaces", [])
    if not interfaces:
        return DeviceType.OTHER

    best = DeviceType.OTHER
    best_order = 99
    for iface in interfaces:
        iface_type = iface.get("device_type", DeviceType.OTHER)
        order = device_type_sort_order(iface_type)
        if order < best_order:
            best = iface_type
            best_order = order
    return best


def group_device_types(dev_info: DeviceInfo) -> list[str]:
    interfaces = dev_info.get("interfaces", [])
    type_set: set[str] = set()
    for iface in interfaces:
        type_set.update(interface_device_types(iface))

    if not type_set:
        return ["other"]

    return sorted(type_set, key=INPUT_CLASS_ORDER.index)


def build_detected_device_row(
    hardware_id: str,
    dev_info: DeviceInfo,
    *,
    show_raw_evdev_devices: bool,
) -> Gtk.ListBoxRow:
    row = Gtk.ListBoxRow()
    row_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    row_box.set_margin_top(8)
    row_box.set_margin_bottom(8)
    row_box.set_margin_start(12)
    row_box.set_margin_end(12)

    name = Gtk.Label(label=str(dev_info.get("display_name") or dev_info.get("name") or "Device"))
    name.set_halign(Gtk.Align.START)
    row_box.append(name)

    grouped_types = group_device_types(dev_info)
    interfaces = dev_info.get("interfaces", [])
    iface_count = len(interfaces)
    type_text = " · ".join(device_type_label(t) for t in grouped_types)
    iface_text = "interface" if iface_count == 1 else "interfaces"

    model_id = str(dev_info.get("model_id", hardware_id))
    vidpid = Gtk.Label(
        label=f"{model_id} · {iface_count} evdev {iface_text} · {type_text}"
    )
    vidpid.add_css_class("dim-label")
    vidpid.add_css_class("caption")
    vidpid.set_halign(Gtk.Align.START)
    row_box.append(vidpid)

    raw_summary = raw_device_summary(show_raw_evdev_devices, interfaces)
    if raw_summary:
        raw_label = Gtk.Label(label=raw_summary)
        raw_label.add_css_class("dim-label")
        raw_label.add_css_class("caption")
        raw_label.set_halign(Gtk.Align.START)
        row_box.append(raw_label)

    in_use_summary = device_in_use_summary(dev_info)
    if in_use_summary:
        in_use = Gtk.Label(label=in_use_summary)
        in_use.add_css_class("caption")
        in_use.set_halign(Gtk.Align.START)
        row_box.append(in_use)

    expander: Gtk.Expander | None = None
    if should_show_interface_expander(show_raw_evdev_devices, interfaces):
        interface_expander = Gtk.Expander(label="Evdev devices")
        interface_expander.set_expanded(False)

        iface_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        iface_box.set_margin_top(4)
        iface_box.set_margin_start(12)

        for iface in interfaces:
            for detail in interface_detail_lines(iface):
                iface_detail = Gtk.Label(label=detail)
                iface_detail.add_css_class("caption")
                iface_detail.set_halign(Gtk.Align.START)
                iface_box.append(iface_detail)

        interface_expander.set_child(iface_box)
        row_box.append(interface_expander)
        expander = interface_expander

    row.set_child(row_box)
    row.hardware_id = hardware_id
    row._search_text = device_search_text(hardware_id, dev_info)
    if expander is not None:
        row._expander = expander
    return row


def build_no_devices_row() -> Gtk.ListBoxRow:
    row = Gtk.ListBoxRow()
    row_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    row_box.set_margin_top(8)
    row_box.set_margin_bottom(8)
    row_box.set_margin_start(12)
    row_box.set_margin_end(12)

    label = Gtk.Label(label="No input devices reported by keymasqd")
    label.set_halign(Gtk.Align.START)
    row_box.append(label)

    hint = Gtk.Label(
        label=(
            "Ensure keymasqd is running and has access to /dev/input/event*. "
            "Touchpads are detected but not supported in Add Device yet."
        )
    )
    hint.add_css_class("dim-label")
    hint.add_css_class("caption")
    hint.set_halign(Gtk.Align.START)
    row_box.append(hint)

    row.set_selectable(False)
    row.set_child(row_box)
    return row
