"""Editable controls for virtual gaming device templates."""

from collections.abc import Callable

import evdev
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.virtual_device_templates import (
    STICK_SIDES,
    VirtualAxis,
    VirtualButton,
    VirtualStick,
)


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
        if name.startswith(prefix)
        and isinstance(code, int)
        and code in table
        and (not axis or name not in {"ABS_MAX", "ABS_CNT"})
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


class TemplateStickRow(Adw.ExpanderRow):
    """Pairs two axis rows into a stick. Axes are tracked by row so ID edits carry over."""

    def __init__(
        self,
        stick: VirtualStick,
        axis_rows: list[TemplateControlRow],
        on_remove: Callable[["TemplateStickRow"], None],
        on_axes_changed: Callable[["TemplateStickRow"], None] | None = None,
    ) -> None:
        super().__init__(title=stick.label)
        self._on_axes_changed = on_axes_changed
        # The editor may replace an ID it chose, but never one the user typed.
        self.id_is_generated = False
        self.fallback_id: str | None = None
        self._setting_id = False
        self.label_row = entry_row("Label", stick.label)
        self.id_row = entry_row("Stick ID", stick.id)
        self.id_row.set_tooltip_text("Stable ID used to refer to this stick in analog mappings")
        self.add_row(self.label_row)
        self._axis_rows: list[TemplateControlRow] = []
        self._selected: dict[str, TemplateControlRow | None] = {
            role: next((row for row in axis_rows if row.id_row.get_text() == axis_id), None)
            for role, axis_id in (("x", stick.x), ("y", stick.y))
        }
        self.axis_choice_rows = {
            "x": Adw.ComboRow(title="Horizontal axis"),
            "y": Adw.ComboRow(title="Vertical axis"),
        }
        for role, row in self.axis_choice_rows.items():
            row.connect("notify::selected", self._axis_selected, role)
            self.add_row(row)
        self.side_row = Adw.ComboRow(
            title="Side",
            subtitle="Left and Right stick mappings route to the stick declared for that side",
        )
        self.side_row.set_model(Gtk.StringList.new(["None", "Left", "Right"]))
        self.side_row.set_selected(STICK_SIDES.index(stick.side) + 1 if stick.side else 0)
        self.add_row(self.side_row)
        self.add_row(self.id_row)
        remove = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER)
        remove.add_css_class("flat")
        remove.set_tooltip_text(f"Remove {stick.label}")
        self._on_remove = on_remove
        remove.connect("clicked", self._remove_clicked)
        self.add_suffix(remove)
        self.label_row.connect("changed", self._update_summary)
        self.id_row.connect("changed", self._id_edited)
        self._syncing = False
        self.set_axis_rows(axis_rows)

    def _remove_clicked(self, _button: Gtk.Button) -> None:
        self._on_remove(self)

    def set_axis_rows(self, axis_rows: list[TemplateControlRow]) -> None:
        """Rebuild the axis choices after axes were added, removed, or renamed."""
        self._axis_rows = list(axis_rows)
        labels = [
            f"{row.label_row.get_text() or row.id_row.get_text()} · {row.id_row.get_text()}"
            for row in self._axis_rows
        ]
        self._syncing = True
        for role, choice in self.axis_choice_rows.items():
            choice.set_model(Gtk.StringList.new(labels))
            selected = self._selected[role]
            if selected in self._axis_rows:
                choice.set_selected(self._axis_rows.index(selected))
            else:
                self._selected[role] = None
                choice.set_selected(Gtk.INVALID_LIST_POSITION)
        self._syncing = False
        self._update_summary()

    def _axis_selected(self, choice: Adw.ComboRow, _param: object, role: str) -> None:
        if self._syncing:
            return
        index = int(choice.get_selected())
        self._selected[role] = self._axis_rows[index] if index < len(self._axis_rows) else None
        self._update_summary()
        if self._on_axes_changed is not None:
            self._on_axes_changed(self)

    def set_generated_id(self, stick_id: str) -> None:
        self._setting_id = True
        self.id_row.set_text(stick_id)
        self._setting_id = False
        self.id_is_generated = True

    def _id_edited(self, *_args: object) -> None:
        if not self._setting_id:
            self.id_is_generated = False

    def axis_id(self, role: str) -> str | None:
        row = self._selected[role]
        return row.id_row.get_text() if row is not None else None

    def axis_code(self, role: str) -> str | None:
        row = self._selected[role]
        return row.codes[int(row.code_row.get_selected())] if row is not None else None

    def _update_summary(self, *_args: object) -> None:
        self.set_title(self.label_row.get_text() or "Unnamed stick")
        self.set_subtitle(
            " / ".join(
                row.codes[int(row.code_row.get_selected())].upper() if row is not None else "?"
                for row in (self._selected["x"], self._selected["y"])
            )
        )

    def to_data(self) -> dict[str, object]:
        data: dict[str, object] = {
            "id": self.id_row.get_text(),
            "label": self.label_row.get_text(),
            "x": self.axis_id("x") or "",
            "y": self.axis_id("y") or "",
        }
        side = int(self.side_row.get_selected())
        if side > 0:
            data["side"] = STICK_SIDES[side - 1]
        return data
