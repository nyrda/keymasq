"""USB hotplug assertions through installed services and ordinary-user access."""

import contextlib
import json
import sys
from pathlib import Path

import control
from behavior import off, unlocked, virtual_paths, wait_for, wait_state
from support import ScenarioContext

STATE = Path.home() / "masking-usb-state.json"


def remember():
    target = control.devices()[control.NAMES[0]]
    STATE.write_text(
        json.dumps(
            {"id": target.identity, "generation": target.generation, "path": str(target.syspath)}
        )
    )


def masked():
    old = json.loads(STATE.read_text())
    target = control.devices()[control.NAMES[0]]
    assert target.transport == "usb" and target.identity == old["id"], (old, target)
    assert target.generation != old["generation"], (old, target)
    state = wait_state(ScenarioContext(), target.identity, "masked")
    assert state["automatic"] and state["persist"], state
    control.assert_masked(forwarding=True)
    ScenarioContext().assert_daemon_has_no_capabilities()
    remember()


def disconnected():
    old = json.loads(STATE.read_text())
    wait_for(
        "USB attachment disappears",
        lambda: control.devices(require_all=False),
        lambda found: control.NAMES[0] not in found,
    )
    wait_for(
        "USB saved mask waits for reconnect",
        lambda: control.mask_state(ScenarioContext(), old["id"]),
        lambda state: state.get("lifecycle") == "waiting_for_device",
    )
    assert not virtual_paths(control.NAMES[0])
    # The other USB device stays accessible with unchanged permissions.
    bystander = control.devices(require_all=False)[control.NAMES[1]]
    expected = json.loads(control.BASELINE.read_text())[control.NAMES[1]]
    assert control.metadata(bystander) == expected
    control.probe(bystander)


def unmasked_attachment(*, moved=False):
    original = json.loads(STATE.read_text())
    name = control.NAMES[0] if moved else "mask-usb-replacement"
    target = next(item for item in control.INVENTORY.scan() if item.name == name)
    assert target.identity != original["id"] and target.transport == "usb"
    assert (str(target.syspath) != original["path"]) == moved, (original, target)
    control.probe(target)
    assert not virtual_paths(target.name)
    assert not control.mask_state(ScenarioContext(), target.identity).get("enabled", False)
    assert (
        control.mask_state(ScenarioContext(), original["id"])["lifecycle"] == "waiting_for_device"
    )


def disable_absent():
    identity = json.loads(STATE.read_text())["id"]
    with unlocked() as ctx:
        state = control.mask_state(ctx, identity)
        ctx.request(
            {
                "command": "restore_hardware",
                "id": identity,
                "token": state["token"],
                "persist": False,
            }
        )
        state = wait_state(ctx, identity, "restored")
        assert not state["enabled"] and not state["persist"], state
    for prefix in ("72", "99-zz"):
        assert not (
            Path("/run/udev/rules.d") / f"{prefix}-keymasq-masking-{identity}.rules"
        ).exists()


def restored():
    for name in control.NAMES:
        control.restored_device(name)
    state = control.mask_state(ScenarioContext(), json.loads(STATE.read_text())["id"])
    assert not state["enabled"] and not state["persist"], state
    assert not virtual_paths(control.NAMES[0])


def hold_output():
    import evdev

    paths = virtual_paths(control.NAMES[0])
    assert len(paths) == 1, paths
    with contextlib.closing(evdev.InputDevice(paths[0])) as output:
        Path("/tmp/keymasq-usb-output-held").touch()
        wait_for(
            "USB disconnect while virtual output is open",
            lambda: control.devices(require_all=False),
            lambda found: control.NAMES[0] not in found,
        )
        control.assert_revoked(output)
    Path("/tmp/keymasq-usb-output-revoked").touch()


def hold_physical():
    target = control.devices()[control.NAMES[0]]
    with contextlib.ExitStack() as stack:
        handles = [
            stack.enter_context(
                open(node, "r+b" if "/bus/usb/" in str(node) else "rb", buffering=0)
            )
            for node in control.nodes(target).values()
        ]
        Path("/tmp/keymasq-usb-physical-held").touch()
        wait_for(
            "USB disconnect while physical nodes are open",
            lambda: control.devices(require_all=False),
            lambda found: control.NAMES[0] not in found,
        )
        for handle in handles:
            control.assert_revoked(handle)
    Path("/tmp/keymasq-usb-physical-revoked").touch()


def run(command):
    if command.startswith("connected-"):
        name = (*control.NAMES, "mask-usb-replacement")[int(command.rsplit("-", 1)[1])]
        target = wait_for(
            "USB enumeration",
            lambda: next((item for item in control.INVENTORY.scan() if item.name == name), None),
            lambda item: item is not None,
        )
        wait_for(
            "USB HID endpoints",
            lambda: control.INVENTORY.nodes(target),
            lambda found: (
                any(node.name.startswith("event") for node in found)
                and any(node.name.startswith("hidraw") for node in found)
            ),
        )
        return
    match command:
        case "baseline":
            control.baseline()
        case "mask":
            # VHCI cannot complete a software port power cycle. Raw USB handles
            # are checked across explicit USB/IP detach; evdev/hidraw handles
            # remain open here to verify masking's real HID driver rebind.
            control.start_mask(confirm=True, persist=True, hold_usb=False)
            remember()
        case "masked":
            masked()
        case "disconnected":
            disconnected()
        case "replacement":
            unmasked_attachment()
        case "moved":
            unmasked_attachment(moved=True)
        case "disable-absent":
            disable_absent()
        case "restored":
            restored()
        case "hold-output":
            hold_output()
        case "hold-physical":
            hold_physical()
        case "off":
            with unlocked() as ctx:
                off(ctx)
        case _:
            raise ValueError(command)


if __name__ == "__main__":
    run(sys.argv[1])
