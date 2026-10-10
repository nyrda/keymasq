#!/usr/bin/env python3

import argparse
import json
import os
import socket
import socketserver
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import evdev

SOURCE_NAME = "upgrade-source-gamepad"
SOURCE_SOCKET = Path("/run/keymasq-upgrade-source.sock")
SOURCE_PATH_FILE = Path("/run/keymasq-upgrade-source.path")
KEYBOARD_OUTPUT_NAMES = {"keymasq-keyboard", "keymasq-test-keyboard"}
SOURCE_BUTTONS = ("BTN_SOUTH", "BTN_EAST", "BTN_NORTH", "BTN_WEST", "BTN_START", "BTN_SELECT")


def settle_udev() -> None:
    subprocess.run(["udevadm", "settle", "--timeout=5"], check=False, timeout=10)


def session_request(payload: dict[str, Any], timeout: float = 10.0) -> dict[str, Any]:
    runtime_dir = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(str(runtime_dir / "keymasq" / "session.sock"))
        client.sendall(json.dumps(payload).encode() + b"\n")
        data = b""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            chunk = client.recv(65536)
            if not chunk:
                break
            data += chunk
            while b"\n" in data:
                line, data = data.split(b"\n", 1)
                if not line.strip():
                    continue
                message = json.loads(line)
                if isinstance(message, dict) and "event" not in message:
                    return message
    raise SystemExit(f"no session response for {payload}")


def run_source(_args: argparse.Namespace) -> None:
    device = evdev.UInput(
        events={
            evdev.ecodes.EV_KEY: [evdev.ecodes.ecodes[name] for name in SOURCE_BUTTONS],
            evdev.ecodes.EV_ABS: [
                (evdev.ecodes.ABS_X, evdev.AbsInfo(0, -32768, 32767, 16, 128, 0)),
                (evdev.ecodes.ABS_Y, evdev.AbsInfo(0, -32768, 32767, 16, 128, 0)),
                (evdev.ecodes.ABS_RX, evdev.AbsInfo(0, -32768, 32767, 16, 128, 0)),
                (evdev.ecodes.ABS_RY, evdev.AbsInfo(0, -32768, 32767, 16, 128, 0)),
            ],
        },
        name=SOURCE_NAME,
        vendor=0xCAFE,
        product=0x0003,
        phys="upgrade-gamepad/input0",
    )
    settle_udev()
    SOURCE_PATH_FILE.write_text(str(device.device.path) + "\n")

    class Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            name = self.rfile.readline().decode().strip()
            if name not in SOURCE_BUTTONS:
                self.wfile.write(f"unknown button {name}\n".encode())
                return
            code = evdev.ecodes.ecodes[name]
            for value in (1, 0):
                device.write(evdev.ecodes.EV_KEY, code, value)
                device.syn()
                time.sleep(0.05)
            self.wfile.write(b"ok\n")

    SOURCE_SOCKET.unlink(missing_ok=True)
    with socketserver.UnixStreamServer(str(SOURCE_SOCKET), Handler) as server:
        server.serve_forever()


def tap_source(button: str) -> None:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        client.connect(str(SOURCE_SOCKET))
        client.sendall(button.encode() + b"\n")
        reply = client.makefile().readline().strip()
    if reply != "ok":
        raise SystemExit(f"source tap failed: {reply}")


def keyboard_output() -> evdev.InputDevice:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        settle_udev()
        for path in evdev.list_devices():
            try:
                device = evdev.InputDevice(path)
            except OSError:
                continue
            if device.name in KEYBOARD_OUTPUT_NAMES:
                return device
            device.close()
        time.sleep(0.5)
    raise SystemExit("Keymasq keyboard output was not created")


def read_keys(device: evdev.InputDevice) -> list[tuple[int, int]]:
    try:
        return [
            (event.code, event.value)
            for event in device.read()
            if event.type == evdev.ecodes.EV_KEY
        ]
    except (BlockingIOError, OSError):
        return []


def run_expect_tap(args: argparse.Namespace) -> None:
    expected = [(evdev.ecodes.ecodes[key], value) for key in args.keys for value in (1, 0)]
    device = keyboard_output()
    try:
        drain_deadline = time.monotonic() + 5
        quiet_until = time.monotonic() + 0.2
        while time.monotonic() < quiet_until:
            if stray := read_keys(device):
                if time.monotonic() > drain_deadline:
                    raise SystemExit(f"keyboard output never went quiet; still seeing {stray}")
                quiet_until = time.monotonic() + 0.2
            time.sleep(0.01)
        tap_source(args.button)
        observed: list[tuple[int, int]] = []
        index = 0
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and index < len(expected):
            for event in read_keys(device):
                observed.append(event)
                if index < len(expected) and event == expected[index]:
                    index += 1
            time.sleep(0.01)
    finally:
        device.close()
    labels = [(evdev.ecodes.KEY.get(code, code), value) for code, value in observed]
    if index < len(expected):
        raise SystemExit(f"{args.button} did not produce {args.keys}; observed {labels}")
    print(json.dumps({"button": args.button, "observed": labels}))


def run_session(args: argparse.Namespace) -> None:
    result = session_request(json.loads(args.payload))
    print(json.dumps(result, sort_keys=True))
    if result.get("status") not in {"ok", None}:
        raise SystemExit(1)


def mapping_ready(payload: dict[str, Any], hardware_id: str, profile_name: str) -> bool:
    if profile_name not in {str(name) for name in payload.get("active_profiles", [])}:
        return False
    devices = payload.get("devices", {})
    device = devices.get(hardware_id) if isinstance(devices, dict) else None
    if not isinstance(device, dict) or profile_name not in device.get("profiles", []):
        return False
    return (
        bool(device.get("grabbed"))
        and not bool(device.get("waiting_for_device"))
        and int(device.get("mapping_count", 0)) > 0
        and bool(device.get("mapping_applied"))
    )


def run_wait_mapping(args: argparse.Namespace) -> None:
    deadline = time.monotonic() + args.timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        try:
            last = session_request({"command": "get_active_profiles"}, timeout=2.0)
        except OSError:
            last = {}
        if mapping_ready(last, args.hardware_id, args.profile):
            print(json.dumps(last["devices"][args.hardware_id], sort_keys=True))
            return
        time.sleep(0.2)
    raise SystemExit(f"{args.profile} mapping on {args.hardware_id} not ready: {json.dumps(last)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("source").set_defaults(run=run_source)
    expect = commands.add_parser("expect-tap")
    expect.add_argument("button", choices=SOURCE_BUTTONS)
    expect.add_argument("keys", nargs="+")
    expect.set_defaults(run=run_expect_tap)
    session = commands.add_parser("session")
    session.add_argument("payload")
    session.set_defaults(run=run_session)
    wait_mapping = commands.add_parser("wait-mapping")
    wait_mapping.add_argument("hardware_id")
    wait_mapping.add_argument("profile")
    wait_mapping.add_argument("--timeout", type=float, default=60.0)
    wait_mapping.set_defaults(run=run_wait_mapping)
    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    sys.exit(main())
