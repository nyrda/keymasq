#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import socket
import threading
from dataclasses import dataclass

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import GLib, Gtk  # noqa: E402


@dataclass(slots=True)
class WindowState:
    window_id: str
    window: Gtk.ApplicationWindow


class WindowLab(Gtk.Application):
    def __init__(
        self,
        *,
        socket_path: str,
        app_id: str,
        initial_window_id: str = "",
        initial_title: str = "",
    ) -> None:
        super().__init__(application_id=app_id)
        self.hold()
        self._socket_path = socket_path
        self._initial_window_id = initial_window_id
        self._initial_title = initial_title
        self._server: socket.socket | None = None
        self._windows: dict[str, WindowState] = {}
        self._opened_initial_window = False

    def do_activate(self) -> None:
        self._start_server()
        if self._initial_window_id and not self._opened_initial_window:
            self._opened_initial_window = True
            self._open_window(self._initial_window_id, self._initial_title)

    def _start_server(self) -> None:
        if self._server is not None:
            return

        os.makedirs(os.path.dirname(self._socket_path), exist_ok=True)
        if os.path.exists(self._socket_path):
            os.unlink(self._socket_path)

        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self._socket_path)
        self._server.listen(16)
        threading.Thread(target=self._serve_forever, daemon=True).start()

    def _serve_forever(self) -> None:
        assert self._server is not None
        while True:
            conn, _ = self._server.accept()
            with conn:
                data = b""
                while not data.endswith(b"\n"):
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                if not data:
                    continue
                request = json.loads(data.decode("utf-8"))
                response = self._invoke_on_main_thread(request)
                conn.sendall(json.dumps(response).encode("utf-8") + b"\n")

    def _invoke_on_main_thread(self, request: dict[str, object]) -> dict[str, object]:
        done = threading.Event()
        response: dict[str, object] = {}

        def dispatch() -> bool:
            nonlocal response
            try:
                response = self._handle_request(request)
            except Exception as exc:  # noqa: BLE001 - callback boundary must answer the client.
                response = {"status": "error", "message": f"request failed: {exc}"}
            finally:
                done.set()
            return False

        GLib.idle_add(dispatch)
        if not done.wait(timeout=10):
            return {"status": "error", "message": "request timed out"}
        return response

    def _handle_request(self, request: dict[str, object]) -> dict[str, object]:
        command = str(request.get("command", "") or "")
        window_id = str(request.get("window_id", "") or "")
        title = str(request.get("title", "") or "")

        if command == "open":
            if not window_id:
                return {"status": "error", "message": "window_id is required"}
            self._open_window(window_id, title)
            return {"status": "ok"}
        if command == "focus":
            state = self._windows.get(window_id)
            if state is None:
                return {"status": "error", "message": "missing window"}
            state.window.present()
            return {"status": "ok"}
        if command == "retitle":
            state = self._windows.get(window_id)
            if state is None:
                return {"status": "error", "message": "missing window"}
            state.window.set_title(title)
            state.window.present()
            return {"status": "ok"}
        if command == "close":
            state = self._windows.pop(window_id, None)
            if state is None:
                return {"status": "error", "message": "missing window"}
            state.window.close()
            return {"status": "ok"}
        if command == "snapshot":
            return {"status": "ok", "windows": sorted(self._windows)}
        if command == "quit":
            self.quit()
            return {"status": "ok"}
        return {"status": "error", "message": f"unknown command: {command}"}

    def _open_window(self, window_id: str, title: str) -> None:
        existing = self._windows.get(window_id)
        if existing is not None:
            existing.window.set_title(title)
            existing.window.present()
            return

        window = Gtk.ApplicationWindow(application=self)
        window.set_title(title)
        window.set_default_size(480, 260)
        window.set_child(Gtk.Label(label=title))

        def on_close_request(_window: Gtk.ApplicationWindow) -> bool:
            self._windows.pop(window_id, None)
            return False

        window.connect("close-request", on_close_request)
        self._windows[window_id] = WindowState(window_id=window_id, window=window)
        window.present()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--initial-window-id", default="")
    parser.add_argument("--initial-title", default="")
    args = parser.parse_args()

    app = WindowLab(
        socket_path=args.socket,
        app_id=args.app_id,
        initial_window_id=args.initial_window_id,
        initial_title=args.initial_title,
    )
    return app.run([])


if __name__ == "__main__":
    raise SystemExit(main())
