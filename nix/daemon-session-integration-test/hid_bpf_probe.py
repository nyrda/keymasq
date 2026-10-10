import array
import json
import os
import socket
import subprocess
import sys
import uuid
from pathlib import Path

HANDOFF = "/run/keymasq/handoff"
REQUESTS = Path("/run/keymasq/hardware-requests")


def handoff() -> dict[str, object]:
    with (
        socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as connection,
        open(os.devnull) as stream,
    ):
        connection.settimeout(5)
        connection.connect(HANDOFF)
        try:
            connection.sendmsg(
                [json.dumps({"token": uuid.uuid4().hex}).encode()],
                [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [stream.fileno()]))],
            )
        except (BrokenPipeError, ConnectionResetError):
            pass
        try:
            reply = connection.recv(64)
        except ConnectionResetError:
            reply = b""
    return {"uid": os.getuid(), "closed": reply == b""}


def attach(systemctl: str, device: str, driver: str) -> dict[str, object]:
    token = uuid.uuid4().hex
    path = REQUESTS / token
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(
            {
                "operation": "hid-bpf-attach",
                "id": "",
                "driver": driver,
                "device": device,
                "token": uuid.uuid4().hex,
            },
            stream,
        )
    try:
        subprocess.run(
            [
                systemctl,
                "--no-ask-password",
                "start",
                "--job-mode=fail",
                f"keymasq-hardware@{token}.service",
            ],
            check=True,
            timeout=60,
        )
        return json.loads(path.read_text())
    finally:
        path.unlink(missing_ok=True)


if __name__ == "__main__":
    systemctl, command, *arguments = sys.argv[1:]
    result = handoff() if command == "handoff" else attach(systemctl, *arguments)
    print(json.dumps(result))
