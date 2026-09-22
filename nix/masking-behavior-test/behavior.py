"""User-visible masking contracts checked against installed services and Linux."""

import contextlib
import select
import sys
import time
from pathlib import Path

import evdev
import fixture
from control import (
    NAMES,
    GuiClient,
    assert_masked,
    assert_revoked,
    devices,
    mask_state,
    metadata,
    nodes,
    restored,
    restored_device,
)
from support import ScenarioContext


def wait_for(label, observe, accept, *, timeout=35):
    deadline = time.monotonic() + timeout
    while True:
        value = observe()
        if accept(value):
            return value
        assert time.monotonic() < deadline, f"{label}: {value}"
        time.sleep(0.1)


def wait_state(ctx, identity, phase, *, timeout=35):
    return wait_for(
        f"{identity} becomes {phase}",
        lambda: mask_state(ctx, identity),
        lambda state: state.get("state") == phase,
        timeout=timeout,
    )


@contextlib.contextmanager
def unlocked():
    with contextlib.closing(GuiClient()) as ctx:
        ctx.request({"command": "claim_recording_unlock_refresh"})
        yield ctx


def trial(ctx, name=NAMES[0]):
    target = devices()[name]
    with contextlib.ExitStack() as stack:
        handles = [
            stack.enter_context(
                open(node, "r+b" if "/bus/usb/" in str(node) else "rb", buffering=0)
            )
            for node in nodes(target).values()
        ]
        ctx.request(
            {
                "command": "mask_hardware",
                "id": target.identity,
                "generation": target.generation,
                "persist": False,
            }
        )
        state = wait_state(ctx, target.identity, "trial")
        for handle in handles:
            assert_revoked(handle)
    assert_masked(name, forwarding=True, check_bystander=False)
    return state


def keep(ctx, state, *, persist=False):
    ctx.request(
        {
            "command": "keep_hardware_mask",
            "id": state["id"],
            "token": state["token"],
            "persist": persist,
        }
    )
    return wait_state(ctx, state["id"], "masked")


def off(ctx, name=NAMES[0]):
    identity = devices()[name].identity
    state = mask_state(ctx, identity)
    ctx.request(
        {"command": "restore_hardware", "id": identity, "token": state["token"], "persist": False}
    )
    state = wait_state(ctx, identity, "restored")
    assert not state["enabled"], state
    if state["has_saved_mask"]:
        assert not state["persist"], state
    restored_device(name)


def assert_off():
    ctx = ScenarioContext()
    # Observe several supervisor polls after restart so a delayed automatic
    # activation cannot pass an immediate post-start filesystem assertion.
    deadline = time.monotonic() + 3
    while True:
        result = ctx.request({"command": "hardware_inventory"})
        assert all(not state["enabled"] for state in result["masks"]), result
        restored()
        if time.monotonic() >= deadline:
            break
        time.sleep(0.1)


def trial_expiry(*, disconnect=False):
    with unlocked() as ctx:
        state = trial(ctx)
        identity = state["id"]
        started = time.monotonic()
        if not disconnect:
            state = wait_state(ctx, identity, "restored", timeout=40)
    if disconnect:
        # The GUI connection is gone. The independent background owner must
        # still enforce the real production confirmation deadline.
        state = wait_state(ScenarioContext(), identity, "restored", timeout=40)
    assert state["reason"] == "trial_expired", state
    assert 20 < time.monotonic() - started < 40, state
    assert not state["enabled"], state
    restored()


def undo():
    with unlocked() as ctx:
        trial(ctx)
        off(ctx)
        assert not mask_state(ctx, devices()[NAMES[0]].identity)["has_saved_mask"]
    restored()


def rejected_locked():
    ctx = ScenarioContext()
    status = ctx.request({"command": "get_status"})
    assert status["recording_unlock_required"] and not status["recording_unlocked"], status
    target = devices()[NAMES[0]]
    result = ctx.request(
        {"command": "mask_hardware", "id": target.identity, "generation": target.generation},
        ok=False,
    )
    assert result.get("status") == "error", result
    assert result.get("error_code") == "sensitive_command_denied", result
    assert_off()


def snapshot(ctx):
    result = ctx.request({"command": "hardware_inventory"})
    return {
        "masks": {
            item["id"]: (item["state"], item["token"], item["enabled"], item["persist"])
            for item in result["masks"]
        },
        "permissions": {name: metadata(item) for name, item in devices().items()},
        "rules": {
            path.name: path.read_text()
            for path in Path("/run/udev/rules.d").glob("*-keymasq-masking-*.rules")
        },
    }


def reject_unchanged(ctx, payload, expected_message):
    before = snapshot(ctx)
    result = ctx.request(payload, ok=False)
    assert result.get("status") == "error", result
    assert expected_message in result.get("message", ""), result
    assert snapshot(ctx) == before, result


def rejected_tokens():
    with unlocked() as ctx:
        first = trial(ctx)
        second = trial(ctx, NAMES[1])
        for command, message in (
            ("keep_hardware_mask", "trial has ended"),
            ("restore_hardware", "mask has changed"),
        ):
            reject_unchanged(
                ctx,
                {"command": command, "id": second["id"], "token": first["token"], "persist": False},
                message,
            )
        keep(ctx, second)
        off(ctx)
        current = trial(ctx)
        assert current["token"] != first["token"], (first, current)
        for command, message in (
            ("keep_hardware_mask", "trial has ended"),
            ("restore_hardware", "mask has changed"),
        ):
            reject_unchanged(
                ctx,
                {
                    "command": command,
                    "id": current["id"],
                    "token": first["token"],
                    "persist": False,
                },
                message,
            )
        keep(ctx, current)
        for name in NAMES:
            assert_masked(name, forwarding=True, check_bystander=False)
        # A second GUI connection cannot use the first client's unlock lease.
        before = snapshot(ctx)
        result = ScenarioContext().request(
            {
                "command": "set_hardware_mask_persistence",
                "id": current["id"],
                "token": current["token"],
                "persist": True,
            },
            ok=False,
        )
        assert result.get("error_code") == "sensitive_command_denied", result
        assert snapshot(ctx) == before
        ctx.request({"command": "restore_hardware", "persist": False})
    restored()


def virtual_paths(name):
    return [
        f"/dev/input/{event.name}"
        for path in Path("/sys/devices/virtual/input").glob("input*/name")
        if path.read_text().strip() == name
        for event in path.parent.glob("event*")
    ]


def reconnect():
    with unlocked() as ctx:
        first = keep(ctx, trial(ctx), persist=True)
        second = keep(ctx, trial(ctx, NAMES[1]), persist=True)
        old = devices()[NAMES[0]]
        old_output = virtual_paths(NAMES[0])
        assert len(old_output) == 1, old_output
        with contextlib.closing(evdev.InputDevice(old_output[0])) as output:
            fixture.request("disconnect")
            wait_for(
                "physical disconnect",
                lambda: devices(require_all=False),
                lambda found: NAMES[0] not in found,
            )
            wait_for(
                "saved mask waiting for reconnect",
                lambda: mask_state(ctx, first["id"]),
                lambda state: state.get("lifecycle") == "waiting_for_device",
            )
            assert_revoked(output)
        assert mask_state(ctx, second["id"])["token"] == second["token"]
        assert_masked(NAMES[1], forwarding=True, check_bystander=False)
        assert not virtual_paths(NAMES[0]), "disconnected controller left a virtual output"
        fixture.request("connect")
        found = wait_for(
            "physical reconnect",
            lambda: devices(require_all=False),
            lambda found: NAMES[0] in found,
        )
        new = found[NAMES[0]]
        assert new.identity == old.identity and new.generation != old.generation, (old, new)
        state = wait_state(ctx, first["id"], "masked")
        assert state["automatic"] and state["token"] != first["token"], state
        for name in NAMES:
            assert_masked(name, forwarding=True, check_bystander=False)
        assert mask_state(ctx, second["id"])["token"] == second["token"]
        ctx.request({"command": "restore_hardware", "persist": False})
    restored()


def write_mapping(
    ctx, *, hardware_id, path, source, kind, profile, target, priority=100, action="keyboard"
):
    vendor, product = hardware_id.split(":")
    hardware_dir = ctx.config_dir / "hardware"
    profiles_dir = ctx.config_dir / "profiles"
    hardware_dir.mkdir(parents=True, exist_ok=True)
    profiles_dir.mkdir(parents=True, exist_ok=True)
    hardware_file = hardware_dir / f"{vendor}_{product}.toml"
    hardware_file.write_text(f'''[hardware]
name = "Mask behavior {kind}"
vendor_id = "{vendor}"
product_id = "{product}"
[[hardware.evdev.devices]]
path = "{path}"
type = "{kind}"
id = "source"
[hardware.layout]
type = "{kind}"
[[hardware.layout.buttons]]
id = "{source}"
label = "Test button"
evdev = "{source}"
source = "source"
type = "key"
''')
    profile_file = profiles_dir / f"{profile}.toml"
    profile_file.write_text(f'''[profile]
name = "{profile}"
enabled = false
is_permanent = true
priority = {priority}
notify_on_activation = false
created_at = "2026-09-21T00:00:00"
[devices."{hardware_id}"]
always_grab_all = false
[devices."{hardware_id}".mapping.{source}]
action = "{action}"
target = "{target}"
''')
    ctx.request({"command": "reload"}, timeout=30)
    ctx.request({"command": "reevaluate_hardware"}, timeout=30)
    return hardware_file, profile_file


def read_keys(output):
    try:
        return [
            (event.code, event.value)
            for event in output.read()
            if event.type == evdev.ecodes.EV_KEY and event.value != 2
        ]
    except BlockingIOError:
        return []


def expect_keys(output, expected):
    seen = []
    deadline = time.monotonic() + 5
    while len(seen) < len(expected):
        assert time.monotonic() < deadline, (expected, seen)
        if select.select([output.fd], [], [], 0.1)[0]:
            seen.extend(read_keys(output))
    assert seen == expected, (expected, seen)
    # Catch duplicate or unexpected events arriving just after the right pair.
    end = time.monotonic() + 0.15
    while time.monotonic() < end:
        if select.select([output.fd], [], [], 0.02)[0]:
            assert not read_keys(output), "unexpected extra keyboard events"


def keyboard_tap(output):
    fixture.request("keyboard", value=1)
    expect_keys(output, [(evdev.ecodes.KEY_Z, 1)])
    fixture.request("keyboard", value=0)
    expect_keys(output, [(evdev.ecodes.KEY_Z, 0)])


def multiple_masks():
    normal = ScenarioContext()
    path = virtual_paths(fixture.KEYBOARD_NAME)
    assert len(path) == 1, path
    with unlocked() as ctx:
        files = write_mapping(
            normal,
            hardware_id="cafe:0004",
            path=path[0],
            source="key_a",
            kind="keyboard",
            profile="Mask ordinary remapping",
            target="key_z",
        )
        normal.set_profile_enabled("Mask ordinary remapping", enabled=True)
        with contextlib.closing(
            normal.wait_for_output_device({"keymasq-keyboard"}, "keyboard")
        ) as output:
            keyboard_tap(output)
            first = keep(ctx, trial(ctx), persist=True)
            second = keep(ctx, trial(ctx, NAMES[1]), persist=True)
            assert first["id"] != second["id"] and first["token"] != second["token"]
            keyboard_tap(output)
            off(ctx)
            assert mask_state(ctx, second["id"])["token"] == second["token"]
            assert_masked(NAMES[1], forwarding=True, check_bystander=False)
            keyboard_tap(output)
            keep(ctx, trial(ctx), persist=True)
            ctx.request({"command": "restore_hardware", "persist": False})
            restored()
            inventory = ctx.request({"command": "hardware_inventory"})
            assert not inventory["remapping_suspended"], inventory
            assert all(
                not item["enabled"] and not item["persist"] for item in inventory["masks"]
            ), inventory
            keyboard_tap(output)
        normal.set_profile_enabled("Mask ordinary remapping", enabled=False)
        for path in files:
            path.unlink()
        normal.request({"command": "reload"})


def remapping():
    normal = ScenarioContext()
    with unlocked() as ctx:
        keep(ctx, trial(ctx))
        fixture.request("button", value=0)
        target = devices()[NAMES[0]]
        event = next(node for node in nodes(target).values() if node.name.startswith("event"))
        files = set(
            write_mapping(
                normal,
                hardware_id="2dc8:6012",
                path=str(event),
                source="btn_south",
                kind="gamepad",
                profile="Mask mapping Q",
                target="key_q",
            )
        )
        files.update(
            write_mapping(
                normal,
                hardware_id="2dc8:6012",
                path=str(event),
                source="btn_south",
                kind="gamepad",
                profile="Mask mapping W",
                target="key_w",
                priority=200,
            )
        )
        normal.set_profile_enabled("Mask mapping Q", enabled=True)
        assert_masked()
        with contextlib.closing(
            normal.wait_for_output_device({"keymasq-keyboard"}, "keyboard")
        ) as output:
            fixture.request("button", value=1)
            expect_keys(output, [(evdev.ecodes.KEY_Q, 1)])
            # Change the route while the physical button is still held.
            normal.set_profile_enabled("Mask mapping W", enabled=True)
            assert_masked()
            # Existing gestures keep their original action until release. The
            # next press uses the new profile, matching ordinary remapping.
            assert output.active_keys() == [evdev.ecodes.KEY_Q], output.active_keys()
            fixture.request("button", value=0)
            expect_keys(output, [(evdev.ecodes.KEY_Q, 0)])
            assert not output.active_keys(), output.active_keys()
            fixture.request("button", value=1)
            expect_keys(output, [(evdev.ecodes.KEY_W, 1)])
            fixture.request("button", value=0)
            expect_keys(output, [(evdev.ecodes.KEY_W, 0)])
            normal.set_profile_enabled("Mask mapping Q", enabled=False)
            fixture.request("button", value=1)
            expect_keys(output, [(evdev.ecodes.KEY_W, 1)])
            normal.request({"command": "disable_profile", "profile_name": "Mask mapping W"})
            fixture.request("button", value=0)
            expect_keys(output, [(evdev.ecodes.KEY_W, 0)])
            normal.wait_for_active_profile("Mask mapping W", enabled=False)
            assert_masked()
            assert not output.active_keys(), output.active_keys()
        for path in files:
            path.unlink()
        normal.request({"command": "reload"})
        normal.request({"command": "reevaluate_hardware"})
        fixture.request("button", value=None)
        wait_for(
            "setup passthrough output",
            lambda: virtual_paths(NAMES[0]),
            lambda paths: len(paths) == 1,
        )
        assert_masked(forwarding=True)
        off(ctx)
    restored()


def saved_on():
    with unlocked() as ctx:
        state = keep(ctx, trial(ctx), persist=True)
        assert state["enabled"] and state["persist"], state
    # Closing the GUI after confirmation must leave the mask usable.
    assert_masked(forwarding=True)


def saved_after_boot():
    identity = devices()[NAMES[0]].identity
    state = wait_state(ScenarioContext(), identity, "masked")
    assert state["automatic"] and state["enabled"] and state["persist"], state
    assert_masked(forwarding=True)


def saved_off():
    # Restoring access is available even when capture is locked.
    off(ScenarioContext())
    restored()


COMMANDS = {
    "locked": rejected_locked,
    "trial-expiry": trial_expiry,
    "trial-close": lambda: trial_expiry(disconnect=True),
    "undo": undo,
    "off": assert_off,
    "multiple": multiple_masks,
    "reconnect": reconnect,
    "remapping": remapping,
    "tokens": rejected_tokens,
    "saved-on": saved_on,
    "saved-after-boot": saved_after_boot,
    "saved-off": saved_off,
}

if __name__ == "__main__":
    ScenarioContext().assert_daemon_has_no_capabilities()
    COMMANDS[sys.argv[1]]()
    ScenarioContext().assert_daemon_has_no_capabilities()
