"""Editable controls for virtual gaming device templates."""

from collections.abc import Callable

import evdev
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.virtual_device_templates import VirtualAxis, VirtualButton


def entry_row(title: str, value: str) -> Adw.EntryRow:
    row = Adw.EntryRow(title=title)
    row.set_text(value)
    return row


def event_names(*, axis: bool) -> list[str]:
    prefix = "ABS_" if axis else "BTN_"
    table = evdev.ecodes.ABS if axis else evdev.ecodes.keys
    return sorted(
        name.lower()
        for name, code in vars(evdev.ecodes).items()
        if name.startswith(prefix) and isinstance(code, int) and code in table
    )


class TemplateControlRow(Adw.ExpanderRow):
    def __init__(
        self,
        control: VirtualButton | VirtualAxis,
        on_remove: Callable[["TemplateControlRow"], None],
    ) -> None:
        super().__init__(title=control.label, subtitle=control.evdev.upper())
        self.is_axis = isinstance(control, VirtualAxis)
        self.label_row = entry_row("Label", control.label)
        self.id_row = entry_row("Control ID", control.id)
        self.id_row.set_tooltip_text("Stable ID used to refer to this control in analog mappings")
        self.add_row(self.label_row)
        self.codes = event_names(axis=self.is_axis)
        self.code_row = Adw.ComboRow(title="Linux axis" if self.is_axis else "Linux button")
        self.code_row.set_model(Gtk.StringList.new([code.upper() for code in self.codes]))
        self.code_row.set_enable_search(True)
        self.code_row.set_selected(self.codes.index(control.evdev))
        self.add_row(self.code_row)
        self.range_rows: dict[str, Adw.SpinRow] = {}
        if isinstance(control, VirtualAxis):
            for key, title, value in (
                ("minimum", "Minimum", control.minimum),
                ("maximum", "Maximum", control.maximum),
                ("rest", "Rest value", control.rest),
            ):
                row = self._number_row(title, value)
                self.range_rows[key] = row
                self.add_row(row)
            self.range_rows["rest"].set_tooltip_text("Value sent when the control is released")
        advanced = Adw.ExpanderRow(title="Advanced")
        advanced.add_row(self.id_row)
        if isinstance(control, VirtualAxis):
            for key, title, value, hint in (
                ("fuzz", "Fuzz", control.fuzz, "Noise tolerance reported to Linux"),
                ("flat", "Flat", control.flat, "Dead zone reported to Linux"),
                (
                    "resolution",
                    "Resolution",
                    control.resolution,
                    "Units per millimeter or radian; 0 means unspecified",
                ),
            ):
                row = self._number_row(title, value, minimum=0)
                row.set_tooltip_text(hint)
                self.range_rows[key] = row
                advanced.add_row(row)
        self.add_row(advanced)
        remove = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER)
        remove.add_css_class("flat")
        remove.set_tooltip_text(f"Remove {control.label}")
        self._on_remove = on_remove
        remove.connect("clicked", self._remove_clicked)
        self.add_suffix(remove)
        self.label_row.connect("changed", self._update_summary)
        self.code_row.connect("notify::selected", self._update_summary)
        for row in self.range_rows.values():
            row.connect("notify::value", self._update_summary)
        self._update_summary()

    def _remove_clicked(self, _button: Gtk.Button) -> None:
        self._on_remove(self)

    @staticmethod
    def _number_row(title: str, value: int, *, minimum: int = -2147483648) -> Adw.SpinRow:
        return Adw.SpinRow(
            title=title,
            adjustment=Gtk.Adjustment(
                value=value, lower=minimum, upper=2147483647, step_increment=1, page_increment=10
            ),
            digits=0,
        )

    def _update_summary(self, *_args: object) -> None:
        self.set_title(self.label_row.get_text() or "Unnamed control")
        code = self.codes[int(self.code_row.get_selected())].upper()
        if self.is_axis:
            minimum = int(self.range_rows["minimum"].get_value())
            maximum = int(self.range_rows["maximum"].get_value())
            rest = int(self.range_rows["rest"].get_value())
            code = f"{code} · {minimum} to {maximum} · rest {rest}"
        self.set_subtitle(code)

    def to_data(self) -> dict[str, object]:
        data: dict[str, object] = {
            "id": self.id_row.get_text(),
            "label": self.label_row.get_text(),
            "evdev": self.codes[int(self.code_row.get_selected())],
        }
        for key, row in self.range_rows.items():
            row.update()
            data[key] = int(row.get_value())
        return data
