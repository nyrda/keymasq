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
from keymasq.session.compositor import (
    detect_compositor_sync,
    get_compositor_capabilities,
    get_compositor_name,
    get_compositor_support_details_sync,
    is_compositor_supported_sync,
)
