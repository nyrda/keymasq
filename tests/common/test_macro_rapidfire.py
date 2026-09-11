from fractions import Fraction

import pytest

from keymasq.common.macro_rapidfire import plan_macro_rapidfire


def test_fits_equal_gaps_and_preserves_every_hold() -> None:
    plan = plan_macro_rapidfire(100_000, 10, 10)
    assert plan.count == 6
    assert plan.wait_us == 8_000
    assert [plan.pulse(i) for i in range(plan.count)] == [
        (0, 10_000),
        (18_000, 28_000),
        (36_000, 46_000),
        (54_000, 64_000),
        (72_000, 82_000),
        (90_000, 100_000),
    ]


@pytest.mark.parametrize("duration", [0, 1, 10_000, 20_000])
def test_short_block_is_one_hold_covering_the_whole_duration(duration: int) -> None:
    plan = plan_macro_rapidfire(duration, 10, 10)
    assert plan.count == 1
    assert plan.pulse(0) == (0, duration)


def test_fractional_intervals_round_without_drift() -> None:
    plan = plan_macro_rapidfire(100_001, 10, 10)
    pulses = [plan.pulse(i) for i in range(plan.count)]
    assert pulses[0][0] == 0
    assert pulses[-1][1] == 100_001
    assert all(release - press == 10_000 for press, release in pulses)
    gaps = [right[0] - left[1] for left, right in zip(pulses, pulses[1:], strict=False)]
    assert max(gaps) - min(gaps) == 1


@pytest.mark.parametrize("hold", [0, 1, 5, 20])
@pytest.mark.parametrize("wait", [1, 3, 10, 1000])
def test_count_minimizes_wait_error_with_positive_gaps(hold: int, wait: int) -> None:
    for duration in [2_000, 23_456, 100_001, 250_000]:
        plan = plan_macro_rapidfire(duration, hold, wait)
        maximum = (duration + 1000) // (hold * 1000 + 1000)
        if maximum < 2:
            assert plan.count == 1
            continue
        best = min(
            range(2, maximum + 1),
            key=lambda n: (abs(Fraction(duration - n * hold * 1000, n - 1) - wait * 1000), n),
        )
        assert plan.count == best
        assert plan.wait_us >= 1000


def test_zero_hold_keeps_press_release_pairs_at_both_endpoints() -> None:
    plan = plan_macro_rapidfire(10_000, 0, 5)
    assert [plan.pulse(i) for i in range(plan.count)] == [(0, 0), (5000, 5000), (10000, 10000)]
