"""Translate real mouse REL reports through session profiles into pointer and axis output."""

import contextlib
import time
from collections.abc import Iterator

import evdev
from support import EVENT_TIMEOUT_S, MOUSE_HARDWARE_ID, POINTER_PROFILES, ScenarioContext

FACTORS = "Integration Pointer Factors"
SWAP = "Integration Pointer Swap"
VELOCITY = "Integration Pointer Stick Velocity"
POSITION = "Integration Pointer Axis Position"
KEEP = "Integration Pointer Axis Keep"
RECENTER = "Integration Pointer Axis Recenter"

REL_X = evdev.ecodes.REL_X
REL_Y = evdev.ecodes.REL_Y
EV_ABS = evdev.ecodes.EV_ABS
ABS_X = evdev.ecodes.ABS_X
ABS_RX = evdev.ecodes.ABS_RX
ABS_RY = evdev.ecodes.ABS_RY
ABS_RZ = evdev.ecodes.ABS_RZ


@contextlib.contextmanager
def _source_mouse(
    ctx: ScenarioContext,
    profile: str,
) -> Iterator[tuple[evdev.UInput, evdev.InputDevice]]:
    source = ctx.create_source_mouse()
    passthrough: evdev.InputDevice | None = None
    try:
        ctx.write_mouse_configs(str(source.device.path))
        ctx.request({"command": "reload"})
        ctx.request({"command": "reevaluate_hardware"})
        ctx.set_profile_enabled(profile, enabled=True)
        ctx.wait_for_hardware_mapping(MOUSE_HARDWARE_ID)
        passthrough = ctx.open_passthrough_output(MOUSE_HARDWARE_ID)
        ctx.read_output_events(passthrough)
        ctx.drain_outputs()
        yield source, passthrough
    finally:
        for name in POINTER_PROFILES:
            ctx.request({"command": "disable_profile", "profile_name": name}, ok=False)
        if passthrough is not None:
            passthrough.close()
        ctx.remove_mouse_configs()
        ctx.request({"command": "reload"}, ok=False)
        ctx.request({"command": "reevaluate_hardware"}, ok=False)
        source.close()
        ctx.settle_udev()


def _move(source: evdev.UInput, x: int = 0, y: int = 0) -> None:
    for code, value in ((REL_X, x), (REL_Y, y)):
        if value:
            source.write(evdev.ecodes.EV_REL, code, value)
    source.syn()


def _expect_rel(
    ctx: ScenarioContext,
    device: evdev.InputDevice,
    expected: list[tuple[int, int]],
) -> None:
    """Match relative output exactly, then reject trailing movement."""
    observed: list[tuple[int, int]] = []
    deadline = time.monotonic() + EVENT_TIMEOUT_S
    while observed != expected and time.monotonic() < deadline:
        observed.extend(
            (int(event.code), int(event.value))
            for event in ctx.read_output_events(device)
            if event.type == evdev.ecodes.EV_REL
        )
        assert observed == expected[: len(observed)], (expected, observed)
        if observed != expected:
            time.sleep(0.01)
    assert observed == expected, (expected, observed)
    ctx.expect_no_events(
        device,
        label="pointer passthrough",
        event_types={evdev.ecodes.EV_REL},
    )


def _expect_axes(ctx: ScenarioContext, *expected: tuple[int, int]) -> None:
    """Match axis output exactly so a late stale value cannot satisfy a later step."""
    if ctx.gamepad_output is None:
        raise AssertionError("gamepad output is not available")
    wanted = list(expected)
    observed: list[tuple[int, int]] = []
    deadline = time.monotonic() + EVENT_TIMEOUT_S
    while observed != wanted and time.monotonic() < deadline:
        observed.extend(
            (int(event.code), int(event.value))
            for event in ctx.read_output_events(ctx.gamepad_output)
            if event.type == EV_ABS
        )
        assert observed == wanted[: len(observed)], (wanted, observed)
        if observed != wanted:
            time.sleep(0.01)
    assert observed == wanted, (wanted, observed)


def run_mouse_factors(ctx: ScenarioContext) -> None:
    with _source_mouse(ctx, FACTORS) as (source, passthrough):
        _move(source, 10, 3)
        _expect_rel(ctx, passthrough, [(REL_X, 5), (REL_Y, 6)])
        _move(source, 1)
        _expect_rel(ctx, passthrough, [])
        _move(source, 1)
        _expect_rel(ctx, passthrough, [(REL_X, 1)])  # Fractions carry between reports.
        _move(source, -7, -2)
        _expect_rel(ctx, passthrough, [(REL_X, -3), (REL_Y, -4)])
        _move(source, -1)
        _expect_rel(ctx, passthrough, [(REL_X, -1)])
        source.write(evdev.ecodes.EV_REL, evdev.ecodes.REL_WHEEL, 1)
        source.syn()
        _expect_rel(ctx, passthrough, [(evdev.ecodes.REL_WHEEL, 1)])

        for value in (1, 0):
            source.write(evdev.ecodes.EV_KEY, evdev.ecodes.BTN_LEFT, value)
            source.syn()
        ctx.expect_events(
            passthrough,
            [
                (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_LEFT, 1),
                (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_LEFT, 0),
            ],
            label="pointer passthrough",
        )
        ctx.expect_no_mouse_events()

        ctx.set_profile_enabled(SWAP, enabled=True)
        _move(source, 4, 9)
        _expect_rel(ctx, passthrough, [(REL_Y, 4), (REL_X, -9)])
        ctx.set_profile_enabled(SWAP, enabled=False)
        _move(source, 10, 3)
        _expect_rel(ctx, passthrough, [(REL_X, 5), (REL_Y, 6)])

        # The release grace keeps the mouse grabbed; unmapped movement is unchanged.
        ctx.set_profile_enabled(FACTORS, enabled=False)
        _move(source, 10, 3)
        _expect_rel(ctx, passthrough, [(REL_X, 10), (REL_Y, 3)])


def run_stick_velocity(ctx: ScenarioContext) -> None:
    with _source_mouse(ctx, VELOCITY) as (source, passthrough):
        _move(source, 50)
        _expect_axes(ctx, (ABS_RX, 16384), (ABS_RX, 0))  # 500 counts/s of 1000.
        _expect_rel(ctx, passthrough, [])

        _move(source, y=-25)
        _expect_axes(ctx, (ABS_RY, -8192), (ABS_RY, 0))

        _move(source, 25)
        _move(source, 25)
        _expect_axes(ctx, (ABS_RX, 8192), (ABS_RX, 16384), (ABS_RX, 0))

        _move(source, 500, 500)
        _expect_axes(ctx, (ABS_RX, 32767), (ABS_RY, 32767), (ABS_RX, 0), (ABS_RY, 0))
        _expect_rel(ctx, passthrough, [])


def run_axis_position(ctx: ScenarioContext) -> None:
    with _source_mouse(ctx, POSITION) as (source, passthrough):
        _move(source, 500)
        _expect_axes(ctx, (ABS_X, 16384))
        ctx.expect_no_gamepad_events(timeout_s=0.3)  # Position holds without movement.
        _move(source, 1000)
        _expect_axes(ctx, (ABS_X, 32767))
        _move(source, -500)
        _expect_axes(ctx, (ABS_X, 16384))  # Overshoot dragged the center along.
        _move(source, -500)
        _expect_axes(ctx, (ABS_X, 0))
        _move(source, -1500)
        _expect_axes(ctx, (ABS_X, -32768))
        _move(source, 1000)
        _expect_axes(ctx, (ABS_X, 0))

        _move(source, y=-600)
        _expect_axes(ctx, (ABS_RZ, 153))  # Inverted Y drives a one-sided trigger.
        _move(source, y=1000)
        _expect_axes(ctx, (ABS_RZ, 0))
        _move(source, y=-200)
        _expect_axes(ctx, (ABS_RZ, 51))  # Pulling past rest does not wind up.
        _expect_rel(ctx, passthrough, [])

        ctx.set_profile_enabled(KEEP, enabled=True)
        _expect_axes(ctx, (ABS_RZ, 0))  # A changed mapping releases its held axes.
        _move(source, 1500)
        _expect_axes(ctx, (ABS_X, 32767))
        _move(source, -500)
        ctx.expect_no_gamepad_events()  # Kept overshoot must be undone first.
        _move(source, -500)
        _expect_axes(ctx, (ABS_X, 16384))

        ctx.set_profile_enabled(RECENTER, enabled=True)
        _expect_axes(ctx, (ABS_X, 0))
        _move(source, 100)
        _expect_axes(ctx, (ABS_X, 18022), (ABS_X, 0))  # 50% minimum output, then recenter.
        ctx.set_profile_enabled(RECENTER, enabled=False)
        ctx.set_profile_enabled(KEEP, enabled=False)

        _move(source, 500)
        _expect_axes(ctx, (ABS_X, 16384))
        ctx.set_profile_enabled(POSITION, enabled=False)
        _expect_axes(ctx, (ABS_X, 0))
