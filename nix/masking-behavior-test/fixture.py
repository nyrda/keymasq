"""Controllable kernel input devices, confined to the disposable masking VM."""

import contextlib
import json
import os
import socket
from pathlib import Path

import evdev
from control import NAMES
from uhid import NativeController

SOCKET = Path("/run/keymasq-test-devices.sock")
KEYBOARD_NAME = "mask-behavior-keyboard"


def request(command, *, name=NAMES[0], value=None):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        client.connect(str(SOCKET))
        client.sendall(
            json.dumps({"command": command, "name": name, "value": value}).encode() + b"\n"
        )
        with client.makefile("rb") as stream:
            response = json.loads(stream.readline())
        assert response == {"status": "ok"}, response


def serve():
    controllers = {}
    with contextlib.ExitStack() as stack:
        keyboard = stack.enter_context(
            contextlib.closing(
                evdev.UInput(
                    {evdev.ecodes.EV_KEY: [evdev.ecodes.KEY_A]},
                    name=KEYBOARD_NAME,
                    vendor=0xCAFE,
                    product=0x0004,
                )
            )
        )
        for name in NAMES:
            controllers[name] = NativeController(name, unique=name, toggle_button=True)
        server = stack.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
        SOCKET.unlink(missing_ok=True)
        server.bind(str(SOCKET))
        # This socket only controls test fixtures and exists only inside this VM.
        os.chmod(SOCKET, 0o666)
        server.listen()
        server.settimeout(0.2)
        try:
            while True:
                for controller in controllers.values():
                    if controller.error is not None:
                        raise controller.error
                try:
                    client, _ = server.accept()
                except TimeoutError:
                    continue
                with client, client.makefile("rb") as stream:
                    client.settimeout(5)
                    data = json.loads(stream.readline())
                    name = data["name"]
                    assert name in NAMES, name
                    match data["command"]:
                        case "disconnect":
                            controllers.pop(name).close()
                        case "connect":
                            assert name not in controllers, name
                            controllers[name] = NativeController(
                                name, unique=name, toggle_button=True
                            )
                        case "button":
                            assert data["value"] in (None, 0, 1), data
                            controllers[name].set_button(data["value"])
                        case "keyboard":
                            assert data["value"] in (0, 1), data
                            keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, data["value"])
                            keyboard.syn()
                        case _:
                            raise ValueError(data)
                    client.sendall(b'{"status": "ok"}\n')
        finally:
            for controller in controllers.values():
                controller.close()


if __name__ == "__main__":
    serve()
