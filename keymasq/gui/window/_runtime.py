# pyright: reportUnusedImport=false
# ruff: noqa: F401
import os
import subprocess

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.paths import KEYMASQ_RECORD_HELPER_PATH, resolve_keymasq_record_helper_path
from keymasq.common.recording_guard import resolve_unlock_status
from keymasq.gui.session_client import (
    GuiTaskResult,
    register_session_event_callback,
    run_gui_task,
    session_request,
    session_request_async,
    unregister_session_event_callback,
)
from keymasq.session.compositor import get_compositor_name


def remove_timeout_source(source_id: int) -> int:
    if source_id <= 0:
        return 0
    GLib.source_remove(source_id)
    return 0
