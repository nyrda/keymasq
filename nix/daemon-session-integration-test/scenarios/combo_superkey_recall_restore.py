import time

import evdev
from support import SECOND_HARDWARE_ID, ScenarioContext


def run(ctx: ScenarioContext) -> None:
    passthrough = ctx.open_passthrough_output(SECOND_HARDWARE_ID)
    try:
        ctx.secondary_key(evdev.ecodes.KEY_I, 1)
        ctx.expect_events(
            passthrough,
            [(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_I, 1)],
            label="secondary passthrough",
        )

        ctx.secondary_key(evdev.ecodes.KEY_J, 1)
        ctx.expect_events(
            passthrough,
            [(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_I, 0)],
            label="secondary passthrough",
        )
        ctx.expect_no_keyboard_events(timeout_s=0.12)

        ctx.secondary_key(evdev.ecodes.KEY_J, 0)
        expect_ordered_outputs(
            ctx,
            [
                ("keyboard", evdev.ecodes.EV_KEY, evdev.ecodes.KEY_R, 1),
                ("keyboard", evdev.ecodes.EV_KEY, evdev.ecodes.KEY_R, 0),
                ("passthrough", evdev.ecodes.EV_KEY, evdev.ecodes.KEY_I, 1),
            ],
            passthrough=passthrough,
        )

        ctx.secondary_key(evdev.ecodes.KEY_I, 0)
        ctx.expect_events(
            passthrough,
            [(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_I, 0)],
            label="secondary passthrough",
        )
    finally:
        passthrough.close()


def expect_ordered_outputs(
    ctx: ScenarioContext,
    expected: list[tuple[str, int, int, int]],
    *,
    passthrough: evdev.InputDevice,
    timeout_s: float = 3.0,
) -> None:
    devices = {
        "keyboard": ctx.keyboard_output,
        "passthrough": passthrough,
    }
    observed: list[tuple[str, int, int, int]] = []
    index = 0
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for label, device in devices.items():
            if device is None:
                continue
            for event in ctx.read_output_events(device):
                if event.type == evdev.ecodes.EV_SYN:
                    continue
                event_tuple = (label, int(event.type), int(event.code), int(event.value))
                observed.append(event_tuple)
                if event_tuple == expected[index]:
                    index += 1
                    if index == len(expected):
                        return
        time.sleep(0.01)

    raise AssertionError(
        "missing ordered combo recall/restore output "
        f"{format_events(ctx, expected)}; observed {format_events(ctx, observed)}"
    )


def format_events(
    ctx: ScenarioContext,
    events: list[tuple[str, int, int, int]],
) -> list[str]:
    return [
        f"{label}:{ctx.event_label((event_type, code, value))}"
        for label, event_type, code, value in events
    ]
