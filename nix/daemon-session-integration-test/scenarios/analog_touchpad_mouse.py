"""Exercise real evdev reports, session profile application, and mouse output."""

import evdev
from support import ScenarioContext

PROFILE = "Integration Touchpad Mouse"
OVERRIDE = "Integration Touchpad Mouse Override"
PADS = (
    (evdev.ecodes.ABS_HAT1X, evdev.ecodes.ABS_HAT1Y),
    (evdev.ecodes.ABS_HAT2X, evdev.ecodes.ABS_HAT2Y),
)


def _report(ctx: ScenarioContext, pad: int, x: int | None = None, y: int | None = None) -> None:
    ctx.source_abs_report(
        [(code, value) for code, value in zip(PADS[pad], (x, y), strict=True) if value is not None]
    )


def _stroke(ctx: ScenarioContext, pad: int) -> None:
    _report(ctx, pad, -24576, -16384)
    ctx.expect_mouse_motion()  # Initial contact must not jump.
    _report(ctx, pad, -16384, -8192)
    ctx.expect_mouse_motion(100, 50)
    _report(ctx, pad, -16384, -8192)
    ctx.expect_mouse_motion()  # The kernel suppresses both unchanged coordinates.
    _report(ctx, pad, y=0)
    ctx.expect_mouse_motion(y=50)  # One zero coordinate is still a touch.
    _report(ctx, pad, 0, -16384)
    ctx.expect_mouse_motion(200, -100)  # No false release between X and Y.
    _report(ctx, pad, 0, 0)
    ctx.expect_mouse_motion()
    _report(ctx, pad, -32768, 0)
    ctx.expect_mouse_motion()  # Retouch elsewhere; Y stays zero and is not reported.
    _report(ctx, pad, x=-24576)
    ctx.expect_mouse_motion(100)
    _report(ctx, pad, 0, 0)
    ctx.expect_mouse_motion()


def _profile_change(ctx: ScenarioContext) -> None:
    for pad in range(2):
        _report(ctx, pad, -24576, -16384)
        ctx.expect_mouse_motion()
    ctx.set_profile_enabled(OVERRIDE, enabled=True)
    ctx.expect_mouse_motion()
    _report(ctx, 0, x=-16384)
    ctx.expect_mouse_motion()  # Changed control anchors with the retained Y coordinate.
    _report(ctx, 0, y=-8192)
    ctx.expect_mouse_motion(y=-100)  # New scale and inversion came through session IPC.
    _report(ctx, 1, x=-16384)
    ctx.expect_mouse_motion(100)  # Unchanged pad retains its stroke across the update.
    ctx.set_profile_enabled(OVERRIDE, enabled=False)
    ctx.expect_mouse_motion()
    _report(ctx, 0, x=-8192)
    ctx.expect_mouse_motion()
    _report(ctx, 0, y=0)
    ctx.expect_mouse_motion(y=50)  # Restored mapping also retains the unchanged axis.
    for pad in range(2):
        _report(ctx, pad, 0, 0)
        ctx.expect_mouse_motion()


def _restart(ctx: ScenarioContext) -> None:
    _report(ctx, 0, -24576, -16384)
    ctx.expect_mouse_motion()
    ctx.restart_keymasqd()
    ctx.wait_for_active_profile(PROFILE, enabled=True)
    ctx.expect_mouse_motion()
    _report(ctx, 0, x=-16384)
    ctx.expect_mouse_motion()  # EVIOCGABS must supply Y, already held before the grab.
    _report(ctx, 0, y=-8192)
    ctx.expect_mouse_motion(y=50)
    _report(ctx, 0, 0, 0)
    ctx.expect_mouse_motion()


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
