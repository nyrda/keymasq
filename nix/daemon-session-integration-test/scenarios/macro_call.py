import json
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import evdev
from support import HARDWARE_ID, ScenarioContext

PROFILE_NAME = "Integration Macro Call"
CHILD_NAME = "integration-call-child"
CALLER_NAME = "integration-call-caller"
MISSING_NAME = "integration-call-missing"
MISSING_CALLER_NAME = "integration-call-missing-caller"
SELF_CALLER_NAME = "integration-call-self"
LOOP_A_NAME = "integration-call-loop-a"
LOOP_B_NAME = "integration-call-loop-b"
CALLER_TRIGGER = evdev.ecodes.KEY_F13
MISSING_TRIGGER = evdev.ecodes.KEY_F14
SELF_TRIGGER = evdev.ecodes.KEY_F15
LOOP_TRIGGER = evdev.ecodes.KEY_F16
CHILD_KEYS = [
    (evdev.ecodes.KEY_1, 1),
    (evdev.ecodes.KEY_1, 0),
    (evdev.ecodes.KEY_2, 1),
    (evdev.ecodes.KEY_2, 0),
]
EV_KEY = evdev.ecodes.EV_KEY
Key = tuple[int, int]


def _key(code: int, value: int, t_us: int = 0) -> dict[str, object]:
    return {"device_type": "keyboard", "type": EV_KEY, "code": code, "value": value, "t_us": t_us}


def _typed(*names: str) -> list[Key]:
    return [
        (evdev.ecodes.ecodes[f"KEY_{name.upper()}"], value) for name in names for value in (1, 0)
    ]


US_SHIFTED = {
    evdev.ecodes.KEY_COMMA: "<",
    evdev.ecodes.KEY_DOT: ">",
    evdev.ecodes.KEY_SEMICOLON: ":",
}
US_SHIFT_KEYS = {evdev.ecodes.KEY_LEFTSHIFT, evdev.ecodes.KEY_RIGHTSHIFT}


def _us_text(keys: list[Key]) -> str:
    shifted = False
    text = []
    for code, value in keys:
        if code in US_SHIFT_KEYS:
            shifted = value != 0
            continue
        if value != 1:
            continue
        name = evdev.ecodes.KEY[code].removeprefix("KEY_").lower()
        if shifted and code in US_SHIFTED:
            text.append(US_SHIFTED[code])
        elif not shifted and code == evdev.ecodes.KEY_MINUS:
            text.append("-")
        elif not shifted and len(name) == 1 and name.isalpha():
            text.append(name)
        else:
            raise AssertionError(f"unexpected key {name} (shift={shifted}) in {keys}")
    return "".join(text)


def _create_type_macro(ctx: ScenarioContext, name: str, text: str) -> None:
    ctx.request(
        {
            "command": "create_macro",
            "macro": {"name": name, "type_binding": True, "type_text": text, "events": []},
        }
    )


def _print_json_events(text: str) -> list[dict[str, Any]]:
    result = subprocess.run(
        ["keymasq", "type", "--print-json", "--layout", "us", text],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise AssertionError(f"keymasq type --print-json {text!r} failed: {result.stdout!r}")
    return json.loads(result.stdout)["events"]


def _type_and_wait(ctx: ScenarioContext, text: str) -> tuple[int, dict[str, Any], list[Key]]:
    keyboard = ctx.keyboard_output
    if keyboard is None:
        raise AssertionError("keyboard output is unavailable")
    keys: list[Key] = []

    def read_keys() -> None:
        keys.extend(
            (int(event.code), int(event.value))
            for event in ctx.read_output_events(keyboard)
            if event.type == EV_KEY
        )

    # Read while typing: the evdev client buffer drops events of a long burst.
    process = subprocess.Popen(
        ["keymasq", "type", "--json", "--wait", "--layout", "us", text],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 30
    while process.poll() is None and time.monotonic() < deadline:
        read_keys()
        time.sleep(0.005)
    if process.poll() is None:
        process.kill()
    stdout, stderr = process.communicate()
    quiet_deadline = time.monotonic() + 0.25
    while time.monotonic() < quiet_deadline:
        read_keys()
        time.sleep(0.01)
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"keymasq type {text!r} printed no JSON (exit={process.returncode}): "
            f"{stdout!r} {stderr!r}"
        ) from exc
    return process.returncode, payload, keys


def _expect_typed(ctx: ScenarioContext, text: str, expected: list[Key]) -> None:
    returncode, payload, keys = _type_and_wait(ctx, text)
    if returncode != 0 or payload.get("state") != "completed":
        raise AssertionError(f"keymasq type {text!r} did not complete: {payload}")
    if keys != expected:
        raise AssertionError(f"keymasq type {text!r} typed {keys}, expected {expected}")


def _expect_type_failure(
    ctx: ScenarioContext, text: str, expected: list[Key], *fragments: str
) -> None:
    returncode, payload, keys = _type_and_wait(ctx, text)
    message = str(payload.get("message", ""))
    if returncode != 1 or payload.get("state") != "failed":
        raise AssertionError(f"keymasq type {text!r} did not fail: {payload}")
    if not all(fragment in message for fragment in fragments):
        raise AssertionError(f"keymasq type {text!r} failed with {message!r}, wanted {fragments}")
    if keys != expected:
        raise AssertionError(f"keymasq type {text!r} typed {keys}, expected {expected}")


def _expect_healthy(ctx: ScenarioContext) -> None:
    status = ctx.request({"command": "get_status"})
    if status.get("keymasqd_connected") is not True:
        raise AssertionError(f"keymasqd disconnected after a failed macro call: {status}")
    _expect_typed(ctx, f"<macro:{CHILD_NAME}>x", [*CHILD_KEYS, *_typed("x")])


@contextmanager
def _call_macros(ctx: ScenarioContext) -> Iterator[None]:
    profile_path = ctx.config_dir / "profiles" / "integration-macro-call.toml"
    names = (
        CHILD_NAME,
        CALLER_NAME,
        MISSING_CALLER_NAME,
        SELF_CALLER_NAME,
        LOOP_A_NAME,
        LOOP_B_NAME,
    )
    try:
        ctx.request(
            {
                "command": "create_macro",
                "macro": {
                    "name": CHILD_NAME,
                    "events": [
                        _key(evdev.ecodes.KEY_1, 1),
                        _key(evdev.ecodes.KEY_1, 0, 300_000),
                        _key(evdev.ecodes.KEY_2, 1, 300_000),
                        _key(evdev.ecodes.KEY_2, 0, 300_000),
                    ],
                },
            }
        )
        _create_type_macro(ctx, CALLER_NAME, f"<macro:{CHILD_NAME}>hi<enter>")
        _create_type_macro(ctx, MISSING_CALLER_NAME, f"a<macro:{MISSING_NAME}>b")
        _create_type_macro(ctx, SELF_CALLER_NAME, f"r<macro:{SELF_CALLER_NAME}>s")
        _create_type_macro(ctx, LOOP_A_NAME, f"a<macro:{LOOP_B_NAME}>c")
        _create_type_macro(ctx, LOOP_B_NAME, f"b<macro:{LOOP_A_NAME}>d")
        ctx.write_fixture(
            profile_path,
            "profiles/macro-call.toml",
            {
                "HARDWARE_ID": HARDWARE_ID,
                "MACRO_CALL_PROFILE_NAME": PROFILE_NAME,
                "CALLER_MACRO_NAME": CALLER_NAME,
                "MISSING_CALLER_MACRO_NAME": MISSING_CALLER_NAME,
                "SELF_CALLER_MACRO_NAME": SELF_CALLER_NAME,
                "LOOP_A_MACRO_NAME": LOOP_A_NAME,
            },
        )
        ctx.request({"command": "reload"})
        ctx.set_profile_enabled(PROFILE_NAME, enabled=True)
        ctx.reopen_outputs()
        yield
    finally:
        for code in (CALLER_TRIGGER, MISSING_TRIGGER, SELF_TRIGGER, LOOP_TRIGGER):
            ctx.source_key(code, 0)
        ctx.request({"command": "cancel_macro_playback"}, ok=False)
        ctx.request({"command": "disable_profile", "profile_name": PROFILE_NAME}, ok=False)
        profile_path.unlink(missing_ok=True)
        ctx.request({"command": "reload"})
        for name in names:
            ctx.request({"command": "delete_macro", "name": name}, ok=False)
        ctx.drain_outputs()


def run_mapped(ctx: ScenarioContext) -> None:
    with _call_macros(ctx):
        ctx.tap_source(CALLER_TRIGGER)
        ctx.expect_exact_keys([*CHILD_KEYS, *_typed("h", "i", "enter")])

        ctx.tap_source(MISSING_TRIGGER)
        ctx.expect_exact_keys(_typed("a"))
        ctx.expect_no_keyboard_events(timeout_s=0.5)

        ctx.tap_source(SELF_TRIGGER)
        ctx.expect_exact_keys(_typed("r"))
        ctx.expect_no_keyboard_events(timeout_s=0.5)

        ctx.tap_source(LOOP_TRIGGER)
        ctx.expect_exact_keys(_typed("a", "b"))
        ctx.expect_no_keyboard_events(timeout_s=0.5)

        ctx.tap_source(CALLER_TRIGGER)
        ctx.expect_exact_keys([*CHILD_KEYS, *_typed("h", "i", "enter")])


def run_cli(ctx: ScenarioContext) -> None:
    with _call_macros(ctx):
        _expect_typed(
            ctx, f"<macro:{CHILD_NAME}>hi<enter>", [*CHILD_KEYS, *_typed("h", "i", "enter")]
        )
        _expect_typed(ctx, f"<MACRO:  {CHILD_NAME}  >x", [*CHILD_KEYS, *_typed("x")])

        escaped = f"\\<macro:{CHILD_NAME}>"
        returncode, payload, keys = _type_and_wait(ctx, escaped)
        if returncode != 0 or payload.get("state") != "completed":
            raise AssertionError(f"keymasq type {escaped!r} did not complete: {payload}")
        if _us_text(keys) != f"<macro:{CHILD_NAME}>":
            raise AssertionError(f"keymasq type {escaped!r} typed {_us_text(keys)!r}")

        _expect_type_failure(
            ctx, f"a<macro:{MISSING_NAME}>b", _typed("a"), MISSING_NAME, "not found"
        )
        _expect_healthy(ctx)

        _expect_type_failure(
            ctx,
            f"<macro:{LOOP_A_NAME}>",
            _typed("a", "b"),
            LOOP_A_NAME,
            "recursive macro call blocked",
        )
        _expect_healthy(ctx)

        call, *typed = _print_json_events(f"<Macro: {CHILD_NAME} >hi")
        expected_call = {
            "macro_action": "macro_sync",
            "macro_name": CHILD_NAME,
            "loop_mode": "none",
            "loop_count": 1,
            "speed": 1.0,
            "replay_mouse_movement": True,
            "replay_mouse_clicks": True,
        }
        if {key: call.get(key) for key in expected_call} != expected_call:
            raise AssertionError(f"--print-json compiled an unexpected call event: {call}")
        if [(event["code"], event["value"]) for event in typed] != _typed("h", "i"):
            raise AssertionError(f"--print-json compiled unexpected typed events: {typed}")
        ctx.expect_no_keyboard_events()
