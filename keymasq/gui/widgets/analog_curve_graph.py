from typing import Any

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.models import analog_gamepad_output_distance


class AnalogCurveGraph(Gtk.DrawingArea):
    def __init__(self) -> None:
        super().__init__()
        self._deadzone = 0.15
        self._sensitivity = 1.0
        self._response_curve = 1.0
        self.set_content_width(260)
        self.set_content_height(160)
        self.set_hexpand(True)
        self.set_draw_func(self._draw, None)

    def set_curve(
        self,
        *,
        deadzone: float,
        sensitivity: float,
        response_curve: float,
    ) -> None:
        self._deadzone = max(0.0, min(0.95, float(deadzone)))
        self._sensitivity = max(0.1, min(2.0, float(sensitivity)))
        self._response_curve = max(0.25, min(4.0, float(response_curve)))
        self.queue_draw()

    def _output_for_input(self, value: float) -> float:
        return analog_gamepad_output_distance(
            value,
            deadzone=self._deadzone,
            sensitivity=self._sensitivity,
            response_curve=self._response_curve,
        )

    def _draw(
        self,
        _area: Gtk.DrawingArea,
        cr: Any,
        width: int,
        height: int,
        _data: object,
    ) -> None:
        del _area, _data
        pad_left = 34.0
        pad_top = 12.0
        pad_right = 14.0
        pad_bottom = 26.0
        graph_w = max(1.0, float(width) - pad_left - pad_right)
        graph_h = max(1.0, float(height) - pad_top - pad_bottom)
        x0 = pad_left
        y0 = pad_top + graph_h

        cr.set_source_rgba(0.09, 0.10, 0.11, 0.75)
        cr.rectangle(pad_left, pad_top, graph_w, graph_h)
        cr.fill()

        deadzone_w = graph_w * self._deadzone
        if deadzone_w > 0.0:
            cr.set_source_rgba(0.35, 0.35, 0.35, 0.18)
            cr.rectangle(pad_left, pad_top, deadzone_w, graph_h)
            cr.fill()

        cr.set_line_width(1.0)
        cr.set_source_rgba(0.45, 0.45, 0.45, 0.45)
        for tick in (0.0, 0.5, 1.0):
            x = x0 + graph_w * tick
            y = y0 - graph_h * tick
            cr.move_to(x, pad_top)
            cr.line_to(x, y0)
            cr.move_to(x0, y)
            cr.line_to(x0 + graph_w, y)
            cr.stroke()

        cr.set_source_rgba(0.70, 0.70, 0.70, 0.35)
        cr.move_to(x0, y0)
        cr.line_to(x0 + graph_w, pad_top)
        cr.stroke()

        cr.set_line_width(2.5)
        cr.set_source_rgba(0.25, 0.66, 0.95, 1.0)
        for step in range(101):
            input_value = float(step) / 100.0
            output_value = self._output_for_input(input_value)
            x = x0 + graph_w * input_value
            y = y0 - graph_h * output_value
            if step == 0:
                cr.move_to(x, y)
            else:
                cr.line_to(x, y)
        cr.stroke()

        saturation_x = _saturation_input(
            deadzone=self._deadzone,
            sensitivity=self._sensitivity,
            response_curve=self._response_curve,
        )
        if saturation_x is not None:
            x = x0 + graph_w * saturation_x
            cr.set_line_width(1.0)
            cr.set_source_rgba(0.25, 0.66, 0.95, 0.65)
            cr.move_to(x, pad_top)
            cr.line_to(x, y0)
            cr.stroke()

        cr.select_font_face("Sans", 0, 0)
        cr.set_font_size(9)
        cr.set_source_rgba(0.70, 0.70, 0.70, 0.95)
        _label(cr, "Input", x0 + graph_w - 28.0, y0 - 6.0)
        _label(cr, "Output", x0 + 4.0, pad_top + 11.0)
        _label(cr, "0", x0 - 5.0, y0 + 17.0)
        _label(cr, "100%", x0 + graph_w - 24.0, y0 + 17.0)


def _saturation_input(
    *,
    deadzone: float,
    sensitivity: float,
    response_curve: float,
) -> float | None:
    if sensitivity <= 1.0:
        return None
    normalized = (1.0 / sensitivity) ** (1.0 / response_curve)
    return max(0.0, min(1.0, deadzone + normalized * (1.0 - deadzone)))


def _label(cr: Any, text: str, x: float, y: float) -> None:
    cr.move_to(x, y)
    cr.show_text(text)
