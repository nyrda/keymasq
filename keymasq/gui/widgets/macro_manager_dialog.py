"""GTK composition roots for macro management and type-macro creation."""

import logging
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import (  # pyright: ignore[reportAttributeAccessIssue]
    Adw,  # pyright: ignore[reportAttributeAccessIssue]
    GLib,  # pyright: ignore[reportAttributeAccessIssue]
    Gtk,  # pyright: ignore[reportAttributeAccessIssue]
)

from keymasq import __version__
from keymasq.gui.session_client import (
    GuiTaskResult,
    JsonDict,
    run_gui_task,
    session_request,
    session_request_async,
)
from keymasq.gui.widgets.docs_links import docs_page_url
from keymasq.gui.widgets.macro_manager.actions import MacroActionsMixin
from keymasq.gui.widgets.macro_manager.catalog import CatalogControllerMixin
from keymasq.gui.widgets.macro_manager.recording import RecordingControllerMixin
from keymasq.gui.widgets.macro_manager.state import (
    CatalogState,
    RecordingState,
)
from keymasq.gui.widgets.macro_manager.type_dialog import TypeMacroDialogMixin
from keymasq.gui.widgets.macro_manager.view import ManagerViewMixin

log = logging.getLogger("keymasq.gui.widgets.macro_manager_dialog")


def _macros_docs_url() -> str:
    return docs_page_url("MACROS", version=__version__)


class MacroManagerDialog(
    Adw.Dialog,
    ManagerViewMixin,
    CatalogControllerMixin,
    MacroActionsMixin,
    RecordingControllerMixin,
):
    """Compose macro catalog, action, recording, and view controllers."""

    def __init__(self, parent: Gtk.Window):
        super().__init__(title="Macros", content_width=560)
        self._parent = parent
        self._catalog = CatalogState()
        self._recording_state = RecordingState()
        self._record_btn: Gtk.Button | None = None
        self._slot_dropdown: Gtk.DropDown | None = None
        self._search_button: Gtk.Button | None = None
        self.macros_docs_btn: Gtk.Button | None = None
        self._build_ui()

        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", self._on_key_pressed)
        self.add_controller(key_controller)
        GLib.idle_add(self._load_initial_state)

        if hasattr(parent, "register_event_handler"):
            parent.register_event_handler("macro_saved", self._on_macro_saved)
            parent.register_event_handler("recording_started", self._on_recording_started)
            parent.register_event_handler("recording_stopped", self._on_recording_stopped)
        self.connect("closed", self._on_dialog_closed)

    def _session_request(self, payload: JsonDict) -> JsonDict | None:
        return session_request(payload)

    def _session_request_async(
        self,
        payload: JsonDict,
        callback: Callable[[JsonDict | None], bool | None],
        timeout: float = 5.0,
        *,
        on_start: Callable[[], None] | None = None,
        on_done: Callable[[], None] | None = None,
    ) -> None:
        if on_start is None and on_done is None:
            session_request_async(payload, callback)
        elif on_start is None:
            session_request_async(payload, callback, on_done=on_done)
        elif on_done is None:
            session_request_async(payload, callback, on_start=on_start)
        else:
            session_request_async(
                payload,
                callback,
                on_start=on_start,
                on_done=on_done,
            )

    def _run_gui_task[T](
        self,
        worker: Callable[[], T],
        callback: Callable[[GuiTaskResult[T]], bool | None],
        *,
        on_start: Callable[[], None] | None = None,
        on_done: Callable[[], None] | None = None,
    ) -> None:
        if on_start is None and on_done is None:
            run_gui_task(worker, callback)
        elif on_start is None:
            run_gui_task(worker, callback, on_done=on_done)
        elif on_done is None:
            run_gui_task(worker, callback, on_start=on_start)
        else:
            run_gui_task(
                worker,
                callback,
                on_start=on_start,
                on_done=on_done,
            )

    def _new_type_macro_dialog(
        self,
        *,
        on_created: Callable[[], object],
    ) -> "TypeMacroDialog":
        return TypeMacroDialog(self._parent, on_created=on_created)

    def _on_macros_docs_clicked(self, _button: Gtk.Button) -> None:
        url = _macros_docs_url()
        try:
            launcher = Gtk.UriLauncher.new(url)
            launcher.launch(None, None, None)
        except Exception:
            log.exception("Could not open Macros documentation %s", url)

    def _on_macro_saved(self, _data: dict) -> None:
        self._load_macros()

    def _on_dialog_closed(self, _dialog: Adw.Dialog) -> None:
        if hasattr(self._parent, "unregister_event_handler"):
            self._parent.unregister_event_handler("macro_saved", self._on_macro_saved)
            self._parent.unregister_event_handler(
                "recording_started",
                self._on_recording_started,
            )
            self._parent.unregister_event_handler(
                "recording_stopped",
                self._on_recording_stopped,
            )


class TypeMacroDialog(Adw.Dialog, TypeMacroDialogMixin):
    """GTK composition root for creating a type macro."""

    def __init__(
        self,
        parent: Gtk.Window,
        on_created: Callable[[], object] | None = None,
    ):
        super().__init__(title="Create Type Macro", content_width=560)
        self._parent = parent
        self._on_created = on_created
        self._build_ui()

    def _session_request_async(
        self,
        payload: JsonDict,
        callback: Callable[[JsonDict | None], bool | None],
        timeout: float = 5.0,
        *,
        on_start: Callable[[], None] | None = None,
        on_done: Callable[[], None] | None = None,
    ) -> None:
        if on_start is None and on_done is None:
            session_request_async(payload, callback)
        elif on_start is None:
            session_request_async(payload, callback, on_done=on_done)
        elif on_done is None:
            session_request_async(payload, callback, on_start=on_start)
        else:
            session_request_async(
                payload,
                callback,
                on_start=on_start,
                on_done=on_done,
            )
