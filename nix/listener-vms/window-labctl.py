#!/usr/bin/env python3

import argparse
import json
import socket
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument(
        "command",
        choices=["open", "focus", "retitle", "close", "quit", "snapshot"],
    )
    parser.add_argument("window_id", nargs="?")
    parser.add_argument("title", nargs="?")
    args = parser.parse_args()

    payload = {"command": args.command}
    if args.window_id is not None:
        payload["window_id"] = args.window_id
    if args.title is not None:
        payload["title"] = args.title

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(args.socket)
        client.sendall(json.dumps(payload).encode("utf-8") + b"\n")

        data = b""
        while not data.endswith(b"\n"):
            chunk = client.recv(4096)
            if not chunk:
                break
            data += chunk

    if data:
        sys.stdout.write(data.decode("utf-8"))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
