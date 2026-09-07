from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import GLib, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from .model import Payload, list_of_dicts, text
from .motion_drawing import draw_controller, draw_rate
from .motion_model import MotionState


@dataclass
class MotionViewer:
    state: MotionState
    area: Gtk.DrawingArea
    status: Gtk.Label
    recenter: Gtk.Button
    rates: dict[str, tuple[Gtk.Label, Gtk.DrawingArea]] = field(default_factory=dict)
    details: dict[tuple[str, str], Gtk.Label] = field(default_factory=dict)
    last_frame_us: int = 0

    def draw(self, _area: Gtk.DrawingArea, cr: Any, width: int, height: int, _data: object) -> None:
        draw_controller(cr, width, height, self.state.display_orientation, self.state.ready)

    def draw_speed(
        self, _area: Gtk.DrawingArea, cr: Any, width: int, height: int, role: str
    ) -> None:
        draw_rate(cr, width, height, math.degrees(self.state.values.get(("gyro", role), 0.0)))

    def refresh(self) -> None:
        live = self.state.ready and GLib.get_monotonic_time() - self.last_frame_us < 1_000_000
        self.status.set_text(
            "Estimated orientation"
            if live
            else "Motion data paused"
            if self.state.ready
            else "Waiting for motion data"
        )
        self.recenter.set_sensitive(live)
        self.area.set_opacity(1.0 if live else 0.45)
        self.area.queue_draw()
        for role, (label, bar) in self.rates.items():
            value = self.state.values.get(("gyro", role))
            label.set_text(f"{math.degrees(value):+7.1f} °/s" if value is not None else "— °/s")
            label.set_opacity(1.0 if live else 0.45)
            bar.queue_draw()
        for (kind, role), label in self.details.items():
            value = self.state.values.get((kind, role))
            raw = self.state.raw.get((kind, role))
            unit = "rad/s" if kind == "gyro" else "m/s²"
            label.set_text(
                f"{role}: {value:+.3f} {unit} · raw {raw}"
                if value is not None
                else f"{role}: waiting"
            )


class MotionMixin:
    def _render_motion(self: Any, snapshot: Payload) -> None:
        self._cancel_motion_render()
        previous = dict(self._motion_viewers)
        self._motion_viewers.clear()
        sensors = list_of_dicts(snapshot.get("motion_sensors"))
        for sensor in sensors:
            section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            section.add_css_class("inspector-axis-section")
            title = Gtk.Label(label=text(sensor.get("label"), "Motion"))
            title.add_css_class("heading")
            title.set_wrap(True)
            section.append(title)
            area = Gtk.DrawingArea()
            area.set_content_width(220)
            area.set_content_height(175)
            area.set_halign(Gtk.Align.CENTER)
            section.append(area)
            status = Gtk.Label()
            status.add_css_class("caption")
            status.add_css_class("dim-label")
            section.append(status)
            recenter = Gtk.Button(label="Recenter preview")
            recenter.set_halign(Gtk.Align.CENTER)
            recenter.add_css_class("flat")
            recenter.set_tooltip_text(
                "Use this pose as the preview's reference. "
                "Does not change calibration or mappings. "
                "Heading is relative and may drift."
            )
            viewer = MotionViewer(MotionState(sensor), area, status, recenter)
            old = previous.get(text(sensor.get("id")))
            if old is not None and all(
                old.state.sensor.get(key) == sensor.get(key)
                for key in ("source", "gyro_axes", "accelerometer_axes")
            ):
                viewer.state = old.state
                viewer.last_frame_us = old.last_frame_us
            area.set_draw_func(viewer.draw, None)
            recenter.connect("clicked", self._recenter_motion, viewer)
            section.append(recenter)
            for role in ("pitch", "yaw", "roll"):
                if not any(
                    text(axis.get("role")) == role
                    for axis in list_of_dicts(sensor.get("gyro_axes"))
                ):
                    continue
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                name = Gtk.Label(label=role.title())
                name.set_xalign(0)
                name.set_width_chars(5)
                name.add_css_class("caption")
                row.append(name)
                bar = Gtk.DrawingArea()
                bar.set_content_width(55)
                bar.set_content_height(12)
                bar.set_hexpand(True)
                bar.set_valign(Gtk.Align.CENTER)
                bar.set_tooltip_text("Rotation speed, −360 to +360 °/s")
                bar.set_draw_func(viewer.draw_speed, role)
                row.append(bar)
                label = Gtk.Label()
                label.set_width_chars(12)
                label.set_xalign(1)
                label.add_css_class("caption")
                label.add_css_class("inspector-axis-value")
                row.append(label)
                viewer.rates[role] = label, bar
                section.append(row)
            details = Gtk.Expander(label="Sensor details")
            detail_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            for kind, heading in (("gyro", "Gyroscope"), ("accelerometer", "Accelerometer")):
                axes = [axis for axis_kind, axis in viewer.state.axes if axis_kind == kind]
                if not axes:
                    continue
                label = Gtk.Label(label=heading)
                label.set_xalign(0)
                label.add_css_class("caption")
                label.add_css_class("heading")
                detail_box.append(label)
                for axis in axes:
                    label = Gtk.Label()
                    label.set_xalign(0)
                    label.set_wrap(True)
                    label.add_css_class("caption")
                    label.add_css_class("inspector-axis-value")
                    detail_box.append(label)
                    viewer.details[kind, text(axis.get("role"))] = label
            details.set_child(detail_box)
            section.append(details)
            self._axes_box.append(section)
            self._motion_viewers[text(sensor.get("id"))] = viewer
            viewer.refresh()
        if any(viewer.state.ready for viewer in self._motion_viewers.values()):
            self._motion_render_source_id = GLib.timeout_add(33, self._flush_motion_render)

    def _recenter_motion(self: Any, _button: Gtk.Button, viewer: MotionViewer) -> None:
        viewer.state.recenter()
        viewer.refresh()

    def _update_motion_event(self: Any, event: Payload) -> None:
        for viewer in self._motion_viewers.values():
            if viewer.state.accept(event):
                viewer.last_frame_us = GLib.get_monotonic_time()
                if not self._motion_render_source_id:
                    self._motion_render_source_id = GLib.timeout_add(33, self._flush_motion_render)

    def _flush_motion_render(self: Any) -> bool:
        if self._session.closing:
            self._motion_render_source_id = 0
            return False
        keep = False
        for viewer in self._motion_viewers.values():
            viewer.refresh()
            keep = keep or GLib.get_monotonic_time() - viewer.last_frame_us < 1_000_000
        if not keep:
            self._motion_render_source_id = 0
        return keep

    def _cancel_motion_render(self: Any) -> None:
        if self._motion_render_source_id:
            GLib.source_remove(self._motion_render_source_id)
            self._motion_render_source_id = 0

    def _reset_motion(self: Any) -> None:
        self._cancel_motion_render()
        for viewer in self._motion_viewers.values():
            viewer.state.reset()
            viewer.last_frame_us = 0
            viewer.refresh()
