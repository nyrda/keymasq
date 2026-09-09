"""Exercise real evdev reports, session profile application, and mouse output."""

import time

import evdev
from support import EVENT_TIMEOUT_S, ScenarioContext

PROFILE = "Integration Touchpad Mouse"
OVERRIDE = "Integration Touchpad Mouse Override"
PADS = (
    (evdev.ecodes.ABS_HAT1X, evdev.ecodes.ABS_HAT1Y),
    (evdev.ecodes.ABS_HAT2X, evdev.ecodes.ABS_HAT2Y),
)


def _report(ctx: ScenarioContext, pad: int, x: int | None = None, y: int | None = None) -> None:
    if ctx.source is None:
        raise AssertionError("source device is not available")
    # Both coordinates belong to one report. The kernel filters unchanged ABS
    # values, so these exercise sparse evdev input without mocking the reader.
    for code, value in zip(PADS[pad], (x, y), strict=True):
        if value is not None:
            ctx.source.write(evdev.ecodes.EV_ABS, code, value)
    ctx.source.syn()


def _expect_motion(ctx: ScenarioContext, x: int = 0, y: int = 0) -> None:
    expected = [
        (evdev.ecodes.EV_REL, code, value)
        for code, value in ((evdev.ecodes.REL_X, x), (evdev.ecodes.REL_Y, y))
        if value
    ]
    if ctx.mouse_output is None:
        raise AssertionError("mouse output is not available")
    observed: list[tuple[int, int, int]] = []
    deadline = time.monotonic() + EVENT_TIMEOUT_S
    while observed != expected and time.monotonic() < deadline:
        observed.extend(
            (event.type, event.code, event.value)
            for event in ctx.read_output_events(ctx.mouse_output)
            if event.type != evdev.ecodes.EV_SYN
        )
        # Unlike a subsequence match, reject extra movement and incorrect deltas.
        assert observed == expected[:len(observed)], (expected, observed)
        if observed != expected:
            time.sleep(0.01)
    assert observed == expected, (expected, observed)
    ctx.expect_no_mouse_events()


def _stroke(ctx: ScenarioContext, pad: int) -> None:
    _report(ctx, pad, -24576, -16384)
    _expect_motion(ctx)  # Initial contact must not jump.
    _report(ctx, pad, -16384, -8192)
    _expect_motion(ctx, 100, 50)
    _report(ctx, pad, -16384, -8192)
    _expect_motion(ctx)  # The kernel suppresses both unchanged coordinates.
    _report(ctx, pad, y=0)
    _expect_motion(ctx, y=50)  # One zero coordinate is still a touch.
    _report(ctx, pad, 0, -16384)
    _expect_motion(ctx, 200, -100)  # No false release between X and Y.
    _report(ctx, pad, 0, 0)
    _expect_motion(ctx)
    _report(ctx, pad, -32768, 0)
    _expect_motion(ctx)  # Retouch elsewhere; Y stays zero and is not reported.
    _report(ctx, pad, x=-24576)
    _expect_motion(ctx, 100)
    _report(ctx, pad, 0, 0)
    _expect_motion(ctx)


def _profile_change(ctx: ScenarioContext) -> None:
    for pad in range(2):
        _report(ctx, pad, -24576, -16384)
        _expect_motion(ctx)
    ctx.set_profile_enabled(OVERRIDE, enabled=True)
    _expect_motion(ctx)
    _report(ctx, 0, x=-16384)
    _expect_motion(ctx)  # Changed control anchors with the retained Y coordinate.
    _report(ctx, 0, y=-8192)
    _expect_motion(ctx, y=-100)  # New scale and inversion came through session IPC.
    _report(ctx, 1, x=-16384)
    _expect_motion(ctx, 100)  # Unchanged pad retains its stroke across the update.
    ctx.set_profile_enabled(OVERRIDE, enabled=False)
    _expect_motion(ctx)
    _report(ctx, 0, x=-8192)
    _expect_motion(ctx)
    _report(ctx, 0, y=0)
    _expect_motion(ctx, y=50)  # Restored mapping also retains the unchanged axis.
    for pad in range(2):
        _report(ctx, pad, 0, 0)
        _expect_motion(ctx)


def _restart(ctx: ScenarioContext) -> None:
    _report(ctx, 0, -24576, -16384)
    _expect_motion(ctx)
    ctx.restart_keymasqd()
    ctx.wait_for_active_profile(PROFILE, enabled=True)
    _expect_motion(ctx)
    _report(ctx, 0, x=-16384)
    _expect_motion(ctx)  # EVIOCGABS must supply Y, already held before the grab.
    _report(ctx, 0, y=-8192)
    _expect_motion(ctx, y=50)
    _report(ctx, 0, 0, 0)
    _expect_motion(ctx)


def run(ctx: ScenarioContext) -> None:
    try:
        ctx.set_profile_enabled(PROFILE, enabled=True)
        ctx.subtest("HAT1 touch, slide, hold, release, retouch", lambda: _stroke(ctx, 0))
        ctx.subtest("HAT2 touch, slide, hold, release, retouch", lambda: _stroke(ctx, 1))
        ctx.subtest("changed and unchanged touchpad mappings", lambda: _profile_change(ctx))
        ctx.subtest("daemon restart with a held touch", lambda: _restart(ctx))
    finally:
        for pad in range(2):
            _report(ctx, pad, 0, 0)
        ctx.set_profile_enabled(OVERRIDE, enabled=False)
        ctx.set_profile_enabled(PROFILE, enabled=False)
