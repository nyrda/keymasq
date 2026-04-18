"""Per-process kernel timer-slack control for keymasqd.

The Linux kernel tracks a per-thread ``timer_slack_ns`` value that lets it
coalesce timer wakeups for power efficiency. The default (50 µs on recent
kernels) perturbs the sub-millisecond ``asyncio.sleep`` deadlines used by
macro replay. Since keymasqd is a long-running input-latency-sensitive
daemon, we tighten the slack once at startup so every thread spawned later
inherits the tighter value.

``PR_SET_TIMERSLACK`` requires no privilege; it operates only on the calling
thread. See ``prctl(2)``.
"""

import ctypes
import logging
import os

_PR_SET_TIMERSLACK = 29


def set_timer_slack_ns(
    slack_ns: int = 1,
    logger: logging.Logger | None = None,
) -> bool:
    """Tighten the calling thread's kernel timer slack to ``slack_ns``.

    Returns ``True`` on success. Returns ``False`` and logs a warning on any
    failure (missing libc or a non-zero prctl return). Never raises. Safe to
    call more than once.
    """
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
    except OSError as exc:
        if logger is not None:
            logger.warning("PR_SET_TIMERSLACK skipped: libc.so.6 unavailable: %s", exc)
        return False

    # prctl(int option, unsigned long arg2, unsigned long arg3, unsigned long arg4,
    #       unsigned long arg5).  Clamp to >=1 so callers can't accidentally reset
    #       the slack back to the kernel default by passing 0.
    effective = max(1, int(slack_ns))
    rc = libc.prctl(
        ctypes.c_int(_PR_SET_TIMERSLACK),
        ctypes.c_ulong(effective),
        ctypes.c_ulong(0),
        ctypes.c_ulong(0),
        ctypes.c_ulong(0),
    )
    if rc != 0:
        err = ctypes.get_errno()
        if logger is not None:
            logger.warning(
                "PR_SET_TIMERSLACK(%d) failed: %s",
                effective,
                os.strerror(err),
            )
        return False

    if logger is not None:
        logger.info("Kernel timer slack set to %d ns", effective)
    return True
