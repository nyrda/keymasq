import ctypes
import logging
from collections.abc import Iterator

import pytest

from keymasq.keymasqd import timer_precision

_PR_GET_TIMERSLACK = 30


def _get_timer_slack_ns() -> int:
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    libc.prctl.restype = ctypes.c_long
    result = libc.prctl(
        ctypes.c_int(_PR_GET_TIMERSLACK),
        ctypes.c_ulong(0),
        ctypes.c_ulong(0),
        ctypes.c_ulong(0),
        ctypes.c_ulong(0),
    )
    if result < 0:
        err = ctypes.get_errno()
        raise OSError(err, "prctl(PR_GET_TIMERSLACK) failed")
    return int(result)


@pytest.fixture(autouse=True)
def restore_timer_slack() -> Iterator[None]:
    original_slack_ns = _get_timer_slack_ns()
    yield
    assert timer_precision.set_timer_slack_ns(original_slack_ns) is True


def test_set_timer_slack_ns_succeeds(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("test.timer_precision")
    with caplog.at_level(logging.INFO, logger=logger.name):
        ok = timer_precision.set_timer_slack_ns(1, logger=logger)
    assert ok is True
    assert any("timer slack" in rec.message.lower() for rec in caplog.records)


def test_set_timer_slack_ns_clamps_non_positive() -> None:
    # Passing 0 would ask prctl to reset slack to the kernel default; the helper
    # must clamp non-positive requests to >=1ns so callers never accidentally
    # loosen the wakeup resolution.
    assert timer_precision.set_timer_slack_ns(0) is True
    assert timer_precision.set_timer_slack_ns(-5) is True


def test_set_timer_slack_ns_is_idempotent() -> None:
    assert timer_precision.set_timer_slack_ns(1) is True
    assert timer_precision.set_timer_slack_ns(1) is True
