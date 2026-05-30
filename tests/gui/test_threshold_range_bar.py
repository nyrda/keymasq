# ruff: noqa: F403, F405, I001
from tests.gui.support import *


def test_threshold_range_bar_reclamps_ranges_when_domain_changes() -> None:
    from keymasq.gui.widgets.threshold_range_bar import ThresholdRangeBar

    bar = ThresholdRangeBar()
    bar.set_ranges(-1.0, 1.0, -0.8, 0.8)

    bar.set_domain(0.0, 1.0)

    assert 0.0 <= bar._trigger_min <= 1.0
    assert 0.0 <= bar._trigger_max <= 1.0
    assert 0.0 <= bar._release_min <= 1.0
    assert 0.0 <= bar._release_max <= 1.0


def test_threshold_range_bar_ticks_use_nonstandard_domain() -> None:
    from keymasq.gui.widgets.threshold_range_bar import ThresholdRangeBar, _threshold_ticks

    bar = ThresholdRangeBar()
    bar.set_domain(10.0, 20.0)

    ticks = _threshold_ticks(bar._minimum, bar._maximum)
    values = [value for value, _label in ticks]
    positions = [bar._x_for_value(value, 12.0, 100.0) for value in values]

    assert values == [10.0, 12.5, 15.0, 17.5, 20.0]
    assert positions == pytest.approx([12.0, 37.0, 62.0, 87.0, 112.0])
    assert len(set(positions)) == len(positions)
