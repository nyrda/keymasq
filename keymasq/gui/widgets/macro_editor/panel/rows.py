"""Compact form rows shared by the macro editor inspector panels."""

import gi

# pyright: reportAttributeAccessIssue=false

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk, Pango  # pyright: ignore[reportAttributeAccessIssue]


class FieldRow(Gtk.Box):
    """One inspector line: an aligned label column followed by its controls."""

    def __init__(
        self,
        title: str,
        *controls: Gtk.Widget,
        subtitle: str = "",
        tooltip: str | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.title_label = Gtk.Label(label=title)
        self.title_label.set_halign(Gtk.Align.START)
        self.title_label.set_xalign(0.0)
        self.title_label.set_visible(bool(title))
        line.append(self.title_label)
        self.controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.controls.set_hexpand(True)
        for control in controls:
            control.set_valign(Gtk.Align.CENTER)
            self.controls.append(control)
        line.append(self.controls)
        self.append(line)
        self.subtitle_label = Gtk.Label()
        self.subtitle_label.add_css_class("dim-label")
        self.subtitle_label.add_css_class("caption")
        self.subtitle_label.set_halign(Gtk.Align.START)
        self.subtitle_label.set_xalign(0.0)
        self.subtitle_label.set_wrap(True)
        self.subtitle_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.append(self.subtitle_label)
        self.set_subtitle(subtitle)
        if tooltip:
            self.set_tooltip_text(tooltip)

    def set_title(self, title: str) -> None:
        self.title_label.set_label(title)
        self.title_label.set_visible(bool(title))

    def get_title(self) -> str:
        return self.title_label.get_label()

    def set_subtitle(self, subtitle: str) -> None:
        self.subtitle_label.set_label(subtitle)
        self.subtitle_label.set_visible(bool(subtitle))

    def get_subtitle(self) -> str:
        return self.subtitle_label.get_label()


def field_row(
    title: str,
    *controls: Gtk.Widget,
    subtitle: str = "",
    tooltip: str | None = None,
) -> FieldRow:
    return FieldRow(title, *controls, subtitle=subtitle, tooltip=tooltip)


def unit_label(text: str) -> Gtk.Label:
    label = Gtk.Label(label=text)
    label.add_css_class("dim-label")
    return label


def check_row(title: str, check: Gtk.CheckButton, tooltip: str | None = None) -> FieldRow:
    """Build a row whose only control is a labelled check button."""
    check.set_label(title)
    return FieldRow("", check, tooltip=tooltip)


def rows_list(*rows: Gtk.Widget) -> Gtk.Box:
    """Stack rows and align their label columns."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    size_group = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
    for row in rows:
        if isinstance(row, FieldRow) and row.get_title():
            size_group.add_widget(row.title_label)
        box.append(row)
    return box


def group_box(header: Gtk.Widget, *children: Gtk.Widget) -> Gtk.Box:
    """Stack a group header above its content."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    box.add_css_class("macro-inspector-group")
    box.append(header)
    for child in children:
        box.append(child)
    return box
