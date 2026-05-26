import logging
import time
from typing import Any, cast

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, GLib, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq import __version__
from keymasq.gui.icons import image_from_icon_names
from keymasq.gui.session_client import (
    JsonDict,
    register_session_event_callback,
    session_request_async,
    unregister_session_event_callback,
)

DIAGNOSTICS_CATEGORIES = ("mainline", "combo", "internal")
OUTPUT_STREAM_FILTERS = ("button", "axis", "mousemove", "syn", "other", "repeat")
OUTPUT_STREAM_DEFAULT_FILTERS = {"button"}
OUTPUT_STREAM_ROW_LIMIT = 200
RESET_LATENCY_TOOLTIP = "Reset collected samples"
RESET_OUTPUT_TOOLTIP = "Reset collected output events"
DIAGNOSTICS_LABELS = {
    "passthrough_fast": "Unmapped passthrough",
    "passthrough_mapped": "Passthrough with mappings",
    "passthrough_other": "Other passthrough",
    "wheel_passthrough": "Wheel passthrough",
    "combo_passthrough": "Combo candidate passthrough",
    "combo_passthrough_held": "Combo held passthrough",
    "syn": "Synchronization event",
    "wheel_high_res_suppressed": "High-res wheel suppressed",
    "combo_recalled_repeat_suppressed": "Combo recall repeat suppressed",
    "combo_recalled_release_suppressed": "Combo recall release suppressed",
}
PEAK_TOOLTIP = (
    "One unusually slow event. If p95 and p99 are low, this usually reflects "
    "OS scheduling noise, not normal input latency."
)
_LABEL_SORT_ORDER: dict[str, int] = {
    "passthrough_mapped": 0,
    "passthrough_fast": 1,
    "passthrough_other": 2,
    "wheel_passthrough": 3,
    "combo_passthrough": 10,
    "combo_passthrough_held": 11,
    "syn": 30,
    "wheel_high_res_suppressed": 31,
    "combo_recalled_repeat_suppressed": 32,
    "combo_recalled_release_suppressed": 33,
}


def _format_latency(value: object) -> str:
    try:
        micros = float(cast(Any, value))
    except (TypeError, ValueError):
        micros = 0.0
    if micros >= 1000.0:
        return f"{micros / 1000.0:.2f} ms"
    return f"{micros:.1f} us"


def _label_title(label: str) -> str:
    if label.startswith("action_"):
        return f"Action: {label.removeprefix('action_').replace('_', ' ')}"
    if label.startswith("combo_release_action_"):
        action = label.removeprefix("combo_release_action_").replace("_", " ")
        return f"Combo release action: {action}"
    return DIAGNOSTICS_LABELS.get(label, label.replace("_", " ").title())


def _label_sort_key(label: str) -> tuple[int, str]:
    if label in _LABEL_SORT_ORDER:
        return (_LABEL_SORT_ORDER[label], label)
    if label.startswith("action_"):
        return (20, label)
    if label.startswith("combo_release_action_"):
        return (21, label)
    return (40, label)


def _sort_by_priority(row1: Gtk.ListBoxRow, row2: Gtk.ListBoxRow) -> int:
    key1: tuple[int, str] = getattr(row1, "_sort_key", (99, ""))
    key2: tuple[int, str] = getattr(row2, "_sort_key", (99, ""))
    if key1 < key2:
        return -1
    if key1 > key2:
        return 1
    return 0


def _text(value: object, default: str = "") -> str:
    text = str(value or "").strip()
    return text if text else default


def _output_event_title(event: JsonDict) -> str:
    output = _text(event.get("output"), "output")
    output_id = _text(event.get("output_id"))
    code_name = _text(event.get("code_name"), _text(event.get("code"), "?"))
    value = _text(event.get("value"), "0")
    if output_id and output_id != output:
        return f"{output} {output_id}: {code_name}={value}"
    return f"{output}: {code_name}={value}"


def _output_event_detail(event: JsonDict) -> str:
    sequence = int(event.get("sequence", 0) or 0)
    category = _text(event.get("category"))
    parts = [f"#{sequence}" if sequence else ""]
    if category:
        parts.append(category)
    type_name = _text(event.get("type_name"), _text(event.get("type")))
    code_name = _text(event.get("code_name"), _text(event.get("code")))
    value = _text(event.get("value"), "0")
    parts.append(f"{type_name} {code_name} value={value}")
    hardware_id = _text(event.get("hardware_id"))
    interface_id = _text(event.get("interface_id"))
    if hardware_id:
        parts.append(hardware_id)
    if interface_id:
        parts.append(interface_id)
    return "  ".join(part for part in parts if part)


def _output_event_export_line(event: JsonDict) -> str:
    sequence = int(event.get("sequence", 0) or 0)
    output = _text(event.get("output"), "output")
    output_id = _text(event.get("output_id"))
    output_label = output if not output_id or output_id == output else f"{output} {output_id}"
    code_name = _text(event.get("code_name"), _text(event.get("code"), "unknown"))
    event_type = _text(event.get("type_name"), _text(event.get("type"), "unknown"))
    value = _text(event.get("value"), "0")
    parts = [
        f"#{sequence}" if sequence else "#-",
        output_label,
        code_name,
        event_type,
        f"value={value}",
    ]
    hardware_id = _text(event.get("hardware_id"))
    interface_id = _text(event.get("interface_id"))
    if hardware_id:
        parts.append(f"hardware={hardware_id}")
    if interface_id:
        parts.append(f"interface={interface_id}")
    return " ".join(parts)


log = logging.getLogger("keymasq.gui.widgets.diagnostics_dialog")


def _docs_version() -> str:
    version = __version__.strip()
    if not version:
        return "master"
    if "dev" in version:
        return "master"
    return f"v{version.removeprefix('v')}"


def _diagnostics_docs_url() -> str:
    return f"https://keymasq.tools/docs/{_docs_version()}/PERFORMANCE/#diagnostics-labels"


class DiagnosticsDialog(Adw.Dialog):
    def __init__(self, parent: Gtk.Window):
        super().__init__(title="Diagnostics", content_width=760, content_height=560)
        self._parent = parent
        self._enabled = False
        self._request_inflight = False
        self._closing = False
        self._syncing_controls = False
        self._category_checks: dict[str, Gtk.ToggleButton] = {}
        self._rows: dict[str, Gtk.ListBoxRow] = {}
        self._last_snapshot_time: float | None = None
        self._tick_source_id: int = 0
        self._output_enabled = False
        self._output_request_inflight = False
        self._output_filter_checks: dict[str, Gtk.ToggleButton] = {}
        self._output_rows: list[Gtk.ListBoxRow] = []
        self._output_events: list[JsonDict] = []

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()

        self._docs_button = Gtk.Button(label="?")
        self._docs_button.add_css_class("flat")
        self._docs_button.add_css_class("actions-docs-button")
        self._docs_button.set_tooltip_text("Open Diagnostics documentation")
        self._docs_button.connect("clicked", self._on_docs_clicked)
        header.pack_end(self._docs_button)

        self._reset_button = Gtk.Button(icon_name="view-refresh-symbolic")
        self._reset_button.set_tooltip_text(RESET_LATENCY_TOOLTIP)
        self._reset_button.set_sensitive(False)
        self._reset_button.connect("clicked", self._on_reset_clicked)
        header.pack_end(self._reset_button)

        toolbar.add_top_bar(header)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        content.set_margin_top(16)
        content.set_margin_bottom(16)
        content.set_margin_start(16)
        content.set_margin_end(16)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        controls.set_valign(Gtk.Align.CENTER)

        self._enable_switch = Gtk.Switch()
        self._enable_switch.set_valign(Gtk.Align.CENTER)
        self._enable_switch.connect("notify::active", self._on_enabled_changed)
        controls.append(self._enable_switch)

        enable_label = Gtk.Label(label="Collect latency diagnostics")
        enable_label.set_halign(Gtk.Align.START)
        enable_label.set_hexpand(True)
        controls.append(enable_label)

        interval_label = Gtk.Label(label="Interval")
        controls.append(interval_label)

        adjustment = Gtk.Adjustment(value=5.0, lower=0.5, upper=60.0, step_increment=0.5)
        self._interval_spin = Gtk.SpinButton(adjustment=adjustment, climb_rate=0.0, digits=1)
        self._interval_spin.set_width_chars(5)
        self._interval_spin.connect("value-changed", self._on_interval_changed)
        controls.append(self._interval_spin)

        suffix_label = Gtk.Label(label="s")
        suffix_label.add_css_class("dim-label")
        controls.append(suffix_label)

        content.append(controls)

        filter_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        filter_row.set_valign(Gtk.Align.CENTER)

        category_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        category_box.add_css_class("linked")
        category_box.set_hexpand(True)
        for category, label_text in (
            ("mainline", "Mainline"),
            ("combo", "Combo"),
            ("internal", "Internal"),
        ):
            btn = Gtk.ToggleButton(label=label_text)
            btn.set_active(category == "mainline")
            btn.connect("toggled", self._on_category_toggled)
            self._category_checks[category] = btn
            category_box.append(btn)
        filter_row.append(category_box)

        self._show_peak_check = Gtk.CheckButton(label="Show peak")
        self._show_peak_check.set_tooltip_text(PEAK_TOOLTIP)
        self._show_peak_check.connect("toggled", self._on_show_peak_toggled)
        filter_row.append(self._show_peak_check)

        content.append(filter_row)

        self._status_label = Gtk.Label(label="Diagnostics are off.")
        self._status_label.set_halign(Gtk.Align.START)
        self._status_label.add_css_class("caption")
        self._status_label.add_css_class("dim-label")
        content.append(self._status_label)

        self._list = Gtk.ListBox()
        self._list.set_selection_mode(Gtk.SelectionMode.NONE)
        self._list.add_css_class("boxed-list")
        self._list.set_sort_func(_sort_by_priority)
        content.append(self._list)

        self._empty_label = Gtk.Label(label="No samples yet.")
        self._empty_label.set_halign(Gtk.Align.START)
        self._empty_label.add_css_class("dim-label")
        content.append(self._empty_label)

        scrolled.set_child(content)

        self._stack = Gtk.Stack()
        self._stack.set_vexpand(True)
        self._stack.add_titled(scrolled, "latency", "Latency")
        self._stack.add_titled(self._build_output_stream_page(), "output", "Output")
        self._stack.connect("notify::visible-child-name", self._on_stack_page_changed)

        switcher = Gtk.StackSwitcher()
        switcher.set_stack(self._stack)
        switcher.set_halign(Gtk.Align.CENTER)
        switcher.set_margin_top(8)
        switcher.set_margin_bottom(8)

        stack_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        stack_box.append(switcher)
        stack_box.append(self._stack)
        toolbar.set_content(stack_box)
        self.set_child(toolbar)

        register_session_event_callback("diagnostics_snapshot", self._on_diagnostics_snapshot)
        register_session_event_callback(
            "diagnostics_output_event",
            self._on_diagnostics_output_event,
        )
        self._tick_source_id = GLib.timeout_add_seconds(1, self._update_status_tick)
        self.connect("closed", self._on_closed)

    def _build_output_stream_page(self) -> Gtk.ScrolledWindow:
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        content.set_margin_top(16)
        content.set_margin_bottom(16)
        content.set_margin_start(16)
        content.set_margin_end(16)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        controls.set_valign(Gtk.Align.CENTER)

        self._output_enable_switch = Gtk.Switch()
        self._output_enable_switch.set_valign(Gtk.Align.CENTER)
        self._output_enable_switch.connect("notify::active", self._on_output_enabled_changed)
        controls.append(self._output_enable_switch)

        enable_label = Gtk.Label(label="Collect output events")
        enable_label.set_halign(Gtk.Align.START)
        enable_label.set_hexpand(True)
        controls.append(enable_label)

        content.append(controls)

        events_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        events_header.set_halign(Gtk.Align.FILL)

        events_title = Gtk.Label(label="Output Events")
        events_title.add_css_class("button-section-title")
        events_title.set_halign(Gtk.Align.START)
        events_header.append(events_title)

        self._copy_output_button = Gtk.Button()
        self._copy_output_button.set_tooltip_text("Copy visible output events")
        self._copy_output_button.add_css_class("flat")
        self._copy_output_button.add_css_class("inspector-copy-button")
        self._copy_output_button.set_child(
            image_from_icon_names("edit-copy-symbolic", "edit-paste-symbolic", pixel_size=14)
        )
        self._copy_output_button.connect("clicked", self._on_copy_output_events_clicked)
        events_header.append(self._copy_output_button)
        content.append(events_header)

        filter_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        filter_box.add_css_class("linked")
        for filter_id, label_text in (
            ("button", "Keys"),
            ("axis", "Axes"),
            ("mousemove", "Move"),
            ("syn", "Syn"),
            ("other", "Other"),
            ("repeat", "Repeat"),
        ):
            btn = Gtk.ToggleButton(label=label_text)
            btn.set_active(filter_id in OUTPUT_STREAM_DEFAULT_FILTERS)
            btn.connect("toggled", self._on_output_filter_toggled)
            self._output_filter_checks[filter_id] = btn
            filter_box.append(btn)
        content.append(filter_box)

        self._output_status_label = Gtk.Label(label="Output event stream is off.")
        self._output_status_label.set_halign(Gtk.Align.START)
        self._output_status_label.add_css_class("caption")
        self._output_status_label.add_css_class("dim-label")
        content.append(self._output_status_label)

        self._output_list = Gtk.ListBox()
        self._output_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self._output_list.add_css_class("boxed-list")
        content.append(self._output_list)

        self._output_empty_label = Gtk.Label(label="No output events yet.")
        self._output_empty_label.set_halign(Gtk.Align.START)
        self._output_empty_label.add_css_class("dim-label")
        content.append(self._output_empty_label)

        scrolled.set_child(content)
        return scrolled

    def _selected_categories(self) -> list[str]:
        selected = [
            category
            for category in DIAGNOSTICS_CATEGORIES
            if self._category_checks[category].get_active()
        ]
        return selected or ["mainline"]

    def _selected_output_filters(self) -> list[str]:
        selected = [
            filter_id
            for filter_id in OUTPUT_STREAM_FILTERS
            if self._output_filter_checks[filter_id].get_active()
        ]
        return selected or ["button"]

    def _on_enabled_changed(self, switch: Gtk.Switch, _param: object) -> None:
        if self._syncing_controls:
            return
        self._enabled = switch.get_active()
        self._apply_diagnostics_settings()

    def _on_output_enabled_changed(self, switch: Gtk.Switch, _param: object) -> None:
        if self._syncing_controls:
            return
        self._output_enabled = switch.get_active()
        self._apply_output_stream_settings()

    def _on_output_filter_toggled(self, button: Gtk.ToggleButton) -> None:
        if self._syncing_controls:
            return
        active = [
            filter_id
            for filter_id in OUTPUT_STREAM_FILTERS
            if self._output_filter_checks[filter_id].get_active()
        ]
        if not active:
            self._syncing_controls = True
            button.set_active(True)
            self._syncing_controls = False
            return
        if self._output_enabled:
            self._apply_output_stream_settings()

    def _on_interval_changed(self, _spin: Gtk.SpinButton) -> None:
        if self._syncing_controls:
            return
        if self._enabled:
            self._apply_diagnostics_settings()

    def _on_category_toggled(self, button: Gtk.ToggleButton) -> None:
        if self._syncing_controls:
            return
        active = [c for c in DIAGNOSTICS_CATEGORIES if self._category_checks[c].get_active()]
        if not active:
            self._syncing_controls = True
            button.set_active(True)
            self._syncing_controls = False
            return
        if self._enabled:
            self._apply_diagnostics_settings()

    def _on_show_peak_toggled(self, _button: Gtk.CheckButton) -> None:
        visible = self._show_peak_check.get_active()
        for row in self._rows.values():
            cells = getattr(row, "_diagnostics_cells", {})
            if isinstance(cells, dict) and "max" in cells:
                cells["max"].set_visible(visible)

    def _on_stack_page_changed(self, _stack: Gtk.Stack, _param: object) -> None:
        self._update_reset_button_state()

    def _apply_diagnostics_settings(self) -> None:
        if self._request_inflight:
            return
        payload = self._diagnostics_payload(self._enabled)
        self._request_inflight = True
        self._set_controls_sensitive(False)
        session_request_async(payload, self._on_diagnostics_response, timeout=2.0)

    def _apply_output_stream_settings(self) -> None:
        if self._output_request_inflight:
            return
        self._output_request_inflight = True
        self._set_output_controls_sensitive(False)
        session_request_async(
            {
                "command": "set_diagnostics_output_stream",
                "enabled": bool(self._output_enabled),
                "filters": self._selected_output_filters(),
            },
            self._on_output_stream_response,
            timeout=2.0,
        )

    def _on_diagnostics_response(self, result: JsonDict | None) -> bool:
        self._request_inflight = False
        if self._closing:
            return False
        self._set_controls_sensitive(True)
        if not result or result.get("status") != "ok":
            message = str((result or {}).get("message") or "Diagnostics request failed.")
            self._status_label.set_text(message)
            return False

        data = result.get("data")
        response = data if isinstance(data, dict) else {}
        self._enabled = bool(response.get("enabled", self._enabled))
        self._sync_enabled_switch()
        self._sync_categories(response.get("categories"))
        interval = response.get("interval")
        if isinstance(interval, (int, float)):
            self._syncing_controls = True
            try:
                self._interval_spin.set_value(float(interval))
            finally:
                self._syncing_controls = False
        self._reset_button.set_sensitive(self._enabled)
        if self._enabled:
            self._status_label.set_text("Waiting for samples...")
        else:
            self._status_label.set_text("Diagnostics are off.")
            self._clear_rows()
        return False

    def _on_output_stream_response(self, result: JsonDict | None) -> bool:
        self._output_request_inflight = False
        if self._closing:
            return False
        self._set_output_controls_sensitive(True)
        if not result or result.get("status") != "ok":
            message = str((result or {}).get("message") or "Output stream request failed.")
            self._output_status_label.set_text(message)
            return False

        data = result.get("data")
        response = data if isinstance(data, dict) else {}
        self._output_enabled = bool(response.get("enabled", self._output_enabled))
        self._sync_output_enabled_switch()
        self._sync_output_filters(response.get("filters"))
        if self._output_enabled:
            self._output_status_label.set_text("Waiting for output events...")
        else:
            self._output_status_label.set_text("Output event stream is off.")
        return False

    def _diagnostics_payload(self, enabled: bool) -> JsonDict:
        return {
            "command": "set_diagnostics",
            "enabled": bool(enabled),
            "interval": self._interval_spin.get_value(),
            "categories": self._selected_categories(),
        }

    def _set_controls_sensitive(self, sensitive: bool) -> None:
        self._enable_switch.set_sensitive(sensitive)
        self._interval_spin.set_sensitive(sensitive)
        for check in self._category_checks.values():
            check.set_sensitive(sensitive)
        self._update_reset_button_state()

    def _set_output_controls_sensitive(self, sensitive: bool) -> None:
        self._output_enable_switch.set_sensitive(sensitive)
        for check in self._output_filter_checks.values():
            check.set_sensitive(sensitive)
        self._update_reset_button_state()

    def _update_reset_button_state(self) -> None:
        page_name = self._stack.get_visible_child_name()
        if page_name == "output":
            self._reset_button.set_tooltip_text(RESET_OUTPUT_TOOLTIP)
            self._reset_button.set_sensitive(True)
            return
        self._reset_button.set_tooltip_text(RESET_LATENCY_TOOLTIP)
        self._reset_button.set_sensitive(not self._request_inflight and self._enabled)

    def _sync_enabled_switch(self) -> None:
        self._syncing_controls = True
        try:
            if self._enable_switch.get_active() != self._enabled:
                self._enable_switch.set_active(self._enabled)
        finally:
            self._syncing_controls = False

    def _sync_categories(self, raw_categories: object) -> None:
        if not isinstance(raw_categories, list):
            return
        selected = {str(category) for category in raw_categories}
        self._syncing_controls = True
        try:
            for category, check in self._category_checks.items():
                check.set_active(category in selected)
        finally:
            self._syncing_controls = False

    def _sync_output_enabled_switch(self) -> None:
        self._syncing_controls = True
        try:
            if self._output_enable_switch.get_active() != self._output_enabled:
                self._output_enable_switch.set_active(self._output_enabled)
        finally:
            self._syncing_controls = False

    def _sync_output_filters(self, raw_filters: object) -> None:
        if not isinstance(raw_filters, list):
            return
        selected = {str(filter_id) for filter_id in raw_filters}
        self._syncing_controls = True
        try:
            for filter_id, check in self._output_filter_checks.items():
                check.set_active(filter_id in selected)
        finally:
            self._syncing_controls = False

    def _on_diagnostics_snapshot(self, event: JsonDict) -> bool:
        samples = event.get("samples")
        if not isinstance(samples, dict):
            return False
        self._enabled = bool(event.get("enabled", True))
        self._sync_enabled_switch()
        self._sync_categories(event.get("categories"))
        interval = event.get("interval")
        if isinstance(interval, (int, float)):
            self._syncing_controls = True
            try:
                self._interval_spin.set_value(float(interval))
            finally:
                self._syncing_controls = False
        self._reset_button.set_sensitive(self._enabled)
        self._render_samples(cast(dict[str, Any], samples))
        return False

    def _on_diagnostics_output_event(self, event: JsonDict) -> bool:
        events = event.get("events")
        if not isinstance(events, list):
            return False
        self._output_enabled = bool(event.get("enabled", True))
        self._sync_output_enabled_switch()
        self._sync_output_filters(event.get("filters"))
        for payload in events:
            if isinstance(payload, dict):
                self._prepend_output_row(cast(JsonDict, payload))
        dropped = int(event.get("dropped", 0) or 0)
        if dropped:
            self._output_status_label.set_text(f"Updated just now. Dropped {dropped} events.")
        elif events:
            self._output_status_label.set_text("Updated just now.")
        return False

    def _prepend_output_row(self, event: JsonDict) -> None:
        row = Gtk.ListBoxRow()
        row.set_selectable(False)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(10)
        box.set_margin_end(10)

        title = Gtk.Label(label=_output_event_title(event))
        title.set_halign(Gtk.Align.START)
        title.add_css_class("heading")
        box.append(title)

        detail = Gtk.Label(label=_output_event_detail(event))
        detail.set_halign(Gtk.Align.START)
        detail.add_css_class("caption")
        detail.add_css_class("dim-label")
        box.append(detail)

        row.set_child(box)
        self._output_list.prepend(row)
        self._output_rows.insert(0, row)
        self._output_events.insert(0, dict(event))
        while len(self._output_rows) > OUTPUT_STREAM_ROW_LIMIT:
            old = self._output_rows.pop()
            self._output_events.pop()
            self._output_list.remove(old)
        self._output_empty_label.set_visible(not self._output_rows)

    def _on_copy_output_events_clicked(self, _button: Gtk.Button) -> None:
        display = Gdk.Display.get_default()
        if display is None:
            return
        display.get_clipboard().set(self._visible_output_event_export_text())

    def _visible_output_event_export_text(self) -> str:
        return "\n".join(_output_event_export_line(event) for event in self._output_events)

    def _render_samples(self, samples: dict[str, Any]) -> None:
        active_labels = set(samples)
        for label in sorted(set(self._rows) - active_labels):
            row = self._rows.pop(label)
            self._list.remove(row)

        for label in sorted(samples):
            stats = samples[label]
            if not isinstance(stats, dict):
                continue
            row = self._rows.get(label)
            if row is None:
                row = self._create_row(label)
                self._rows[label] = row
                self._list.append(row)
            self._update_row(row, label, stats)

        self._list.invalidate_sort()
        self._empty_label.set_visible(not self._rows)
        if self._rows:
            self._last_snapshot_time = time.monotonic()
            self._status_label.set_text("Updated just now.")

    def _create_row(self, label: str) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.set_selectable(False)
        row._sort_key = _label_sort_key(label)  # pyright: ignore[reportAttributeAccessIssue]
        grid = Gtk.Grid(column_spacing=12, row_spacing=2)
        grid.set_margin_top(8)
        grid.set_margin_bottom(8)
        grid.set_margin_start(10)
        grid.set_margin_end(10)

        title = Gtk.Label(label=_label_title(label))
        title.set_halign(Gtk.Align.START)
        title.set_hexpand(True)
        title.set_tooltip_text(label)
        title.add_css_class("heading")
        grid.attach(title, 0, 0, 1, 1)

        raw = Gtk.Label(label=label)
        raw.set_halign(Gtk.Align.START)
        raw.add_css_class("caption")
        raw.add_css_class("dim-label")
        grid.attach(raw, 0, 1, 1, 1)

        values = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        values.set_halign(Gtk.Align.END)
        values.set_hexpand(True)
        grid.attach(values, 1, 0, 1, 2)

        cells: dict[str, Gtk.Label] = {}
        for key in ("n", "p50", "p95", "p99", "max"):
            cell = Gtk.Label(label="")
            cell.set_width_chars(10 if key != "n" else 7)
            cell.set_xalign(1.0)
            cell.add_css_class("caption")
            cell.add_css_class("diagnostics-stat")
            if key == "max":
                cell.add_css_class("dim-label")
                cell.set_tooltip_text(PEAK_TOOLTIP)
                cell.set_visible(self._show_peak_check.get_active())
            values.append(cell)
            cells[key] = cell
        row._diagnostics_cells = cells  # pyright: ignore[reportAttributeAccessIssue]
        row.set_child(grid)
        return row

    def _update_row(self, row: Gtk.ListBoxRow, _label: str, stats: dict[str, Any]) -> None:
        cells = getattr(row, "_diagnostics_cells", {})
        if not isinstance(cells, dict):
            return
        labels = cast(dict[str, Gtk.Label], cells)
        labels["n"].set_text(f"n {int(stats.get('n', 0) or 0)}")
        labels["p50"].set_text(f"p50 {_format_latency(stats.get('p50'))}")
        labels["p95"].set_text(f"p95 {_format_latency(stats.get('p95'))}")
        labels["p99"].set_text(f"p99 {_format_latency(stats.get('p99'))}")
        labels["max"].set_text(f"peak {_format_latency(stats.get('max'))}")

    def _clear_rows(self) -> None:
        for row in list(self._rows.values()):
            self._list.remove(row)
        self._rows.clear()
        self._empty_label.set_visible(True)
        self._last_snapshot_time = None

    def _clear_output_rows(self) -> None:
        for row in list(self._output_rows):
            self._output_list.remove(row)
        self._output_rows.clear()
        self._output_events.clear()
        self._output_empty_label.set_visible(True)

    def _reset_output_events(self) -> None:
        self._clear_output_rows()
        status = (
            "Waiting for output events..."
            if self._output_enabled
            else "Output event stream is off."
        )
        self._output_status_label.set_text(status)

    def _on_reset_clicked(self, _button: Gtk.Button) -> None:
        if self._stack.get_visible_child_name() == "output":
            self._reset_output_events()
            return
        if self._request_inflight:
            return
        self._clear_rows()
        self._status_label.set_text(
            "Waiting for samples..." if self._enabled else "Diagnostics are off."
        )
        if self._enabled:
            self._apply_diagnostics_settings()

    def _on_docs_clicked(self, _button: Gtk.Button) -> None:
        url = _diagnostics_docs_url()
        try:
            launcher = Gtk.UriLauncher.new(url)
            launcher.launch(None, None, None)
        except Exception as exc:
            log.warning("Could not open Diagnostics documentation %s: %s", url, exc)

    def _update_status_tick(self) -> bool:
        if self._last_snapshot_time is None:
            return GLib.SOURCE_CONTINUE
        elapsed = int(time.monotonic() - self._last_snapshot_time)
        if elapsed < 2:
            self._status_label.set_text("Updated just now.")
        elif elapsed < 60:
            self._status_label.set_text(f"Updated {elapsed}s ago.")
        else:
            minutes = elapsed // 60
            self._status_label.set_text(f"Updated {minutes}m ago.")
        return GLib.SOURCE_CONTINUE

    def _on_closed(self, _dialog: Adw.Dialog) -> None:
        self._closing = True
        if self._tick_source_id:
            GLib.source_remove(self._tick_source_id)
            self._tick_source_id = 0
        unregister_session_event_callback("diagnostics_snapshot", self._on_diagnostics_snapshot)
        unregister_session_event_callback(
            "diagnostics_output_event",
            self._on_diagnostics_output_event,
        )
        if self._enabled:
            self._enabled = False
            session_request_async(
                self._diagnostics_payload(False),
                lambda _result: False,
                timeout=1.0,
            )
        if self._output_enabled:
            self._output_enabled = False
            session_request_async(
                {
                    "command": "set_diagnostics_output_stream",
                    "enabled": False,
                    "filters": self._selected_output_filters(),
                },
                lambda _result: False,
                timeout=1.0,
            )
