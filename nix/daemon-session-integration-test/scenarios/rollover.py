import contextlib

import evdev
from support import (
    HARDWARE_ID,
    ROLLOVER_COMBO_PROFILE_NAME,
    ROLLOVER_OVERRIDE_PROFILE_NAME,
    ROLLOVER_PROFILE_NAME,
    SECOND_HARDWARE_ID,
    ScenarioContext,
)

ecodes = evdev.ecodes
LEFT = -32768
RIGHT = 32767

# Fixture layout, see fixtures/profiles/rollover.toml.
STRAFE_LEFT = ecodes.KEY_F13
STRAFE_RIGHT = ecodes.KEY_F14
SNAP_LEFT = ecodes.KEY_F15
SNAP_RIGHT = ecodes.KEY_F16
NEUTRAL_UP = ecodes.KEY_F17
NEUTRAL_DOWN = ecodes.KEY_F18
PASS_FIRST = ecodes.KEY_F19
PASS_SECOND = ecodes.KEY_F20
OVERLOAD_MEMBER = ecodes.KEY_F21
PLAIN_MEMBER = ecodes.KEY_F22
PRIMARY_DEVICE_MEMBER = ecodes.KEY_F23
# On the secondary keyboard.
SECONDARY_DEVICE_MEMBER = ecodes.KEY_F13
# fixtures/profiles/rollover-combo.toml: PASS_FIRST + COMBO_PARTNER recalls
# PASS_FIRST and presses COMBO_OUTPUT.
COMBO_PARTNER = ecodes.KEY_SPACE
COMBO_OUTPUT = ecodes.KEY_Z
MEMBER_KEYS = (
    STRAFE_LEFT,
    STRAFE_RIGHT,
    SNAP_LEFT,
    SNAP_RIGHT,
    NEUTRAL_UP,
    NEUTRAL_DOWN,
    PASS_FIRST,
    PASS_SECOND,
    OVERLOAD_MEMBER,
    PLAIN_MEMBER,
    PRIMARY_DEVICE_MEMBER,
    COMBO_PARTNER,
)

# fixtures/superkeys/overload-superkey.toml
OVERLOAD_PRESS = [
    (ecodes.KEY_LEFTCTRL, 1),
    (ecodes.KEY_LEFTSHIFT, 1),
    (ecodes.KEY_1, 1),
    (ecodes.KEY_1, 0),
    (ecodes.KEY_2, 1),
    (ecodes.KEY_2, 0),
]
OVERLOAD_RELEASE = [
    (ecodes.KEY_3, 1),
    (ecodes.KEY_3, 0),
    (ecodes.KEY_4, 1),
    (ecodes.KEY_4, 0),
    (ecodes.KEY_LEFTCTRL, 0),
    (ecodes.KEY_LEFTSHIFT, 0),
]


def _x(value: int) -> tuple[int, int, int]:
    return (ecodes.EV_ABS, ecodes.ABS_X, value)


def _y(value: int) -> tuple[int, int, int]:
    return (ecodes.EV_ABS, ecodes.ABS_Y, value)


def _rx(value: int) -> tuple[int, int, int]:
    return (ecodes.EV_ABS, ecodes.ABS_RX, value)


def run(ctx: ScenarioContext) -> None:
    passthrough = ctx.open_passthrough_output(HARDWARE_ID)
    try:
        ctx.set_profile_enabled(ROLLOVER_PROFILE_NAME, enabled=True)
        ctx.wait_for_hardware_mapping(HARDWARE_ID)
        ctx.wait_for_hardware_mapping(SECOND_HARDWARE_ID)
        ctx.subtest(
            "rollover axis follows the newest held key without centering",
            lambda: _axis_follows_newest(ctx),
        )
        ctx.subtest(
            "rollover keyboard members release and restore their outputs",
            lambda: _keyboard_snap_tap(ctx),
        )
        ctx.subtest(
            "rollover neutral centers while opposing keys are held",
            lambda: _neutral(ctx),
        )
        ctx.subtest(
            "rollover passes unmapped members through the source output",
            lambda: _unmapped_passthrough(ctx, passthrough),
        )
        ctx.subtest(
            "rollover hands over between an overload superkey and a plain key",
            lambda: _overload_superkey_member(ctx),
        )
        ctx.subtest(
            "rollover hands over between members on two devices",
            lambda: _across_devices(ctx),
        )
        ctx.subtest(
            "rollover forgets a member when its device goes away",
            lambda: _device_loss(ctx),
        )
        ctx.subtest(
            "rollover group from a higher profile replaces the lower group",
            lambda: _higher_profile_override(ctx),
        )
        ctx.subtest(
            "rollover keeps held keys of a group when another group changes",
            lambda: _other_group_changes(ctx),
        )
        ctx.subtest(
            "rollover keeps held keys when their own group changes",
            lambda: _own_group_changes(ctx),
        )
        ctx.subtest(
            "rollover forgets a member whose release a combo swallowed",
            lambda: _combo_swallows_member_release(ctx, passthrough),
        )
        ctx.subtest(
            "rollover members held across profile disable release cleanly",
            lambda: _disable_while_held(ctx),
        )
    finally:
        for code in MEMBER_KEYS:
            with contextlib.suppress(Exception):
                ctx.source_key(code, 0)
        with contextlib.suppress(Exception):
            ctx.secondary_key(SECONDARY_DEVICE_MEMBER, 0)
        passthrough.close()
        ctx.set_profile_enabled(ROLLOVER_COMBO_PROFILE_NAME, enabled=False)
        ctx.set_profile_enabled(ROLLOVER_OVERRIDE_PROFILE_NAME, enabled=False)
        ctx.set_profile_enabled(ROLLOVER_PROFILE_NAME, enabled=False)


def _axis_follows_newest(ctx: ScenarioContext) -> None:
    # Releasing the older key keeps the newer direction.
    ctx.source_key(STRAFE_LEFT, 1)
    ctx.expect_exact_gamepad_events([_x(LEFT)])
    ctx.source_key(STRAFE_RIGHT, 1)
    ctx.expect_exact_gamepad_events([_x(RIGHT)])
    ctx.source_key(STRAFE_LEFT, 0)
    ctx.expect_no_gamepad_events()
    ctx.source_key(STRAFE_RIGHT, 0)
    ctx.expect_exact_gamepad_events([_x(0)])

    # Releasing the newer key returns to the one still held, never through 0.
    ctx.source_key(STRAFE_LEFT, 1)
    ctx.expect_exact_gamepad_events([_x(LEFT)])
    ctx.source_key(STRAFE_RIGHT, 1)
    ctx.expect_exact_gamepad_events([_x(RIGHT)])
    ctx.source_key(STRAFE_RIGHT, 0)
    ctx.expect_exact_gamepad_events([_x(LEFT)])
    ctx.source_key(STRAFE_LEFT, 0)
    ctx.expect_exact_gamepad_events([_x(0)])


def _keyboard_snap_tap(ctx: ScenarioContext) -> None:
    ctx.source_key(SNAP_LEFT, 1)
    ctx.expect_exact_keys([(ecodes.KEY_LEFT, 1)])
    ctx.source_key(SNAP_RIGHT, 1)
    ctx.expect_exact_keys([(ecodes.KEY_LEFT, 0), (ecodes.KEY_RIGHT, 1)])
    ctx.source_key(SNAP_RIGHT, 0)
    ctx.expect_exact_keys([(ecodes.KEY_RIGHT, 0), (ecodes.KEY_LEFT, 1)])
    ctx.source_key(SNAP_LEFT, 0)
    ctx.expect_exact_keys([(ecodes.KEY_LEFT, 0)])


def _neutral(ctx: ScenarioContext) -> None:
    ctx.source_key(NEUTRAL_UP, 1)
    ctx.expect_exact_gamepad_events([_y(LEFT)])
    ctx.source_key(NEUTRAL_DOWN, 1)
    ctx.expect_exact_gamepad_events([_y(0)])
    ctx.source_key(NEUTRAL_UP, 0)
    ctx.expect_exact_gamepad_events([_y(RIGHT)])
    ctx.source_key(NEUTRAL_DOWN, 0)
    ctx.expect_exact_gamepad_events([_y(0)])


def _unmapped_passthrough(ctx: ScenarioContext, passthrough: evdev.InputDevice) -> None:
    ctx.drain_device(passthrough)

    def expect(events: list[tuple[int, int]]) -> None:
        ctx.expect_exact_events(
            passthrough,
            [(ecodes.EV_KEY, code, value) for code, value in events],
            label="primary passthrough",
            event_types={ecodes.EV_KEY},
        )

    ctx.source_key(PASS_FIRST, 1)
    expect([(PASS_FIRST, 1)])
    ctx.source_key(PASS_SECOND, 1)
    expect([(PASS_FIRST, 0), (PASS_SECOND, 1)])
    ctx.source_key(PASS_SECOND, 0)
    expect([(PASS_SECOND, 0), (PASS_FIRST, 1)])
    ctx.source_key(PASS_FIRST, 0)
    expect([(PASS_FIRST, 0)])
    ctx.expect_no_keyboard_events()


def _overload_superkey_member(ctx: ScenarioContext) -> None:
    # Every handover is a full release and press of the superkey, so its
    # On Press and On Release actions run each time it gains or loses.
    ctx.source_key(OVERLOAD_MEMBER, 1)
    ctx.expect_exact_keys(OVERLOAD_PRESS)
    ctx.source_key(PLAIN_MEMBER, 1)
    ctx.expect_exact_keys([*OVERLOAD_RELEASE, (ecodes.KEY_X, 1)])
    ctx.source_key(PLAIN_MEMBER, 0)
    ctx.expect_exact_keys([(ecodes.KEY_X, 0), *OVERLOAD_PRESS])
    ctx.source_key(OVERLOAD_MEMBER, 0)
    ctx.expect_exact_keys(OVERLOAD_RELEASE)


def _across_devices(ctx: ScenarioContext) -> None:
    # The secondary keyboard's key takes over the axis and hands it back without
    # centering, like two keys on one device.
    ctx.source_key(PRIMARY_DEVICE_MEMBER, 1)
    ctx.expect_exact_gamepad_events([_rx(LEFT)])
    ctx.secondary_key(SECONDARY_DEVICE_MEMBER, 1)
    ctx.expect_exact_gamepad_events([_rx(RIGHT)])
    ctx.secondary_key(SECONDARY_DEVICE_MEMBER, 0)
    ctx.expect_exact_gamepad_events([_rx(LEFT)])
    ctx.source_key(PRIMARY_DEVICE_MEMBER, 0)
    ctx.expect_exact_gamepad_events([_rx(0)])

    # Releasing the older key on the other device keeps the newer direction.
    ctx.secondary_key(SECONDARY_DEVICE_MEMBER, 1)
    ctx.expect_exact_gamepad_events([_rx(RIGHT)])
    ctx.source_key(PRIMARY_DEVICE_MEMBER, 1)
    ctx.expect_exact_gamepad_events([_rx(LEFT)])
    ctx.secondary_key(SECONDARY_DEVICE_MEMBER, 0)
    ctx.expect_no_gamepad_events()
    ctx.source_key(PRIMARY_DEVICE_MEMBER, 0)
    ctx.expect_exact_gamepad_events([_rx(0)])
    ctx.expect_no_keyboard_events()


def _unplug_secondary(ctx: ScenarioContext) -> None:
    assert ctx.secondary_source is not None
    ctx.secondary_source.close()
    ctx.secondary_source = None
    ctx.settle_udev()


def _replug_secondary(ctx: ScenarioContext) -> None:
    ctx.recreate_secondary_source()
    ctx.wait_for_hardware_mapping(SECOND_HARDWARE_ID)
    ctx.drain_outputs()


def _device_loss(ctx: ScenarioContext) -> None:
    # The secondary key hands the axis over to the primary key. Unplugging the
    # secondary keyboard afterwards must not release the axis the primary key
    # now drives.
    ctx.secondary_key(SECONDARY_DEVICE_MEMBER, 1)
    ctx.expect_exact_gamepad_events([_rx(RIGHT)])
    ctx.source_key(PRIMARY_DEVICE_MEMBER, 1)
    ctx.expect_exact_gamepad_events([_rx(LEFT)])
    _unplug_secondary(ctx)
    ctx.expect_no_gamepad_events(timeout_s=1.0)
    _replug_secondary(ctx)
    ctx.source_key(PRIMARY_DEVICE_MEMBER, 0)
    ctx.expect_exact_gamepad_events([_rx(0)])

    # Unplugging the keyboard of the active member counts as its release, so
    # the key still held on the primary keyboard takes over.
    ctx.source_key(PRIMARY_DEVICE_MEMBER, 1)
    ctx.expect_exact_gamepad_events([_rx(LEFT)])
    ctx.secondary_key(SECONDARY_DEVICE_MEMBER, 1)
    ctx.expect_exact_gamepad_events([_rx(RIGHT)])
    _unplug_secondary(ctx)
    ctx.expect_exact_gamepad_events([_rx(LEFT)])
    _replug_secondary(ctx)
    ctx.source_key(PRIMARY_DEVICE_MEMBER, 0)
    ctx.expect_exact_gamepad_events([_rx(0)])
    ctx.expect_no_keyboard_events()


def _higher_profile_override(ctx: ScenarioContext) -> None:
    ctx.set_profile_enabled(ROLLOVER_OVERRIDE_PROFILE_NAME, enabled=True)
    ctx.wait_for_hardware_mapping(HARDWARE_ID)
    ctx.drain_outputs()

    # First wins: the later key only takes over once the first is released.
    ctx.source_key(STRAFE_LEFT, 1)
    ctx.expect_exact_gamepad_events([_x(LEFT)])
    ctx.source_key(STRAFE_RIGHT, 1)
    ctx.expect_no_gamepad_events()
    ctx.source_key(STRAFE_LEFT, 0)
    ctx.expect_exact_gamepad_events([_x(RIGHT)])
    ctx.source_key(STRAFE_RIGHT, 0)
    ctx.expect_exact_gamepad_events([_x(0)])

    ctx.set_profile_enabled(ROLLOVER_OVERRIDE_PROFILE_NAME, enabled=False)
    ctx.wait_for_hardware_mapping(HARDWARE_ID)
    ctx.drain_outputs()

    ctx.source_key(STRAFE_LEFT, 1)
    ctx.expect_exact_gamepad_events([_x(LEFT)])
    ctx.source_key(STRAFE_RIGHT, 1)
    ctx.expect_exact_gamepad_events([_x(RIGHT)])
    ctx.source_key(STRAFE_LEFT, 0)
    ctx.expect_no_gamepad_events()
    ctx.source_key(STRAFE_RIGHT, 0)
    ctx.expect_exact_gamepad_events([_x(0)])


def _other_group_changes(ctx: ScenarioContext) -> None:
    # The override profile replaces only the Strafe group. The Neutral group
    # keeps its held key through the change.
    ctx.source_key(NEUTRAL_UP, 1)
    ctx.expect_exact_gamepad_events([_y(LEFT)])
    ctx.set_profile_enabled(ROLLOVER_OVERRIDE_PROFILE_NAME, enabled=True)
    ctx.wait_for_hardware_mapping(HARDWARE_ID)
    ctx.expect_no_gamepad_events()

    ctx.source_key(NEUTRAL_DOWN, 1)
    ctx.expect_exact_gamepad_events([_y(0)])
    ctx.source_key(NEUTRAL_UP, 0)
    ctx.expect_exact_gamepad_events([_y(RIGHT)])
    ctx.source_key(NEUTRAL_DOWN, 0)
    ctx.expect_exact_gamepad_events([_y(0)])

    ctx.set_profile_enabled(ROLLOVER_OVERRIDE_PROFILE_NAME, enabled=False)
    ctx.wait_for_hardware_mapping(HARDWARE_ID)
    ctx.drain_outputs()


def _own_group_changes(ctx: ScenarioContext) -> None:
    # The override profile replaces the Strafe group with a first-wins group.
    # The held key moves into it and keeps driving the axis.
    ctx.source_key(STRAFE_LEFT, 1)
    ctx.expect_exact_gamepad_events([_x(LEFT)])
    ctx.set_profile_enabled(ROLLOVER_OVERRIDE_PROFILE_NAME, enabled=True)
    ctx.wait_for_hardware_mapping(HARDWARE_ID)
    ctx.expect_no_gamepad_events()

    ctx.source_key(STRAFE_RIGHT, 1)
    ctx.expect_no_gamepad_events()
    ctx.source_key(STRAFE_LEFT, 0)
    ctx.expect_exact_gamepad_events([_x(RIGHT)])
    ctx.source_key(STRAFE_RIGHT, 0)
    ctx.expect_exact_gamepad_events([_x(0)])

    ctx.set_profile_enabled(ROLLOVER_OVERRIDE_PROFILE_NAME, enabled=False)
    ctx.wait_for_hardware_mapping(HARDWARE_ID)
    ctx.drain_outputs()


def _combo_swallows_member_release(
    ctx: ScenarioContext,
    passthrough: evdev.InputDevice,
) -> None:
    def expect(events: list[tuple[int, int]]) -> None:
        ctx.expect_exact_events(
            passthrough,
            [(ecodes.EV_KEY, code, value) for code, value in events],
            label="primary passthrough",
            event_types={ecodes.EV_KEY},
        )

    ctx.set_profile_enabled(ROLLOVER_COMBO_PROFILE_NAME, enabled=True)
    ctx.wait_for_hardware_mapping(HARDWARE_ID)
    ctx.drain_outputs()
    ctx.drain_device(passthrough)
    try:
        ctx.source_key(PASS_FIRST, 1)
        expect([(PASS_FIRST, 1)])
        # Combo recall releases the passed-through member and swallows its
        # release.
        ctx.source_key(COMBO_PARTNER, 1)
        ctx.expect_keys([(COMBO_OUTPUT, 1)])
        ctx.expect_events(
            passthrough,
            [(ecodes.EV_KEY, PASS_FIRST, 0)],
            label="primary passthrough",
        )
        ctx.source_key(COMBO_PARTNER, 0)
        ctx.source_key(PASS_FIRST, 0)
        ctx.expect_keys([(COMBO_OUTPUT, 0)])
        ctx.drain_outputs()
        ctx.drain_device(passthrough)

        # The swallowed member is no longer held, so releasing the other
        # member must not hand the group back to it.
        ctx.source_key(PASS_SECOND, 1)
        expect([(PASS_SECOND, 1)])
        ctx.source_key(PASS_SECOND, 0)
        expect([(PASS_SECOND, 0)])

        # A recalled member that is still held stays out of the group until
        # its release, which the recall swallows.
        ctx.source_key(PASS_FIRST, 1)
        expect([(PASS_FIRST, 1)])
        ctx.source_key(COMBO_PARTNER, 1)
        ctx.expect_keys([(COMBO_OUTPUT, 1)])
        ctx.expect_events(
            passthrough,
            [(ecodes.EV_KEY, PASS_FIRST, 0)],
            label="primary passthrough",
        )
        ctx.source_key(COMBO_PARTNER, 0)
        ctx.expect_keys([(COMBO_OUTPUT, 0)])
        ctx.drain_outputs()
        ctx.drain_device(passthrough)
        ctx.source_key(PASS_SECOND, 1)
        expect([(PASS_SECOND, 1)])
        ctx.source_key(PASS_SECOND, 0)
        expect([(PASS_SECOND, 0)])
        ctx.source_key(PASS_FIRST, 0)
        ctx.expect_no_events(
            passthrough,
            label="primary passthrough",
            event_types={ecodes.EV_KEY},
        )
        ctx.expect_no_keyboard_events()
    finally:
        ctx.set_profile_enabled(ROLLOVER_COMBO_PROFILE_NAME, enabled=False)
        ctx.wait_for_hardware_mapping(HARDWARE_ID)


def _disable_while_held(ctx: ScenarioContext) -> None:
    ctx.source_key(STRAFE_LEFT, 1)
    ctx.expect_exact_gamepad_events([_x(LEFT)])
    ctx.source_key(STRAFE_RIGHT, 1)
    ctx.expect_exact_gamepad_events([_x(RIGHT)])

    ctx.set_profile_enabled(ROLLOVER_PROFILE_NAME, enabled=False)
    ctx.wait_for_hardware_mapping(HARDWARE_ID)
    ctx.expect_no_gamepad_events()

    # The suppressed key never drove the axis, so its release changes nothing.
    ctx.source_key(STRAFE_LEFT, 0)
    ctx.expect_no_gamepad_events()
    ctx.expect_no_keyboard_events()
    # The active key keeps the action it pressed with and releases the axis.
    ctx.source_key(STRAFE_RIGHT, 0)
    ctx.expect_exact_gamepad_events([_x(0)])

    ctx.set_profile_enabled(ROLLOVER_PROFILE_NAME, enabled=True)
    ctx.wait_for_hardware_mapping(HARDWARE_ID)
