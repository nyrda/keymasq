#!/usr/bin/env python3

import argparse
import json
import os
import socket
import time
import traceback


def append_debug(path: str | None, message: str) -> None:
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(message.rstrip("\n"))
        handle.write("\n")


def recv_line(conn: socket.socket, buffer: bytearray, timeout: float) -> dict | None:
    conn.settimeout(timeout)
    while b"\n" not in buffer:
        try:
            chunk = conn.recv(4096)
        except TimeoutError:
            return None
        if not chunk:
            return None
        buffer.extend(chunk)

    line, _, remainder = buffer.partition(b"\n")
    buffer[:] = remainder
    return json.loads(line.decode("utf-8"))


def send_message(conn: socket.socket, payload: dict) -> None:
    conn.sendall(json.dumps(payload).encode("utf-8") + b"\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--expect-title", action="append", default=[])
    parser.add_argument("--require-focus", action="store_true")
    parser.add_argument("--debug-output")
    args = parser.parse_args()

    result: dict[str, object] = {
        "hello": False,
        "pointer": None,
        "focus_titles": [],
        "messages": [],
    }
    exit_code = 1

    try:
        append_debug(args.debug_output, f"uid={os.getuid()} socket={args.socket}")
        os.makedirs(os.path.dirname(args.socket), exist_ok=True)
        append_debug(args.debug_output, "created socket directory")
        if os.path.exists(args.socket):
            os.unlink(args.socket)
            append_debug(args.debug_output, "removed stale socket")

        deadline = time.monotonic() + args.timeout
        request_id = 1
        buffer = bytearray()

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(args.socket)
            append_debug(args.debug_output, "bound socket")
            server.listen(1)
            server.settimeout(args.timeout)
            append_debug(args.debug_output, "listening")

            conn, _ = server.accept()
            append_debug(args.debug_output, "accepted connection")
            with conn:
                while time.monotonic() < deadline:
                    message = recv_line(conn, buffer, max(0.1, deadline - time.monotonic()))
                    if message is None:
                        continue
                    result["messages"].append(message)

                    message_type = str(message.get("type", ""))
                    append_debug(args.debug_output, f"message={message_type}")
                    if message_type == "hello":
                        result["hello"] = True
                        send_message(conn, {"type": "get_pointer", "request_id": request_id})
                        request_id += 1
                    elif message_type == "pointer":
                        result["pointer"] = message
                    elif message_type == "focus_changed":
                        title = str(message.get("title", "") or "")
                        result["focus_titles"].append(title)

                    focus_titles = result["focus_titles"]
                    if (
                        result["hello"] is True
                        and result["pointer"] is not None
                        and (not args.require_focus or bool(result["focus_titles"]))
                        and all(title in focus_titles for title in args.expect_title)
                    ):
                        break

        exit_code = 0
        if result["hello"] is not True:
            exit_code = 1
        if result["pointer"] is None:
            exit_code = 1
        if args.require_focus and not result["focus_titles"]:
            exit_code = 1
        if not all(title in result["focus_titles"] for title in args.expect_title):
            exit_code = 1
    except Exception:
        result["exception"] = traceback.format_exc()
        append_debug(args.debug_output, result["exception"])
        exit_code = 1
    finally:
        if os.path.exists(args.socket):
            os.unlink(args.socket)
            append_debug(args.debug_output, "removed socket on exit")

        with open(args.output, "w", encoding="utf-8") as handle:
            json.dump(result, handle)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
