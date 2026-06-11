from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

from keymasq import __version__ as _package_version
from keymasq.common.slurp import get_slurp_capture as _get_slurp_capture
from keymasq.gui.session_client import (
    JsonDict,
)
from keymasq.gui.session_client import (
    session_request_async as _session_request_async,
)
from keymasq.gui.session_reload import notify_session_reload_async as _notify_session_reload_async
from keymasq.gui.widgets.gamepad_output_choices import (
    virtual_gamepad_count as _virtual_gamepad_count,
)
from keymasq.session.compositor import detect_compositor_sync as _detect_compositor_sync
from keymasq.session.hardware import HardwareManager as _HardwareManager

_SHIM_MODULE = "keymasq.gui.widgets.key_selector_dialog"


def _shim_attr[T](name: str, default: T) -> T:
    module = sys.modules.get(_SHIM_MODULE)
    if module is None:
        return default
    return getattr(module, name, default)


def package_version() -> str:
    return str(_shim_attr("__version__", _package_version))


def session_request_async(
    payload: JsonDict,
    callback: Callable[[JsonDict | None], bool | None],
    timeout: float | None = None,
) -> Any:
    func = _shim_attr("session_request_async", _session_request_async)
    if timeout is None:
        return func(payload, callback)
    return func(payload, callback, timeout=timeout)


def notify_session_reload_async(
    callback: Callable[[bool], None] | None = None,
    timeout: float = 5.0,
) -> Any:
    func = _shim_attr("notify_session_reload_async", _notify_session_reload_async)
    if callback is None and timeout == 5.0:
        return func()
    if timeout == 5.0:
        return func(callback=callback)
    return func(callback=callback, timeout=timeout)


def get_slurp_capture() -> Any:
    func = _shim_attr("get_slurp_capture", _get_slurp_capture)
    return func()


def detect_compositor_sync() -> str | None:
    func = _shim_attr("detect_compositor_sync", _detect_compositor_sync)
    return func()


def virtual_gamepad_count() -> int:
    func = _shim_attr("virtual_gamepad_count", _virtual_gamepad_count)
    return int(func())


def hardware_manager() -> Any:
    factory = _shim_attr("HardwareManager", _HardwareManager)
    return factory()
