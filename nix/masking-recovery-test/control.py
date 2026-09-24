"""Observe masking through session IPC and real kernel devices in a disposable VM."""

import contextlib
import errno
import fcntl
import grp
import json
import os
import pwd
import select
import socket
import subprocess
import sys
import time
from pathlib import Path

import evdev
from support import ScenarioContext
from uhid import NativeController

from keymasq.masking.inventory import HardwareInventory

NAMES = tuple(
    os.environ.get("KEYMASQ_MASK_TEST_NAMES", "mask-recovery-target,mask-recovery-bystander").split(
        ","
    )
)
BASELINE = Path.home() / f"masking-baseline-{NAMES[0]}.json"
INVENTORY = HardwareInventory()
USBDEVFS_CONNECTINFO = 0x40085511


class GuiClient:
    def __init__(self):
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(30)
        self.socket.connect(str(ScenarioContext().session_socket))
        self.stream = self.socket.makefile("rwb")

    def request(self, payload, *, timeout=30, ok=True):
        self.socket.settimeout(timeout)
        self.stream.write(json.dumps(payload).encode() + b"\n")
        self.stream.flush()
        while line := self.stream.readline():
            result = json.loads(line)
            if "event" in result:
                continue
            if ok:
                assert result.get("status") == "ok", (payload, result)
            return result
        raise AssertionError(f"session disconnected during {payload}")

    def close(self):
        self.stream.close()
        self.socket.close()


def devices(*, require_all=True):
    found = {item.name: item for item in INVENTORY.scan() if item.name in NAMES}
    if require_all:
        assert set(found) == set(NAMES), found
    return found


def nodes(attachment):
    found = {INVENTORY.node_role(attachment, node): node for node in INVENTORY.nodes(attachment)}
    assert any(node.name.startswith("event") for node in found.values()), found
    assert any(node.name.startswith("hidraw") for node in found.values()), found
    return found


def metadata(attachment):
    result = {}
    for role, node in nodes(attachment).items():
        info = node.stat()
        acl = subprocess.check_output(["getfacl", "-cn", str(node)], text=True)
        result[role] = {
            "uid": info.st_uid,
            "gid": info.st_gid,
            "mode": info.st_mode & 0o777,
            "acl": sorted(line for line in acl.splitlines() if line.strip()),
        }
    return result


def probe(attachment, *, denied=False):
    assert os.geteuid() == 1000, "access checks must use the ordinary desktop user"
    for node in nodes(attachment).values():
        usb = "/bus/usb/" in str(node)
        try:
            fd = os.open(node, (os.O_RDWR if usb else os.O_RDONLY) | os.O_NONBLOCK | os.O_CLOEXEC)
        except PermissionError:
            assert denied, f"device access was not restored: {node}"
            continue
        try:
            assert not denied, f"masked device remains accessible: {node}"
            if usb:
                # usbfs exposes device control rather than an input event stream.
                info = bytearray(8)
                fcntl.ioctl(fd, USBDEVFS_CONNECTINFO, info)
                assert int.from_bytes(info[:4], "little") == int(node.name), info
                continue
            assert select.select([fd], [], [], 3)[0], f"no input after recovery: {node}"
            assert os.read(fd, 4096), f"device returned EOF: {node}"
        finally:
            os.close(fd)


def serve():
    with contextlib.ExitStack() as stack:
        controllers = [
            stack.enter_context(
                contextlib.closing(NativeController(name, unique=name, toggle_button=True))
            )
            for name in NAMES
        ]
        while True:
            for controller in controllers:
                if controller.error is not None:
                    raise controller.error
            time.sleep(0.1)


def baseline():
    found = devices()
    for attachment in found.values():
        probe(attachment)
    BASELINE.write_text(json.dumps({name: metadata(item) for name, item in found.items()}))


def assert_bystander():
    other = devices()[NAMES[1]]
    expected = json.loads(BASELINE.read_text())[NAMES[1]]
    assert metadata(other) == expected, "unrelated controller permissions changed"
    probe(other)


def mask_state(ctx, identity):
    result = ctx.request({"command": "hardware_inventory"}, timeout=30)
    return next((item for item in result["masks"] if item.get("id") == identity), {})


def assert_forwarding(name=NAMES[0]):
    outputs = [
        node
        for path in Path("/sys/devices/virtual/input").glob("input*/name")
        if path.read_text().strip() == name
        for node in path.parent.glob("event*")
    ]
    assert len(outputs) == 1, outputs
    with contextlib.closing(evdev.InputDevice(f"/dev/input/{outputs[0].name}")) as output:
        values = set()
        deadline = time.monotonic() + 3
        while values != {0, 1}:
            assert time.monotonic() < deadline, f"passthrough stopped forwarding: {values}"
            if select.select([output.fd], [], [], 0.1)[0]:
                values.update(
                    event.value
                    for event in output.read()
                    if event.type == evdev.ecodes.EV_KEY and event.code == evdev.ecodes.BTN_SOUTH
                )


def assert_revoked(stream):
    os.set_blocking(stream.fileno(), False)
    deadline = time.monotonic() + 3
    # hidraw may return reports queued before disconnection. Drain those, then
    # require a terminal disconnect instead of accepting a temporarily quiet fd.
    while time.monotonic() < deadline:
        try:
            if "/bus/usb/" in str(stream.name):
                fcntl.ioctl(stream.fileno(), USBDEVFS_CONNECTINFO, bytearray(8))
                time.sleep(0.05)
            elif not os.read(stream.fileno(), 4096):
                return
        except BlockingIOError:
            select.select([stream.fileno()], [], [], 0.05)
        except OSError as exc:
            assert exc.errno in {errno.ENODEV, errno.EIO}, exc
            return
    raise AssertionError(f"takeover did not revoke the old descriptor: {stream.name}")


def assert_masked(name=NAMES[0], *, forwarding=False, check_bystander=True):
    target = devices(require_all=False)[name]
    identity = target.identity
    rules = Path("/run/udev/rules.d")
    for prefix in ("72", "99-zz"):
        assert (rules / f"{prefix}-keymasq-masking-{identity}.rules").is_file()
    record = Path("/run/keymasq-masking/reservations") / identity
    for filename in ("journal.json", "permissions.json", "armed.json"):
        assert (record / filename).is_file(), filename
    for node in nodes(target).values():
        info = node.stat()
        assert (info.st_uid, info.st_gid, info.st_mode & 0o777) == (
            0,
            grp.getgrnam("keymasq").gr_gid,
            0o660,
        ), node
    probe(target, denied=True)
    if check_bystander:
        assert_bystander()
    if forwarding:
        assert_forwarding(name)


def start_mask(*, confirm, persist=False, hold_usb=True):
    ScenarioContext().assert_daemon_has_no_capabilities()
    target = devices()[NAMES[0]]
    # Hold real desktop descriptors across takeover. A rebind must revoke them.
    with contextlib.ExitStack() as stack:
        ctx = stack.enter_context(contextlib.closing(GuiClient()))
        ctx.request({"command": "claim_recording_unlock_refresh"})
        opened = [
            stack.enter_context(
                open(node, "r+b" if "/bus/usb/" in str(node) else "rb", buffering=0)
            )
            for node in nodes(target).values()
            if hold_usb or "/bus/usb/" not in str(node)
        ]
        ctx.request(
            {
                "command": "mask_hardware",
                "id": target.identity,
                "generation": target.generation,
                "persist": False,
            },
            timeout=30,
        )
        if not confirm:
            return
        deadline = time.monotonic() + 30
        while True:
            state = mask_state(ctx, target.identity)
            if state.get("state") == "trial":
                break
            assert state.get("state") in {"applying", "acquiring"}, state
            assert time.monotonic() < deadline, state
            time.sleep(0.1)
        ctx.request(
            {
                "command": "keep_hardware_mask",
                "id": target.identity,
                "token": state["token"],
                "persist": persist,
            }
        )
        assert mask_state(ctx, target.identity)["state"] == "masked"
        for stream in opened:
            assert_revoked(stream)
    assert_masked(forwarding=True)


def wait_masked():
    ctx = ScenarioContext()
    identity = devices()[NAMES[0]].identity
    deadline = time.monotonic() + 30
    while True:
        state = mask_state(ctx, identity)
        if state.get("state") == "masked":
            break
        assert time.monotonic() < deadline, state
        time.sleep(0.1)
    assert_masked(forwarding=True)


def restored_device(name):
    attachment = devices()[name]
    identity = attachment.identity
    for prefix in ("72", "99-zz"):
        assert not (
            Path("/run/udev/rules.d") / f"{prefix}-keymasq-masking-{identity}.rules"
        ).exists()
    record = Path("/run/keymasq-masking/reservations") / identity
    for filename in ("journal.json", "armed.json", "permissions.json"):
        assert not (record / filename).exists(), filename
    expected = json.loads(BASELINE.read_text())[name]
    actual = metadata(attachment)
    assert actual == expected, f"{name}: {actual} != {expected}"
    assert INVENTORY.bindings(attachment), f"driver was not restored: {name}"
    probe(attachment)


def restored():
    for pattern in ("72-keymasq-masking*.rules", "99-zz-keymasq-masking*.rules"):
        assert not list(Path("/run/udev/rules.d").glob(pattern)), pattern
    for name in ("journal.json", "armed.json", "permissions.json"):
        assert not list(Path("/run/keymasq-masking").rglob(name)), name
    for name in NAMES:
        restored_device(name)


def keymasq_acl():
    return f"user:{pwd.getpwnam('keymasq').pw_uid}:"


def restricted():
    """A refused package removal leaves the mask and its recovery records in place."""
    attachment = devices()[NAMES[0]]
    for prefix in ("72", "99-zz"):
        assert (
            Path("/run/udev/rules.d") / f"{prefix}-keymasq-masking-{attachment.identity}.rules"
        ).exists()
    record = Path("/run/keymasq-masking/reservations") / attachment.identity
    assert (record / "journal.json").exists()
    probe(attachment, denied=True)


def removed():
    """Removal restores access and drops only keymasq's own ACL entries."""
    for pattern in ("72-keymasq-masking*.rules", "99-zz-keymasq-masking*.rules"):
        assert not list(Path("/run/udev/rules.d").glob(pattern)), pattern
    for name in ("journal.json", "armed.json", "permissions.json"):
        assert not list(Path("/run/keymasq-masking").rglob(name)), name
    baseline = json.loads(BASELINE.read_text())
    for name in NAMES:
        attachment = devices()[name]
        expected = {
            role: {
                **original,
                "acl": [entry for entry in original["acl"] if not entry.startswith(keymasq_acl())],
            }
            for role, original in baseline[name].items()
        }
        actual = metadata(attachment)
        assert actual == expected, f"{name}: {actual} != {expected}"
        probe(attachment)
    uinput = subprocess.check_output(["getfacl", "-cn", "/dev/uinput"], text=True)
    assert keymasq_acl() not in uinput, uinput


def restore():
    ctx = ScenarioContext()
    identity = devices()[NAMES[0]].identity
    state = mask_state(ctx, identity)
    ctx.request({"command": "restore_hardware", "id": identity, "token": state["token"]})
    # Restore acknowledges the request before the asynchronous root job ends.
    # Wait for its reported completion, then independently check the OS state.
    deadline = time.monotonic() + 30
    while True:
        state = mask_state(ctx, identity)
        if state.get("state") == "restored":
            return
        assert state.get("state") == "restoring", state
        assert time.monotonic() < deadline, state
        time.sleep(0.1)


if __name__ == "__main__":
    command = sys.argv[1]
    if command == "serve":
        serve()
    elif command == "baseline":
        baseline()
    elif command == "mask":
        start_mask(confirm=True)
    elif command == "mask-persistent":
        start_mask(confirm=True, persist=True)
    elif command == "start-mask":
        start_mask(confirm=False)
    elif command == "masked":
        assert_masked()
    elif command == "ready":
        devices()
        ctx = ScenarioContext()
        ctx.wait_for_keymasqd_connection()
        ctx.assert_daemon_has_no_capabilities()
    elif command == "restored":
        restored()
    elif command == "restricted":
        restricted()
    elif command == "removed":
        removed()
    elif command == "wait-masked":
        wait_masked()
    elif command == "restore":
        restore()
    else:
        raise ValueError(command)
