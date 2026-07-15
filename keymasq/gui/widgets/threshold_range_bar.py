import math
from typing import Any

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # pyright: ignore[reportAttributeAccessIssue]


def _clamp(value: float, minimum: float = -1.0, maximum: float = 1.0) -> float:
    return max(minimum, min(maximum, float(value)))


def _threshold_ticks(minimum: float, maximum: float) -> tuple[tuple[float, str], ...]:
    if minimum == 0.0 and maximum == 1.0:
        return (
            (0.0, "0"),
            (0.25, "25%"),
            (0.5, "50%"),
            (0.75, "75%"),
            (1.0, "100%"),
        )
    if minimum == -1.0 and maximum == 1.0:
        return (
            (-1.0, "-100%"),
            (-0.5, "-50%"),
            (0.0, "0"),
            (0.5, "50%"),
            (1.0, "100%"),
        )
    span = maximum - minimum
    return tuple(
        (value, f"{value:g}") for value in (minimum + span * index / 4.0 for index in range(5))
    )


class ThresholdRangeBar(Gtk.DrawingArea):
    def __init__(self) -> None:
        super().__init__()
        self._minimum = -1.0
        self._maximum = 1.0
        self._trigger_min = 0.65
        self._trigger_max = 1.0
        self._release_min = 0.55
        self._release_max = 1.0
        self.set_content_width(300)
        self.set_content_height(48)
        self.set_hexpand(True)
        self.set_draw_func(self._draw, None)

    def set_domain(self, minimum: float, maximum: float) -> None:
        if minimum >= maximum:
            minimum = -1.0
            maximum = 1.0
        self._minimum = float(minimum)
        self._maximum = float(maximum)
        self._trigger_min = _clamp(self._trigger_min, self._minimum, self._maximum)
        self._trigger_max = _clamp(self._trigger_max, self._minimum, self._maximum)
        self._release_min = _clamp(self._release_min, self._minimum, self._maximum)
        self._release_max = _clamp(self._release_max, self._minimum, self._maximum)
        self.queue_draw()

    def set_ranges(
        self,
        trigger_min: float,
        trigger_max: float,
        release_min: float,
        release_max: float,
    ) -> None:
        self._trigger_min = _clamp(trigger_min, self._minimum, self._maximum)
        self._trigger_max = _clamp(trigger_max, self._minimum, self._maximum)
        self._release_min = _clamp(release_min, self._minimum, self._maximum)
        self._release_max = _clamp(release_max, self._minimum, self._maximum)
        self.queue_draw()

    def _x_for_value(self, value: float, left: float, width: float) -> float:
        normalized = (_clamp(value, self._minimum, self._maximum) - self._minimum) / (
            self._maximum - self._minimum
        )
        return left + normalized * width

    def _draw(
        self,
        _area: Gtk.DrawingArea,
        cr: Any,
        width: int,
        height: int,
        _data: object,
    ) -> None:
        del _area, _data
        left = 12.0
        right = 12.0
        track_w = max(1.0, float(width) - left - right)
        track_h = 10.0
        track_y = 14.0
        radius = track_h / 2.0

        cr.set_source_rgba(0.18, 0.18, 0.18, 0.75)
        _rounded_rect(cr, left, track_y, track_w, track_h, radius)
        cr.fill()

        release_x1 = self._x_for_value(self._release_min, left, track_w)
        release_x2 = self._x_for_value(self._release_max, left, track_w)
        cr.set_source_rgba(0.23, 0.50, 0.95, 0.35)
        _rounded_rect(
            cr,
            min(release_x1, release_x2),
            track_y,
            max(1.0, abs(release_x2 - release_x1)),
            track_h,
            radius,
        )
        cr.fill()

        trigger_x1 = self._x_for_value(self._trigger_min, left, track_w)
        trigger_x2 = self._x_for_value(self._trigger_max, left, track_w)
        cr.set_source_rgba(0.23, 0.50, 0.95, 0.95)
        _rounded_rect(
            cr,
            min(trigger_x1, trigger_x2),
            track_y,
            max(1.0, abs(trigger_x2 - trigger_x1)),
            track_h,
            radius,
        )
        cr.fill()

        cr.select_font_face("Sans", 0, 0)
        cr.set_font_size(9)
        cr.set_source_rgba(0.55, 0.55, 0.55, 1.0)
        tick_y1 = track_y + track_h + 4.0
        tick_y2 = tick_y1 + 5.0
        label_y = min(float(height) - 4.0, tick_y2 + 12.0)
        for value, label in _threshold_ticks(self._minimum, self._maximum):
            x = self._x_for_value(value, left, track_w)
            cr.move_to(x, tick_y1)
            cr.line_to(x, tick_y2)
            cr.stroke()
            extents = cr.text_extents(label)
            label_w = float(getattr(extents, "width", 0.0))
            cr.move_to(x - label_w / 2.0, label_y)
            cr.show_text(label)


def _rounded_rect(cr: Any, x: float, y: float, width: float, height: float, radius: float) -> None:
    right = x + width
    bottom = y + height
    cr.new_sub_path()
    cr.arc(right - radius, y + radius, radius, -math.pi / 2.0, 0)
    cr.arc(right - radius, bottom - radius, radius, 0, math.pi / 2.0)
    cr.arc(x + radius, bottom - radius, radius, math.pi / 2.0, math.pi)
    cr.arc(x + radius, y + radius, radius, math.pi, 3.0 * math.pi / 2.0)
    cr.close_path()
