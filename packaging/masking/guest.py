"""Check installed masking behavior, including after Keymasq is uninstalled.

Uses only the standard library and the shared UHID fixture. In particular,
restoration checks cannot accidentally import a checkout or an uninstalled app.
Run access checks as the isolated, unprivileged test account.
"""

import contextlib
import errno
import grp
import json
import os
import pwd
import select
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

from uhid import NativeController

NAMES = ("mask-package-target", "mask-package-bystander")
BASELINE = Path.home() / "masking-baseline.json"
INPUT_EVENT = struct.Struct("llHHi")


class Client:
    def __init__(self):
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(30)
        self.socket.connect(f"/run/user/{os.getuid()}/keymasq/session.sock")
        self.stream = self.socket.makefile("rwb")

    def request(self, command, **data):
        self.stream.write(json.dumps({"command": command, **data}).encode() + b"\n")
        self.stream.flush()
        while line := self.stream.readline():
            result = json.loads(line)
            if "event" in result:
                continue
            assert result.get("status") == "ok", (command, result)
            return result
        raise AssertionError(f"session disconnected during {command}")

    def close(self):
        self.stream.close()
        self.socket.close()


def devices():
    found = {}
    for device in Path("/sys/bus/hid/devices").glob("*"):
        attributes = dict(
            line.split("=", 1) for line in (device / "uevent").read_text().splitlines()
        )
        name = attributes.get("HID_NAME")
        if name in NAMES:
            assert name not in found, (name, device)
            found[name] = device
    assert set(found) == set(NAMES), found
    return found


def nodes(device):
    result = {}
    for role, pattern, root in (
        ("event", "input/input*/event*", "/dev/input"),
        ("js", "input/input*/js*", "/dev/input"),
        ("hidraw", "hidraw/hidraw*", "/dev"),
    ):
        matches = list(device.glob(pattern))
        assert len(matches) <= 1, matches
        if matches:
            result[role] = Path(root) / matches[0].name
    assert {"event", "hidraw"} <= result.keys(), result
    return result


def metadata(device):
    result = {}
    for role, node in nodes(device).items():
        info = node.stat()
        acl = subprocess.check_output(["getfacl", "-cnp", str(node)], text=True, timeout=10)
        result[role] = {
            "uid": info.st_uid,
            "gid": info.st_gid,
            "mode": info.st_mode & 0o777,
            "acl": sorted(line for line in acl.splitlines() if line.strip()),
        }
    return result


def probe(device, *, denied=False):
    assert os.geteuid() != 0, "access checks must run as an ordinary user"
    for node in nodes(device).values():
        try:
            fd = os.open(node, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        except PermissionError:
            assert denied, f"access not restored: {node}"
            continue
        try:
            assert not denied, f"masked physical node remains accessible: {node}"
            assert select.select([fd], [], [], 3)[0], f"no fresh input: {node}"
            assert os.read(fd, 4096), f"unexpected EOF: {node}"
        finally:
            os.close(fd)


def forwarding():
    outputs = [
        node
        for path in Path("/sys/devices/virtual/input").glob("input*/name")
        if path.read_text().strip() == NAMES[0]
        for node in path.parent.glob("event*")
    ]
    assert len(outputs) == 1, outputs
    with open(f"/dev/input/{outputs[0].name}", "rb", buffering=0) as output:
        os.set_blocking(output.fileno(), False)
        values = set()
        deadline = time.monotonic() + 3
        while values != {0, 1}:
            assert time.monotonic() < deadline, f"passthrough stopped: {values}"
            if select.select([output], [], [], 0.1)[0]:
                values.update(
                    value
                    for _, _, kind, code, value in INPUT_EVENT.iter_unpack(
                        os.read(output.fileno(), INPUT_EVENT.size * 64)
                    )
                    if kind == 1 and code == 304  # EV_KEY, BTN_SOUTH
                )


def capabilities():
    pid = int(
        subprocess.check_output(
            ["systemctl", "show", "keymasqd.service", "-p", "MainPID", "--value"],
            text=True,
            timeout=10,
        ).strip()
    )
    assert pid > 0, pid
    expected = {"CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"}
    observed = {
        name: int(value.strip(), 16)
        for line in Path(f"/proc/{pid}/status").read_text().splitlines()
        for name, _, value in [line.partition(":")]
        if name in expected
    }
    assert observed.keys() == expected and not any(observed.values()), observed
    return pid


def state(client, identity):
    return next(
        (item for item in client.request("hardware_inventory")["masks"] if item["id"] == identity),
        {},
    )


def target(client):
    inventory = client.request("hardware_inventory")
    assert inventory.get("available"), inventory
    return next(item for item in inventory["devices"] if item["name"] == NAMES[0])


def wait_state(client, identity, expected):
    deadline = time.monotonic() + 30
    while True:
        current = state(client, identity)
        if current.get("state") == expected:
            return current
        assert current.get("state") != "error", current
        assert time.monotonic() < deadline, current
        time.sleep(0.1)


def unchanged(name, *, uninstalled=False):
    device = devices()[name]
    expected = json.loads(BASELINE.read_text())[name]
    actual = metadata(device)
    assert actual.keys() == expected.keys(), (name, actual, expected)
    for role, original in expected.items():
        allowed = [original]
        if uninstalled:
            # A package manager's udev hook may also remove the uninstalled
            # daemon's own ACL. Every other permission must remain unchanged.
            prefix = f"user:{pwd.getpwnam('keymasq').pw_uid}:"
            allowed.append(
                {
                    **original,
                    "acl": [entry for entry in original["acl"] if not entry.startswith(prefix)],
                }
            )
        assert actual[role] in allowed, (name, role, actual[role], allowed)
    assert (device / "driver").exists(), f"driver unbound: {name}"
    probe(device)


def masked(identity):
    capabilities()
    for prefix in ("72", "99-zz"):
        assert Path(f"/run/udev/rules.d/{prefix}-keymasq-masking-{identity}.rules").is_file()
    for filename in ("journal.json", "permissions.json", "armed.json"):
        assert Path(f"/run/keymasq-masking/reservations/{identity}/{filename}").is_file()
    device = devices()[NAMES[0]]
    for node in nodes(device).values():
        info = node.stat()
        assert (info.st_uid, info.st_gid, info.st_mode & 0o777) == (
            0,
            grp.getgrnam("keymasq").gr_gid,
            0o660,
        ), node
    probe(device, denied=True)
    unchanged(NAMES[1])
    forwarding()


def revoked(stream):
    os.set_blocking(stream.fileno(), False)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            if not os.read(stream.fileno(), 4096):
                return
        except BlockingIOError:
            select.select([stream], [], [], 0.05)
        except OSError as exc:
            assert exc.errno in {errno.ENODEV, errno.EIO}, exc
            return
    raise AssertionError(f"old descriptor not revoked: {stream.name}")


def mask():
    with contextlib.ExitStack() as stack:
        client = stack.enter_context(contextlib.closing(Client()))
        client.request("claim_recording_unlock_refresh")
        attachment = target(client)
        opened = [
            stack.enter_context(open(node, "rb", buffering=0))
            for node in nodes(devices()[NAMES[0]]).values()
        ]
        client.request(
            "mask_hardware",
            id=attachment["id"],
            generation=attachment["generation"],
            persist=False,
        )
        current = wait_state(client, attachment["id"], "trial")
        client.request(
            "keep_hardware_mask", id=attachment["id"], token=current["token"], persist=True
        )
        wait_state(client, attachment["id"], "masked")
        for stream in opened:
            revoked(stream)
    masked(attachment["id"])


def removed_files(*, appimage=False):
    target_home = Path(pwd.getpwnam("keymasq-masktest").pw_dir) if appimage else Path.home()
    installed_paths = (
        "/usr/bin/keymasq",
        "/usr/bin/keymasqd",
        "/usr/bin/keymasq-session",
        "/usr/bin/keymasq-record",
        "/usr/lib/systemd/system/keymasqd.service",
        "/usr/lib/systemd/system/keymasq-hardware@.service",
        "/usr/lib/systemd/user/keymasq-session.service",
        "/usr/lib/udev/rules.d/91-keymasq-acl.rules",
        "/usr/lib/udev/rules.d/99-keymasq-hide-grabbed.rules",
        "/usr/share/polkit-1/rules.d/49-keymasq-hardware.rules",
    )
    if appimage:
        installed_paths = (
            "/opt/keymasq/bin",
            "/opt/keymasq/runtime",
            "/opt/keymasq/Keymasq.AppImage",
            "/opt/keymasq/share",
            "/opt/keymasq/version",
            "/etc/systemd/system/keymasqd.service",
            "/etc/systemd/system/keymasq-hardware@.service",
            "/etc/systemd/system/multi-user.target.wants/keymasqd.service",
            "/etc/udev/rules.d/91-keymasq-acl.rules",
            "/etc/udev/rules.d/99-keymasq-hide-grabbed.rules",
            "/etc/polkit-1/rules.d/49-keymasq-hardware.rules",
            "/etc/polkit-1/rules.d/50-keymasq-record.rules",
            "/etc/atomic-update.conf.d/keymasq.conf",
            "/etc/profile.d/keymasq.sh",
            "/etc/sysusers.d/keymasq.conf",
            "/etc/tmpfiles.d/keymasq.conf",
            str(target_home / ".config/systemd/user/keymasq-session.service"),
            str(target_home / ".config/systemd/user/default.target.wants/keymasq-session.service"),
            str(
                target_home
                / ".config/systemd/user/graphical-session.target.wants/keymasq-session.service"
            ),
            str(target_home / ".local/share/applications/tools.keymasq.keymasq.desktop"),
            str(target_home / ".config/autostart/tools.keymasq.keymasq-session.desktop"),
            str(target_home / ".local/share/icons/hicolor/scalable/apps/tools.keymasq.keymasq.svg"),
            *(
                str(target_home / ".local/bin" / name)
                for name in (
                    "keymasq",
                    "keymasqd",
                    "keymasq-session",
                    "keymasq-record",
                    "waypipe",
                    "gtk4-brotway-run",
                )
            ),
        )
    for path in installed_paths:
        assert not Path(path).exists() and not Path(path).is_symlink(), path


def restored(*, uninstalled=False, appimage=False):
    assert not list(Path("/run/udev/rules.d").glob("*-keymasq-masking-*.rules"))
    for filename in ("journal.json", "permissions.json", "armed.json"):
        assert not list(Path("/run/keymasq-masking").rglob(filename)), filename
    for name in NAMES:
        unchanged(name, uninstalled=uninstalled)
    for path in Path("/sys/devices/virtual/input").glob("input*/name"):
        assert path.read_text().strip() not in NAMES, f"virtual device leaked: {path}"
    if uninstalled:
        if not appimage:
            removed_files()
        assert (
            subprocess.check_output(
                ["systemctl", "show", "keymasqd.service", "-p", "MainPID", "--value"],
                text=True,
                timeout=10,
            ).strip()
            == "0"
        )
        assert not json.loads(
            subprocess.check_output(
                [
                    "systemctl",
                    "list-units",
                    "keymasq-hardware@*.service",
                    "--state=active,activating,deactivating",
                    "--output=json",
                ],
                text=True,
                timeout=10,
            )
        )


def main(command):
    if command == "serve":
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
    elif command == "ready":
        deadline = time.monotonic() + 30
        while True:
            try:
                with contextlib.closing(Client()) as client:
                    target(client)
                devices()
                capabilities()
                break
            except (OSError, AssertionError, StopIteration):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)
    elif command == "baseline":
        found = devices()
        for device in found.values():
            probe(device)
        BASELINE.write_text(json.dumps({name: metadata(device) for name, device in found.items()}))
        capabilities()
    elif command == "mask":
        mask()
    elif command == "masked":
        with contextlib.closing(Client()) as client:
            identity = target(client)["id"]
            wait_state(client, identity, "masked")
        masked(identity)
    elif command == "restore":
        with contextlib.closing(Client()) as client:
            identity = target(client)["id"]
            current = state(client, identity)
            client.request("restore_hardware", id=identity, token=current["token"], persist=False)
            current = wait_state(client, identity, "restored")
            assert not current["persist"] and not current["enabled"], current
        restored()
    elif command == "restored":
        restored()
    elif command == "uninstalled":
        restored(uninstalled=True)
    elif command == "uninstalled-appimage":
        restored(uninstalled=True, appimage=True)
    elif command == "appimage-files-removed":
        assert os.geteuid() == 0, "SteamOS Polkit directory checks require root"
        removed_files(appimage=True)
    else:
        raise ValueError(command)
    print(json.dumps({"command": command, "result": "pass", "uid": os.geteuid()}), flush=True)


if __name__ == "__main__":
    main(sys.argv[1])
