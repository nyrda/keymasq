"""Unbranded flight stick artwork drawn as scalable, theme-aware paths."""

import math
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # pyright: ignore[reportAttributeAccessIssue]


class FlightStickDrawing(Gtk.DrawingArea):
    def __init__(self) -> None:
        super().__init__(content_width=220, content_height=240)
        self.set_draw_func(self._draw, None)
        self.set_tooltip_text("Flight stick with twist, throttle, and hat switch")

    def _draw(self, area: Gtk.DrawingArea, cr: Any, width: int, height: int, _data: object) -> None:
        foreground = area.get_color()
        found, background = area.get_style_context().lookup_color("window_bg_color")
        dark = foreground.red + foreground.green + foreground.blue > 1.5
        bg = (
            (background.red, background.green, background.blue)
            if found
            else ((0.13,) * 3 if dark else (0.98,) * 3)
        )
        fg = (foreground.red, foreground.green, foreground.blue)
        shell, plate, edge = (0.045, 0.25, 0.48) if dark else (0.78, 0.20, 0.85)
        cr.save()
        scale = min(width / 300, height / 365)
        cr.translate((width - 300 * scale) / 2, (height - 365 * scale) / 2)
        cr.scale(scale, scale)
        cr.set_line_join(1)
        cr.set_line_cap(1)

        def ink(tone: float) -> None:
            cr.set_source_rgb(*(b + (f - b) * tone for b, f in zip(bg, fg, strict=True)))

        def path(*segments: tuple[float, ...]) -> None:
            cr.new_path()
            for index, segment in enumerate(segments):
                if not segment:
                    cr.close_path()
                elif len(segment) == 6:
                    cr.curve_to(*segment)
                elif index == 0:
                    cr.move_to(*segment)
                else:
                    cr.line_to(*segment)

        def finish(tone: float, outline: float = edge, line_width: float = 1.5) -> None:
            ink(tone)
            cr.fill_preserve()
            ink(outline)
            cr.set_line_width(line_width)
            cr.stroke()

        def ellipse(
            x: float, y: float, rx: float, ry: float, tone: float, outline: float = edge
        ) -> None:
            cr.new_path()
            cr.save()
            cr.translate(x, y)
            cr.scale(rx, ry)
            cr.arc(0, 0, 1, 0, math.tau)
            cr.restore()
            finish(tone, outline)

        # Four broad feet wrap around the raised top plate. Interior subpaths
        # punch real holes through the frame rather than painting over it.
        path(
            (28, 204),
            (53, 181, 85, 172, 111, 174),
            (181, 168, 219, 180, 241, 201),
            (253, 219),
            (253, 251),
            (269, 288),
            (278, 322, 266, 340, 250, 345),
            (204, 349),
            (179, 337),
            (110, 339),
            (87, 353),
            (43, 350, 18, 341, 15, 326),
            (23, 278),
            (20, 248),
            (12, 225, 18, 214, 28, 204),
            (),
        )
        # Front-left, front-right, and the two smaller rear openings.
        for points in (
            ((30, 292), (74, 316), (64, 336), (31, 329), (25, 317)),
            ((235, 285), (256, 291), (266, 317), (250, 334), (217, 332), (211, 320)),
            ((28, 212), (51, 193), (69, 191), (53, 210), (26, 231)),
            ((218, 194), (236, 207), (245, 228), (244, 241), (229, 220)),
        ):
            cr.move_to(*points[0])
            for point in points[1:]:
                cr.line_to(*point)
            cr.close_path()
        cr.set_fill_rule(1)
        finish(shell, edge, 2.2)
        cr.set_fill_rule(0)
        # A visible lower lip gives the base thickness.
        path((19, 329), (38, 343, 68, 345, 85, 346), (109, 333), (179, 331), (204, 343), (252, 339))
        ink(edge)
        cr.set_line_width(1)
        cr.stroke()

        # Sculpted top plate, wider at the shoulders and tapered at the front.
        path(
            (49, 207),
            (76, 188, 112, 178, 144, 180),
            (180, 177, 210, 188, 226, 206),
            (239, 235, 241, 263, 232, 281),
            (215, 300, 205, 314, 190, 325),
            (150, 333, 117, 332, 91, 324),
            (71, 311, 48, 294, 39, 278),
            (32, 253, 36, 222, 49, 207),
            (),
        )
        finish(plate, edge, 1.8)
        # Narrow highlights follow the bevel instead of outlining a flat slab.
        path((48, 210), (80, 192, 106, 186, 130, 185))
        ink(0.50 if dark else 0.08)
        cr.set_line_width(2)
        cr.stroke()
        path((228, 224), (239, 256, 233, 276, 214, 298), (190, 322))
        ink(0.14 if dark else 0.38)
        cr.set_line_width(3)
        cr.stroke()

        # The six base switches form two concentric banks to the left of the
        # stick, as in the reference, rather than being scattered across it.
        for start in (153, 178, 203):
            for inner, outer in ((0.72, 0.92), (0.98, 1.18)):
                cr.new_path()
                cr.save()
                cr.translate(145, 242)
                cr.scale(78, 59)
                cr.arc(0, 0, outer, math.radians(start), math.radians(start + 20))
                cr.arc_negative(0, 0, inner, math.radians(start + 20), math.radians(start))
                cr.close_path()
                cr.restore()
                finish(shell, 0.48 if dark else 0.65, 1)
        # Offset socket, stepped bearing, and rubber gaiter beneath the grip.
        ellipse(153, 235, 57, 36, shell)
        ellipse(153, 229, 51, 30, shell + (0.05 if dark else -0.08))
        ellipse(154, 219, 38, 23, shell)
        path((124, 219), (140, 228, 170, 227, 184, 217))
        ink(edge)
        cr.set_line_width(1)
        cr.stroke()

        # Front throttle sits in a recessed horizontal slot with a raised tab.
        path(
            (102, 297),
            (118, 286, 163, 284, 180, 292),
            (184, 300, 178, 305, 171, 307),
            (107, 309),
            (98, 307, 96, 302, 102, 297),
            (),
        )
        finish(plate * 0.65, plate, 1)
        path((110, 297), (169, 294), (171, 302), (108, 305), ())
        finish(shell, edge, 1)
        path((118, 291), (147, 288), (150, 309), (119, 312), (113, 307, 114, 296, 118, 291), ())
        finish(shell, edge, 1.5)
        for y in (298, 302, 306):
            path((120, y), (143, y - 2))
            ink(edge)
            cr.set_line_width(0.8)
            cr.stroke()

        # Flared palm shelf with a lower rim; the grip rises from its left side.
        path(
            (139, 194),
            (163, 190, 178, 202, 191, 218),
            (199, 226, 206, 236, 201, 241),
            (181, 250, 144, 241, 127, 230),
            (120, 223, 128, 204, 139, 194),
            (),
        )
        finish(shell, edge, 1.8)
        path((128, 230), (145, 238, 181, 246, 200, 238))
        ink(0.25 if dark else 0.6)
        cr.set_line_width(2)
        cr.stroke()

        # Tapered ergonomic grip, leaning slightly forward. Its highlights and
        # side seam describe the volume without the old horizontal finger lines.
        path(
            (134, 78),
            (126, 66, 123, 41, 130, 27),
            (138, 11, 173, 13, 187, 26),
            (203, 42, 194, 71, 183, 91),
            (174, 116, 168, 161, 172, 187),
            (172, 198, 183, 211, 183, 218),
            (176, 228, 149, 224, 140, 215),
            (136, 200, 141, 179, 136, 156),
            (132, 133, 122, 107, 126, 92),
            (134, 78),
            (),
        )
        finish(shell, edge, 1.9)
        path(
            (180, 89),
            (169, 119, 163, 160, 166, 191),
            (165, 203, 175, 215, 177, 216),
            (170, 219, 164, 216, 161, 210),
            (155, 179, 160, 119, 172, 91),
            (),
        )
        finish(0.15 if dark else 0.61, 0.15 if dark else 0.61, 0.5)
        path((136, 95), (133, 118, 146, 157, 145, 184), (144, 209))
        ink(0.2 if dark else 0.9)
        cr.set_line_width(1)
        cr.stroke()
        # Flush side button follows the grip contour below the head.
        path(
            (133, 83),
            (129, 89, 127, 96, 128, 103),
            (130, 108, 137, 107, 139, 101),
            (140, 95, 136, 87, 133, 83),
            (),
        )
        finish(shell + (0.12 if dark else -0.18), shell, 0.6)
        # Top trigger tip.
        path((150, 17), (151, 11), (166, 10), (169, 16), ())
        finish(shell, edge, 1)

        # Inset face: one hat and four shaped buttons, not generic circles.
        path(
            (137, 28),
            (149, 21, 172, 23, 181, 33),
            (190, 43, 185, 65, 175, 77),
            (168, 88, 153, 90, 143, 79),
            (133, 65, 128, 40, 137, 28),
            (),
        )
        finish(shell + (0.03 if dark else -0.03), 0.28 if dark else 0.9, 1)
        ellipse(158, 48, 12, 10, shell, edge)
        ellipse(157, 45, 8, 7, shell + (0.14 if dark else -0.14), edge)
        for segments in (
            ((135, 47), (141, 45), (145, 57), (139, 61), (135, 56), ()),
            ((176, 45), (182, 46), (180, 60), (174, 58), (173, 51), ()),
            ((140, 65), (147, 60), (156, 72), (151, 80), (146, 77), ()),
            ((171, 61), (178, 65), (174, 77), (167, 82), (161, 75), ()),
        ):
            path(*segments)
            finish(0.62 if dark else 0.22, edge, 1)
        cr.restore()
