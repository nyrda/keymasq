"""Keep real shared output descriptors across removal of a composite mask."""

import contextlib
import json
import select
import sys
import time
from pathlib import Path

import control
import evdev
from behavior import keep, off, unlocked, wait_for, wait_state, write_mapping
from support import ScenarioContext

STATE = Path("/tmp/keymasq-shared-output-state.json")
PROFILES = ("Composite target mapping", "Unaffected gamepad mapping")


def setup():
    found = wait_for(
        "both USB attachments",
        lambda: control.devices(require_all=False),
        lambda found: set(found) == set(control.NAMES),
    )
    control.baseline()
    identities = [found[name].identity for name in control.NAMES]
    files = []
    with unlocked() as client:
        for name in control.NAMES:
            attachment = control.devices()[name]
            client.request(
                {
                    "command": "mask_hardware",
                    "id": attachment.identity,
                    "generation": attachment.generation,
                    "persist": False,
                }
            )
            keep(client, wait_state(client, attachment.identity, "trial"), persist=True)
            control.assert_masked(name, check_bystander=False)
    ctx = ScenarioContext()
    for index, name in enumerate(control.NAMES):
        attachment = control.devices()[name]
        events = [
            node for node in control.nodes(attachment).values() if node.name.startswith("event")
        ]
        assert len(events) == (2 if index == 0 else 1), events
        gamepad = next(
            node
            for node in events
            if (Path("/sys/class/input") / node.name / "device/capabilities/abs")
            .read_text()
            .strip()
            != "0"
        )
        stable_path = Path("/dev/input/by-id") / f"usb-{name}-event-joystick"
        assert stable_path.resolve() == gamepad, (stable_path, gamepad)
        files.extend(
            str(path)
            for path in write_mapping(
                ctx,
                hardware_id=f"{attachment.vendor}:{attachment.product}",
                path=str(stable_path),
                source="btn_south",
                kind="gamepad",
                profile=PROFILES[index],
                target="btn_west" if index == 0 else "btn_east",
                action="gamepad",
            )
        )
        ctx.set_profile_enabled(PROFILES[index], enabled=True)
    STATE.write_text(json.dumps({"ids": identities, "files": files}))


def buttons(output, expected):
    # Require new input in each phase, not events queued before disconnect.
    with contextlib.suppress(BlockingIOError):
        while True:
            list(output.read())
    seen = {code: set() for code in expected}
    deadline = time.monotonic() + 10
    while not all(values == {0, 1} for values in seen.values()):
        assert time.monotonic() < deadline, seen
        if select.select([output.fd], [], [], 0.1)[0]:
            for event in output.read():
                if event.type == evdev.ecodes.EV_KEY and event.code in seen:
                    seen[event.code].add(event.value)


def hold():
    ctx = ScenarioContext()
    identity = json.loads(STATE.read_text())["ids"][0]
    with contextlib.ExitStack() as stack:
        shared = [
            stack.enter_context(
                contextlib.closing(ctx.wait_for_output_device({f"keymasq-{kind}"}, kind))
            )
            for kind in ("keyboard", "mouse", "gamepad")
        ]
        buttons(shared[2], {evdev.ecodes.BTN_EAST, evdev.ecodes.BTN_WEST})
        Path("/tmp/keymasq-shared-held").touch()
        wait_for(
            "target disconnect",
            lambda: control.mask_state(ctx, identity),
            lambda item: item.get("lifecycle") == "waiting_for_device",
        )
        buttons(shared[2], {evdev.ecodes.BTN_EAST})
        for output in shared:
            output.active_keys()  # ENODEV if the shared device was destroyed and recreated.
        Path("/tmp/keymasq-shared-absent-ok").touch()
        wait_state(ctx, identity, "masked")
        buttons(shared[2], {evdev.ecodes.BTN_EAST, evdev.ecodes.BTN_WEST})
        for output in shared:
            output.active_keys()
        Path("/tmp/keymasq-shared-returned-ok").touch()


def cleanup():
    ctx = ScenarioContext()
    with unlocked() as client:
        for name in PROFILES:
            ctx.set_profile_enabled(name, enabled=False)
        for name in control.NAMES:
            off(client, name)
    for path in json.loads(STATE.read_text())["files"]:
        Path(path).unlink()
    ctx.request({"command": "reload"})


if __name__ == "__main__":
    {"setup": setup, "hold": hold, "cleanup": cleanup}[sys.argv[1]]()
