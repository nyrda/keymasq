import evdev
from support import ScenarioContext

PROFILE = "Integration Analog Mouse Area"
SHAPED_PROFILE = "Integration Analog Mouse Area Shaped"


def _report(ctx: ScenarioContext, x: int, y: int = 0) -> None:
    ctx.source_abs_report([(evdev.ecodes.ABS_X, x), (evdev.ecodes.ABS_Y, y)])


def _stick_motion(ctx: ScenarioContext) -> None:
    # The original fixture omits input style: existing area controls stay sticks.
    # Unequal first and last samples must still return the pointer to its origin.
    for x, dx in [(-8192, -25), (-24576, -50), (-16384, 25), (0, 50)]:
        _report(ctx, x)
        ctx.expect_mouse_motion(dx)
    # A fast flick must include its entire initial and final displacement.
    _report(ctx, -32768)
    ctx.expect_mouse_motion(-100)
    _report(ctx, -32768)
    ctx.expect_mouse_motion()
    _report(ctx, 0)
    ctx.expect_mouse_motion(100)
    _report(ctx, -32768, -32768)
    ctx.expect_mouse_motion(-100, -50)
    _report(ctx, 0, -32768)
    ctx.expect_mouse_motion(100)
    _report(ctx, 0, 0)
    ctx.expect_mouse_motion(y=50)


def _stick_shaping(ctx: ScenarioContext) -> None:
    ctx.set_profile_enabled(SHAPED_PROFILE, enabled=True)
    ctx.expect_mouse_motion()
    for x, y in [(100, -200), (-400, 300), (-8192, 8192), (0, 0)]:
        _report(ctx, x, y)
        ctx.expect_mouse_motion()
    _report(ctx, -20480)
    ctx.expect_mouse_motion(-20)
    _report(ctx, -32768)
    ctx.expect_mouse_motion(-60)
    _report(ctx, -4096)
    ctx.expect_mouse_motion(80)  # Entering the deadzone returns to the origin.
    _report(ctx, 0)
    ctx.expect_mouse_motion()


def run(ctx: ScenarioContext) -> None:
    try:
        ctx.set_profile_enabled(PROFILE, enabled=True)
        ctx.subtest(
            "stick first sample, return, fast flick, hold and diagonal", lambda: _stick_motion(ctx)
        )
        ctx.subtest("stick deadzone, sensitivity and response curve", lambda: _stick_shaping(ctx))
    finally:
        _report(ctx, 0)
        ctx.set_profile_enabled(SHAPED_PROFILE, enabled=False)
        ctx.set_profile_enabled(PROFILE, enabled=False)
