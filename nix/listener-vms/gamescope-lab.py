#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import select
import socket
import struct
import time
from dataclasses import dataclass

from Xlib import X, Xatom, display

STEAM_APP_ID = 769


@dataclass
class _GameWindow:
    key: str
    app_id: int
    display_name: str
    window: object
    gc: object
    width: int
    height: int


def _encode_utf8(value: str) -> bytes:
    return value.encode("utf-8")


def _set_wm_class(disp: display.Display, win: object, instance: str, cls_name: str) -> None:
    payload = f"{instance}\x00{cls_name}\x00".encode("latin-1", errors="replace")
    win.change_property(disp.intern_atom("WM_CLASS"), Xatom.STRING, 8, payload)


def _set_titles(disp: display.Display, win: object, title: str) -> None:
    raw = _encode_utf8(title)
    win.change_property(disp.intern_atom("_NET_WM_NAME"), disp.intern_atom("UTF8_STRING"), 8, raw)
    win.change_property(disp.intern_atom("WM_NAME"), Xatom.STRING, 8, title.encode("latin-1", errors="replace"))


def _set_cardinal(disp: display.Display, win: object, atom_name: str, values: list[int]) -> None:
    win.change_property(disp.intern_atom(atom_name), Xatom.CARDINAL, 32, values)


def _read_cardinals(disp: display.Display, win: object, atom_name: str) -> list[int] | None:
    prop = win.get_full_property(disp.intern_atom(atom_name), X.AnyPropertyType)
    if prop is None:
        return None
    val = prop.value
    if hasattr(val, "tolist"):
        return [int(v) for v in val.tolist()]
    if isinstance(val, (list, tuple)):
        return [int(v) for v in val]
    return [int(val)]


def _decode_display_cardinals(values: list[int] | None) -> str:
    if not values:
        return ""
    raw = b"".join(struct.pack("=I", v & 0xFFFFFFFF) for v in values)
    return raw.split(b"\x00", 1)[0].decode("ascii", errors="ignore")


class GamescopeLab:
    def __init__(self, socket_path: str) -> None:
        self._socket_path = socket_path
        self._displays: dict[str, display.Display] = {
            ":0": display.Display(":0"),
            ":1": display.Display(":1"),
        }
        self._steam_window: object | None = None
        self._steam_gc: object | None = None
        self._mango_window: object | None = None
        self._mango_gc: object | None = None
        self._games: dict[str, _GameWindow] = {}
        self._app_order: list[int] = []
        self._running = True

    def _paint(self, disp: display.Display, win: object, gc: object, width: int, height: int) -> None:
        win.fill_rectangle(gc, 0, 0, width, height)
        disp.flush()

    def _create_painted_window(
        self,
        disp: display.Display,
        *,
        width: int,
        height: int,
        instance: str,
        cls_name: str,
        title: str,
        app_id: int | None = None,
        extra_cardinals: dict[str, list[int]] | None = None,
    ) -> tuple[object, object]:
        screen = disp.screen()
        root = screen.root
        win = root.create_window(
            0,
            0,
            width,
            height,
            0,
            screen.root_depth,
            X.InputOutput,
            X.CopyFromParent,
            background_pixel=screen.black_pixel,
            event_mask=X.ExposureMask | X.StructureNotifyMask,
        )
        _set_wm_class(disp, win, instance, cls_name)
        _set_titles(disp, win, title)
        _set_cardinal(disp, win, "_NET_WM_PID", [os.getpid()])
        if app_id is not None:
            _set_cardinal(disp, win, "STEAM_GAME", [app_id])
        for atom_name, values in (extra_cardinals or {}).items():
            _set_cardinal(disp, win, atom_name, values)
        gc = win.create_gc(foreground=screen.white_pixel, background=screen.black_pixel)
        win.map()
        disp.sync()
        self._paint(disp, win, gc, width, height)
        disp.sync()
        return win, gc

    def _apply_baselayer(self) -> None:
        disp0 = self._displays[":0"]
        root0 = disp0.screen().root
        order = [app_id for app_id in self._app_order if app_id > 0]
        if order:
            _set_cardinal(disp0, root0, "GAMESCOPECTRL_BASELAYER_APPID", order)
            _set_cardinal(disp0, root0, "GAMESCOPECTRL_BASELAYER_WINDOW", [0])
            disp0.sync()

    def _promote_app(self, app_id: int) -> None:
        if app_id <= 0 or app_id > 0xFFFFFFFF:
            raise ValueError(f"invalid app_id: {app_id}")
        self._app_order = [app_id] + [item for item in self._app_order if item != app_id]
        self._apply_baselayer()

    def _handle_request(self, request: dict[str, object]) -> dict[str, object]:
        cmd = str(request.get("command", "") or "")
        if cmd == "ping":
            return {"status": "ok"}

        if cmd == "start_steam":
            disp0 = self._displays[":0"]
            if self._steam_window is None:
                self._steam_window, self._steam_gc = self._create_painted_window(
                    disp0,
                    width=1280,
                    height=800,
                    instance="steamwebhelper",
                    cls_name="steam",
                    title="Steam Big Picture Mode",
                    app_id=STEAM_APP_ID,
                    extra_cardinals={
                        "STEAM_OVERLAY": [0],
                        "STEAM_INPUT_FOCUS": [1],
                        "STEAM_NOTIFICATION": [0],
                    },
                )
            self._promote_app(STEAM_APP_ID)
            return {"status": "ok", "steam_window": int(self._steam_window.id)}

        if cmd == "start_mango":
            disp0 = self._displays[":0"]
            if self._mango_window is None:
                self._mango_window, self._mango_gc = self._create_painted_window(
                    disp0,
                    width=1280,
                    height=800,
                    instance="mangoapp overlay window",
                    cls_name="mangoapp overlay window",
                    title="mangoapp overlay window",
                    extra_cardinals={
                        "GAMESCOPE_EXTERNAL_OVERLAY": [1],
                        "GAMESCOPE_NO_FOCUS": [1],
                    },
                )
            attrs = self._mango_window.get_attributes()
            return {
                "status": "ok",
                "mango_window": int(self._mango_window.id),
                "mapped": attrs.map_state == X.IsViewable,
            }

        if cmd == "open_game":
            key = str(request["key"])
            app_id = int(request["app_id"])
            if app_id <= 0 or app_id > 0xFFFFFFFF:
                raise ValueError(f"invalid app_id: {app_id}")
            title = str(request["title"])
            display_name = str(request.get("display", ":1"))
            width = int(request.get("width", 800))
            height = int(request.get("height", 600))
            disp = self._displays[display_name]
            cls_name = f"steam_app_{app_id}"
            win, gc = self._create_painted_window(
                disp,
                width=width,
                height=height,
                instance=cls_name,
                cls_name=cls_name,
                title=title,
                app_id=app_id,
            )
            self._games[key] = _GameWindow(
                key=key,
                app_id=app_id,
                display_name=display_name,
                window=win,
                gc=gc,
                width=width,
                height=height,
            )
            if self._steam_window is not None:
                disp0 = self._displays[":0"]
                _set_cardinal(disp0, self._steam_window, "STEAM_OVERLAY", [0])
                _set_cardinal(disp0, self._steam_window, "STEAM_INPUT_FOCUS", [0])
            self._promote_app(app_id)
            return {"status": "ok", "window_id": int(win.id)}

        if cmd == "focus_app":
            app_id = int(request["app_id"])
            if app_id <= 0 or app_id > 0xFFFFFFFF:
                raise ValueError(f"invalid app_id: {app_id}")
            if self._steam_window is not None:
                disp0 = self._displays[":0"]
                _set_cardinal(disp0, self._steam_window, "STEAM_OVERLAY", [0])
                _set_cardinal(
                    disp0,
                    self._steam_window,
                    "STEAM_INPUT_FOCUS",
                    [1 if app_id == STEAM_APP_ID else 0],
                )
            self._promote_app(app_id)
            return {"status": "ok"}

        if cmd == "retitle_game":
            key = str(request["key"])
            title = str(request["title"])
            game = self._games.get(key)
            if game is None:
                return {"status": "error", "message": f"unknown game {key}"}
            disp = self._displays[game.display_name]
            _set_titles(disp, game.window, title)
            disp.sync()
            return {"status": "ok"}

        if cmd == "close_game":
            key = str(request["key"])
            game = self._games.pop(key, None)
            if game is None:
                return {"status": "error", "message": f"unknown game {key}"}
            disp = self._displays[game.display_name]
            game.window.unmap()
            game.window.destroy()
            disp.sync()
            remaining_ids = {item.app_id for item in self._games.values()}
            if game.app_id not in remaining_ids:
                self._app_order = [item for item in self._app_order if item != game.app_id]
            if self._steam_window is not None and self._app_order and self._app_order[0] == STEAM_APP_ID:
                disp0 = self._displays[":0"]
                _set_cardinal(disp0, self._steam_window, "STEAM_OVERLAY", [0])
                _set_cardinal(disp0, self._steam_window, "STEAM_INPUT_FOCUS", [1])
            self._apply_baselayer()
            return {"status": "ok"}

        if cmd == "set_overlay":
            if self._steam_window is None:
                return {"status": "error", "message": "steam window not started"}
            overlay = int(request["overlay"])
            input_focus = int(request["input_focus"])
            disp0 = self._displays[":0"]
            _set_cardinal(disp0, self._steam_window, "STEAM_OVERLAY", [overlay])
            _set_cardinal(disp0, self._steam_window, "STEAM_INPUT_FOCUS", [input_focus])
            self._paint(disp0, self._steam_window, self._steam_gc, 1280, 800)
            disp0.sync()
            return {"status": "ok"}

        if cmd == "warp_pointer":
            display_name = str(request["display"])
            x = int(request["x"])
            y = int(request["y"])
            disp = self._displays[display_name]
            disp.screen().root.warp_pointer(x, y)
            disp.sync()
            return {"status": "ok"}

        if cmd == "debug_state":
            disp0 = self._displays[":0"]
            root0 = disp0.screen().root
            return {
                "status": "ok",
                "focused_window": _read_cardinals(disp0, root0, "GAMESCOPE_FOCUSED_WINDOW"),
                "focused_app": _read_cardinals(disp0, root0, "GAMESCOPE_FOCUSED_APP"),
                "focused_app_gfx": _read_cardinals(disp0, root0, "GAMESCOPE_FOCUSED_APP_GFX"),
                "focusable_windows": _read_cardinals(disp0, root0, "GAMESCOPE_FOCUSABLE_WINDOWS"),
                "focus_display": _decode_display_cardinals(
                    _read_cardinals(disp0, root0, "GAMESCOPE_FOCUS_DISPLAY")
                ),
                "mouse_focus_display": _decode_display_cardinals(
                    _read_cardinals(disp0, root0, "GAMESCOPE_MOUSE_FOCUS_DISPLAY")
                ),
                "keyboard_focus_display": _decode_display_cardinals(
                    _read_cardinals(disp0, root0, "GAMESCOPE_KEYBOARD_FOCUS_DISPLAY")
                ),
            }

        if cmd == "quit":
            self._running = False
            return {"status": "ok"}

        return {"status": "error", "message": f"unknown command: {cmd}"}

    def _pump_x11(self) -> None:
        for disp in self._displays.values():
            while disp.pending_events():
                ev = disp.next_event()
                if ev.type == X.Expose:
                    if self._steam_window is not None and ev.window.id == self._steam_window.id:
                        self._paint(disp, self._steam_window, self._steam_gc, 1280, 800)
                    elif self._mango_window is not None and ev.window.id == self._mango_window.id:
                        self._paint(disp, self._mango_window, self._mango_gc, 1280, 800)
                    else:
                        for game in self._games.values():
                            if ev.window.id == game.window.id:
                                self._paint(disp, game.window, game.gc, game.width, game.height)
                                break

    def serve(self) -> None:
        os.makedirs(os.path.dirname(self._socket_path), exist_ok=True)
        if os.path.exists(self._socket_path):
            os.unlink(self._socket_path)

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(self._socket_path)
        server.listen(16)
        server.setblocking(False)
        try:
            last_repaint = time.monotonic()
            while self._running:
                fds = [server.fileno()] + [d.fileno() for d in self._displays.values()]
                ready, _, _ = select.select(fds, [], [], 0.2)
                if server.fileno() in ready:
                    try:
                        conn, _ = server.accept()
                    except OSError:
                        continue
                    with conn:
                        try:
                            deadline = time.monotonic() + 5.0
                            data = b""
                            while not data.endswith(b"\n"):
                                remaining = deadline - time.monotonic()
                                if remaining <= 0 or len(data) >= 65536:
                                    raise TimeoutError("request timed out or exceeded limit")
                                conn.settimeout(remaining)
                                chunk = conn.recv(4096)
                                if not chunk:
                                    break
                                data += chunk
                                if len(data) > 65536:
                                    raise TimeoutError("request exceeded limit")
                            if data and data.endswith(b"\n"):
                                try:
                                    request = json.loads(data.decode("utf-8"))
                                    response = self._handle_request(request)
                                except Exception as exc:  # noqa: BLE001
                                    response = {"status": "error", "message": str(exc)}
                                conn.settimeout(5.0)
                                conn.sendall(json.dumps(response).encode("utf-8") + b"\n")
                        except (OSError, TimeoutError):
                            pass
                self._pump_x11()
                now = time.monotonic()
                if now - last_repaint >= 0.5:
                    last_repaint = now
                    if self._steam_window is not None:
                        self._paint(self._displays[":0"], self._steam_window, self._steam_gc, 1280, 800)
                    for game in self._games.values():
                        self._paint(
                            self._displays[game.display_name],
                            game.window,
                            game.gc,
                            game.width,
                            game.height,
                        )
        finally:
            server.close()
            if os.path.exists(self._socket_path):
                os.unlink(self._socket_path)
            for disp in self._displays.values():
                disp.close()


def _send_command(socket_path: str, payload: dict[str, object]) -> int:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(10.0)
        sock.connect(socket_path)
        sock.sendall(json.dumps(payload).encode("utf-8") + b"\n")
        data = b""
        while not data.endswith(b"\n"):
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
    text = data.decode("utf-8").strip()
    print(text)
    parsed = json.loads(text) if text else {}
    return 0 if parsed.get("status") == "ok" else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    sub = parser.add_subparsers(dest="subcommand", required=True)

    sub.add_parser("serve")
    sub.add_parser("ping")
    sub.add_parser("start-steam")
    sub.add_parser("start-mango")
    sub.add_parser("debug-state")
    sub.add_parser("quit")

    p_open = sub.add_parser("open-game")
    p_open.add_argument("key")
    p_open.add_argument("app_id", type=int)
    p_open.add_argument("title")
    p_open.add_argument("--display", default=":1")

    p_focus = sub.add_parser("focus-app")
    p_focus.add_argument("app_id", type=int)

    p_retitle = sub.add_parser("retitle-game")
    p_retitle.add_argument("key")
    p_retitle.add_argument("title")

    p_close = sub.add_parser("close-game")
    p_close.add_argument("key")

    p_overlay = sub.add_parser("set-overlay")
    p_overlay.add_argument("overlay", type=int)
    p_overlay.add_argument("input_focus", type=int)

    p_warp = sub.add_parser("warp-pointer")
    p_warp.add_argument("display")
    p_warp.add_argument("x", type=int)
    p_warp.add_argument("y", type=int)

    args = parser.parse_args()
    if args.subcommand == "serve":
        GamescopeLab(args.socket).serve()
        return 0
    if args.subcommand == "ping":
        return _send_command(args.socket, {"command": "ping"})
    if args.subcommand == "start-steam":
        return _send_command(args.socket, {"command": "start_steam"})
    if args.subcommand == "start-mango":
        return _send_command(args.socket, {"command": "start_mango"})
    if args.subcommand == "debug-state":
        return _send_command(args.socket, {"command": "debug_state"})
    if args.subcommand == "quit":
        return _send_command(args.socket, {"command": "quit"})
    if args.subcommand == "open-game":
        return _send_command(
            args.socket,
            {
                "command": "open_game",
                "key": args.key,
                "app_id": args.app_id,
                "title": args.title,
                "display": args.display,
            },
        )
    if args.subcommand == "focus-app":
        return _send_command(args.socket, {"command": "focus_app", "app_id": args.app_id})
    if args.subcommand == "retitle-game":
        return _send_command(
            args.socket,
            {"command": "retitle_game", "key": args.key, "title": args.title},
        )
    if args.subcommand == "close-game":
        return _send_command(args.socket, {"command": "close_game", "key": args.key})
    if args.subcommand == "set-overlay":
        return _send_command(
            args.socket,
            {
                "command": "set_overlay",
                "overlay": args.overlay,
                "input_focus": args.input_focus,
            },
        )
    if args.subcommand == "warp-pointer":
        return _send_command(
            args.socket,
            {
                "command": "warp_pointer",
                "display": args.display,
                "x": args.x,
                "y": args.y,
            },
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
