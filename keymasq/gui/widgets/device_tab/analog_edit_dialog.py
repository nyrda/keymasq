"""Edit saved analog calibration without replacing the source binding."""

from collections.abc import Callable
from dataclasses import replace

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.devices import resolve_evdev_code
from keymasq.common.model.hardware import (
    AnalogAxisDefinition,
    AnalogInputDefinition,
    HardwareConfig,
)


class AnalogInputEditDialog(Adw.Dialog):
    def __init__(
        self,
        analog: AnalogInputDefinition,
        *,
        on_save: Callable[[AnalogInputDefinition], bool],
        on_delete_clicked: Callable[[Gtk.Button, Adw.Dialog, AnalogInputDefinition], None],
        on_close_clicked: Callable[[Gtk.Button, Adw.Dialog], None],
    ) -> None:
        super().__init__(title="Edit Analog Input", content_width=520, content_height=-1)
        self._analog = analog
        self._on_save = on_save
        self._detected: dict[str, object] = {}
        self._runtime: dict[str, object] = {}
        self.axis_entries: list[dict[str, Gtk.Entry]] = []
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(16)
        box.append(Gtk.Label(label="Name", xalign=0))
        self.name_entry = Gtk.Entry(text=analog.label)
        box.append(self.name_entry)
        source = Gtk.Label(label=f"Source interface: {analog.source or 'Default'}", xalign=0)
        source.add_css_class("dim-label")
        source.set_selectable(True)
        box.append(source)
        hint = Gtk.Label(
            label="Blank fields show known automatic values. Unavailable values are not estimated.",
            xalign=0,
            wrap=True,
        )
        hint.add_css_class("dim-label")
        box.append(hint)
        neutral_field = "center" if analog.type == "stick" else "rest"
        for axis in analog.axes:
            label = Gtk.Label(label=f"{axis.role.upper()} · {axis.evdev}", xalign=0)
            label.add_css_class("heading")
            box.append(label)
            grid = Gtk.Grid(column_spacing=12, row_spacing=6)
            entries = {}
            for col, (field, title) in enumerate(
                (
                    ("minimum", "Minimum"),
                    ("maximum", "Maximum"),
                    (neutral_field, "Center" if neutral_field == "center" else "Rest"),
                )
            ):
                grid.attach(Gtk.Label(label=title, xalign=0), col, 0, 1, 1)
                value = getattr(axis, field)
                entry = Gtk.Entry(
                    text="" if value is None else str(value),
                    placeholder_text="Automatic",
                    width_chars=10,
                    hexpand=True,
                )
                entries[field] = entry
                grid.attach(entry, col, 1, 1, 1)
            self.axis_entries.append(entries)
            box.append(grid)
        self.error_label = Gtk.Label(xalign=0, wrap=True, visible=False)
        self.error_label.add_css_class("error")
        box.append(self.error_label)
        footer = Gtk.Box(spacing=8)
        delete = Gtk.Button(label="Delete")
        delete.add_css_class("destructive-action")
        delete.connect("clicked", on_delete_clicked, self, analog)
        footer.append(delete)
        footer.append(Gtk.Box(hexpand=True))
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", on_close_clicked, self)
        footer.append(cancel)
        self.save_button = Gtk.Button(label="Save")
        self.save_button.add_css_class("suggested-action")
        self.save_button.connect("clicked", self._save)
        footer.append(self.save_button)
        box.append(footer)
        self.set_child(box)
        for entries in self.axis_entries:
            for entry in entries.values():
                entry.connect("changed", self._refresh_automatic_values)
        self._refresh_automatic_values()

    def update_device_inventory(self, response: object, hardware: HardwareConfig) -> bool:
        if not isinstance(response, dict):
            return False
        paths = {
            device.path
            for device in hardware.evdev_devices
            if self._analog.source is None or device.id == self._analog.source
        }
        matches = [
            device
            for device in response.get("devices", [])
            if isinstance(device, dict)
            and (
                (
                    device.get("source_hardware_id") == hardware.hardware_id
                    and (
                        self._analog.source is None
                        or device.get("source_interface_id") == self._analog.source
                    )
                )
                or (
                    not device.get("source_hardware_id")
                    and (device.get("path") in paths or device.get("stable_path") in paths)
                )
            )
        ]
        if matches:
            # A hidden source may be represented by its passthrough device.
            device = matches[0]
            self._detected = device.get("abs_info") or {}
            self._runtime = (device.get("analog_calibration") or {}).get(self._analog.id, {})
        self._refresh_automatic_values()
        return False

    def _refresh_automatic_values(self, *_args: object) -> None:
        for axis, entries in zip(self._analog.axes, self.axis_entries, strict=True):
            code = (
                axis.evdev_code if axis.evdev_code is not None else resolve_evdev_code(axis.evdev)
            )
            info = self._detected.get(str(code), {})
            if not isinstance(info, dict):
                info = {}
            effective: dict[str, int | None] = {}
            for field in ("minimum", "maximum"):
                detected = info.get(field)
                value = detected if isinstance(detected, int) else None
                entries[field].set_placeholder_text(
                    f"{value} (auto)" if value is not None else "Unavailable"
                )
                entries[field].set_tooltip_text(
                    f"Device-reported {field}: {value}"
                    if value is not None
                    else "Device-reported value is unavailable"
                )
                try:
                    effective[field] = int(entries[field].get_text())
                except ValueError:
                    effective[field] = value
            if self._analog.type == "stick":
                minimum, maximum = effective["minimum"], effective["maximum"]
                placeholder = "Unavailable"
                if minimum is not None and maximum is not None and minimum < maximum:
                    placeholder = f"{(minimum + maximum) / 2:g} (auto)"
                entries["center"].set_placeholder_text(placeholder)
                entries["center"].set_tooltip_text("Calculated midpoint of the effective bounds")
            else:
                calibration = self._runtime.get(axis.role, {})
                rest = calibration.get("rest") if isinstance(calibration, dict) else None
                if axis.rest is None and isinstance(rest, int):
                    entries["rest"].set_placeholder_text(f"{rest} (auto)")
                    entries["rest"].set_tooltip_text(
                        "Automatic rest sampled when the device was grabbed"
                    )
                else:
                    entries["rest"].set_placeholder_text("On next grab")
                    entries["rest"].set_tooltip_text(
                        "Rest is sampled when the device is grabbed, not from its current position."
                    )

    def _edited_axis(
        self,
        axis: AnalogAxisDefinition,
        entries: dict[str, Gtk.Entry],
    ) -> AnalogAxisDefinition:
        values: dict[str, int | None] = {}
        for field, entry in entries.items():
            text = entry.get_text().strip()
            try:
                value = int(text) if text else None
            except ValueError as exc:
                raise ValueError(f"{axis.evdev}: {field} must be a whole number or blank.") from exc
            if value is not None and not -2147483648 <= value <= 2147483647:
                raise ValueError(f"{axis.evdev}: {field} is outside the supported integer range.")
            values[field] = value
        code = axis.evdev_code if axis.evdev_code is not None else resolve_evdev_code(axis.evdev)
        detected = self._detected.get(str(code), {})
        if not isinstance(detected, dict):
            detected = {}
        minimum, maximum = values["minimum"], values["maximum"]
        if minimum is None and isinstance(detected.get("minimum"), int):
            minimum = detected["minimum"]
        if maximum is None and isinstance(detected.get("maximum"), int):
            maximum = detected["maximum"]
        neutral = values.get("center", values.get("rest"))
        if minimum is not None and maximum is not None and minimum >= maximum:
            raise ValueError(f"{axis.evdev}: minimum must be less than maximum.")
        if neutral is not None and (
            (minimum is not None and neutral < minimum)
            or (maximum is not None and neutral > maximum)
        ):
            raise ValueError(f"{axis.evdev}: center/rest must be within the axis range.")
        return replace(axis, **values)

    def _save(self, _button: Gtk.Button) -> None:
        try:
            label = self.name_entry.get_text().strip()
            if not label:
                raise ValueError("Enter a name for this analog input.")
            axes = [
                self._edited_axis(axis, entries)
                for axis, entries in zip(self._analog.axes, self.axis_entries, strict=True)
            ]
            edited = replace(self._analog, label=label, axes=axes)
            if not self._on_save(edited):
                raise ValueError(
                    "Could not save the analog input. Your changes have not been applied."
                )
        except (ValueError, OSError) as exc:
            self.error_label.set_text(str(exc))
            self.error_label.set_visible(True)
            return
        self.close()
