from collections.abc import Callable

import pytest

pytest.importorskip("gi")

from keymasq.gui.widgets.threshold_range_bar import ThresholdRangeBar

type _DrawFunc = Callable[[object, object, int, int, object], None]


class _DrawableThresholdRangeBar(ThresholdRangeBar):
    draw_func: _DrawFunc | None
    draw_data: object

    def __init__(self) -> None:
        self.draw_func = None
        self.draw_data = None
        super().__init__()

    def set_draw_func(self, draw_func: _DrawFunc | None, user_data: object) -> None:
        self.draw_func = draw_func
        self.draw_data = user_data
        super().set_draw_func(draw_func, user_data)


class _TextExtents:
    def __init__(self, width: float) -> None:
        self.width = width


class _RecordingContext:
    def __init__(self) -> None:
        self.fills: list[tuple[float, float, float, float]] = []
        self.lines: list[tuple[float, float, float, float]] = []
        self.labels: list[str] = []
        self._pending_arcs: list[tuple[float, float, float]] = []
        self._current_point: tuple[float, float] | None = None

    def set_source_rgba(self, _red: float, _green: float, _blue: float, _alpha: float) -> None:
        pass

    def new_sub_path(self) -> None:
        self._pending_arcs = []

    def arc(
        self,
        center_x: float,
        center_y: float,
        radius: float,
        _angle1: float,
        _angle2: float,
    ) -> None:
        self._pending_arcs.append((center_x, center_y, radius))

    def close_path(self) -> None:
        pass

    def fill(self) -> None:
        if not self._pending_arcs:
            return
        left = min(center_x - radius for center_x, _center_y, radius in self._pending_arcs)
        top = min(center_y - radius for _center_x, center_y, radius in self._pending_arcs)
        right = max(center_x + radius for center_x, _center_y, radius in self._pending_arcs)
        bottom = max(center_y + radius for _center_x, center_y, radius in self._pending_arcs)
        self.fills.append((left, top, right - left, bottom - top))
        self._pending_arcs = []

    def select_font_face(self, _family: str, _slant: int, _weight: int) -> None:
        pass

    def set_font_size(self, _size: int) -> None:
        pass

    def move_to(self, x: float, y: float) -> None:
        self._current_point = (x, y)

    def line_to(self, x: float, y: float) -> None:
        if self._current_point is None:
            return
        start_x, start_y = self._current_point
        self.lines.append((start_x, start_y, x, y))

    def stroke(self) -> None:
        pass

    def text_extents(self, label: str) -> _TextExtents:
        return _TextExtents(width=float(len(label)) * 4.0)

    def show_text(self, label: str) -> None:
        self.labels.append(label)


def _draw_threshold_range_bar(
    bar: _DrawableThresholdRangeBar, width: int = 300, height: int = 48
) -> _RecordingContext:
    context = _RecordingContext()
    assert bar.draw_func is not None
    bar.draw_func(bar, context, width, height, bar.draw_data)
    return context


def test_threshold_range_bar_draws_ranges_inside_new_domain() -> None:
    bar = _DrawableThresholdRangeBar()
    bar.set_ranges(-1.0, 1.0, -0.8, 0.8)

    bar.set_domain(0.0, 1.0)

    context = _draw_threshold_range_bar(bar)
    assert len(context.fills) == 3

    track_left, _track_top, track_width, _track_height = context.fills[0]
    track_right = track_left + track_width
    overlay_fills = context.fills[1:]

    for left, _top, width, _height in overlay_fills:
        assert track_left <= left <= track_right
        assert track_left <= left + width <= track_right


def test_threshold_range_bar_renders_ticks_for_nonstandard_domain() -> None:
    bar = _DrawableThresholdRangeBar()
    bar.set_domain(10.0, 20.0)

    context = _draw_threshold_range_bar(bar, width=124)
    tick_positions = [
        start_x for start_x, _start_y, end_x, _end_y in context.lines if start_x == end_x
    ]

    assert context.labels == ["10", "12.5", "15", "17.5", "20"]
    assert tick_positions == pytest.approx([12.0, 37.0, 62.0, 87.0, 112.0])
    assert len(set(tick_positions)) == len(tick_positions)
