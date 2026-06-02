import asyncio
import contextlib
import inspect
import json
import logging
import os
import tempfile
import time
import uuid
from collections.abc import Awaitable
from pathlib import Path
from typing import cast

from dbus_next.constants import MessageType
from dbus_next.message import Message
from dbus_next.service import ServiceInterface, method

from keymasq.session.dbus import SessionDBus, name_has_owner
from keymasq.session.listeners.base import WindowChangeCallback, WindowListener

log = logging.getLogger("keymasq-session.listeners.kde")
type JsonObject = dict[str, object]
s = str

KDE_DBUS_INTERFACE = "keymasq.kde.Listener"
KDE_DBUS_OBJECT_PATH = "/keymasq/KDEListener"
KDE_IGNORED_PAYLOAD_LOG_INTERVAL_SECONDS = 10.0
KDE_DISPATCH_TIMEOUT_SECONDS = 1.5
KDE_DISPATCH_METHODS: dict[str, str] = {
    "desktop_next": "slotSwitchDesktopNext",
    "desktop_prev": "slotSwitchDesktopPrevious",
    "window_close": "slotWindowClose",
    "fullscreen_toggle": "slotWindowFullScreen",
    "focus_left": "slotSwitchWindowLeft",
    "focus_right": "slotSwitchWindowRight",
    "focus_up": "slotSwitchWindowUp",
    "focus_down": "slotSwitchWindowDown",
    "move_left": "slotWindowMoveLeft",
    "move_right": "slotWindowMoveRight",
    "move_up": "slotWindowMoveUp",
    "move_down": "slotWindowMoveDown",
    "tile_left": "slotWindowQuickTileLeft",
    "tile_right": "slotWindowQuickTileRight",
    "tile_top": "slotWindowQuickTileTop",
    "tile_bottom": "slotWindowQuickTileBottom",
    "all_desktops_toggle": "slotWindowOnAllDesktops",
    "show_desktop_toggle": "slotToggleShowDesktop",
}


def has_kde_wayland_support() -> bool:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    if not Path(runtime_dir, "bus").exists():
        return False
    return True


async def _probe_kwin_owner(dbus: SessionDBus | None = None) -> bool:
    return await name_has_owner("org.kde.KWin", dbus)


def _parse_json_object(payload: str) -> JsonObject | None:
    try:
        data = json.loads(payload)
        if isinstance(data, str):
            data = json.loads(data)
    except Exception:
        return None
    return cast(JsonObject, data) if isinstance(data, dict) else None


def _object_str(data: JsonObject, key: str, default: str = "") -> str:
    value = data.get(key, default)
    return value if isinstance(value, str) else default


def parse_kde_window_payload(payload: str) -> tuple[str, str] | None:
    data = _parse_json_object(payload)
    if data is None:
        return None

    window_class = _object_str(data, "class")
    window_title = _object_str(data, "title")
    return window_class, window_title


def parse_kde_cursor_payload(payload: str) -> tuple[str, int, int] | None:
    data = _parse_json_object(payload)
    if data is None:
        return None

    request_id = _object_str(data, "id")
    if not request_id:
        return None

    x_raw = data.get("x")
    y_raw = data.get("y")
    if x_raw is None or y_raw is None:
        return None

    try:
        x = int(float(str(x_raw)))
        y = int(float(str(y_raw)))
    except Exception:
        return None

    return request_id, x, y


def parse_kde_dispatch_payload(payload: str) -> tuple[str, bool, str] | None:
    data = _parse_json_object(payload)
    if data is None:
        return None

    request_id = _object_str(data, "id")
    if not request_id:
        return None

    ok_raw = data.get("ok")
    if not isinstance(ok_raw, bool):
        return None

    message = _object_str(data, "message")
    return request_id, ok_raw, message


class _KDEBridge(ServiceInterface):
    def __init__(self, listener: "KDEListener") -> None:
        super().__init__(KDE_DBUS_INTERFACE)
        self._listener = listener

    @method()
    def windowChanged(self, payload: "s") -> None:
        self._listener.handle_window_payload(payload)

    @method()
    def cursorPosition(self, payload: "s") -> None:
        self._listener.handle_cursor_payload(payload)

    @method()
    def dispatchResult(self, payload: "s") -> None:
        self._listener.handle_dispatch_payload(payload)


class _KDEEphemeralScriptLoadError(RuntimeError):
    def __init__(self, script_id: int) -> None:
        super().__init__(f"loadScript returned {script_id}")
        self.script_id = script_id


class _KDEEphemeralScriptRunError(RuntimeError):
    pass


class KDEListener(WindowListener):
    def __init__(
        self,
        callback: WindowChangeCallback,
        client: object | None = None,
        dbus: SessionDBus | None = None,
    ) -> None:
        super().__init__(callback, client, dbus=dbus)
        self._plugin_name = ""
        self._script_path: Path | None = None
        self._script_id: int | None = None
        self._last_class = ""
        self._last_title = ""
        self._bus = None
        self._bridge: _KDEBridge | None = None
        self._kwin_scripting = None
        self._window_script_iface = None
        self._callback_tasks: set[asyncio.Future[None]] = set()
        self._cursor_waiters: dict[str, asyncio.Future[tuple[int, int]]] = {}
        self._dispatch_waiters: dict[str, asyncio.Future[tuple[bool, str]]] = {}
        self._ignored_window_payloads = 0
        self._ignored_cursor_payloads = 0
        self._last_window_payload_log_at = 0.0
        self._last_cursor_payload_log_at = 0.0

    @property
    def name(self) -> str:
        return "kde"

    @property
    def available(self) -> bool:
        return self.quick_probe()

    @property
    def supports_compositor_dispatch(self) -> bool:
        return True

    @classmethod
    def quick_probe(cls) -> bool:
        return has_kde_wayland_support()

    @classmethod
    async def probe_available(cls, dbus: SessionDBus | None = None) -> bool:
        if not cls.quick_probe():
            return False
        return await _probe_kwin_owner(dbus)

    async def start(self) -> None:
        if self.dbus is None:
            raise RuntimeError("KDE Wayland listener requires shared session D-Bus access")
        if not await self.__class__.probe_available(self.dbus):
            raise RuntimeError("KDE Wayland listener requires KWin session D-Bus access")

        self._plugin_name = f"keymasq-kde-{os.getpid()}-{int(time.time() * 1000)}"

        try:
            self._bus = await self.dbus.bus()
            self._bridge = _KDEBridge(self)
            self._bus.export(KDE_DBUS_OBJECT_PATH, self._bridge)

            self._script_path = await asyncio.to_thread(
                self._write_script_file,
                self._build_window_script_source(),
            )

            self._kwin_scripting = await self._get_kwin_scripting_interface()
            script_id = await self._call_load_script(str(self._script_path), self._plugin_name)
            self._script_id = int(script_id)
            if self._script_id < 0:
                raise RuntimeError("failed to load KDE listener script")
            log.info(
                "KDE listener script loaded id=%s plugin=%s",
                self._script_id,
                self._plugin_name,
            )

            self._window_script_iface = await self._get_script_interface(self._script_id)
            call_run = getattr(self._window_script_iface, "call_run", None)
            if not callable(call_run):
                raise RuntimeError("KDE script interface is missing call_run")
            result = call_run()
            if not inspect.isawaitable(result):
                raise RuntimeError("KDE script interface returned a non-awaitable run result")
            await cast(Awaitable[object], result)
        except Exception:
            await self.stop()
            raise

        self.running = True
        log.info("KDE Wayland listener started")

    async def stop(self) -> None:
        self.running = False

        for task in list(self._callback_tasks):
            task.cancel()
        self._callback_tasks.clear()

        for request_id, future in list(self._cursor_waiters.items()):
            if not future.done():
                future.cancel()
            self._cursor_waiters.pop(request_id, None)

        for request_id, future in list(self._dispatch_waiters.items()):
            if not future.done():
                future.cancel()
            self._dispatch_waiters.pop(request_id, None)

        if self._window_script_iface:
            with contextlib.suppress(Exception):
                call_stop = getattr(self._window_script_iface, "call_stop", None)
                if callable(call_stop):
                    result = call_stop()
                    if inspect.isawaitable(result):
                        await cast(Awaitable[object], result)
            self._window_script_iface = None

        if self._kwin_scripting and self._plugin_name:
            with contextlib.suppress(Exception):
                await self._call_unload_script(self._plugin_name)

        self._script_id = None
        self._kwin_scripting = None
        self._plugin_name = ""

        if self._script_path:
            with contextlib.suppress(Exception):
                self._script_path.unlink(missing_ok=True)
            self._script_path = None

        if self._bus and self._bridge:
            with contextlib.suppress(Exception):
                self._bus.unexport(KDE_DBUS_OBJECT_PATH, self._bridge)
        self._bridge = None
        self._bus = None

        log.info("KDE Wayland listener stopped")

    async def get_active_window(self) -> tuple[str, str, list[str]]:
        return self._last_class, self._last_title, []

    async def get_cursor_position(self) -> tuple[int, int] | None:
        if not self.running or self._kwin_scripting is None:
            log.debug("KDE cursor get skipped: listener not running")
            return None

        request_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        future: asyncio.Future[tuple[int, int]] = loop.create_future()
        self._cursor_waiters[request_id] = future

        plugin_name = f"keymasq-kde-cursor-{os.getpid()}-{request_id[:8]}"

        try:
            return await self._run_ephemeral_kwin_script(
                source=self._build_cursor_script_source(request_id),
                plugin_name=plugin_name,
                result_future=future,
                timeout=KDE_DISPATCH_TIMEOUT_SECONDS,
            )
        except _KDEEphemeralScriptLoadError as exc:
            log.debug("KDE cursor get failed: loadScript returned %s", exc.script_id)
            return None
        except _KDEEphemeralScriptRunError:
            return None
        except TimeoutError:
            log.debug("KDE cursor get timed out waiting for response")
            return None
        except Exception:
            log.debug("KDE cursor get failed", exc_info=True)
            return None
        finally:
            self._cursor_waiters.pop(request_id, None)

    async def dispatch(self, dispatcher: str, args: str = "") -> tuple[bool, str]:
        if not self.running or self._kwin_scripting is None:
            return False, "KDE listener not running"

        dispatcher_name = "_".join(str(dispatcher or "").strip().split())
        dispatcher_args = str(args or "").strip()
        if not dispatcher_name:
            return False, "missing dispatcher"
        if dispatcher_args:
            return False, "KDE compositor actions do not accept arguments"

        method_name = KDE_DISPATCH_METHODS.get(dispatcher_name)
        if method_name is None:
            return False, f"unsupported KDE dispatcher: {dispatcher_name}"

        request_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        future: asyncio.Future[tuple[bool, str]] = loop.create_future()
        self._dispatch_waiters[request_id] = future

        plugin_name = f"keymasq-kde-dispatch-{os.getpid()}-{request_id[:8]}"

        try:
            return await self._run_ephemeral_kwin_script(
                source=self._build_dispatch_script_source(request_id, method_name),
                plugin_name=plugin_name,
                result_future=future,
                timeout=KDE_DISPATCH_TIMEOUT_SECONDS,
            )
        except _KDEEphemeralScriptLoadError as exc:
            log.debug("KDE dispatch failed: loadScript returned %s", exc.script_id)
            return False, "failed to load KWin dispatch script"
        except _KDEEphemeralScriptRunError:
            return False, "failed to run KWin dispatch script"
        except TimeoutError:
            log.debug("KDE dispatch timed out waiting for response")
            return False, "timed out waiting for KDE dispatch response"
        except Exception:
            log.debug("KDE dispatch failed", exc_info=True)
            return False, "KDE dispatch failed"
        finally:
            self._dispatch_waiters.pop(request_id, None)

    async def _run_ephemeral_kwin_script[ResultT](
        self,
        *,
        source: str,
        plugin_name: str,
        result_future: asyncio.Future[ResultT],
        timeout: float,
    ) -> ResultT:
        script_path: Path | None = None
        script_iface: object | None = None

        try:
            script_path = await asyncio.to_thread(self._write_script_file, source)
            script_id = await self._call_load_script(str(script_path), plugin_name)
            if script_id < 0:
                raise _KDEEphemeralScriptLoadError(script_id)

            script_iface = await self._get_script_interface(script_id)
            call_run = getattr(script_iface, "call_run", None)
            if not callable(call_run):
                raise _KDEEphemeralScriptRunError("KDE script interface is missing call_run")
            result = call_run()
            if not inspect.isawaitable(result):
                raise _KDEEphemeralScriptRunError(
                    "KDE script interface returned a non-awaitable run result"
                )
            await cast(Awaitable[object], result)
            return await asyncio.wait_for(result_future, timeout=timeout)
        finally:
            if script_iface is not None:
                with contextlib.suppress(Exception):
                    call_stop = getattr(script_iface, "call_stop", None)
                    if callable(call_stop):
                        result = call_stop()
                        if inspect.isawaitable(result):
                            await cast(Awaitable[object], result)
            if self._kwin_scripting:
                with contextlib.suppress(Exception):
                    await self._call_unload_script(plugin_name)
            if script_path:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(script_path.unlink, missing_ok=True)

    def handle_window_payload(self, payload: str) -> None:
        parsed = parse_kde_window_payload(payload)
        if not parsed:
            self._log_ignored_payload("window", payload)
            return

        window_class, window_title = parsed
        if window_class == self._last_class and window_title == self._last_title:
            return

        log.debug("KDE window event: class=%s title=%s", window_class, window_title)

        self._last_class = window_class
        self._last_title = window_title

        task = asyncio.ensure_future(self.callback(window_class, window_title, []))
        self._callback_tasks.add(task)
        task.add_done_callback(self._on_callback_done)

    def _on_window_payload(self, payload: str) -> None:
        self.handle_window_payload(payload)

    def handle_cursor_payload(self, payload: str) -> None:
        parsed = parse_kde_cursor_payload(payload)
        if not parsed:
            self._log_ignored_payload("cursor", payload)
            return

        request_id, x, y = parsed
        future = self._cursor_waiters.get(request_id)
        if future and not future.done():
            future.set_result((x, y))

    def _on_cursor_payload(self, payload: str) -> None:
        self.handle_cursor_payload(payload)

    def handle_dispatch_payload(self, payload: str) -> None:
        parsed = parse_kde_dispatch_payload(payload)
        if not parsed:
            clipped_payload = payload if len(payload) <= 160 else f"{payload[:157]}..."
            log.debug("Ignored KDE dispatch payload: %s", clipped_payload)
            return

        request_id, ok, message = parsed
        future = self._dispatch_waiters.get(request_id)
        if future and not future.done():
            future.set_result((ok, message))

    def _on_dispatch_payload(self, payload: str) -> None:
        self.handle_dispatch_payload(payload)

    def _log_ignored_payload(self, payload_type: str, payload: str) -> None:
        now = time.monotonic()

        if payload_type == "window":
            self._ignored_window_payloads += 1
            if (now - self._last_window_payload_log_at) < KDE_IGNORED_PAYLOAD_LOG_INTERVAL_SECONDS:
                return
            count = self._ignored_window_payloads
            self._ignored_window_payloads = 0
            self._last_window_payload_log_at = now
        else:
            self._ignored_cursor_payloads += 1
            if (now - self._last_cursor_payload_log_at) < KDE_IGNORED_PAYLOAD_LOG_INTERVAL_SECONDS:
                return
            count = self._ignored_cursor_payloads
            self._ignored_cursor_payloads = 0
            self._last_cursor_payload_log_at = now

        clipped_payload = payload if len(payload) <= 160 else f"{payload[:157]}..."
        log.debug(
            "Ignored KDE %s payload (%d recent): %s",
            payload_type,
            count,
            clipped_payload,
        )

    def _on_callback_done(self, task: asyncio.Future[None]) -> None:
        self._callback_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            log.error(f"KDE window callback error: {exc}")

    async def _get_kwin_scripting_interface(self):
        if self._bus is None:
            raise RuntimeError("missing DBus session connection")
        introspection = await self._bus.introspect("org.kde.KWin", "/Scripting")
        proxy = self._bus.get_proxy_object("org.kde.KWin", "/Scripting", introspection)
        return proxy.get_interface("org.kde.kwin.Scripting")

    async def _call_load_script(self, file_path: str, plugin_name: str) -> int:
        if self._bus is None:
            raise RuntimeError("missing DBus session connection")

        reply = await self._bus.call(
            Message(
                destination="org.kde.KWin",
                path="/Scripting",
                interface="org.kde.kwin.Scripting",
                member="loadScript",
                signature="ss",
                body=[file_path, plugin_name],
            )
        )
        if reply is None:
            raise RuntimeError("loadScript failed")
        if reply.message_type == MessageType.ERROR:
            err = str(reply.body[0]) if reply.body else "loadScript failed"
            raise RuntimeError(err)
        if not reply.body:
            raise RuntimeError("loadScript returned no body")
        return int(reply.body[0])

    async def _call_unload_script(self, plugin_name: str) -> None:
        if self._bus is None:
            return

        reply = await self._bus.call(
            Message(
                destination="org.kde.KWin",
                path="/Scripting",
                interface="org.kde.kwin.Scripting",
                member="unloadScript",
                signature="s",
                body=[plugin_name],
            )
        )
        if reply is None:
            raise RuntimeError("unloadScript failed")
        if reply.message_type == MessageType.ERROR:
            err = str(reply.body[0]) if reply.body else "unloadScript failed"
            raise RuntimeError(err)

    async def _get_script_interface(self, script_id: int):
        if self._bus is None:
            raise RuntimeError("missing DBus session connection")
        path = f"/Scripting/Script{script_id}"
        introspection = await self._bus.introspect("org.kde.KWin", path)
        proxy = self._bus.get_proxy_object("org.kde.KWin", path, introspection)
        return proxy.get_interface("org.kde.kwin.Script")

    def _write_script_file(self, source: str) -> Path:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".js", prefix="keymasq-kde-", delete=False
        ) as handle:
            handle.write(source)
            return Path(handle.name)

    def _build_window_script_source(self) -> str:
        return f"""
const DBUS_NAME = \"{self._bus.unique_name if self._bus else ""}\";
const DBUS_PATH = \"{KDE_DBUS_OBJECT_PATH}\";
const DBUS_IFACE = \"{KDE_DBUS_INTERFACE}\";

function safeString(value) {{
    if (value === undefined || value === null) {{
        return \"\";
    }}
    return String(value);
}}

function connectSignal(source, name, callback) {{
    try {{
        const sig = source ? source[name] : null;
        if (sig && typeof sig.connect === "function") {{
            sig.connect(callback);
            return true;
        }}
    }} catch (e) {{}}
    return false;
}}

function disconnectSignal(source, name, callback) {{
    try {{
        const sig = source ? source[name] : null;
        if (sig && typeof sig.disconnect === "function") {{
            sig.disconnect(callback);
            return true;
        }}
    }} catch (e) {{}}
    return false;
}}

function windowClass(win) {{
    let cls = safeString(win.resourceClass);
    if (!cls) cls = safeString(win.desktopFileName);
    if (!cls) cls = safeString(win.resourceName);
    return cls;
}}

function windowTitle(win) {{
    let title = safeString(win.caption);
    if (!title) title = safeString(win.windowRole);
    return title;
}}

function sendWindow(win) {{
    const payload = JSON.stringify({{
        class: win ? windowClass(win) : \"\",
        title: win ? windowTitle(win) : \"\"
    }});
    try {{
        callDBus(DBUS_NAME, DBUS_PATH, DBUS_IFACE, \"windowChanged\", payload);
    }} catch (e) {{}}
}}

let currentWindow = workspace.activeWindow || null;

function onWindowMetaChanged() {{
    sendWindow(workspace.activeWindow || currentWindow);
}}

function reconnectWindowSignals(win) {{
    disconnectSignal(currentWindow, "captionChanged", onWindowMetaChanged);
    disconnectSignal(currentWindow, "windowClassChanged", onWindowMetaChanged);
    disconnectSignal(currentWindow, "resourceClassChanged", onWindowMetaChanged);
    disconnectSignal(currentWindow, "resourceNameChanged", onWindowMetaChanged);
    disconnectSignal(currentWindow, "desktopFileNameChanged", onWindowMetaChanged);
    disconnectSignal(currentWindow, "windowRoleChanged", onWindowMetaChanged);

    currentWindow = win || null;

    connectSignal(currentWindow, "captionChanged", onWindowMetaChanged);
    connectSignal(currentWindow, "windowClassChanged", onWindowMetaChanged);
    connectSignal(currentWindow, "resourceClassChanged", onWindowMetaChanged);
    connectSignal(currentWindow, "resourceNameChanged", onWindowMetaChanged);
    connectSignal(currentWindow, "desktopFileNameChanged", onWindowMetaChanged);
    connectSignal(currentWindow, "windowRoleChanged", onWindowMetaChanged);
}}

function onWindowActivated(win) {{
    reconnectWindowSignals(win);
    sendWindow(workspace.activeWindow || win || currentWindow);
}}

function onWorkspaceWindowChange() {{
    reconnectWindowSignals(workspace.activeWindow || currentWindow);
    sendWindow(workspace.activeWindow || currentWindow);
}}

connectSignal(workspace, "windowActivated", onWindowActivated);
connectSignal(workspace, "activeWindowChanged", onWorkspaceWindowChange);
connectSignal(workspace, "windowAdded", onWorkspaceWindowChange);
connectSignal(workspace, "windowRemoved", onWorkspaceWindowChange);

reconnectWindowSignals(currentWindow);
sendWindow(currentWindow);
""".strip()

    def _build_cursor_script_source(self, request_id: str) -> str:
        return f"""
const DBUS_NAME = \"{self._bus.unique_name if self._bus else ""}\";
const DBUS_PATH = \"{KDE_DBUS_OBJECT_PATH}\";
const DBUS_IFACE = \"{KDE_DBUS_INTERFACE}\";
const REQUEST_ID = \"{request_id}\";

try {{
    const p = workspace.cursorPos;
    const payload = JSON.stringify({{id: REQUEST_ID, x: p.x, y: p.y}});
    callDBus(DBUS_NAME, DBUS_PATH, DBUS_IFACE, \"cursorPosition\", payload);
}} catch (e) {{}}
""".strip()

    def _build_dispatch_script_source(self, request_id: str, method_name: str) -> str:
        return f"""
const DBUS_NAME = \"{self._bus.unique_name if self._bus else ""}\";
const DBUS_PATH = \"{KDE_DBUS_OBJECT_PATH}\";
const DBUS_IFACE = \"{KDE_DBUS_INTERFACE}\";
const REQUEST_ID = \"{request_id}\";
const METHOD_NAME = \"{method_name}\";

function sendResult(ok, message) {{
    const payload = JSON.stringify({{
        id: REQUEST_ID,
        ok: !!ok,
        message: String(message || "")
    }});
    try {{
        callDBus(DBUS_NAME, DBUS_PATH, DBUS_IFACE, \"dispatchResult\", payload);
    }} catch (e) {{}}
}}

try {{
    const method = workspace ? workspace[METHOD_NAME] : null;
    if (typeof method !== "function") {{
        sendResult(false, "unsupported method: " + METHOD_NAME);
    }} else {{
        method.call(workspace);
        sendResult(true, "ok");
    }}
}} catch (e) {{
    sendResult(false, String(e));
}}
""".strip()

    async def health_check(self) -> bool:
        if not await super().health_check():
            return False
        return await self.__class__.probe_available(self.dbus)
