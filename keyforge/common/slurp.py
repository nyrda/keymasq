import asyncio
import logging
import os
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from enum import Enum
from typing import Any, cast

from keyforge.common.paths import resolve_slurp_path

log = logging.getLogger("keyforge.slurp")

SLURP_INVISIBLE_COLORS = {
    "background": "#00000000",
    "border": "#00000000",
    "selection": "#00000000",
}

SLURP_COMPATIBLE_COMPOSITORS = {"hyprland", "niri", "wayland-wlr", "kde", "cosmic"}


class SlurpMode(Enum):
    POINT = "point"
    POINT_IMMEDIATE = "point_immediate"


@dataclass
class SlurpResult:
    x: int
    y: int


class SlurpCapture:
    _instance: "SlurpCapture | None" = None

    def __new__(cls) -> "SlurpCapture":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        self._initialized = True
        self._slurp_path: str | None = resolve_slurp_path()
        self._compositor_id: str | None = None
        self._available: bool | None = None
        self._process: asyncio.subprocess.Process | None = None

    def set_compositor(self, compositor_id: str | None) -> None:
        if self._compositor_id != compositor_id:
            self._compositor_id = compositor_id
            self._available = None

    @property
    def available(self) -> bool:
        if self._available is not None:
            return self._available

        if not self._slurp_path:
            log.debug("slurp binary not found")
            self._available = False
            return False

        if not self._compositor_id or self._compositor_id not in SLURP_COMPATIBLE_COMPOSITORS:
            log.debug(
                "compositor %s does not support wlr-layer-shell-unstable-v1",
                self._compositor_id,
            )
            self._available = False
            return False

        self._available = True
        return True

    def get_unavailable_reason(self) -> str | None:
        if not self._slurp_path:
            return "slurp is not installed"
        if not self._compositor_id or self._compositor_id not in SLURP_COMPATIBLE_COMPOSITORS:
            return (
                f"compositor '{self._compositor_id or 'unknown'}' does not support slurp "
                "(requires wlr-layer-shell)"
            )
        return None

    async def capture_point_async(
        self,
        mode: SlurpMode = SlurpMode.POINT,
        on_ready: Callable[[], Awaitable[None]] | None = None,
    ) -> SlurpResult | None:
        if not self.available:
            log.debug("slurp capture not available")
            return None

        slurp_path = self._slurp_path
        if slurp_path is None:
            return None

        args = [
            slurp_path,
            "-p",
            "-b",
            SLURP_INVISIBLE_COLORS["background"],
            "-c",
            SLURP_INVISIBLE_COLORS["border"],
            "-s",
            SLURP_INVISIBLE_COLORS["selection"],
            "-w",
            "0",
            "-f",
            "%x,%y",
        ]

        try:
            log.debug(
                "Starting slurp capture: path=%s compositor=%s "
                "wayland_display=%s xdg_runtime_dir=%s",
                slurp_path,
                self._compositor_id,
                os.environ.get("WAYLAND_DISPLAY", ""),
                os.environ.get("XDG_RUNTIME_DIR", ""),
            )
            self._process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            if mode == SlurpMode.POINT_IMMEDIATE:
                log.debug("slurp waiting 150ms before triggering callback")
                await asyncio.sleep(0.15)
                if on_ready:
                    log.debug("slurp triggering on_ready callback")
                    await on_ready()

            stdout, stderr = await self._process.communicate()

            if self._process.returncode != 0:
                stderr_text = stderr.decode().strip() if stderr else ""
                log.warning(
                    "slurp failed: returncode=%s stderr=%s wayland_display=%s xdg_runtime_dir=%s",
                    self._process.returncode,
                    stderr_text,
                    os.environ.get("WAYLAND_DISPLAY", ""),
                    os.environ.get("XDG_RUNTIME_DIR", ""),
                )
                return None

            output = stdout.decode().strip()
            if not output:
                log.warning("slurp returned empty output")
                return None

            return self._parse_output(output)

        except asyncio.CancelledError:
            if self._process:
                self._process.terminate()
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=1.0)
                except TimeoutError:
                    self._process.kill()
            raise
        except Exception:
            log.exception("slurp capture failed")
            return None
        finally:
            self._process = None

    async def cancel_async(self) -> None:
        if self._process:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=1.0)
            except Exception:
                pass
            self._process = None

    def _parse_output(self, output: str) -> SlurpResult | None:
        parts = output.split(",")
        if len(parts) != 2:
            log.debug("unexpected slurp output format: %s", output)
            return None

        try:
            x = int(parts[0])
            y = int(parts[1])
            return SlurpResult(x=x, y=y)
        except ValueError:
            log.debug("failed to parse slurp output: %s", output)
            return None

    def capture_point(
        self,
        callback: Callable[[SlurpResult | None], None],
        mode: SlurpMode = SlurpMode.POINT,
        on_ready: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        if not self.available:
            self._run_async_task(self._invoke_callback_none(callback))
            return

        async def _run_capture() -> None:
            result = await self.capture_point_async(mode=mode, on_ready=on_ready)
            callback(result)

        self._run_async_task(_run_capture())

    async def _invoke_callback_none(self, callback: Callable[[SlurpResult | None], None]) -> None:
        callback(None)

    def _run_async_task(self, coro: Awaitable[object]) -> None:
        try:
            loop = asyncio.get_running_loop()
            asyncio.ensure_future(coro, loop=loop)
        except RuntimeError:
            asyncio.run(cast(Coroutine[Any, Any, object], coro))


def get_slurp_capture() -> SlurpCapture:
    return SlurpCapture()
