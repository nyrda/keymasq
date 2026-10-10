"""Steam Deck touch inputs through the bundled HID-BPF program on a hid-steam device.

The emulated Deck is bound by the kernel's hid-steam driver. The keymasq-hardware@
root job attaches the program and hands its descriptors to the capability-free
daemon, which reads the touch bits that hid-steam never reports.
"""

import contextlib
import json
import os
import pwd
import re
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import evdev
from steam_deck import PHYS, SteamDeck
from support import EVENT_TIMEOUT_S, ScenarioContext

DECK_HARDWARE_ID = "28de:1205"
DECK_TOUCH_PROFILE_NAME = "Integration Steam Deck Touch"
PROGRAM_NAME = "steam_deck_touc"
HID_GROUP_GENERIC = "g0001"
HID_GROUP_STEAM = "g0103"
LS_TOUCH = (13, 0x40)
RS_TOUCH = (13, 0x80)
LP_TOUCH = (10, 0x08)
RP_TOUCH = (10, 0x10)
BUTTON_A = (8, 0x80)
LEFT_PAD_X = 16
LEFT_STICK_X = 48
TOUCH_KEYS = (
    (LS_TOUCH, evdev.ecodes.KEY_1),
    (RS_TOUCH, evdev.ecodes.KEY_2),
    (LP_TOUCH, evdev.ecodes.KEY_3),
    (RP_TOUCH, evdev.ecodes.KEY_4),
)


def _run(*command: str) -> str:
    return subprocess.run(command, check=True, capture_output=True, text=True, timeout=90).stdout


def _attachments() -> list[int]:
    sudo = os.environ["KEYMASQ_INTEGRATION_SUDO"]
    output = _run(sudo, os.environ["KEYMASQ_INTEGRATION_BPFTOOL"], "--json", "struct_ops", "show")
    return sorted(item["id"] for item in json.loads(output or "[]") if item["name"] == PROGRAM_NAME)


def _probe(*arguments: str) -> dict[str, object]:
    sudo = os.environ["KEYMASQ_INTEGRATION_SUDO"]
    probe = os.environ["KEYMASQ_INTEGRATION_HID_BPF_PROBE"]
    return json.loads(_run(sudo, "-u", "keymasq", probe, *arguments))


def _daemon_journal(since: int) -> str:
    return _run("journalctl", "-u", "keymasqd.service", f"--since=@{since}", "-o", "cat")


def _deck_hid_devices() -> dict[str, str]:
    devices: dict[str, str] = {}
    for device in Path("/sys/bus/hid/devices").glob("*"):
        with contextlib.suppress(OSError):
            fields = dict(
                line.split("=", 1)
                for line in (device / "uevent").read_text().splitlines()
                if "=" in line
            )
            group = re.search(r"g[0-9A-F]{4}", fields.get("MODALIAS", ""))
            if fields.get("HID_PHYS") == PHYS and group is not None:
                devices[group.group(0)] = device.name
    return devices


def _input_names(hid_device: str) -> dict[str, str]:
    return {
        (event.parent / "name").read_text().strip(): f"/dev/input/{event.name}"
        for event in Path("/sys/bus/hid/devices", hid_device).glob("input/input*/event*")
    }


def _bound() -> bool:
    devices = _deck_hid_devices()
    gamepad = devices.get(HID_GROUP_GENERIC)
    return (
        gamepad is not None
        and HID_GROUP_STEAM in devices
        and Path("/sys/bus/hid/devices", gamepad, "driver").resolve().name == "hid-steam"
        and {"Steam Deck", "Steam Deck Motion Sensors"} <= _input_names(gamepad).keys()
    )


@contextlib.contextmanager
def _connected(ctx: ScenarioContext) -> Iterator[SteamDeck]:
    with contextlib.closing(SteamDeck()) as deck:
        ctx.wait_until("hid-steam gamepad, motion, and client devices", _bound)
        ctx.settle_udev()
        yield deck
    ctx.wait_until("Steam Deck HID devices removed", lambda: not _deck_hid_devices())


def _wait_attached(ctx: ScenarioContext, label: str, *, previous: list[int]) -> list[int]:
    observed: list[list[int]] = [[]]

    def attached() -> bool:
        observed[0] = _attachments()
        return len(observed[0]) == 1 and observed[0] != previous

    try:
        ctx.wait_until(label, attached)
    except AssertionError as exc:
        raise AssertionError(f"{exc}; HID-BPF struct_ops maps: {observed[0]}") from exc
    ctx.assert_daemon_has_no_capabilities()
    return observed[0]


def _wait_detached(ctx: ScenarioContext, label: str) -> None:
    ctx.wait_until(label, lambda: not _attachments())


def _expect_touches(ctx: ScenarioContext, deck: SteamDeck) -> None:
    ctx.drain_outputs()
    for bits, key in TOUCH_KEYS:
        deck.report(bits)
        ctx.expect_exact_keys([(key, 1)])
        deck.report()
        ctx.expect_exact_keys([(key, 0)])
    deck.report(LS_TOUCH, RP_TOUCH)
    pressed: set[tuple[int, int]] = set()
    deadline = time.monotonic() + EVENT_TIMEOUT_S
    while len(pressed) < 2 and time.monotonic() < deadline:
        pressed.update(
            (event.code, event.value)
            for event in ctx.read_output_events(ctx.keyboard_output)
            if event.type == evdev.ecodes.EV_KEY
        )
        time.sleep(0.01)
    assert pressed == {(evdev.ecodes.KEY_1, 1), (evdev.ecodes.KEY_4, 1)}, pressed
    deck.report(RP_TOUCH)
    ctx.expect_exact_keys([(evdev.ecodes.KEY_1, 0)])
    deck.report()
    ctx.expect_exact_keys([(evdev.ecodes.KEY_4, 0)])


def _expect_gamepad(gamepad: evdev.InputDevice, expected: set[tuple[int, int, int]]) -> None:
    observed: set[tuple[int, int, int]] = set()
    deadline = time.monotonic() + EVENT_TIMEOUT_S
    while not expected <= observed and time.monotonic() < deadline:
        with contextlib.suppress(BlockingIOError):
            observed.update(
                (event.type, event.code, event.value)
                for event in gamepad.read()
                if event.type in {evdev.ecodes.EV_KEY, evdev.ecodes.EV_ABS}
            )
        time.sleep(0.01)
    assert expected <= observed, f"hid-steam output {sorted(observed)} lacks {sorted(expected)}"


def _expect_hid_steam_output(ctx: ScenarioContext, deck: SteamDeck) -> None:
    node = _input_names(_deck_hid_devices()[HID_GROUP_GENERIC])["Steam Deck"]
    with contextlib.closing(evdev.InputDevice(node)) as gamepad:
        ctx.drain_device(gamepad)
        deck.report(BUTTON_A, LP_TOUCH, axes={LEFT_PAD_X: -9000, LEFT_STICK_X: 12000})
        _expect_gamepad(
            gamepad,
            {
                (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SOUTH, 1),
                (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X, 12000),
                (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_HAT0X, -9000),
            },
        )
        ctx.expect_exact_keys([(evdev.ecodes.KEY_3, 1)])
        deck.report()
        _expect_gamepad(
            gamepad,
            {
                (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SOUTH, 0),
                (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X, 0),
                (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_HAT0X, 0),
            },
        )
        ctx.expect_exact_keys([(evdev.ecodes.KEY_3, 0)])


def _expect_refusals(ctx: ScenarioContext, attached: list[int]) -> None:
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as client:
        try:
            client.connect("/run/keymasq/handoff")
        except PermissionError:
            pass
        else:
            raise AssertionError("the handoff socket accepted a session user")

    since = int(time.time()) - 1
    assert _probe("handoff") == {"uid": pwd.getpwnam("keymasq").pw_uid, "closed": True}
    rejection = f"Rejected fd handoff from uid {pwd.getpwnam('keymasq').pw_uid}"
    ctx.wait_until(
        "handoff rejection in the keymasqd journal",
        lambda: rejection in _daemon_journal(since),
    )

    devices = _deck_hid_devices()
    client_device = _probe("attach", devices[HID_GROUP_STEAM], "steam-deck-touch")
    assert client_device == {
        "status": "error",
        "message": "The HID device does not match this driver",
    }, client_device
    hidraw_driver = _probe("attach", devices[HID_GROUP_GENERIC], "8bitdo-ultimate2")
    assert hidraw_driver == {"status": "error", "message": "Unknown HID-BPF driver"}, hidraw_driver
    assert _attachments() == attached


def run(ctx: ScenarioContext) -> None:
    values = {
        "DECK_PHYS": PHYS,
        "DECK_HARDWARE_ID": DECK_HARDWARE_ID,
        "DECK_TOUCH_PROFILE_NAME": DECK_TOUCH_PROFILE_NAME,
    }
    hardware = ctx.config_dir / "hardware" / "28de_1205.toml"
    profile = ctx.config_dir / "profiles" / "steam-deck-touch.toml"
    ctx.write_fixture(hardware, "hardware/steam-deck.toml", values)
    ctx.write_fixture(profile, "profiles/steam-deck-touch.toml", values)
    ctx.request({"command": "reload"})
    try:
        with _connected(ctx) as deck:
            ctx.request({"command": "reevaluate_hardware"})
            ctx.set_profile_enabled(DECK_TOUCH_PROFILE_NAME, enabled=True)
            ctx.wait_for_hardware_mapping(DECK_HARDWARE_ID)
            attached = _wait_attached(ctx, "HID-BPF attachment", previous=[])
            _expect_touches(ctx, deck)
            _expect_hid_steam_output(ctx, deck)
            _expect_refusals(ctx, attached)

            ctx.restart_keymasqd()
            ctx.wait_for_hardware_mapping(DECK_HARDWARE_ID)
            attached = _wait_attached(
                ctx, "HID-BPF attachment after daemon restart", previous=attached
            )
            _expect_touches(ctx, deck)
        _wait_detached(ctx, "HID-BPF detach after Steam Deck removal")

        with _connected(ctx) as deck:
            ctx.wait_for_hardware_mapping(DECK_HARDWARE_ID)
            _wait_attached(ctx, "HID-BPF attachment after reconnect", previous=attached)
            _expect_touches(ctx, deck)
            _expect_hid_steam_output(ctx, deck)
        _wait_detached(ctx, "HID-BPF detach after second removal")
    finally:
        ctx.request(
            {"command": "disable_profile", "profile_name": DECK_TOUCH_PROFILE_NAME}, ok=False
        )
        hardware.unlink(missing_ok=True)
        profile.unlink(missing_ok=True)
        ctx.request({"command": "reload"})
