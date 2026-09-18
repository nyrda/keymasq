"""Compact form rows shared by the macro editor inspector panels."""

import gi

# pyright: reportAttributeAccessIssue=false

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk, Pango  # pyright: ignore[reportAttributeAccessIssue]

LABEL_COLUMN_CHARS = 10


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
        self.title_label = Gtk.Label(label=title)
        self.title_label.set_halign(Gtk.Align.END)
        self.title_label.set_xalign(1.0)
        # A shared floor keeps the control column at the same x across panels.
        self.title_label.set_width_chars(LABEL_COLUMN_CHARS)
        self.controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.controls.set_hexpand(True)
        for control in controls:
            control.set_valign(Gtk.Align.CENTER)
            self.controls.append(control)
        self.subtitle_label = Gtk.Label()
        self.subtitle_label.add_css_class("dim-label")
        self.subtitle_label.add_css_class("caption")
        self.subtitle_label.set_halign(Gtk.Align.START)
        self.subtitle_label.set_xalign(0.0)
        self.subtitle_label.set_wrap(True)
        self.subtitle_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        # Plain boxes, not a grid: a grid measures wrapping labels inconsistently.
        # The spacer shares the label column so the subtitle starts under the controls.
        line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        line.append(self.title_label)
        line.append(self.controls)
        self.append(line)
        self._subtitle_spacer = Gtk.Label()
        self._subtitle_spacer.set_width_chars(LABEL_COLUMN_CHARS)
        self._subtitle_line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._subtitle_line.append(self._subtitle_spacer)
        self.subtitle_label.set_hexpand(True)
        self._subtitle_line.append(self.subtitle_label)
        self.append(self._subtitle_line)
        self.set_subtitle(subtitle)
        if tooltip:
            self.set_tooltip_text(tooltip)
        self._size_group: Gtk.SizeGroup | None = None
        self._in_size_group = False
        self.connect("notify::visible", self._on_visible_changed)

    def bind_label_column(self, size_group: Gtk.SizeGroup) -> None:
        """Share the label column with other rows while this row is visible."""
        self._size_group = size_group
        self._on_visible_changed()

    def _on_visible_changed(self, *_args: object) -> None:
        # Size groups keep measuring hidden members, so hidden rows leave the group.
        if self._size_group is None:
            return
        visible = self.get_visible()
        self.title_label.set_visible(visible)
        if visible == self._in_size_group:
            return
        for member in (self.title_label, self._subtitle_spacer):
            if visible:
                self._size_group.add_widget(member)
            else:
                self._size_group.remove_widget(member)
        self._in_size_group = visible

    def set_title(self, title: str) -> None:
        self.title_label.set_label(title)

    def get_title(self) -> str:
        return self.title_label.get_label()

    def set_subtitle(self, subtitle: str) -> None:
        self.subtitle_label.set_label(subtitle)
        self._subtitle_line.set_visible(bool(subtitle))

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
    """Build a row whose only control is a labelled check button.

    The row keeps an empty label slot so the check lines up with the control column.
    """
    check.set_label(title)
    return FieldRow("", check, tooltip=tooltip)


def rows_list(*rows: Gtk.Widget) -> Gtk.Box:
    """Stack rows and align their label columns."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    size_group = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
    for row in rows:
        if isinstance(row, FieldRow):
            row.bind_label_column(size_group)
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
