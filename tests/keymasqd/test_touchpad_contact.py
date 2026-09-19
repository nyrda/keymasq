import pytest

from keymasq.keymasqd.runtime.analog.touchpad_contact import (
    LIFT_HOLD_S,
    SETTLE_S,
    TouchpadContact,
)

REPORT_S = 0.004


def settled_contact(x: float = 0.0, y: float = 0.0) -> tuple[TouchpadContact, float]:
    contact = TouchpadContact()
    contact.move(0.0, x, y)
    return contact, SETTLE_S


def test_first_position_and_landing_only_move_the_reference():
    contact = TouchpadContact()

    assert contact.move(0.0, 0.5, 0.5) == (0.0, 0.0)
    assert contact.move(SETTLE_S / 2, 0.52, 0.5) == (0.0, 0.0)
    assert contact.next_due() is None

    contact.move(SETTLE_S, 0.53, 0.5)
    assert contact.drain(SETTLE_S + LIFT_HOLD_S) == pytest.approx((0.01, 0.0))


def test_step_out_of_rest_is_held_until_the_hold_expires():
    contact, now = settled_contact()

    assert contact.move(now, 0.03, 0.0) == (0.0, 0.0)
    assert contact.next_due() == pytest.approx(now + LIFT_HOLD_S)
    assert contact.drain(now + LIFT_HOLD_S / 2) == (0.0, 0.0)
    assert contact.drain(now + LIFT_HOLD_S) == pytest.approx((0.03, 0.0))
    assert contact.next_due() is None


def test_sustained_motion_stops_being_held_and_keeps_its_order():
    contact, now = settled_contact()
    emitted = 0.0
    latest_delay = None
    for step in range(1, 40):
        now += REPORT_S
        dx, _ = contact.move(now, step * 0.02, 0.0)  # 5 half-widths per second
        emitted += dx
        due = contact.next_due()
        latest_delay = None if due is None else due - now

    assert latest_delay is None
    assert emitted == pytest.approx(39 * 0.02)


def replay(positions: list[tuple[float, float, float]]) -> tuple[float, float]:
    """Total motion emitted for one touch, with the flush timer firing on time."""
    contact = TouchpadContact()
    total_x = total_y = 0.0
    for now, x, y in positions:
        while (due := contact.next_due()) is not None and due <= now:
            dx, dy = contact.drain(due)
            total_x, total_y = total_x + dx, total_y + dy
        dx, dy = contact.move(now, x, y)
        total_x, total_y = total_x + dx, total_y + dy
    return total_x, total_y


def touch(steps: list[tuple[float, float]]) -> list[tuple[float, float, float]]:
    """Positions at the pad's report rate from per-report steps, starting at the origin."""
    positions = [(0.0, 0.0, 0.0)]
    for index, (dx, dy) in enumerate(steps, start=1):
        _, x, y = positions[-1]
        positions.append((index * REPORT_S, x + dx, y + dy))
    return positions


def test_lift_from_rest_does_not_move_the_pointer():
    # Shape of a lift on a Steam Deck pad: a slide, a rest, a faint ramp while
    # the contact shrinks, then a spike in the last reports before release.
    slide = [(0.004, 0.0)] * 50
    rest = [(0.0001, 0.0)] * 50
    ramp = [(0.001, 0.001)] * 6
    spike = [(0.0, 0.025), (0.002, 0.01), (0.0, 0.02)]

    emitted = replay(touch(slide + rest + ramp + spike))

    settled = SETTLE_S / REPORT_S * 0.004
    assert emitted[0] == pytest.approx(50 * 0.004 - settled + 50 * 0.0001, abs=0.007)
    assert abs(emitted[1]) < 0.007


def test_staged_lift_longer_than_the_hold_only_leaks_its_first_stage():
    rest = [(0.0, 0.0)] * 20
    first_stage = [(0.0, 0.02)]
    gap = [(0.0, 0.0005)] * 8  # 32 ms, longer than the hold
    last_stage = [(0.0, 0.03)]

    _, emitted_y = replay(touch(rest + first_stage + gap + last_stage))

    assert emitted_y == pytest.approx(0.02, abs=0.004)


def test_flick_keeps_its_travel():
    flick = [(0.01 + 0.002 * min(step, 10), 0.0) for step in range(40)]
    positions = touch(flick)

    emitted_x, _ = replay(positions)

    settled_x = next(x for now, x, _ in reversed(positions) if now < SETTLE_S)
    assert emitted_x == pytest.approx(positions[-1][1] - settled_x, rel=0.02)
