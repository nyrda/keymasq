"""Pause/resume contracts through saved profiles, session IPC, and real evdev output."""

import time
from collections.abc import Iterator
from contextlib import contextmanager

import evdev
from support import HARDWARE_ID, ScenarioContext

PROFILE_NAME = "Integration Macro Pause"
PARENT_NAME = "integration-pause-parent"
CHILD_NAME = "integration-pause-child"
TRIGGER = evdev.ecodes.KEY_F13
OPENING = evdev.ecodes.KEY_1
CHILD_HELD = evdev.ecodes.KEY_2
CHILD_END = evdev.ecodes.KEY_3
PARENT_END = evdev.ecodes.KEY_4


def _key(code: int, value: int, t_us: int = 0) -> dict[str, object]:
    return {
        "device_type": "keyboard",
        "type": evdev.ecodes.EV_KEY,
        "code": code,
        "value": value,
        "t_us": t_us,
    }


def _expect_exact_keys(
    ctx: ScenarioContext, expected: list[tuple[int, int]], *, timeout_s: float = 5.0
) -> None:
    if ctx.keyboard_output is None:
        raise AssertionError("keyboard output is unavailable")
    observed: list[tuple[int, int]] = []
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for event in ctx.read_output_events(ctx.keyboard_output):
            if event.type == evdev.ecodes.EV_KEY:
                observed.append((int(event.code), int(event.value)))
        if observed != expected[: len(observed)]:
            raise AssertionError(f"expected exact key sequence {expected}; observed {observed}")
        if observed == expected:
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out expecting exact key sequence {expected}; observed {observed}")


@contextmanager
def _mapped_macros(ctx: ScenarioContext) -> Iterator[None]:
    profile_path = ctx.config_dir / "profiles" / "integration-macro-pause.toml"
    try:
        ctx.request(
            {
                "command": "create_macro",
                "macro": {
                    "name": CHILD_NAME,
                    "events": [
                        _key(CHILD_HELD, 1),
                        _key(CHILD_HELD, 0, 500_000),
                        _key(CHILD_END, 1, 500_000),
                        _key(CHILD_END, 0, 500_000),
                    ],
                },
            }
        )
        ctx.request(
            {
                "command": "create_macro",
                "macro": {
                    "name": PARENT_NAME,
                    "loop_mode": "none",
                    "loop_stop_behavior": "pause_run",
                    "pause_timeout_s": 0,
                    "events": [
                        _key(OPENING, 1),
                        _key(OPENING, 0),
                        {
                            "device_type": "macro",
                            "type": 0,
                            "code": 0,
                            "value": 0,
                            "t_us": 0,
                            "macro_action": "macro_sync",
                            "macro_name": CHILD_NAME,
                            "loop_mode": "none",
                            "loop_stop_behavior": "pause_run",
                            "pause_timeout_s": 1,
                        },
                        _key(PARENT_END, 1),
                        _key(PARENT_END, 0),
                    ],
                },
            }
        )
        profile_path.write_text(
            f'''[profile]
name = "{PROFILE_NAME}"
enabled = false
is_permanent = true
priority = 600
notify_on_activation = false
created_at = "2026-09-07T00:00:00"

[devices."{HARDWARE_ID}"]
always_grab_all = false

[devices."{HARDWARE_ID}".mapping.key_f13]
action = "macro"
target = "{PARENT_NAME}"
''',
            encoding="utf-8",
        )
        ctx.request({"command": "reload"})
        ctx.set_profile_enabled(PROFILE_NAME, enabled=True)
        ctx.reopen_outputs()
        yield
    finally:
        ctx.source_key(TRIGGER, 0)
        ctx.request({"command": "cancel_macro_playback"}, ok=False)
        ctx.request({"command": "disable_profile", "profile_name": PROFILE_NAME}, ok=False)
        profile_path.unlink(missing_ok=True)
        ctx.request({"command": "reload"})
        for name in (PARENT_NAME, CHILD_NAME):
            ctx.request({"command": "delete_macro", "name": name}, ok=False)
        ctx.drain_outputs()


def _start_and_pause(ctx: ScenarioContext) -> None:
    ctx.source_key(TRIGGER, 1)
    _expect_exact_keys(ctx, [(OPENING, 1), (OPENING, 0), (CHILD_HELD, 1)])
    ctx.source_key(TRIGGER, 0)
    _expect_exact_keys(ctx, [(CHILD_HELD, 0)])


def _verify_next_press_starts_fresh(ctx: ScenarioContext) -> None:
    ctx.source_key(TRIGGER, 0)
    ctx.expect_no_keyboard_events(timeout_s=0.1)
    ctx.source_key(TRIGGER, 1)
    _expect_exact_keys(ctx, [(OPENING, 1), (OPENING, 0), (CHILD_HELD, 1)])
    ctx.source_key(TRIGGER, 0)
    _expect_exact_keys(ctx, [(CHILD_HELD, 0)])


def run_resume(ctx: ScenarioContext) -> None:
    with _mapped_macros(ctx):
        _start_and_pause(ctx)
        # Longer than the child's timeline, shorter than its one-second timeout.
        ctx.expect_no_keyboard_events(timeout_s=0.65)
        ctx.source_key(TRIGGER, 1)
        ctx.expect_no_keyboard_events(timeout_s=0.15)
        _expect_exact_keys(ctx, [(CHILD_END, 1), (CHILD_END, 0), (PARENT_END, 1), (PARENT_END, 0)])
        ctx.expect_no_keyboard_events(timeout_s=0.2)
        _verify_next_press_starts_fresh(ctx)


def run_child_expiry(ctx: ScenarioContext) -> None:
    with _mapped_macros(ctx):
        _start_and_pause(ctx)
        ctx.expect_no_keyboard_events(timeout_s=1.3)
        ctx.source_key(TRIGGER, 1)
        # No opening replay, no child completion, and no duplicate cleanup release.
        _expect_exact_keys(ctx, [(PARENT_END, 1), (PARENT_END, 0)])
        ctx.expect_no_keyboard_events(timeout_s=0.6)
        _verify_next_press_starts_fresh(ctx)
