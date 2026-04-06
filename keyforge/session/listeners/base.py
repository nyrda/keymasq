import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from keyforge.session.dbus import SessionDBus

WindowChangeCallback = Callable[[str, str, list[str]], Awaitable[None]]

log = logging.getLogger("keyforge-session.listeners.base")


class WindowListener(ABC):
    def __init__(
        self,
        callback: WindowChangeCallback,
        client: Any | None = None,
        dbus: "SessionDBus | None" = None,
    ) -> None:
        self.callback = callback
        self.client = client
        self.dbus = dbus
        self.running = False
        self._task: asyncio.Task[None] | None = None

    @abstractmethod
    async def start(self) -> None:
        pass

    @abstractmethod
    async def stop(self) -> None:
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @property
    def supports_tags(self) -> bool:
        return False

    @property
    def supports_compositor_dispatch(self) -> bool:
        return False

    @property
    def compositor_dispatch_available(self) -> bool:
        return bool(self.running and self.supports_compositor_dispatch)

    @classmethod
    @abstractmethod
    async def probe_available(cls, dbus: "SessionDBus | None" = None) -> bool:
        _ = dbus
        raise NotImplementedError

    async def health_check(self) -> bool:
        if not self.running:
            return False
        if self._task is None:
            return True
        return not self._task.done()

    async def get_active_window(self) -> tuple[str, str, list[str]]:
        return "", "", []

    async def get_cursor_position(self) -> tuple[int, int] | None:
        return None

    def runtime_support_details(self) -> dict[str, bool | str | int]:
        return {}

    async def dispatch(self, dispatcher: str, args: str = "") -> tuple[bool, str]:
        log.info(
            "Ignored compositor dispatch for unsupported listener=%s dispatcher=%s args=%s",
            self.name,
            str(dispatcher or "").strip(),
            str(args or "").strip(),
        )
        return False, f"{self.name} does not implement compositor dispatch"
