#!/usr/bin/env python3

import argparse
import json
import socket
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--command", required=True)
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="Socket read timeout in seconds",
    )
    parser.add_argument("--field", action="append", default=[],
                        help="Extra JSON fields as key=value pairs")
    parser.add_argument("--payload-file",
                        help="Read extra JSON fields from a file and merge them")
    args = parser.parse_args()

    payload: dict[str, object] = {"command": args.command}
    for field in args.field:
        key, _, value = field.partition("=")
        if key:
            try:
                payload[key] = json.loads(value)
            except (json.JSONDecodeError, ValueError):
                payload[key] = value

    if args.payload_file:
        with open(args.payload_file) as f:
            extra = json.load(f)
        if isinstance(extra, dict):
            payload.update(extra)

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(args.timeout)
        client.connect(args.socket)
        client.sendall(json.dumps(payload).encode("utf-8") + b"\n")

        data = b""
        while True:
            try:
                chunk = client.recv(4096)
            except TimeoutError:
                print(
                    json.dumps(
                        {
                            "status": "error",
                            "message": (
                                f"timed out waiting for response to {args.command}"
                            ),
                        }
                    )
                )
                return 1
            if not chunk:
                break
            data += chunk
            try:
                lines = data.decode("utf-8").splitlines()
            except UnicodeDecodeError:
                continue

            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(message, dict):
                    continue
                if "status" in message:
                    sys.stdout.write(json.dumps(message))
                    return 0
                if "event" not in message:
                    sys.stdout.write(json.dumps(message))
                    return 0

    if not data:
        print(json.dumps({"status": "error", "message": "no response"}))
        return 1

    # If no status-bearing response arrived, fall back to the first valid JSON line.
    for line in data.decode("utf-8").splitlines():
        line = line.strip()
        if line:
            sys.stdout.write(line)
            break
    else:
        print(json.dumps({"status": "error", "message": "empty response"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
