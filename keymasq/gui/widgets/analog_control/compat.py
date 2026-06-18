from __future__ import annotations

import sys
from typing import Any

from keymasq import __version__ as _package_version
from keymasq.common.slurp import get_slurp_capture as _get_slurp_capture
from keymasq.gui.widgets.docs_links import actions_docs_url as _actions_docs_url
from keymasq.gui.widgets.gamepad_output_choices import (
    virtual_gamepad_count as _virtual_gamepad_count,
)
from keymasq.session.compositor import detect_compositor_sync as _detect_compositor_sync
from keymasq.session.hardware import HardwareManager as _HardwareManager

_SHIM_MODULE = "keymasq.gui.widgets.analog_control_dialog"


def _shim_attr[T](name: str, default: T) -> T:
    module = sys.modules.get(_SHIM_MODULE)
    if module is None:
        return default
    return getattr(module, name, default)


def package_version() -> str:
    return str(_shim_attr("__version__", _package_version))


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


def analog_controls_docs_url() -> str:
    return _actions_docs_url("analog-controls", version=package_version())
