"""Tell a silent input source from a daemon that could not observe it.

CLOCK_MONOTONIC keeps running while user space is frozen around a suspend and
while the event loop is blocked, so a quiet interval measured with it alone can
blame the device for time the daemon itself was not running.
"""

import time

STALL_NS = 250_000_000


def suspended_ns() -> int:
    return time.clock_gettime_ns(time.CLOCK_BOOTTIME) - time.monotonic_ns()


def unobserved(due_ns: int, suspended_at_ns: int) -> bool:
    return time.monotonic_ns() - due_ns > STALL_NS or suspended_ns() - suspended_at_ns > STALL_NS
