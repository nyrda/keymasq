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


def _fuzz(ctx: ScenarioContext, expected: int) -> None:
    assert ctx.source is not None and ctx.source.device is not None
    for axes in PADS:
        for code in axes:
            info = ctx.source.device.absinfo(code)
            assert info is not None and info.fuzz == expected, (code, info)
    info = ctx.source.device.absinfo(evdev.ecodes.ABS_X)
    assert info is not None and info.fuzz == 0  # Stick metadata remains unchanged.


def _kernel_fuzz_probe(ctx: ScenarioContext) -> None:
    _fuzz(ctx, 256)
    _report(ctx, 0, 1000, 0)
    _report(ctx, 0, 200, 0)
    for _ in range(3):
        _report(ctx, 0, 0, 0)
    assert ctx.source is not None and ctx.source.device is not None
    info = ctx.source.device.absinfo(PADS[0][0])
    assert info is not None and info.value == 112, info
    # A large excursion allows the unmodified kernel filter to reach zero.
    _report(ctx, 0, 1000, 0)
    _report(ctx, 0, 0, 0)
    ctx.expect_mouse_motion()


def _near_center_release(ctx: ScenarioContext) -> None:
    _fuzz(ctx, 0)
    for pad in range(2):
        _report(ctx, pad, 200, 0)
        ctx.expect_mouse_motion()
        _report(ctx, pad, 0, 0)
        ctx.expect_mouse_motion()
        assert ctx.source is not None and ctx.source.device is not None
        info = ctx.source.device.absinfo(PADS[pad][0])
        assert info is not None and info.value == 0, info
        _report(ctx, pad, -24576, 0)
        ctx.expect_mouse_motion()
        _report(ctx, pad, -16384, 0)
        ctx.expect_mouse_motion(100)
        _report(ctx, pad, 0, 0)
        ctx.expect_mouse_motion()


def _contact_filter(ctx: ScenarioContext, pad: int) -> None:
    # Queue landing reports together, then let the contact settle. The drift
    # must update the reference without moving the pointer.
    _report(ctx, pad, -24576, -16384)
    _report(ctx, pad, -23552, -15360)
    ctx.expect_mouse_motion()
    _report(ctx, pad, -15360, -7168)
    # No more source reports: the lift-hold timer must emit this movement.
    ctx.expect_mouse_motion(100, 50)

    # The assertion above waits for silence, so this small step starts from
    # rest. A release in the next report must discard it before the timer fires.
    _report(ctx, pad, -14336, -6144)
    _report(ctx, pad, 0, 0)
    ctx.expect_mouse_motion()
    _stroke(ctx, pad)  # Retouch must not inherit the discarded displacement.


def _button_report(ctx: ScenarioContext, x: int, y: int, pressed: int) -> None:
    assert ctx.source is not None
    ec = evdev.ecodes
    ctx.source.write(ec.EV_ABS, PADS[0][0], x)
    ctx.source.write(ec.EV_ABS, PADS[0][1], y)
    ctx.source.write(ec.EV_KEY, ec.KEY_F23, pressed)
    ctx.source.syn()


def _drag_from_rest(ctx: ScenarioContext) -> None:
    assert ctx.source is not None
    ec = evdev.ecodes
    _report(ctx, 0, -24576, -16384)
    ctx.expect_mouse_motion()
    for x, y, pressed in [(-16384, -8192, 1), (-8192, 0, 0)]:
        _button_report(ctx, x, y, pressed)
        # Buttons are immediate, while motion out of rest waits for the lift
        # hold. The silence check between iterations returns the pad to rest.
        ctx.expect_mouse_events(
            [
                (ec.EV_KEY, ec.BTN_LEFT, pressed),
                (ec.EV_REL, ec.REL_X, 100),
                (ec.EV_REL, ec.REL_Y, 50),
            ]
        )
        ctx.expect_no_mouse_events()
    _report(ctx, 0, 0, 0)
    ctx.expect_mouse_motion()


def _drag_in_motion(ctx: ScenarioContext) -> None:
    ec = evdev.ecodes
    for pressed in (1, 0):
        _report(ctx, 0, -24576, -16384)
        ctx.expect_mouse_motion()
        # Queue two moving reports without an output wait between them. The
        # first step establishes speed; the second releases both steps without
        # a lift hold, before dispatching the button in that report.
        _report(ctx, 0, -16384, -8192)
        _button_report(ctx, -8192, 0, pressed)
        ctx.expect_mouse_events(
            [
                (ec.EV_REL, ec.REL_X, 200),
                (ec.EV_REL, ec.REL_Y, 100),
                (ec.EV_KEY, ec.BTN_LEFT, pressed),
            ]
        )
        ctx.expect_no_mouse_events()
        _report(ctx, 0, 0, 0)
        ctx.expect_mouse_motion()


def run(ctx: ScenarioContext) -> None:
    try:
        ctx.subtest("kernel fuzz suppresses a near-center release", lambda: _kernel_fuzz_probe(ctx))
        ctx.set_profile_enabled(PROFILE, enabled=True)
        ctx.subtest(
            "exact near-center release with touchpad fuzz disabled",
            lambda: _near_center_release(ctx),
        )
        ctx.subtest("HAT1 touch, slide, hold, release, retouch", lambda: _stroke(ctx, 0))
        ctx.subtest("HAT2 touch, slide, hold, release, retouch", lambda: _stroke(ctx, 1))
        ctx.subtest("HAT1 landing, idle flush, lift and retouch", lambda: _contact_filter(ctx, 0))
        ctx.subtest("HAT2 landing, idle flush, lift and retouch", lambda: _contact_filter(ctx, 1))
        ctx.subtest("changed and unchanged touchpad mappings", lambda: _profile_change(ctx))
        ctx.subtest("daemon restart with a held touch", lambda: _restart(ctx))
        ctx.subtest("drag buttons precede held motion from rest", lambda: _drag_from_rest(ctx))
        ctx.subtest(
            "continuous motion precedes drag button transitions", lambda: _drag_in_motion(ctx)
        )
    finally:
        ctx.source_key(evdev.ecodes.KEY_F23, 0)
        for pad in range(2):
            _report(ctx, pad, 0, 0)
        ctx.set_profile_enabled(OVERRIDE, enabled=False)
        ctx.set_profile_enabled(PROFILE, enabled=False)
    ctx.subtest("touchpad fuzz restored after profile teardown", lambda: _fuzz(ctx, 256))
