import math

import pytest

from keymasq.common.model.motion import MotionAnalogConfig
from keymasq.keymasqd.runtime.motion_filter import OneEuroFilter


def test_adaptive_filter_reduces_resting_jitter() -> None:
    axis = OneEuroFilter()
    output = [
        axis.update(0.01 * math.sin(2 * math.pi * 12 * i / 100), i * 10_000_000, 1, 10)
        for i in range(501)
    ]
    assert max(abs(value) for value in output[100:]) < 0.0015


def test_adaptive_filter_keeps_tiny_sustained_motion() -> None:
    axis = OneEuroFilter()
    axis.update(0, 0, 1, 10)
    output = [axis.update(0.005, i * 10_000_000, 1, 10) for i in range(1, 101)]
    assert output[0] > 0
    assert all(a < b for a, b in zip(output, output[1:], strict=False))
    assert output[-1] == pytest.approx(0.005, abs=0.00001)


def test_adaptive_filter_follows_fast_motion_and_reversal() -> None:
    axis = OneEuroFilter()
    axis.update(0, 0, 1, 10)
    output = [axis.update(0.5, i * 10_000_000, 1, 10) for i in range(1, 11)]
    assert output[2] > 0.475
    assert all(0 < value <= 0.5 for value in output)
    output = [axis.update(-0.5, i * 10_000_000, 1, 10) for i in range(11, 21)]
    assert output[2] < -0.475
    assert all(-0.5 <= value <= 0.5 for value in output)


def test_adaptive_filter_response_is_consistent_across_report_rates() -> None:
    endpoints = []
    for rate in (60, 100, 250):
        axis = OneEuroFilter()
        for i in range(rate + 1):
            axis.update(0.1 * i / rate, round(i * 1_000_000_000 / rate), 1, 10)
        endpoints.append(axis.value)
    assert max(endpoints) - min(endpoints) < 0.002
    assert min(endpoints) > 0.09


def test_adaptive_filter_initialization_duplicate_timestamp_and_gap() -> None:
    axis = OneEuroFilter()
    assert axis.update(0.4, 1_000_000_000, 1, 10) == 0.4
    assert axis.update(-0.4, 1_000_000_000, 1, 10) == 0.4
    assert axis.update(-0.4, 999_000_000, 1, 10) == -0.4
    assert axis.update(-0.4, 2_000_000_000, 1, 10) == -0.4
    assert axis.update(-0.4, 2_010_000_000, 1, 10) == -0.4


@pytest.mark.parametrize(
    "mode,cutoff,beta,expected",
    [
        (" ADAPTIVE ", 2.5, 17, ("adaptive", 2.5, 17)),
        ("unknown", 0, -1, ("fixed", 0.1, 0)),
        ("fixed", 100, 200, ("fixed", 30, 100)),
        ("adaptive", float("nan"), float("inf"), ("adaptive", 1, 10)),
    ],
)
def test_adaptive_settings_are_normalized(mode, cutoff, beta, expected) -> None:
    config = MotionAnalogConfig(
        smoothing_mode=mode, adaptive_min_cutoff_hz=cutoff, adaptive_beta=beta
    )
    assert (config.smoothing_mode, config.adaptive_min_cutoff_hz, config.adaptive_beta) == expected
