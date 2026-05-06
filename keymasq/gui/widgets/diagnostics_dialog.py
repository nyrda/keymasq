from typing import Any, cast

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.gui.session_client import (
    JsonDict,
    register_session_event_callback,
    session_request_async,
    unregister_session_event_callback,
)

DIAGNOSTICS_CATEGORIES = ("mainline", "combo", "internal")
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
    "Peak is the single slowest sample in the current sample window. "
    "p95 and p99 are better indicators of normal latency."
)


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


class DiagnosticsDialog(Adw.Dialog):
    def __init__(self, parent: Gtk.Window):
        super().__init__(title="Diagnostics", content_width=760, content_height=560)
        self._parent = parent
        self._enabled = False
        self._request_inflight = False
        self._syncing_controls = False
        self._category_checks: dict[str, Gtk.CheckButton] = {}
        self._rows: dict[str, Gtk.ListBoxRow] = {}

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
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

        content.append(controls)

        category_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        for category, label in (
            ("mainline", "Mainline"),
            ("combo", "Combo"),
            ("internal", "Internal"),
        ):
            check = Gtk.CheckButton(label=label)
            check.set_active(category == "mainline")
            check.connect("toggled", self._on_category_toggled)
            self._category_checks[category] = check
            category_box.append(check)
        content.append(category_box)

        self._status_label = Gtk.Label(label="Diagnostics are off.")
        self._status_label.set_halign(Gtk.Align.START)
        self._status_label.add_css_class("caption")
        self._status_label.add_css_class("dim-label")
        content.append(self._status_label)

        self._list = Gtk.ListBox()
        self._list.set_selection_mode(Gtk.SelectionMode.NONE)
        self._list.add_css_class("boxed-list")
        content.append(self._list)

        self._empty_label = Gtk.Label(label="No samples yet.")
        self._empty_label.set_halign(Gtk.Align.START)
        self._empty_label.add_css_class("dim-label")
        content.append(self._empty_label)

        scrolled.set_child(content)
        toolbar.set_content(scrolled)
        self.set_child(toolbar)

        register_session_event_callback("diagnostics_snapshot", self._on_diagnostics_snapshot)
        self.connect("closed", self._on_closed)

    def _selected_categories(self) -> list[str]:
        selected = [
            category
            for category in DIAGNOSTICS_CATEGORIES
            if self._category_checks[category].get_active()
        ]
        return selected or ["mainline"]

    def _on_enabled_changed(self, switch: Gtk.Switch, _param: object) -> None:
        if self._syncing_controls:
            return
        self._enabled = switch.get_active()
        self._apply_diagnostics_settings()

    def _on_interval_changed(self, _spin: Gtk.SpinButton) -> None:
        if self._syncing_controls:
            return
        if self._enabled:
            self._apply_diagnostics_settings()

    def _on_category_toggled(self, _button: Gtk.CheckButton) -> None:
        if self._syncing_controls:
            return
        if not self._selected_categories():
            self._category_checks["mainline"].set_active(True)
            return
        if self._enabled:
            self._apply_diagnostics_settings()

    def _apply_diagnostics_settings(self) -> None:
        if self._request_inflight:
            return
        payload: JsonDict = {
            "command": "set_diagnostics",
            "enabled": self._enabled,
            "interval": self._interval_spin.get_value(),
            "categories": self._selected_categories(),
        }
        self._request_inflight = True
        self._set_controls_sensitive(False)
        session_request_async(payload, self._on_diagnostics_response, timeout=2.0)

    def _on_diagnostics_response(self, result: JsonDict | None) -> bool:
        self._request_inflight = False
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
            self._interval_spin.set_value(float(interval))
        if self._enabled:
            self._status_label.set_text("Waiting for samples...")
        else:
            self._status_label.set_text("Diagnostics are off.")
            self._clear_rows()
        return False

    def _set_controls_sensitive(self, sensitive: bool) -> None:
        self._enable_switch.set_sensitive(sensitive)
        self._interval_spin.set_sensitive(sensitive)
        for check in self._category_checks.values():
            check.set_sensitive(sensitive)

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

    def _on_diagnostics_snapshot(self, event: JsonDict) -> bool:
        samples = event.get("samples")
        if not isinstance(samples, dict):
            return False
        self._enabled = bool(event.get("enabled", True))
        self._sync_enabled_switch()
        self._sync_categories(event.get("categories"))
        interval = event.get("interval")
        if isinstance(interval, (int, float)):
            self._interval_spin.set_value(float(interval))
        self._render_samples(cast(dict[str, Any], samples))
        return False

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

        self._empty_label.set_visible(not self._rows)
        if self._rows:
            self._status_label.set_text("Showing latest diagnostics snapshot.")

    def _create_row(self, label: str) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.set_selectable(False)
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
            if key == "max":
                cell.add_css_class("dim-label")
                cell.set_tooltip_text(PEAK_TOOLTIP)
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

    def _on_closed(self, _dialog: Adw.Dialog) -> None:
        unregister_session_event_callback("diagnostics_snapshot", self._on_diagnostics_snapshot)
