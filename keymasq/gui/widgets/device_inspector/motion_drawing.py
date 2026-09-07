"""Small projected controller and signed rate bars, drawn with GTK's Cairo context."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import cairo

from .motion_model import Quaternion, Vector, rotate

Point = tuple[float, float]
_FACE_Z = 0.14
_VIEW_SIN = math.sin(math.radians(52))
_VIEW_COS = math.cos(math.radians(52))


def _shell_outline() -> tuple[Point, ...]:
    """Sample a curved right grip and reflect it to make a continuous shell."""
    curves = (
        ((0.26, 0.58), (0.54, 0.64), (0.73, 0.55)),
        ((0.87, 0.49), (0.94, 0.32), (0.98, 0.10)),
        ((1.02, -0.10), (1.13, -0.48), (1.04, -0.68)),
        ((0.99, -0.85), (0.82, -0.91), (0.71, -0.76)),
        ((0.62, -0.64), (0.52, -0.35), (0.40, -0.30)),
        ((0.24, -0.25), (0.15, -0.29), (0.0, -0.29)),
    )
    start = (0.0, 0.58)
    right = [start]
    for control_a, control_b, end in curves:
        for step in range(1, 9):
            t = step / 8
            u = 1 - t
            right.append(
                (
                    u**3 * start[0]
                    + 3 * u * u * t * control_a[0]
                    + 3 * u * t * t * control_b[0]
                    + t**3 * end[0],
                    u**3 * start[1]
                    + 3 * u * u * t * control_a[1]
                    + 3 * u * t * t * control_b[1]
                    + t**3 * end[1],
                )
            )
        start = end
    return tuple(right + [(-x, y) for x, y in reversed(right[1:-1])])


_OUTLINE = _shell_outline()
# Rounded bevels join the face to the underside without exposed polygon edges.
_RINGS = tuple(
    tuple((x * inset, y * inset, z) for x, y in _OUTLINE)
    for inset, z in ((0.94, _FACE_Z), (0.987, 0.10), (1.0, 0.0), (0.977, -0.11), (0.92, -0.16))
)


@dataclass(frozen=True)
class _Face:
    points: tuple[Vector, ...]
    normal: Vector
    surface: str = "side"


def _shell_faces() -> tuple[_Face, ...]:
    faces = [
        _Face(_RINGS[0], (0.0, 0.0, 1.0), "top"),
        _Face(_RINGS[-1], (0.0, 0.0, -1.0), "bottom"),
    ]
    for upper, lower in zip(_RINGS, _RINGS[1:], strict=False):
        for index in range(len(_OUTLINE)):
            following = (index + 1) % len(_OUTLINE)
            points = upper[index], upper[following], lower[following], lower[index]
            ax, ay, az = (points[1][i] - points[0][i] for i in range(3))
            bx, by, bz = (points[3][i] - points[0][i] for i in range(3))
            nx, ny, nz = ay * bz - az * by, az * bx - ax * bz, ax * by - ay * bx
            length = math.sqrt(nx * nx + ny * ny + nz * nz)
            faces.append(_Face(points, (nx / length, ny / length, nz / length)))
    return tuple(faces)


_SHELL = _shell_faces()


class _ControllerDrawing:
    def __init__(self, cr: Any, width: int, height: int, q: Quaternion):
        self.cr = cr
        self.scale = min(width / 2.55, height / 2.5)
        self.cx, self.cy = width / 2.0, height / 2.0
        self.q = q

    def project(self, point: Vector, *, fixed: bool = False) -> Vector:
        x, y, z = point if fixed else rotate(self.q, point)
        return (
            self.cx + self.scale * x,
            self.cy - self.scale * (_VIEW_SIN * y + _VIEW_COS * z),
            _VIEW_COS * y - _VIEW_SIN * z,
        )

    def path(self, points: tuple[Vector, ...], *, fixed: bool = False) -> None:
        for index, point in enumerate(points):
            x, y, _depth = self.project(point, fixed=fixed)
            if index:
                self.cr.line_to(x, y)
            else:
                self.cr.move_to(x, y)
        self.cr.close_path()

    def gradient(self, light: float, dark: float) -> None:
        gradient = cairo.LinearGradient(
            self.cx - self.scale,
            self.cy - self.scale,
            self.cx + self.scale * 0.6,
            self.cy + self.scale,
        )
        gradient.add_color_stop_rgb(0, light, light + 0.006, light + 0.014)
        gradient.add_color_stop_rgb(1, dark, dark + 0.006, dark + 0.014)
        self.cr.set_source(gradient)

    def shell(self) -> None:
        cr = self.cr
        # A stationary ellipse gives the pose a reference without a grid behind it.
        self.path(
            tuple(
                (1.15 * math.cos(i * math.tau / 96), 0.75 * math.sin(i * math.tau / 96), -0.29)
                for i in range(96)
            ),
            fixed=True,
        )
        cr.set_source_rgba(0.60, 0.62, 0.65, 0.10)
        cr.set_line_width(0.8)
        cr.stroke()

        projected = {point: self.project(point) for ring in _RINGS for point in ring}
        visible: list[tuple[float, _Face, Vector]] = []
        for face in _SHELL:
            normal = rotate(self.q, face.normal)
            if _VIEW_COS * normal[1] - _VIEW_SIN * normal[2] < -0.001:
                depth = sum(projected[p][2] for p in face.points) / len(face.points)
                visible.append((depth, face, normal))
        visible.sort(key=lambda item: item[0], reverse=True)
        for _depth, face, normal in visible:
            for index, point in enumerate(face.points):
                x, y, _ = projected[point]
                if index:
                    cr.line_to(x, y)
                else:
                    cr.move_to(x, y)
            cr.close_path()
            light = max(0.0, -0.35 * normal[0] - 0.25 * normal[1] + 0.90 * normal[2])
            if face.surface == "top":
                self.gradient(0.25 + 0.065 * light, 0.19 + 0.045 * light)
            else:
                shade = 0.15 + 0.13 * light
                cr.set_source_rgb(shade, shade + 0.006, shade + 0.014)
            cr.fill_preserve()
            # Overlap adjacent fills to avoid antialiasing cracks. No wireframe strokes.
            cr.set_line_width(0.65)
            cr.set_line_join(cairo.LINE_JOIN_ROUND)
            cr.stroke()
            if face.surface == "top":
                cr.save()
                self.path(face.points)
                cr.clip()
                self.controls()
                cr.restore()

    def disc(self, x: float, y: float, radius: float, z: float, light: float, dark: float) -> None:
        self.path(
            tuple(
                (
                    x + radius * math.cos(i * math.tau / 32),
                    y + radius * math.sin(i * math.tau / 32),
                    z,
                )
                for i in range(32)
            )
        )
        self.gradient(light, dark)
        self.cr.fill_preserve()
        self.cr.set_source_rgba(0.62, 0.64, 0.67, 0.16)
        self.cr.set_line_width(0.65)
        self.cr.stroke()

    def dpad(self) -> None:
        # Positioned inward and below the left stick, fully inside the face bridge.
        cx, cy = -0.34, -0.045
        arm, half_width = 0.145, 0.052
        outline = (
            (-half_width, arm),
            (half_width, arm),
            (half_width, half_width),
            (arm, half_width),
            (arm, -half_width),
            (half_width, -half_width),
            (half_width, -arm),
            (-half_width, -arm),
            (-half_width, -half_width),
            (-arm, -half_width),
            (-arm, half_width),
            (-half_width, half_width),
        )
        cr = self.cr
        # Round the cross in controller coordinates so its corners follow the pose.
        for index, (x, y) in enumerate(outline):
            previous, following = outline[index - 1], outline[(index + 1) % len(outline)]
            incoming = math.hypot(previous[0] - x, previous[1] - y)
            outgoing = math.hypot(following[0] - x, following[1] - y)
            entry = (
                x + (previous[0] - x) * 0.014 / incoming,
                y + (previous[1] - y) * 0.014 / incoming,
            )
            leave = (
                x + (following[0] - x) * 0.014 / outgoing,
                y + (following[1] - y) * 0.014 / outgoing,
            )
            start = self.project((cx + entry[0], cy + entry[1], _FACE_Z + 0.008))
            corner = self.project((cx + x, cy + y, _FACE_Z + 0.008))
            end = self.project((cx + leave[0], cy + leave[1], _FACE_Z + 0.008))
            if index:
                cr.line_to(*start[:2])
            else:
                cr.move_to(*start[:2])
            cr.curve_to(
                start[0] + (corner[0] - start[0]) * 2 / 3,
                start[1] + (corner[1] - start[1]) * 2 / 3,
                end[0] + (corner[0] - end[0]) * 2 / 3,
                end[1] + (corner[1] - end[1]) * 2 / 3,
                *end[:2],
            )
        cr.close_path()
        self.gradient(0.15, 0.075)
        cr.fill_preserve()
        cr.set_source_rgba(0.62, 0.64, 0.67, 0.21)
        cr.set_line_width(0.7)
        cr.stroke()

    def controls(self) -> None:
        for x, y in ((-0.57, 0.28), (0.34, -0.045)):
            self.disc(x, y, 0.169, _FACE_Z + 0.002, 0.11, 0.065)
            self.disc(x, y, 0.124, _FACE_Z + 0.035, 0.24, 0.12)
        self.dpad()
        for x, y in ((0.59, 0.42), (0.73, 0.28), (0.59, 0.14), (0.45, 0.28)):
            self.disc(x, y, 0.054, _FACE_Z + 0.01, 0.18, 0.085)
        for x in (-0.14, 0.14):
            self.disc(x, 0.30, 0.025, _FACE_Z + 0.005, 0.20, 0.11)
        cr = self.cr
        cr.set_line_width(2)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.move_to(*self.project((-0.10, 0.515, _FACE_Z + 0.005))[:2])
        cr.line_to(*self.project((0.10, 0.515, _FACE_Z + 0.005))[:2])
        cr.set_source_rgb(0.39, 0.78, 0.63)
        cr.stroke()


def draw_controller(cr: Any, width: int, height: int, q: Quaternion, ready: bool) -> None:
    cr.save()
    if not ready:
        cr.push_group()
    _ControllerDrawing(cr, width, height, q).shell()
    if not ready:
        cr.pop_group_to_source()
        cr.paint_with_alpha(0.45)
    cr.restore()


def draw_rate(cr: Any, width: int, height: int, value: float) -> None:
    center = width / 2.0
    cr.set_line_width(4)
    cr.set_source_rgba(0.5, 0.5, 0.5, 0.25)
    cr.move_to(0, height / 2)
    cr.line_to(width, height / 2)
    cr.stroke()
    cr.set_source_rgba(0.39, 0.83, 0.66, 0.85)
    cr.move_to(center, height / 2)
    cr.line_to(center + max(-1.0, min(1.0, value / 360.0)) * center, height / 2)
    cr.stroke()
    cr.set_line_width(1)
    cr.set_source_rgba(0.6, 0.6, 0.6, 0.7)
    cr.move_to(center, 1)
    cr.line_to(center, height - 1)
    cr.stroke()
