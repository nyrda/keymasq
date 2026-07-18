"""GTK composition root for the macro timeline editor."""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from collections.abc import Callable

from gi.repository import (  # pyright: ignore[reportAttributeAccessIssue]
    Adw,  # pyright: ignore[reportAttributeAccessIssue]
    Gdk,  # pyright: ignore[reportAttributeAccessIssue]
    Gtk,  # pyright: ignore[reportAttributeAccessIssue]
)

from keymasq.common.model.actions import DEFAULT_MACRO_LOOP_STOP_BEHAVIOR
from keymasq.common.slurp import get_slurp_capture
from keymasq.gui.compositor_state import session_compositor_id
from keymasq.gui.session_client import (
    GuiTaskResult,
    JsonDict,
    run_gui_task,
    session_request,
    session_request_async,
)
from keymasq.gui.session_reload import notify_session_reload_async
from keymasq.gui.widgets.macro_editor.add_popovers import MacroEditorAddPopoversMixin
from keymasq.gui.widgets.macro_editor.controller.lifecycle import (
    LifecycleControllerMixin,
)
from keymasq.gui.widgets.macro_editor.controller.load import LoadControllerMixin
from keymasq.gui.widgets.macro_editor.controller.save import SaveControllerMixin
from keymasq.gui.widgets.macro_editor.controller.timing import TimelineControllerMixin
from keymasq.gui.widgets.macro_editor.model import (
    EditableControl,
    EditableEvent,
    EditableMove,
    MacroEvent,
)
from keymasq.gui.widgets.macro_editor.panel.capture import PositionCaptureMixin
from keymasq.gui.widgets.macro_editor.panel.chrome import EditorChromeMixin
from keymasq.gui.widgets.macro_editor.panel.controls import ControlEditorMixin
from keymasq.gui.widgets.macro_editor.panel.properties import EventPropertiesMixin
from keymasq.gui.widgets.macro_editor.panel.settings import MacroSettingsMixin
from keymasq.gui.widgets.position_capture import PositionCaptureController


def _compute_macro_editor_dialog_size(parent: Gtk.Window) -> tuple[int, int]:
    width = 760
    height = 680

    parent_width = parent.get_width()
    parent_height = parent.get_height()
    if parent_width > 1:
        width = int(max(760, min(1500, parent_width * 0.9)))
    if parent_height > 1:
        height = int(max(620, min(1000, parent_height * 0.9)))

    return width, height


class MacroEditorDialog(
    Adw.Dialog,
    EditorChromeMixin,
    EventPropertiesMixin,
    ControlEditorMixin,
    MacroSettingsMixin,
    PositionCaptureMixin,
    MacroEditorAddPopoversMixin,
    LoadControllerMixin,
    TimelineControllerMixin,
    SaveControllerMixin,
    LifecycleControllerMixin,
):
    """Compose the macro document controllers and GTK editor panels."""

    def __init__(
        self,
        parent: Gtk.Window,
        macro_name: str,
        *,
        select_initial_event: bool = True,
    ):
        dialog_width, dialog_height = _compute_macro_editor_dialog_size(parent)
        super().__init__(
            title=f"Edit macro ({macro_name})",
            content_width=dialog_width,
            content_height=dialog_height,
        )
        self._parent = parent
        self._macro_name = macro_name
        self._select_initial_event = bool(select_initial_event)
        self._macro_data: dict = {}
        self._events: list[EditableEvent] = []
        self._rel_events: list[MacroEvent] = []
        self._passthrough_events: list[MacroEvent] = []
        self._synthetic_moves: list[EditableMove] = []
        self._control_events: list[EditableControl] = []
        self._duration_us: int = 0
        self._macro_loop_mode: str = "none"
        self._macro_loop_count: int = 1
        self._macro_loop_stop_behavior: str = DEFAULT_MACRO_LOOP_STOP_BEHAVIOR
        self._macro_has_move_to_start_setting: bool = False
        self._macro_move_to_start: bool = False
        self._macro_start_x: int = 0
        self._macro_start_y: int = 0
        self._macro_block_mouse_movement: bool = False
        self._slurp_capture = get_slurp_capture()
        self._slurp_capture.set_compositor(session_compositor_id())
        self._start_position_capture = PositionCaptureController(
            slurp_capture=self._slurp_capture,
            slurp_available=self._slurp_capture.available,
            request_async=session_request_async,
            on_state_changed=self._update_macro_move_start_controls,
        )
        self._selected_move_capture = PositionCaptureController(
            slurp_capture=self._slurp_capture,
            slurp_available=self._slurp_capture.available,
            request_async=session_request_async,
            on_state_changed=self._update_selected_move_capture_controls,
        )
        self._timing_scale_spin: Gtk.SpinButton | None = None
        self._timing_min_gap_spin: Gtk.SpinButton | None = None
        self._timing_max_gap_spin: Gtk.SpinButton | None = None
        self._timing_extend_ms_spin: Gtk.SpinButton | None = None
        self._insert_gap_at_spin: Gtk.SpinButton | None = None
        self._insert_gap_ms_spin: Gtk.SpinButton | None = None
        self._timeline_scroll_x: float = 0.0
        self._timeline_scroll_max: float = 0.0
        self._timeline_scroll_adj: Gtk.Adjustment | None = None
        self._auto_zoom_enabled: bool = True
        self._auto_zoom_min_pps: float = 90.0
        self._zoom_min_pps: float = 50.0
        self._zoom_max_pps: float = 4000.0
        self._macro_exec_timeout_max_ms: int = 30000
        self._compositor_action_status: dict[str, bool | str | None] = {
            "compositor_id": None,
            "listener_name": None,
            "compositor_dispatch_available": False,
        }
        self._initial_macro_data: dict = {}
        self._initial_state_loaded = False
        self._macro_exists = False
        self._close_warning_dialog: Adw.AlertDialog | None = None
        self._save_in_flight = False
        self._footer_action_buttons: list[Gtk.Button] = []
        self._editor_content: Gtk.Widget | None = None
        self._editor_busy_overlay: Gtk.Widget | None = None
        self._editor_busy_spinner: Gtk.Spinner | None = None
        self._editor_busy_label: Gtk.Label | None = None
        self._dialog_closed: bool = False
        self._updating_props = False
        self._drag_locked: bool = True
        self._erase_mode: bool = False

        self._install_css()
        self._build_ui()
        self.set_can_close(False)
        self._load_initial_state_async()

    def _install_css(self) -> None:
        provider = Gtk.CssProvider()
        provider.load_from_string(
            """
            .macro-editor-outline {
                border: 1px solid #000;
                border-radius: 6px;
            }
            .macro-editor-busy-overlay {
                background-color: alpha(@window_bg_color, 0.7);
            }
            """
        )
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display,
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )

    def _session_request_async(
        self,
        payload: JsonDict,
        callback: Callable[[JsonDict | None], bool | None],
        timeout: float = 5.0,
    ) -> None:
        session_request_async(payload, callback, timeout=timeout)

    def _session_request(self, payload: JsonDict) -> JsonDict | None:
        return session_request(payload)

    def _run_gui_task[T](
        self,
        worker: Callable[[], T],
        callback: Callable[[GuiTaskResult[T]], bool | None],
        *,
        on_start: Callable[[], None] | None = None,
        on_done: Callable[[], None] | None = None,
    ) -> None:
        run_gui_task(
            worker,
            callback,
            on_start=on_start,
            on_done=on_done,
        )

    def _notify_session_reload(self) -> None:
        notify_session_reload_async()

    def do_close_attempt(self) -> None:
        self._request_close()

    def close(self) -> None:
        self._request_close()
