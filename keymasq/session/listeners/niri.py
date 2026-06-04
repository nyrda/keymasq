import asyncio
import contextlib
import json
import logging
import os
import re
import shlex
from collections.abc import Callable
from pathlib import Path
from typing import cast

from keymasq.common.coercion import int_or_none as _int_or_none
from keymasq.common.coercion import json_object as _json_object
from keymasq.common.slurp import get_slurp_capture
from keymasq.common.types import JsonObject
from keymasq.session.dbus import SessionDBus
from keymasq.session.listeners._socket_helpers import unix_socket_connectable
from keymasq.session.listeners.base import WindowChangeCallback, WindowListener
from keymasq.session.slurp import capture_slurp_cursor_position

log = logging.getLogger("keymasq-session.listeners.niri")

type NiriDispatchBuilder = Callable[[str], tuple[bool, str, JsonObject | None]]

NIRI_DISPATCH_TIMEOUT_SECONDS = 1.5
NIRI_FOCUSED_WINDOW_TIMEOUT_SECONDS = 0.6
NIRI_ACTIVATE_TIMEOUT_SECONDS = 2.0
NIRI_CLI_DISPATCH_TIMEOUT_SECONDS = 3.0

def _normalize_string(value: object) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return str(value)


def _parse_workspace_reference(args: str) -> tuple[bool, str, JsonObject | None]:
    value = str(args or "").strip()
    if not value:
        return False, "missing workspace reference", None

    if value.isdigit():
        index = int(value)
        if index < 1 or index > 255:
            return False, "workspace index must be between 1 and 255", None
        return True, "", {"Index": index}

    if value.startswith("name:"):
        name = value[5:].strip()
        if not name:
            return False, "workspace name cannot be empty", None
        return True, "", {"Name": name}

    return False, "workspace reference must be an index like '2' or a name like 'name:web'", None


def _pascal_to_kebab(value: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "-", value).lower()


def normalize_niri_dispatcher(dispatcher: str) -> str:
    value = " ".join(str(dispatcher or "").strip().split())
    if not value:
        return ""

    lowered = value.lower()
    for prefix in ("niri msg action ", "msg action ", "action "):
        if lowered.startswith(prefix):
            value = value[len(prefix) :].strip()
            lowered = value.lower()
            break

    if not value:
        return ""

    parts = value.split()
    if len(parts) == 1 and "_" not in value and "-" not in value and re.search(r"[A-Z]", value):
        value = _pascal_to_kebab(value)
    else:
        value = value.replace("_", "-").replace(" ", "-")

    return value.strip("-").lower()


def _no_arg_action(variant: str) -> NiriDispatchBuilder:
    def _builder(args: str) -> tuple[bool, str, JsonObject | None]:
        if str(args or "").strip():
            return False, f"{variant} does not accept arguments", None
        return True, "", {variant: {}}

    return _builder


def _focused_window_optional_id_action(variant: str) -> NiriDispatchBuilder:
    def _builder(args: str) -> tuple[bool, str, JsonObject | None]:
        if str(args or "").strip():
            return False, f"{variant} does not accept arguments", None
        return True, "", {variant: {"id": None}}

    return _builder


def _focus_workspace_action(args: str) -> tuple[bool, str, JsonObject | None]:
    ok, message, reference = _parse_workspace_reference(args)
    if not ok or reference is None:
        return ok, message, None
    return True, "", {"FocusWorkspace": {"reference": reference}}


def _move_window_to_workspace_action(focus: bool) -> NiriDispatchBuilder:
    def _builder(args: str) -> tuple[bool, str, JsonObject | None]:
        ok, message, reference = _parse_workspace_reference(args)
        if not ok or reference is None:
            return ok, message, None
        return True, "", {
            "MoveWindowToWorkspace": {
                "window_id": None,
                "reference": reference,
                "focus": focus,
            }
        }

    return _builder


NIRI_DISPATCH_BUILDERS: dict[str, NiriDispatchBuilder] = {
    "close-window": _focused_window_optional_id_action("CloseWindow"),
    "fullscreen-window": _focused_window_optional_id_action("FullscreenWindow"),
    "toggle-windowed-fullscreen": _focused_window_optional_id_action(
        "ToggleWindowedFullscreen"
    ),
    "toggle-window-floating": _focused_window_optional_id_action("ToggleWindowFloating"),
    "center-window": _focused_window_optional_id_action("CenterWindow"),
    "focus-column-left-or-last": _no_arg_action("FocusColumnLeftOrLast"),
    "focus-column-right-or-first": _no_arg_action("FocusColumnRightOrFirst"),
    "focus-column-left": _no_arg_action("FocusColumnLeft"),
    "focus-column-right": _no_arg_action("FocusColumnRight"),
    "focus-window-up": _no_arg_action("FocusWindowUp"),
    "focus-window-down": _no_arg_action("FocusWindowDown"),
    "move-column-left": _no_arg_action("MoveColumnLeft"),
    "move-column-right": _no_arg_action("MoveColumnRight"),
    "move-window-up": _no_arg_action("MoveWindowUp"),
    "move-window-down": _no_arg_action("MoveWindowDown"),
    "focus-workspace-up": _no_arg_action("FocusWorkspaceUp"),
    "focus-workspace-down": _no_arg_action("FocusWorkspaceDown"),
    "focus-workspace-previous": _no_arg_action("FocusWorkspacePrevious"),
    "focus-workspace": _focus_workspace_action,
    "move-window-to-workspace": _move_window_to_workspace_action(True),
    "send-window-to-workspace": _move_window_to_workspace_action(False),
}


def parse_niri_event(payload: str) -> tuple[str, JsonObject] | None:
    try:
        data = cast(object, json.loads(payload))
    except (json.JSONDecodeError, TypeError):
        return None

    event = _json_object(data)
    if event is None or len(event) != 1:
        return None

    variant, body = next(iter(event.items()))
    body_object = _json_object(body)
    if body_object is None:
        return None

    return str(variant), body_object


def parse_niri_reply(payload: str) -> tuple[bool, object]:
    data = cast(object, json.loads(payload))
    reply = _json_object(data)
    if reply is None or len(reply) != 1:
        raise ValueError("invalid Niri reply")
    variant, body = next(iter(reply.items()))
    if variant == "Ok":
        return True, body
    if variant == "Err":
        return False, _normalize_string(body)
    raise ValueError("invalid Niri reply")


def parse_niri_focused_window_response(payload: str) -> JsonObject | None:
    ok, body = parse_niri_reply(payload)
    if not ok:
        return None

    response = _json_object(body)
    if response is None or len(response) != 1:
        return None
    variant, response_body = next(iter(response.items()))
    if variant != "FocusedWindow":
        return None
    return _json_object(response_body)


class NiriListener(WindowListener):
    def __init__(
        self,
        callback: WindowChangeCallback,
        client: object | None = None,
        dbus: SessionDBus | None = None,
    ) -> None:
        super().__init__(callback, client, dbus=dbus)
        self.socket_path: str | None = None
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._cmd_reader: asyncio.StreamReader | None = None
        self._cmd_writer: asyncio.StreamWriter | None = None
        self._cmd_lock = asyncio.Lock()
        self._focused_window_id: int | None = None
        self._windows: dict[int, JsonObject] = {}
        self._last_class = ""
        self._last_title = ""
        self._slurp = get_slurp_capture()
        self._slurp.set_compositor("niri")

    @property
    def name(self) -> str:
        return "niri"

    @property
    def supports_compositor_dispatch(self) -> bool:
        return True

    @classmethod
    def _socket_path(cls) -> Path | None:
        socket_path = os.environ.get("NIRI_SOCKET")
        if not socket_path:
            return None
        return Path(socket_path)

    @classmethod
    async def _connectable(cls, path: Path, timeout_s: float = 0.2) -> bool:
        return await unix_socket_connectable(path, timeout_s=timeout_s)

    @classmethod
    async def probe_available(cls, dbus: SessionDBus | None = None) -> bool:
        _ = dbus
        socket_path = cls._socket_path()
        if socket_path is None or not socket_path.exists():
            return False
        return await cls._connectable(socket_path)

    async def start(self) -> None:
        socket_path = self.__class__._socket_path()
        if socket_path is None or not socket_path.exists():
            raise RuntimeError("Niri socket not available")

        self.socket_path = str(socket_path)
        self.running = True
        self._task = asyncio.create_task(self._listen())
        await self._refresh_focused_window()
        log.info("Niri listener started")

    async def stop(self) -> None:
        self.running = False

        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        if self.writer:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except (OSError, ConnectionError, RuntimeError):
                log.debug("Failed while closing Niri event writer", exc_info=True)
            self.writer = None
            self.reader = None

        if self._cmd_writer:
            self._cmd_writer.close()
            try:
                await self._cmd_writer.wait_closed()
            except (OSError, ConnectionError, RuntimeError):
                log.debug("Failed while closing Niri command writer", exc_info=True)
            self._cmd_writer = None
            self._cmd_reader = None

        log.info("Niri listener stopped")

    async def health_check(self) -> bool:
        if not await super().health_check():
            return False
        socket_path = self.__class__._socket_path()
        return bool(socket_path and socket_path.exists())

    async def _listen(self) -> None:
        try:
            if not self.socket_path:
                raise RuntimeError("Niri socket path missing")
            self.reader, self.writer = await asyncio.open_unix_connection(self.socket_path)
            await self._send_event_stream_request()

            while self.running:
                data = await self.reader.readline()
                if not data:
                    break

                event = parse_niri_event(data.decode("utf-8", errors="replace").strip())
                if event is None:
                    continue
                await self._handle_event(*event)
        except asyncio.CancelledError:
            pass
        except (OSError, ConnectionError, RuntimeError, UnicodeDecodeError):
            log.exception("Niri listener error")

    async def _send_event_stream_request(self) -> None:
        writer = self.writer
        reader = self.reader
        if writer is None or reader is None:
            raise RuntimeError("Niri event stream not connected")

        writer.write((json.dumps("EventStream") + "\n").encode("utf-8"))
        await writer.drain()
        reply = await asyncio.wait_for(reader.readline(), timeout=NIRI_DISPATCH_TIMEOUT_SECONDS)
        if not reply:
            raise RuntimeError("Niri event stream did not acknowledge")

        ok, body = parse_niri_reply(reply.decode("utf-8", errors="replace").strip())
        if not ok or body != "Handled":
            raise RuntimeError("Niri rejected event stream request")

    async def _handle_event(self, event_type: str, body: JsonObject) -> None:
        if event_type == "WindowsChanged":
            windows_value = body.get("windows")
            if not isinstance(windows_value, list):
                return
            windows = [
                window
                for item in cast(list[object], windows_value)
                if (window := _json_object(item)) is not None
            ]
            self._apply_windows_snapshot(windows)
            await self._emit_current_window_if_changed()
            return

        if event_type == "WindowOpenedOrChanged":
            window = _json_object(body.get("window"))
            if window is None:
                return
            window_id = self._window_id(window)
            if window_id is None:
                return
            existed = window_id in self._windows
            if bool(window.get("is_focused")):
                self._mark_window_focused(window_id)
            self._windows[window_id] = window
            if not self._has_explicit_focus() and not existed:
                self._mark_window_focused(window_id)
            await self._emit_current_window_if_changed()
            return

        if event_type == "WindowClosed":
            window_id = _int_or_none(body.get("id"))
            if window_id is None:
                return
            self._windows.pop(window_id, None)
            if self._focused_window_id == window_id:
                self._mark_window_focused(self._select_best_window_id())
            await self._emit_current_window_if_changed()
            return

        if event_type == "WindowFocusChanged":
            focused_window_id = _int_or_none(body.get("id"))
            self._mark_window_focused(focused_window_id)
            await self._emit_current_window_if_changed()

    def _window_id(self, window: JsonObject) -> int | None:
        return _int_or_none(window.get("id"))

    def _window_info(self, window: JsonObject | None) -> tuple[str, str]:
        if window is None:
            return "", ""
        return (
            _normalize_string(window.get("app_id")),
            _normalize_string(window.get("title")),
        )

    def _has_explicit_focus(self, windows: dict[int, JsonObject] | None = None) -> bool:
        active_windows = windows if windows is not None else self._windows
        return any(bool(window.get("is_focused")) for window in active_windows.values())

    def _select_best_window_id(self, windows: dict[int, JsonObject] | None = None) -> int | None:
        active_windows = windows if windows is not None else self._windows
        if not active_windows:
            return None

        for window_id, window in active_windows.items():
            if bool(window.get("is_focused")):
                return window_id

        if self._focused_window_id in active_windows:
            return self._focused_window_id

        if len(active_windows) == 1:
            return next(iter(active_windows.keys()))

        return max(active_windows.keys())

    def _mark_window_focused(self, window_id: int | None) -> None:
        self._focused_window_id = window_id
        for existing_id, existing_window in self._windows.items():
            existing_window["is_focused"] = existing_id == window_id

    def _apply_windows_snapshot(self, windows: list[JsonObject]) -> None:
        next_windows: dict[int, JsonObject] = {}
        explicit_focused_window_id: int | None = None

        for window in windows:
            window_id = self._window_id(window)
            if window_id is None:
                continue
            next_windows[window_id] = window
            if bool(window.get("is_focused")):
                explicit_focused_window_id = window_id

        self._windows = next_windows
        if explicit_focused_window_id is not None:
            self._mark_window_focused(explicit_focused_window_id)
        else:
            self._mark_window_focused(self._select_best_window_id(next_windows))

    async def _emit_current_window_if_changed(self) -> None:
        window = self._windows.get(self._focused_window_id) if self._focused_window_id else None
        window_class, window_title = self._window_info(window)
        if window_class == self._last_class and window_title == self._last_title:
            return

        self._last_class = window_class
        self._last_title = window_title
        log.debug("Active window changed: app_id=%s title=%s", window_class, window_title)
        await self.callback(window_class, window_title, [])

    async def _refresh_focused_window(self) -> None:
        focused_window = await self._request_focused_window()
        if focused_window is not None:
            window_id = self._window_id(focused_window)
            if window_id is not None:
                focused_window["is_focused"] = True
                self._windows[window_id] = focused_window
                self._mark_window_focused(window_id)
                await self._emit_current_window_if_changed()
                return
        if not self._windows:
            self._focused_window_id = None
        await self._emit_current_window_if_changed()

    async def _request_focused_window(self) -> JsonObject | None:
        ok, response = await self._send_cmd_request(
            "FocusedWindow",
            timeout_s=NIRI_FOCUSED_WINDOW_TIMEOUT_SECONDS,
        )
        if not ok or response is None:
            return None
        response_object = _json_object(response)
        if response_object is None or len(response_object) != 1:
            return None
        variant, body = next(iter(response_object.items()))
        if variant != "FocusedWindow":
            return None
        return _json_object(body)

    async def _request_windows(self) -> list[JsonObject] | None:
        ok, response = await self._send_cmd_request(
            "Windows",
            timeout_s=NIRI_FOCUSED_WINDOW_TIMEOUT_SECONDS,
        )
        if not ok or response is None:
            return None
        response_object = _json_object(response)
        if response_object is None or len(response_object) != 1:
            return None
        variant, body = next(iter(response_object.items()))
        if variant != "Windows" or not isinstance(body, list):
            return None
        return [
            window
            for item in cast(list[object], body)
            if (window := _json_object(item)) is not None
        ]

    async def _refresh_windows_snapshot(self) -> None:
        windows = await self._request_windows()
        if windows is None:
            return
        self._apply_windows_snapshot(windows)
        await self._emit_current_window_if_changed()

    def _resolve_action_window_targets(self, action: JsonObject) -> JsonObject:
        if self._focused_window_id is None or len(action) != 1:
            return action

        variant, body = next(iter(action.items()))
        body_object = _json_object(body)
        if body_object is None:
            return action

        if variant in {
            "CloseWindow",
            "FullscreenWindow",
            "ToggleWindowedFullscreen",
            "ToggleWindowFloating",
            "CenterWindow",
            "FocusWindow",
        } and body_object.get("id") is None:
            body_object["id"] = self._focused_window_id
            return action

        if variant == "MoveWindowToWorkspace" and body_object.get("window_id") is None:
            body_object["window_id"] = self._focused_window_id
            return action

        return action

    async def get_active_window(self) -> tuple[str, str, list[str]]:
        if not self._windows and self.running:
            await self._refresh_focused_window()
            if not self._windows:
                await self._refresh_windows_snapshot()
        window = self._windows.get(self._focused_window_id) if self._focused_window_id else None
        window_class, window_title = self._window_info(window)
        return window_class, window_title, []

    async def activate_window_by_title(self, title: str) -> JsonObject | None:
        expected_title = str(title or "").strip()
        if not expected_title:
            return {"found": False, "message": "title parameter required"}

        windows = await self._request_windows()
        if not windows:
            return {"found": False}

        target_window = next(
            (
                window
                for window in windows
                if _normalize_string(window.get("title")) == expected_title
            ),
            None,
        )
        if target_window is None:
            return {"found": False}

        window_id = self._window_id(target_window)
        if window_id is None:
            return {"found": False}

        ok, response = await self._send_cmd_request(
            {"Action": {"FocusWindow": {"id": window_id}}},
            timeout_s=NIRI_ACTIVATE_TIMEOUT_SECONDS,
        )
        if not ok:
            return {"found": False, "message": _normalize_string(response) or "focus failed"}

        self._apply_windows_snapshot(windows)
        self._mark_window_focused(window_id)
        await self._emit_current_window_if_changed()
        return {"found": True, "id": window_id, "title": expected_title}

    async def get_cursor_position(self) -> tuple[int, int] | None:
        return await capture_slurp_cursor_position(self._slurp, self.client, log)

    async def dispatch(self, dispatcher: str, args: str = "") -> tuple[bool, str]:
        raw_dispatcher = str(dispatcher or "").strip()
        raw_args = str(args or "").strip()

        if raw_dispatcher and not raw_args and raw_dispatcher.lower().startswith(
            ("niri msg action ", "msg action ", "action ")
        ):
            try:
                parts = shlex.split(raw_dispatcher)
            except ValueError as exc:
                return False, f"invalid Niri dispatcher syntax: {exc}"
            while parts and parts[0].lower() in {"niri", "msg", "action"}:
                parts.pop(0)
            if not parts:
                return False, "dispatcher parameter required"
            raw_dispatcher = parts[0]
            raw_args = shlex.join(parts[1:])

        if (
            raw_dispatcher
            and not raw_args
            and " " in raw_dispatcher
            and not raw_dispatcher.lower().startswith(
                ("niri msg action ", "msg action ", "action ")
            )
        ):
            try:
                parts = shlex.split(raw_dispatcher)
            except ValueError:
                parts = raw_dispatcher.split()
            if len(parts) > 1:
                raw_dispatcher = parts[0]
                raw_args = shlex.join(parts[1:])

        dispatcher_name = normalize_niri_dispatcher(raw_dispatcher)
        if not dispatcher_name:
            return False, "dispatcher parameter required"

        builder = NIRI_DISPATCH_BUILDERS.get(dispatcher_name)
        if builder is not None:
            ok, _message, action = builder(raw_args)
            if ok and action is not None:
                if self._focused_window_id is None and self.running:
                    await self._refresh_focused_window()
                    if self._focused_window_id is None:
                        await self._refresh_windows_snapshot()

                action = self._resolve_action_window_targets(action)

                ok, response = await self._send_cmd_request(
                    {"Action": action},
                    timeout_s=NIRI_DISPATCH_TIMEOUT_SECONDS,
                )
                if not ok:
                    if isinstance(response, str) and response:
                        return False, response
                    return False, "no response from Niri"
                if response == "Handled":
                    return True, "ok"
                return True, _normalize_string(response) or "ok"

        return await self._dispatch_with_niri_msg(dispatcher_name, raw_args)

    async def _dispatch_with_niri_msg(
        self,
        dispatcher: str,
        args: str,
    ) -> tuple[bool, str]:
        socket_path = self.socket_path or os.environ.get("NIRI_SOCKET", "")
        if not socket_path:
            return False, "NIRI_SOCKET is not available"

        cmd = ["niri", "msg", "action", dispatcher]
        if args:
            try:
                cmd.extend(shlex.split(args))
            except ValueError as exc:
                return False, f"invalid Niri dispatcher arguments: {exc}"

        env = os.environ.copy()
        env["NIRI_SOCKET"] = socket_path

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except FileNotFoundError:
            return False, "niri command is not installed"
        except (OSError, RuntimeError) as exc:
            return False, f"failed to launch niri msg: {exc}"

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=NIRI_CLI_DISPATCH_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            with contextlib.suppress(OSError, RuntimeError):
                await process.communicate()
            return False, f"niri msg action timed out: {dispatcher}"

        stdout_text = stdout.decode("utf-8", errors="replace").strip()
        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        if process.returncode != 0:
            return False, stderr_text or stdout_text or f"niri msg action failed: {dispatcher}"
        return True, stdout_text or "ok"

    async def _ensure_cmd_connection(self) -> bool:
        if not self.socket_path:
            return False
        if self._cmd_reader and self._cmd_writer:
            return True
        try:
            self._cmd_reader, self._cmd_writer = await asyncio.open_unix_connection(
                self.socket_path
            )
            return True
        except (OSError, ConnectionError, RuntimeError):
            self._cmd_reader = None
            self._cmd_writer = None
            return False

    async def _reset_failed_cmd_connection(self) -> None:
        cmd_writer = self._cmd_writer
        self._cmd_reader = None
        self._cmd_writer = None

        if cmd_writer is None:
            return

        try:
            cmd_writer.close()
            await cmd_writer.wait_closed()
        except OSError as exc:
            log.debug("Failed while closing failed Niri command writer: %s", exc)
        except Exception:
            log.exception("Unexpected failure while closing failed Niri command writer")

    async def _send_cmd_request(
        self,
        request: object,
        timeout_s: float,
    ) -> tuple[bool, object | None]:
        async with self._cmd_lock:
            for _ in range(2):
                if not await self._ensure_cmd_connection():
                    return False, None
                try:
                    cmd_reader = self._cmd_reader
                    cmd_writer = self._cmd_writer
                    if cmd_reader is None or cmd_writer is None:
                        return False, None

                    cmd_writer.write((json.dumps(request) + "\n").encode("utf-8"))
                    await cmd_writer.drain()
                    reply = await asyncio.wait_for(cmd_reader.readline(), timeout=timeout_s)
                    if not reply:
                        raise ConnectionError("Niri command socket closed")

                    ok, body = parse_niri_reply(reply.decode("utf-8", errors="replace").strip())
                    if ok:
                        return True, body
                    log.debug("Niri request failed: %s", body)
                    return False, body
                except TimeoutError as exc:
                    log.debug("Niri command request timed out: %s", exc)
                    await self._reset_failed_cmd_connection()
                except OSError as exc:
                    log.debug("Niri command request failed: %s", exc)
                    await self._reset_failed_cmd_connection()
                except (json.JSONDecodeError, ValueError) as exc:
                    log.debug("Niri command reply was invalid: %s", exc)
                    await self._reset_failed_cmd_connection()
                except Exception:
                    log.exception("Unexpected Niri command request failure")
                    await self._reset_failed_cmd_connection()
            return False, None
