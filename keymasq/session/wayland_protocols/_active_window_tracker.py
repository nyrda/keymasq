import asyncio
from dataclasses import dataclass

type _ToplevelState = list[int] | tuple[int, ...]


@dataclass(slots=True)
class _WindowState:
    app_id: str = ""
    title: str = ""
    activated: bool = False


class ActiveWindowTracker:
    def __init__(self, activated_state: int) -> None:
        self._activated_state = int(activated_state)
        self._windows: dict[str, _WindowState] = {}
        self._active_handle: str | None = None
        self._last_emitted: tuple[str, str] = ("", "")
        self._updates: asyncio.Queue[tuple[str, str]] = asyncio.Queue()

    def add_toplevel(self, handle_id: str) -> None:
        self._windows.setdefault(str(handle_id), _WindowState())

    def close_toplevel(self, handle_id: str) -> None:
        handle = str(handle_id)
        was_active = self._active_handle == handle
        self._windows.pop(handle, None)
        if was_active:
            self._active_handle = self._find_active_handle()
            self._emit_if_changed()

    def update_title(self, handle_id: str, title: str) -> None:
        window = self._windows.setdefault(str(handle_id), _WindowState())
        window.title = str(title or "")
        if self._active_handle == str(handle_id):
            self._emit_if_changed()

    def update_app_id(self, handle_id: str, app_id: str) -> None:
        window = self._windows.setdefault(str(handle_id), _WindowState())
        window.app_id = str(app_id or "")
        if self._active_handle == str(handle_id):
            self._emit_if_changed()

    def update_state(self, handle_id: str, state: _ToplevelState) -> None:
        handle = str(handle_id)
        window = self._windows.setdefault(handle, _WindowState())
        activated = _state_has_activated(state, self._activated_state)
        window.activated = activated

        if activated:
            self._active_handle = handle
            self._emit_if_changed()
            return

        if self._active_handle == handle:
            self._active_handle = self._find_active_handle()
            self._emit_if_changed()

    def get_active_window(self) -> tuple[str, str]:
        handle = self._active_handle
        if handle is None:
            return "", ""
        window = self._windows.get(handle)
        if window is None:
            return "", ""
        return window.app_id, window.title

    async def next_active_window(self, timeout: float | None = None) -> tuple[str, str] | None:
        if timeout is None:
            return await self._updates.get()
        try:
            return await asyncio.wait_for(self._updates.get(), timeout=timeout)
        except TimeoutError:
            return None

    def _mark_done(self) -> None:
        if self._active_handle is None:
            self._active_handle = self._find_active_handle()
        self._emit_if_changed()

    def _find_active_handle(self) -> str | None:
        for handle, window in self._windows.items():
            if window.activated:
                return handle
        return None

    def _emit_if_changed(self) -> None:
        current = self.get_active_window()
        if current == self._last_emitted:
            return
        self._last_emitted = current
        self._updates.put_nowait(current)


def _state_has_activated(state: _ToplevelState, activated_state: int) -> bool:
    return any(value == activated_state for value in state)
