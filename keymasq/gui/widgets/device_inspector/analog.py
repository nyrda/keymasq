from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # pyright: ignore[reportAttributeAccessIssue]

from .model import Payload, axis_value_label, level_bar_value, list_of_dicts, normalize_axis, text


@dataclass
class AnalogViewer:
    analog_id: str
    analog_type: str
    axes: dict[str, Payload]
    value_labels: dict[str, Gtk.Label] = field(default_factory=dict)
    drawing_area: Gtk.DrawingArea | None = None
    level_bar: Gtk.LevelBar | None = None
    normalized: dict[str, float] = field(default_factory=dict)


class AnalogMixin:
    def _render_axes(self: Any, snapshot: Payload) -> None:
        self._clear_box(self._axes_box)
        self._analog_viewers.clear()
        analogs = list_of_dicts(snapshot.get("analog_inputs"))
        self._axes_title.set_visible(bool(analogs))
        self._axes_box.set_visible(bool(analogs))
        if not analogs:
            return

        for analog in analogs:
            viewer = self._create_analog_viewer(analog)
            self._analog_viewers[viewer.analog_id] = viewer

    def _create_analog_viewer(self: Any, analog: Payload) -> AnalogViewer:
        analog_id = text(analog.get("id"))
        analog_type = text(analog.get("type"), "axis").lower()
        axes = {
            text(axis.get("role")): axis
            for axis in list_of_dicts(analog.get("axes"))
            if text(axis.get("role"))
        }
        viewer = AnalogViewer(
            analog_id=analog_id,
            analog_type=analog_type,
            axes=axes,
            normalized={role: normalize_axis(axis, 0, analog_type) for role, axis in axes.items()},
        )

        section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        section.add_css_class("inspector-axis-section")
        title = Gtk.Label(label=text(analog.get("label"), analog_id))
        title.add_css_class("heading")
        title.set_halign(Gtk.Align.CENTER)
        section.append(title)

        if analog_type == "stick" and {"x", "y"} <= set(axes):
            area = Gtk.DrawingArea()
            area.set_content_width(150)
            area.set_content_height(150)
            area.set_halign(Gtk.Align.CENTER)
            area.add_css_class("inspector-axis-pad")

            def draw_stick(
                _area: Gtk.DrawingArea,
                cr: Any,
                width: int,
                height: int,
                _data: object,
            ) -> None:
                self._draw_stick(viewer, cr, width, height)

            area.set_draw_func(draw_stick, None)
            viewer.drawing_area = area
            section.append(area)
        else:
            bar = Gtk.LevelBar()
            bar.set_min_value(0.0)
            bar.set_max_value(1.0)
            first_role = sorted(axes)[0] if axes else ""
            bar.set_value(level_bar_value(analog_type, viewer.normalized.get(first_role, 0.0)))
            bar.add_css_class("inspector-axis-bar")
            bar.set_halign(Gtk.Align.CENTER)
            bar.set_size_request(220, -1)
            viewer.level_bar = bar
            section.append(bar)

        values = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        values.set_halign(Gtk.Align.CENTER)
        for role in sorted(axes):
            label = Gtk.Label(label=axis_value_label(role, 0, viewer.normalized.get(role, 0.0)))
            label.add_css_class("caption")
            label.add_css_class("dim-label")
            label.add_css_class("inspector-axis-value")
            label.set_xalign(0.5)
            label.set_width_chars(29)
            label.set_max_width_chars(29)
            values.append(label)
            viewer.value_labels[role] = label
        section.append(values)
        self._axes_box.append(section)
        return viewer

    def _draw_stick(
        self: Any,
        viewer: AnalogViewer,
        cr: Any,
        width: int,
        height: int,
    ) -> None:
        size = min(width, height)
        cx = width / 2.0
        cy = height / 2.0
        radius = max(10.0, (size / 2.0) - 10.0)

        cr.set_line_width(1.0)
        cr.set_source_rgba(0.45, 0.45, 0.45, 0.28)
        cr.arc(cx, cy, radius, 0, 6.28318)
        cr.fill_preserve()
        cr.set_source_rgba(0.45, 0.45, 0.45, 0.55)
        cr.stroke()

        cr.set_source_rgba(0.45, 0.45, 0.45, 0.35)
        cr.move_to(cx - radius, cy)
        cr.line_to(cx + radius, cy)
        cr.move_to(cx, cy - radius)
        cr.line_to(cx, cy + radius)
        cr.stroke()

        x = max(-1.0, min(1.0, viewer.normalized.get("x", 0.0)))
        y = max(-1.0, min(1.0, viewer.normalized.get("y", 0.0)))
        cr.set_source_rgba(0.0, 0.45, 0.85, 0.95)
        cr.arc(cx + x * radius, cy + y * radius, 6.0, 0, 6.28318)
        cr.fill()

    def _update_analog_value(self: Any, analog_id: str, role: str, value: int) -> None:
        viewer = self._analog_viewers.get(analog_id)
        if viewer is None:
            return
        axis = viewer.axes.get(role)
        if axis is None:
            return
        normalized = normalize_axis(axis, value, viewer.analog_type)
        viewer.normalized[role] = normalized
        value_label = viewer.value_labels.get(role)
        if value_label is not None:
            value_label.set_text(axis_value_label(role, value, normalized))
        if viewer.drawing_area is not None:
            viewer.drawing_area.queue_draw()
        if viewer.level_bar is not None:
            viewer.level_bar.set_value(level_bar_value(viewer.analog_type, normalized))
