from typing import cast

import gi

# pyright: reportUnusedFunction=false

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # pyright: ignore[reportAttributeAccessIssue]


def _make_capture_status_row(status_label: Gtk.Label) -> Gtk.Box:
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    row.set_halign(Gtk.Align.START)
    row.set_margin_top(4)
    dot = Gtk.Box()
    dot.add_css_class("capture-recording-dot")
    dot.set_size_request(10, 10)
    dot.set_valign(Gtk.Align.CENTER)
    dot.set_visible(False)
    status_label._capture_recording_dot = dot
    row.append(dot)
    row.append(status_label)
    return row


def _set_capture_status(
    status_label: Gtk.Label,
    text: object,
    *,
    recording: bool = False,
) -> None:
    status_label.set_text(str(text))
    dot = getattr(status_label, "_capture_recording_dot", None)
    if dot is None:
        return
    cast(Gtk.Widget, dot).set_visible(recording)


def make_unlock_button_content(label: str) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    icon = Gtk.Image.new_from_icon_name("channel-insecure-symbolic")
    box.append(icon)
    lbl = Gtk.Label(label=label)
    box.append(lbl)
    return box
